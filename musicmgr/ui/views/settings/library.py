"""Settings › Library (watched folders and scans)."""

from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
)
from sqlalchemy import select
from ....db.models import WatchedFolder
from ....db.session import reset_database
from ....services import library as lib
from ....services import scanner
from ....services import video_scanner
from ....services import videos as vid_svc
from ...widgets.common import TouchButton, TouchList, dim_label
from .parts import (
    NukeConfirmDialog,
    ScanThread,
    _plural,
    _purge_breakdown,
)


class LibrarySection:
    """Settings › Library (watched folders and scans): a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_library(self, section) -> None:
        """Build the Library group."""
        # ---- folders ----
        body = section("Library", "library")
        folder_head = QHBoxLayout()
        label = QLabel("Watched folders")
        label.setObjectName("Crumb")
        folder_head.addWidget(label)
        folder_head.addStretch(1)
        add_btn = TouchButton("Add folder…", primary=True)
        # Settings page's own brown pallet rather than the app-wide
        # #Primary - see theme.py's #PrimaryWarm/#BigNumber/#ProgressWarm
        # comments
        add_btn.setObjectName("PrimaryWarm")
        add_btn.clicked.connect(self.add_folder)
        remove_btn = TouchButton("Remove")
        remove_btn.clicked.connect(self.remove_folder)
        scan_selected_btn = TouchButton("Scan selected")
        scan_selected_btn.clicked.connect(self.scan_selected)
        scan_btn = TouchButton("Scan now")
        scan_btn.clicked.connect(self.scan_all)
        for b in (add_btn, remove_btn, scan_selected_btn, scan_btn):
            folder_head.addWidget(b)
        body.addLayout(folder_head)

        # an ordinary scan only re-reads tags from files whose mtime/size
        # changed, so a tag-reading fix doesn't reach an already-scanned
        # library. Checked, "Scan selected"/"Scan now" re-read every file's
        # tags (see scanner.py's `import_file` `force` docstring) - slow, so
        # it defaults off.
        self.force_rescan_cb = QCheckBox("Force full re-read (slow)")
        self.force_rescan_cb.setToolTip(
            "Re-reads every file's tags even if it hasn't changed on disk.\n"
            "Use this once after a tag-reading fix; leave it off otherwise."
        )
        force_row = QHBoxLayout()
        force_row.addWidget(self.force_rescan_cb)
        force_row.addStretch(1)
        body.addLayout(force_row)

        self.folder_list = TouchList()
        body.addWidget(self.folder_list, 1)

        # a scan's progress paints into its row of self.folder_list (see
        # _on_progress and ui/widgets/common.py's TouchList.update_row/
        # RowDelegate); this page-wide bar is for jobs with no row of their
        # own (Move data location…, the Maintenance tools)
        self.progress = QProgressBar()
        self.progress.setObjectName("ProgressWarm")  # this page's brown pallet - see theme.py
        self.progress.setVisible(False)
        # pinned below the scrolling groups, so a running job's
        # progress and Cancel stay in view whichever groups are open
        self.body().addWidget(self.progress)

        # one Cancel button for every Settings job, not one per action -
        # it reuses `_busy_with`'s enumeration of running threads (minus
        # MigrationThread - see `_active_cancellable_thread` for why a data
        # move can't take a mid-flight stop). It sits beside
        # `progress_label`, the line every bulk action updates; ScanThread
        # reports per row but still uses `_show_cancel`/`_hide_cancel`.
        self.progress_label = dim_label("")
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_label, 1)
        self.cancel_btn = TouchButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        progress_row.addWidget(self.cancel_btn, 0, Qt.AlignRight)
        self.body().addLayout(progress_row)

    def _library_summary(self) -> str:
        tracks = self._stat_labels["Tracks"].text()
        folders = self.folder_list.count()
        return f"{tracks} tracks  ·  {folders} watched folder{'s' if folders != 1 else ''}"

    def add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose a folder to watch", str(Path.home())
        )
        if not path:
            return
        # normalize before it ever touches the database - see the module
        # docstring's note on the duplicate-folder bug this fixes. The
        # scanners below normalize the exact same way, so both sides
        # always agree on one canonical string for the same folder.
        path = str(Path(path).expanduser())
        with self.ctx.session() as session:
            existing = session.scalar(
                select(WatchedFolder).where(WatchedFolder.path == path)
            )
            if existing is None:
                session.add(WatchedFolder(path=path))
        self.refresh()
        self.scan_paths([path])

    def remove_folder(self) -> None:
        payload = self.folder_list.current_payload()
        if not payload:
            return
        with self.ctx.session() as session:
            folder = session.get(WatchedFolder, payload["key"])
            if folder is not None:
                session.delete(folder)
        self.ctx.notify("Folder removed. Existing tracks and videos were kept.")
        self.refresh()

    def purge_missing_files(self) -> None:
        """Permanently delete both missing tracks and missing videos in one
        action.

        The two sides keep their own, already-established safety rules
        rather than being forced to match each other: `purge_orphaned_tracks`
        still skips a missing track that's on a playlist, a chart, or a
        jukebox slot, or has play history (see its own docstring) - only
        `purge_missing_videos` is unconditional, since nothing references a
        `Video` row the way those tables reference a `Track`. So "N missing"
        and "N removed" can legitimately differ for audio; the confirmation
        text below says so rather than that reading as a bug.

        Missing file rows aren't the same as missing tracks (a song can
        still play from another copy), so the dialog spells out what a purge
        will do (scanner.missing_summary), and the purge also clears missing
        extra copies of still-playable songs (scanner.purge_stale_copies).

        Re-checks `is_missing` against disk right before purging (via
        mark_missing_files/mark_missing_videos) rather than trusting
        whatever it was left at by the last scan - this button is meant to
        give a correct answer right now, not only right after a fresh scan.
        A library carried over to a new machine (different drive letter, a
        USB stick mounted somewhere else) can show everything as "missing"
        purely because the old paths don't resolve there; the confirmation
        text says so explicitly rather than letting that read as data loss,
        and points at rescanning as the fix for a merely-moved drive rather
        than purging.
        """
        with self.ctx.session() as session:
            # re-check disk first - see above. Their return values (files
            # *newly* flagged this call) don't matter here; missing_summary
            # reads the real totals off the refreshed flags.
            scanner.mark_missing_files(session)
            video_scanner.mark_missing_videos(session)
            summary = scanner.missing_summary(session)
            missing_videos = vid_svc.video_counts(session)["video_missing"]
        if not summary.files and not missing_videos:
            self.ctx.notify("No missing files to remove")
            return
        # say what a purge will actually do, track by track, rather than
        # counting missing *file rows*
        if not summary.removable and not summary.stale_copies and not missing_videos:
            self.ctx.notify(
                f"{_plural(summary.files, 'missing file')} found, but nothing to "
                "remove - " + _purge_breakdown(summary)
            )
            return
        will = []
        if summary.stale_copies:
            will.append(
                f"clear {_plural(summary.stale_copies, 'stale copy')}"
                .replace("copys", "copies")
                + " of songs that still play from another file (the songs stay)"
            )
        if summary.removable:
            will.append(f"remove {_plural(summary.removable, 'track')} with no file left")
        if missing_videos:
            will.append(f"remove {_plural(missing_videos, 'video')}")
        kept = _purge_breakdown(summary, removing=True)
        confirm = QMessageBox.question(
            self,
            "Purge missing files",
            f"{_plural(summary.files, 'file')} and "
            f"{_plural(missing_videos, 'video')} can't currently be found.\n\n"
            "Purging will " + "; ".join(will) + "."
            + (f" {kept}" if kept else "")
            + "\n\nThis never touches anything on disk - only MusicMgr's own "
            "records.\n\n"
            "If files are only missing because a drive isn't connected "
            "right now (or a watched folder points at an old location, "
            "like a path from a different machine), reconnect it or fix "
            "the folder and scan again instead - anything purged here has "
            "to be rescanned from scratch to come back.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            copies_removed = scanner.purge_stale_copies(session)
            tracks_removed = scanner.purge_orphaned_tracks(session)
            videos_removed = video_scanner.purge_missing_videos(session)
        copies = (
            f"{copies_removed} stale cop{'y' if copies_removed == 1 else 'ies'}, "
            if copies_removed else ""
        )
        self.ctx.notify(
            f"Removed {copies}{tracks_removed} missing track{'s' if tracks_removed != 1 else ''} "
            f"and {videos_removed} missing video{'s' if videos_removed != 1 else ''}"
        )
        self.refresh()

    def nuke_library(self) -> None:
        """Permanently erase the entire library database and start over -
        see NukeConfirmDialog's own docstring for why this needs more
        than the usual single Yes/No tap, and db.session.reset_database's
        for why no backup is kept.

        Guards against every other long-running job the same way
        download_artist_profiles/update_track_popularity/
        search_missing_artwork above do, but for the opposite reason:
        this action itself is fast enough to need no progress bar of its
        own - what matters is making sure nothing else is mid-write to
        the database it's about to erase out from under it.
        """
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is running — wait for it to finish before nuking the library")
            return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish before nuking the library")
            return
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify(
                "Artist profiles are downloading — wait for it to finish before nuking the library"
            )
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify(
                "Track popularity is updating — wait for it to finish before nuking the library"
            )
            return
        if self._artwork_thread is not None and self._artwork_thread.isRunning():
            self.ctx.notify(
                "Album artwork is downloading — wait for it to finish before nuking the library"
            )
            return

        with self.ctx.session() as session:
            stats = lib.library_stats(session)

        dialog = NukeConfirmDialog(stats, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        # a stale queue/now-playing pointed at rows that are about to stop
        # existing is worse than just stopping playback outright
        self.ctx.player.stop()
        self.ctx.player.clear_queue()

        reset_database()

        self.refresh()
        self.ctx.libraryChanged.emit()
        self.ctx.videosChanged.emit()
        QMessageBox.information(
            self,
            "Nuke library",
            "Your library database has been permanently erased.\n\n"
            "Add a watched folder and scan to rebuild it from your files.",
        )

    def scan_all(self) -> None:
        with self.ctx.session() as session:
            paths = [
                f.path
                for f in session.scalars(
                    select(WatchedFolder).where(WatchedFolder.enabled.is_(True))
                )
            ]
        if not paths:
            QMessageBox.information(
                self, "No folders", "Add a folder first, then scan."
            )
            return
        self.scan_paths(paths, force=self.force_rescan_cb.isChecked())

    def scan_selected(self) -> None:
        payload = self.folder_list.current_payload()
        if not payload:
            self.ctx.notify("Select a folder in the list first, then Scan selected")
            return
        self.scan_paths([payload["path"]], force=self.force_rescan_cb.isChecked())

    def scan_paths(self, paths: list[str], force: bool = False) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is already running")
            return
        for usb_thread in (self._usb_compare_thread, self._usb_sync_thread):
            if usb_thread is not None and usb_thread.isRunning():
                self.ctx.notify("The USB sync is running — wait for it to finish before scanning")
                return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish before scanning")
            return
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify(
                "Artist profiles are downloading — wait for it to finish before scanning"
            )
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify(
                "Track popularity is updating — wait for it to finish before scanning"
            )
            return
        if self._artwork_thread is not None and self._artwork_thread.isRunning():
            self.ctx.notify(
                "Album artwork is downloading — wait for it to finish before scanning"
            )
            return
        if self._metadata_scan_thread is not None and self._metadata_scan_thread.isRunning():
            self.ctx.notify(
                "Missing metadata is scanning — wait for it to finish before scanning"
            )
            return
        if self._artist_images_thread is not None and self._artist_images_thread.isRunning():
            self.ctx.notify(
                "Artist images are importing — wait for it to finish before scanning"
            )
            return
        if self._verify_thread is not None and self._verify_thread.isRunning():
            self.ctx.notify(
                "Files are being verified — wait for it to finish before scanning"
            )
            return
        if self._rematch_thread is not None and self._rematch_thread.isRunning():
            self.ctx.notify(
                "Charts are re-matching — wait for it to finish before scanning"
            )
            return
        # give every folder about to be scanned an immediate row-level state rather than leaving its stale "last
        # scan: …" text sitting there until the first progress tick - see
        # _on_progress for the live updates once scanning actually starts.
        for path in paths:
            self.folder_list.update_row(
                path, "path", secondary="Waiting to scan…", progress=0.0
            )
        self._thread = ScanThread(paths, force=force, parent=self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_with.connect(self._on_scan_done)
        self._thread.start()
        self._show_cancel()

    def _on_progress(self, done: int, total: int, folder: str, name: str) -> None:
        fraction = done / total if total else 0.0
        self.folder_list.update_row(
            folder, "path", secondary=f"{done}/{total} — {name}", progress=fraction
        )

    def _on_scan_done(self, summary: str) -> None:
        # refresh() (triggered by the two emits below, both connected in
        # __init__) rebuilds every row fresh from the database, which is
        # what clears the in-progress look above back to an ordinary "last
        # scan: …" row - nothing else to clean up here.
        self.cancel_btn.setVisible(False)
        self.ctx.notify(summary)
        self.ctx.libraryChanged.emit()
        self.ctx.videosChanged.emit()
        # thumbnails and years for new or changed videos
        from ... import video_details_worker

        video_details_worker.start(self.ctx, only_if_pending=True)
