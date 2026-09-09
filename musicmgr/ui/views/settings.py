"""Watched folders, scanning, and housekeeping.

2026-09-07 follow-up (James: "I really don't need a different block for
audio and video. Just one block to add folders and then one option to scan
all those folders") - this view used to show two entirely parallel
sections, each with its own Add/Remove/Scan now buttons, its own list, and
its own progress bar: "Watched folders" for music (`LibraryFolder`) and
"Watched video folders" for videos (`VideoFolder`). That split is gone -
see `db.models.WatchedFolder`'s docstring for the merged table it now reads
and writes. One list, one Add/Remove/Scan now row, one progress bar:
`ScanThread` below runs *both* `services.scanner.scan_folder` and
`services.video_scanner.scan_video_folder` over every watched folder,
back to back, so a folder never has to be told which kind of media it
holds - each scanner just finds whatever it understands and ignores the
rest.

This also fixed a real duplicate-folder bug that had nothing to do with
the two-block layout itself: `add_folder` used to store whatever raw
string `QFileDialog.getExistingDirectory` returned, while `scan_folder`/
`scan_video_folder` looked up (and, if not found, silently re-created) a
folder row by `str(Path(root))`. On Windows, Qt's file dialog returns
forward-slash paths ("D:/Music") but `str(Path(...))` normalizes to
backslashes ("D:\\Music") - two different strings for the same folder, so
every "add a folder, then scan" sequence quietly created a *second*,
never-scanned row for the folder just added. `add_folder` now normalizes
through `Path(...)` before ever touching the database, the same
normalization the scanners already used, so both sides always agree on one
canonical string. The one-time `WatchedFolder` migration also merges any
such duplicate rows an already-affected database was left with (see
`db.session._migrate_watched_folders`).

2026-09-07 follow-up #2 (James: renaming a file on disk to fix a stray
"01 " prefix in its title left the *old* title still showing in search,
looking like a duplicate song): every audio scan now also calls
`scanner.purge_orphaned_tracks` right after `mark_missing_files`, which
deletes any track left with no playable file and no play/playlist/chart
history to lose - see that function's docstring. This runs automatically,
with no confirmation step, by James's own choice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from ... import config
from ...db.models import WatchedFolder
from ...db.session import session_scope
from ...services import data_migration
from ...services import library as lib
from ...services import scanner
from ...services import video_scanner
from ...services import videos as vid_svc
from ...services.library import format_duration
from ..context import AppContext
from ..widgets.common import TouchButton, TouchList, dim_label
from .base import BaseView


class ScanThread(QThread):
    """Scans every given folder for *both* music and videos, one after the
    other, and reports one combined summary - see the module docstring for
    why there's no longer a separate video scan thread/button. Progress
    messages are prefixed "Audio:"/"Video:" so it's clear which pass is
    running.

    2026-09-08 follow-up - James: "have the progress bar appear on the line
    with the folder." `progress` grew a `folder` argument (the exact
    `WatchedFolder.path` string the caller passed to scan_folder/
    scan_video_folder) alongside the existing (done, total, filename), so
    SettingsView can route each tick to that one folder's row in the list
    instead of a single page-wide bar - see SettingsView._on_progress and
    ui/widgets/common.py's TouchList.update_row. Only one folder is ever
    "current" at a time either way (both passes scan self.folders one at a
    time, never concurrently), so a per-row readout is never ambiguous
    about which row it belongs to."""

    progress = Signal(int, int, str, str)
    finished_with = Signal(str)

    def __init__(self, folders: list[str], parent=None) -> None:
        super().__init__(parent)
        self.folders = folders

    def run(self) -> None:  # pragma: no cover - exercised interactively
        audio_result = scanner.ScanResult()
        video_result = video_scanner.ScanResult()
        try:
            with session_scope() as session:
                for folder in self.folders:
                    scanner.scan_folder(
                        session,
                        folder,
                        progress=lambda done, total, name, folder=folder: self.progress.emit(
                            done, total, folder, f"Audio: {name}"
                        ),
                        result=audio_result,
                    )
                audio_result.missing = scanner.mark_missing_files(session)
                audio_result.removed = scanner.purge_orphaned_tracks(session)
            with session_scope() as session:
                for folder in self.folders:
                    video_scanner.scan_video_folder(
                        session,
                        folder,
                        progress=lambda done, total, name, folder=folder: self.progress.emit(
                            done, total, folder, f"Video: {name}"
                        ),
                        result=video_result,
                    )
                video_result.missing = video_scanner.mark_missing_videos(session)
            self.finished_with.emit(
                f"Audio: {audio_result.summary()} · Video: {video_result.summary()}"
            )
        except Exception as exc:
            self.finished_with.emit(f"Scan failed: {exc}")


class MigrationThread(QThread):
    """Runs data_migration.migrate() off the UI thread for the "Move data
    location…" button - see musicmgr.services.data_migration's module
    docstring for what that actually does, and specifically why running it
    from inside a live app (rather than the closed-app case
    tools/migrate_data.py is documented for) needs its own care.

    remove_source is deliberately never passed here as True: this app's own
    database is still open against `source` (config.DATA_DIR) while this
    thread runs, so deleting that folder out from under the running process
    is a sharper risk than anything the CLI's closed-app --remove-source
    faces. The originals are always left in place; SettingsView's
    completion dialog (data_migration.MigrationResult.summary()) tells
    James which files are now safe to delete by hand, once he's restarted
    and confirmed the new location looks right.
    """

    progress = Signal(str)
    finished_with = Signal(object)  # data_migration.MigrationResult

    def __init__(self, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.dest = dest

    def run(self) -> None:  # pragma: no cover - exercised interactively
        result = data_migration.migrate(
            config.DATA_DIR,
            self.dest,
            remove_source=False,
            progress=self.progress.emit,
        )
        self.finished_with.emit(result)


class SettingsView(BaseView):
    title_text = "Library & Settings"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._thread: Optional[ScanThread] = None
        self._migration_thread: Optional[MigrationThread] = None

        # ---- stats card ----
        card = QFrame()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(20, 18, 20, 18)
        grid.setHorizontalSpacing(36)
        self._stat_labels: dict[str, QLabel] = {}
        for idx, name in enumerate(
            ["Artists", "Releases", "Tracks", "Files", "Videos", "Playlists", "Plays"]
        ):
            value = QLabel("0")
            value.setObjectName("BigNumber")
            key = QLabel(name)
            key.setObjectName("Dim")
            grid.addWidget(value, 0, idx, alignment=Qt.AlignHCenter)
            grid.addWidget(key, 1, idx, alignment=Qt.AlignHCenter)
            self._stat_labels[name] = value
        self.total_time = dim_label("")
        grid.addWidget(self.total_time, 2, 0, 1, 6, alignment=Qt.AlignHCenter)
        self.body().addWidget(card)

        # ---- folders ----
        folder_head = QHBoxLayout()
        label = QLabel("Watched folders")
        label.setObjectName("Crumb")
        folder_head.addWidget(label)
        folder_head.addStretch(1)
        add_btn = TouchButton("Add folder…", primary=True)
        # Settings page's own brown pallet rather than the app-wide red-
        # orange #Primary - see theme.py's #PrimaryWarm/#BigNumber/
        # #ProgressWarm comments (James: "change all that bright orange/red
        # color on this settings page to the brown pallet").
        add_btn.setObjectName("PrimaryWarm")
        add_btn.clicked.connect(self.add_folder)
        remove_btn = TouchButton("Remove")
        remove_btn.clicked.connect(self.remove_folder)
        scan_btn = TouchButton("Scan now")
        scan_btn.clicked.connect(self.scan_all)
        for b in (add_btn, remove_btn, scan_btn):
            folder_head.addWidget(b)
        self.body().addLayout(folder_head)

        self.folder_list = TouchList()
        self.body().addWidget(self.folder_list, 1)

        # 2026-09-08 follow-up - James: "have the progress bar appear on the
        # line with the folder." A scan's progress now paints directly into
        # the relevant row of self.folder_list above (see _on_progress and
        # ui/widgets/common.py's TouchList.update_row/RowDelegate), so this
        # page-wide bar is left for "Move data location…" only - that copy
        # targets one destination folder, not a folder in this list, so it
        # has no row of its own to report progress into.
        self.progress = QProgressBar()
        self.progress.setObjectName("ProgressWarm")  # this page's brown pallet - see theme.py
        self.progress.setVisible(False)
        self.body().addWidget(self.progress)
        self.progress_label = dim_label("")
        self.body().addWidget(self.progress_label)

        # ---- maintenance ----
        tools = QHBoxLayout()
        tools.setSpacing(8)
        artist_art = TouchButton("Import artist images…")
        artist_art.clicked.connect(self.import_artist_images)
        tools.addWidget(artist_art)
        verify = TouchButton("Verify files")
        verify.clicked.connect(self.verify_files)
        rematch = TouchButton("Re-match all charts")
        rematch.clicked.connect(self.rematch_all)
        where = TouchButton("Where is my data?")
        where.clicked.connect(self.show_paths)
        move_btn = TouchButton("Move data location…")
        move_btn.clicked.connect(self.move_data)
        for b in (verify, rematch, where, move_btn):
            tools.addWidget(b)
        tools.addStretch(1)
        self.body().addLayout(tools)

        ctx.libraryChanged.connect(self.refresh)
        ctx.videosChanged.connect(self.refresh)

    # -- loading -------------------------------------------------------------

    def refresh(self) -> None:
        rows = []
        with self.ctx.session() as session:
            stats = lib.library_stats(session)
            vstats = vid_svc.video_counts(session)
            for folder in session.scalars(select(WatchedFolder).order_by(WatchedFolder.path)):
                last = (
                    folder.last_scan_at.strftime("%d %b %Y %H:%M")
                    if folder.last_scan_at
                    else "never scanned"
                )
                rows.append({
                    "primary": folder.path,
                    "secondary": f"last scan: {last}",
                    "key": folder.id,
                    "path": folder.path,
                })
        mapping = {
            "Artists": stats["artists"], "Releases": stats["releases"],
            "Tracks": stats["tracks"], "Files": stats["files"],
            "Videos": vstats["videos"],
            "Playlists": stats["playlists"], "Plays": stats["plays"],
        }
        for name, value in mapping.items():
            self._stat_labels[name].setText(f"{value:,}")
        missing_bits = []
        if stats["missing"]:
            missing_bits.append(f"{stats['missing']} missing audio file(s)")
        if vstats["video_missing"]:
            missing_bits.append(f"{vstats['video_missing']} missing video file(s)")
        missing = f" · {' · '.join(missing_bits)}" if missing_bits else ""
        self.total_time.setText(
            f"Total run time {format_duration(stats['total_ms'])}{missing}"
        )
        self.folder_list.set_rows(rows)

    # -- actions -------------------------------------------------------------

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
        self.scan_paths(paths)

    def scan_paths(self, paths: list[str]) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is already running")
            return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish before scanning")
            return
        # 2026-09-08 follow-up - James: "have the progress bar appear on
        # the line with the folder." Give every folder about to be scanned
        # an immediate row-level state rather than leaving its stale "last
        # scan: …" text sitting there until the first progress tick - see
        # _on_progress for the live updates once scanning actually starts.
        for path in paths:
            self.folder_list.update_row(
                path, "path", secondary="Waiting to scan…", progress=0.0
            )
        self._thread = ScanThread(paths, self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_with.connect(self._on_scan_done)
        self._thread.start()

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
        self.ctx.notify(summary)
        self.ctx.libraryChanged.emit()
        self.ctx.videosChanged.emit()

    def import_artist_images(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose your folder of artist images", str(Path.home())
        )
        if not folder:
            return
        from ...services import artist_images as art_svc

        with self.ctx.session() as session:
            result = art_svc.import_artist_images(session, folder)
            missing = art_svc.artists_without_images(session, limit=12)

        lines = [result.summary()]
        if result.fuzzy:
            lines.append("")
            lines.append("Matched by similar name (check these):")
            lines += [
                f"  {f} → {name} ({score:.0%})" for f, name, score in result.fuzzy[:8]
            ]
        if result.unmatched:
            lines.append("")
            lines.append("No artist in your library matched:")
            lines += [f"  {n}" for n in result.unmatched[:8]]
            if len(result.unmatched) > 8:
                lines.append(f"  …and {len(result.unmatched) - 8} more")
        if missing:
            lines.append("")
            lines.append("Still without a picture:")
            lines += [f"  {n}" for n in missing[:8]]
        for err in result.errors[:5]:
            lines.append(f"  ! {err}")

        QMessageBox.information(self, "Artist images", "\n".join(lines))
        self.ctx.libraryChanged.emit()

    def verify_files(self) -> None:
        # 2026-09-07 follow-up: checks video files too now, not just audio -
        # the same "one unified thing" this whole view's folder merge is
        # about (see module docstring); there's no reason "Verify files"
        # would only mean half of what's watched.
        with self.ctx.session() as session:
            missing = scanner.mark_missing_files(session)
            missing += video_scanner.mark_missing_videos(session)
        self.ctx.notify(f"{missing} file(s) newly marked missing")
        self.refresh()

    def rematch_all(self) -> None:
        from ...db.models import Chart
        from ...services import charts as chart_svc

        total = 0
        with self.ctx.session() as session:
            for chart in session.scalars(
                select(Chart).where(Chart.kind == Chart.KIND_EXTERNAL)
            ):
                total += chart_svc.rematch_chart(session, chart.id)
        self.ctx.notify(f"{total} chart entries now point at tracks you own")
        self.ctx.libraryChanged.emit()

    def show_paths(self) -> None:
        QMessageBox.information(
            self,
            "Data locations",
            f"Database:\n{config.DB_PATH}\n\n"
            f"Artwork cache:\n{config.ART_DIR}\n\n"
            "Set the MUSICMGR_HOME environment variable to move them, or use "
            "\"Move data location…\" below to do it from here.",
        )

    def move_data(self) -> None:
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A move is already running")
            return
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is running — wait for it to finish before moving data")
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to move your data", str(config.PROJECT_ROOT)
        )
        if not folder:
            return
        dest = Path(folder).expanduser().resolve()
        if dest == config.DATA_DIR:
            # Not a move - but this is exactly where someone lands after
            # copying data\ to a new PC by hand: the files are already in
            # the right folder, but cover_path inside library.db can still
            # remember the old machine's paths (see data_migration.repair's
            # own docstring). Offer the fix instead of a dead end.
            confirm = QMessageBox.question(
                self,
                "Move data location",
                "Your data already lives in this folder.\n\n"
                "Would you like to repair any cover art paths that still "
                "point somewhere else instead - for example, left over from "
                "copying this folder here by hand on another PC?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm == QMessageBox.Yes:
                result = data_migration.repair(dest)
                QMessageBox.information(self, "Move data location", result.summary())
            return

        confirm = QMessageBox.question(
            self,
            "Move data location",
            f"Copy your database and artwork cache to:\n{dest}\n\n"
            f"Your current data at {config.DATA_DIR} is left in place — nothing "
            "is deleted here. You'll need to fully close and restart MusicMgr "
            "afterwards for it to actually use the new location (and, unless "
            "the new folder is your portable data\\ folder, set the "
            "MUSICMGR_HOME environment variable to it first).\n\n"
            "For the safest result, avoid scanning or importing anything in "
            "MusicMgr until this finishes. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Starting move…")
        self._migration_thread = MigrationThread(dest, self)
        self._migration_thread.progress.connect(self.progress_label.setText)
        self._migration_thread.finished_with.connect(self._on_move_done)
        self._migration_thread.start()

    def _on_move_done(self, result: data_migration.MigrationResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        QMessageBox.information(self, "Move data location", result.summary())
