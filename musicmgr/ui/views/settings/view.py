"""The Settings page itself: builds the page from its sections (one
module each in this package) and owns what they share - the busy check,
the Cancel button, the folding groups (split out of settings.py,
2026-10-22)."""

from __future__ import annotations

from typing import Optional
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QScrollArea, QVBoxLayout, QWidget
from sqlalchemy import select
from ....db.models import WatchedFolder
from ....services import library as lib
from ....services import metadata_health
from ....services import updater
from ....services import videos as vid_svc
from ....services.library import format_duration
from ...context import AppContext
from ...widgets.common import (
    CollapsibleSection,
    ArtistImagesImportThread,
    ArtworkBulkThread,
    BioDownloadThread,
    MetadataScanThread,
    PopularityDownloadThread,
    RematchChartsThread,
    VerifyFilesThread,
    dim_label,
    guard_wheel,
)
from ..base import BaseView
from .parts import (
    LoudnessAnalyzeThread,
    MigrationThread,
    SECTIONS_OPEN_DEFAULT,
    SECTIONS_OPEN_KEY,
    ScanThread,
)
from .library import LibrarySection
from .playback import PlaybackSection
from .remote import RemoteSection
from .appearance import AppearanceSection
from .maintenance import MaintenanceSection
from .files import FilesSection
from .updates import UpdatesSection
from .usb import UsbSection


