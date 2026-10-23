"""The Settings page itself: builds the page from its sections (one
module each in this package) and owns what they share - the busy check,
the Cancel button, the folding groups."""

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
        #: same QThread-with-a-progress-signal shape as everything above,
        #: so these scans report progress instead of blocking the UI thread.
        self._artist_images_thread: Optional[ArtistImagesImportThread] = None
        self._verify_thread: Optional[VerifyFilesThread] = None
        self._rematch_thread: Optional[RematchChartsThread] = None
        self._loudness_thread: Optional[LoudnessAnalyzeThread] = None
        #: Settings › Duplicates (services/duplicates.py)
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
        # the artist/release page's "Missing metadata" breadcrumb crumb
        # (LibraryView._return_to_missing_metadata) lands back here by
        # emitting this rather than calling Settings directly - Library and
        # Settings only talk through ctx.
        ctx.missingMetadataBackRequested.connect(self.view_missing_metadata)

        # `BaseView.body()` is a plain, non-scrolling QVBoxLayout, and a
        # view taller than its window gets squeezed (every row compressed
        # toward zero height) rather than scrolled. This page is tall
        # enough for that, so its body gets its own scroll area - the same
        # fix ArtistDetailPanel (ui/widgets/artist_panel.py) uses - rather
        # than changing `BaseView` for every page. `body` (a local, distinct
        # from self.body()) is what everything below builds into; only the
        # page title (added by BaseView.__init__) stays outside the scroll
        # area, always visible.
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

        # ---- collapsible groups ----
        # Everything below the library numbers sits in a folding group
        # (CollapsibleSection); a folded group's header still says what's in
        # it. Which groups are open is remembered (SECTIONS_OPEN_KEY);
        # Library starts open. `body` is pointed at each group's own layout
        # in turn, so the code building each part of the page is unchanged.
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
        most one such job ever touches the database at once."""
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

    #: every bulk-action thread attribute that's safe to interrupt mid-run
    #: - all but `_migration_thread`. `MigrationThread`
    #: (data_migration.migrate) copies files to a new location in several
    #: steps (db, artwork/, artists/, then rewriting paths) with no
    #: per-step undo, so stopping it partway could leave a plausible but
    #: unsafe half-copied destination. Every thread listed here either
    #: commits/flushes per item or is read-only (see each service
    #: function's should_stop note), so a break is always safe.
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