class SettingsView(
    LibrarySection,
    PlaybackSection,
    RemoteSection,
    AppearanceSection,
    MaintenanceSection,
    FilesSection,
    UpdatesSection,
    UsbSection,
    BaseView,
):
    title_text = "Library & Settings"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._thread: Optional[ScanThread] = None
        self._migration_thread: Optional[MigrationThread] = None
        self._bio_thread: Optional[BioDownloadThread] = None
        self._popularity_thread: Optional[PopularityDownloadThread] = None
        self._artwork_thread: Optional[ArtworkBulkThread] = None
        self._metadata_scan_thread: Optional[MetadataScanThread] = None
        #: 2026-09-22 - James: "does the progress bar also work on the
        #: import of artist images and the other settings options that
        #: scan" (it didn't - "Artist images"/"Verify files"/"Charts" all
        #: used to run entirely on the UI thread with no feedback). Same
        #: QThread-with-a-progress-signal shape as everything above.
        self._artist_images_thread: Optional[ArtistImagesImportThread] = None
        self._verify_thread: Optional[VerifyFilesThread] = None
        self._rematch_thread: Optional[RematchChartsThread] = None
        self._loudness_thread: Optional[LoudnessAnalyzeThread] = None
        #: 2026-10-13 Settings › Duplicates (services/duplicates.py)
        self._duplicates_thread = None
        #: last "Missing metadata" scan, reused so reopening the dashboard
        #: (e.g. right after fixing one item) doesn't rerun the whole thing
        #: - see view_missing_metadata's own docstring. Cleared (forcing a
        #: fresh scan) whenever a bulk action below changes the data it
        #: reports on, and by the dialog's own "Rescan" button.
        self._last_metadata_scan: Optional[metadata_health.ScanResult] = None
        #: (kind, id) pairs picked from the dashboard at least once - purely
        #: a "you've already been here" marker (MissingMetadataDialog), so
        #: it's additive only, never cleared by a rescan.
        self._metadata_visited: set = set()
        # 2026-09-22 follow-up (James: "is there any way we can have a back
        # button when you go from missing metadata to the album and then
        # back") - the artist/release page's own "Missing metadata"
        # breadcrumb crumb (LibraryView._return_to_missing_metadata) lands
        # back here by emitting this rather than by calling anything on
        # Settings directly, the same "Library and Settings only talk
        # through ctx" convention every other cross-view hand-off in this
        # app already follows.
        ctx.missingMetadataBackRequested.connect(self.view_missing_metadata)

        # 2026-09-16 follow-up (same one that added the row-per-tool
        # Maintenance table below) - this page's content used to fit
        # `BaseView`'s own plain (non-scrolling) body every time, because
        # the old cramped-buttons "tools" row was short enough that the
        # whole page's total height rarely exceeded a real window. Turning
        # those buttons into nine full rows (see _ToolRow) made the page
        # tall enough that it regularly doesn't - and unlike
        # `ArtistDetailPanel` (ui/widgets/artist_panel.py), which has
        # wrapped its own content in a QScrollArea since it grew a
        # biography and Top Tracks section, `BaseView.body()` is a plain
        # QVBoxLayout directly on the view widget with nothing scrollable
        # about it: a view taller than the window it's given doesn't
        # scroll, it just gets squeezed - every row's label and button
        # compressed toward zero height rather than clipped cleanly, which
        # is what actually happened (not a rendering bug in the new rows
        # themselves - James's screenshot on a normal-height window showed
        # everything crushed into illegibility). Wrapping this page's own
        # body in its own scroll area - the same fix ArtistDetailPanel
        # already uses, just applied here instead of changing `BaseView`
        # for every other page - means a short window scrolls this page
        # instead of crushing it. `body` (a local, shadowing but distinct
        # from self.body()) is what every line below now builds into,
        # instead of `self.body()` directly - only the page's own title
        # (added by BaseView.__init__ above, before this) stays outside the
        # scroll area, always visible.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        scroll.setWidget(content)
        self.body().addWidget(scroll, 1)

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
        body.addWidget(card)

        # ---- collapsible groups (2026-10-01) ----
        # James: "The Settings page is now way to 'busy' how can we better
        # organize the settings? Maybe collapsable groups". Everything below
        # the library numbers sits in a folding group (CollapsibleSection);
        # a folded group's header still says what's in it. Which groups are
        # open is remembered (SECTIONS_OPEN_KEY); Library starts open.
        # `body` is pointed at each group's own layout in turn, so the code
        # building each part of the page is unchanged.
        page = body
        self._sections: dict[str, CollapsibleSection] = {}
        open_keys = self._load_open_sections()

        def section(title: str, key: str) -> QVBoxLayout:
            sec = CollapsibleSection(title, key, expanded=key in open_keys)
            sec.toggled.connect(lambda _on: self._save_open_sections())
            page.addWidget(sec)
            self._sections[key] = sec
            return sec.content_layout

        self._build_library(section)
        self._build_playback(section)
        self._build_remote(section)
        self._build_appearance(section)
        self._build_maintenance(section)
        self._build_updates(section)
        self._build_usb(section)

        # the wheel scrolls the page, never a drop-down it passes over -
        # click a control first to change it with the wheel
        self._wheel_guard = guard_wheel(scroll, content)

        # what each group's header says while it's folded
        self._sections["library"].set_summary(self._library_summary)
        self._sections["playback"].set_summary(
            lambda: f"Crossfade: {self.crossfade_combo.currentText()}  ·  "
                    f"Levelling: {self.levelling_combo.currentText().split(' — ')[0]}")
        self._sections["info"].set_summary(
            lambda: "Artwork, biographies, popularity, volume levels, chart matching")
        self._sections["files"].set_summary(
            lambda: "Verify, purge, locate or move your library files")
        self._sections["services"].set_summary(lambda: "Last.fm and Discogs keys")
        self._sections["appearance"].set_summary(self._look_summary)
        self._sections["remote"].set_summary(self._remote_summary)
        self._sections["updates"].set_summary(lambda: self.version_label.text())
        self._sections["usb"].set_summary(self._usb_summary)

        ctx.libraryChanged.connect(self.refresh)
        ctx.videosChanged.connect(self.refresh)

    def _busy_with(self) -> Optional[str]:
        """None if no other bulk background job is currently running, else
        the notify() message to show instead of starting a new one - every
        bulk-action handler below checks this (after its own "am I already
        running" check, which gets its own more specific message) so at
        most one such job ever touches the database at once.

        2026-09-22 follow-up (the same "does the progress bar also work
        on ... the other settings options that scan" conversation that
        added ArtistImagesImportThread/VerifyFilesThread/RematchChartsThread
        below) - one shared check replacing each handler's own repeated
        "if self._x_thread ... elif self._y_thread ..." chain, which was
        still fine at four sibling threads to cross-check but stopped being
        worth copy-pasting once there were eight."""
        for thread, busy_message in (
            (self._thread, "A scan is running — wait for it to finish first"),
            (self._migration_thread, "A data move is running — wait for it to finish first"),
            (self._bio_thread, "Artist profiles are downloading — wait for it to finish first"),
            (self._popularity_thread, "Track popularity is updating — wait for it to finish first"),
            (self._artwork_thread, "Album artwork is downloading — wait for it to finish first"),
            (self._metadata_scan_thread, "Missing metadata is scanning — wait for it to finish first"),
            (self._artist_images_thread, "Artist images are importing — wait for it to finish first"),
            (self._verify_thread, "Files are being verified — wait for it to finish first"),
            (self._rematch_thread, "Charts are re-matching — wait for it to finish first"),
            (self._loudness_thread, "Volume levels are being analyzed — wait for it to finish first"),
            (self._duplicates_thread, "Duplicates are being searched for — wait for it to finish first"),
            (getattr(self, "_usb_compare_thread", None), "The USB is being checked — wait for it to finish first"),
            (getattr(self, "_usb_sync_thread", None), "The USB is syncing — wait for it to finish first"),
        ):
            if thread is not None and thread.isRunning():
                return busy_message
        return None

    #: every bulk-action thread attribute that's actually safe to interrupt
    #: mid-run - the same nine minus `_migration_thread`. 2026-09-22, James:
    #: "add a real cancel button that stops any process running within
    #: Settings" - `MigrationThread` (data_migration.migrate) copies files
    #: to a new location in several discrete steps (db, artwork/, artists/,
    #: then rewriting paths) with no per-step undo; stopping it partway
    #: could leave a half-copied, half-rewritten destination that looks
    #: plausible but isn't safe to actually switch to. Every thread listed
    #: here, by contrast, either commits/flushes per-item or is read-only -
    #: see each service function's own should_stop docstring note for the
    #: specifics - so a break is always safe.
    _CANCELLABLE_THREAD_ATTRS = (
        "_thread",
        "_bio_thread",
        "_popularity_thread",
        "_artwork_thread",
        "_metadata_scan_thread",
        "_artist_images_thread",
        "_verify_thread",
        "_rematch_thread",
        "_loudness_thread",
        "_duplicates_thread",
        "_usb_compare_thread",
        "_usb_sync_thread",
    )

    def _active_cancellable_thread(self) -> Optional[QThread]:
        """The one currently-running thread Cancel is allowed to act on, or
        None if nothing cancellable is running right now (including "the
        thing running is the migration, which Cancel can't touch")."""
        for attr in self._CANCELLABLE_THREAD_ATTRS:
            thread = getattr(self, attr, None)
            if thread is not None and thread.isRunning():
                return thread
        return None

    def _show_cancel(self) -> None:
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)

    def _hide_cancel(self, thread: Optional[QThread] = None) -> None:
        """Called from every bulk action's `_on_..._done` handler, mirroring
        the matching `self.progress.setVisible(False)` right next to each
        one. `thread` - the just-finished thread, if this action has a
        cancellable one - lets a run that was actually stopped early say so
        once, here, rather than every individual result dialog below
        needing its own "was this cancelled" wording."""
        self.cancel_btn.setVisible(False)
        if thread is not None and thread.isInterruptionRequested():
            self.ctx.notify("Cancelled — showing partial results")

    def _on_cancel_clicked(self) -> None:
        thread = self._active_cancellable_thread()
        if thread is None:
            return
        thread.requestInterruption()
        self.cancel_btn.setEnabled(False)
        self.ctx.notify("Cancelling — finishing the current item…")

    def refresh(self) -> None:
        self._refresh_levels_status()
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
        self._refresh_section_summaries()

    def _load_open_sections(self) -> set[str]:
        """Which groups were left open (Library only, the first time)."""
        with self.ctx.session() as db:
            value = updater.get_pref(db, SECTIONS_OPEN_KEY)
        if value is None:
            return set(SECTIONS_OPEN_DEFAULT)
        return {k for k in value.split(",") if k}

    def _save_open_sections(self) -> None:
        keys = ",".join(k for k, sec in self._sections.items() if sec.expanded)
        self._save_update_pref(SECTIONS_OPEN_KEY, keys)

    def _refresh_section_summaries(self) -> None:
        for sec in getattr(self, "_sections", {}).values():
            sec.refresh_summary()

    def expand_section(self, key: str) -> None:
        """Open a group (and remember it), e.g. when something inside needs
        attention."""
        sec = self._sections.get(key)
        if sec is not None:
            sec.set_expanded(True)

    def shutdown(self) -> None:
        """MainWindow.closeEvent: a "Volume levels" run can take hours on a
        big library, so closing the app mid-run is normal - stop it cleanly
        (everything measured so far is already saved)."""
        thread = self._loudness_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.wait(5000)
