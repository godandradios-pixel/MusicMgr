"""Main window: nav rail, stacked views, persistent player bar."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..db.session import init_engine, session_scope
from ..services import artist_merge
from ..services import artwork_names
from ..services import charts as chart_svc
from ..services import library as lib_svc
from ..services import playlists as pl_svc
from ..services import scanner as scanner_svc
from ..services import library_backup
from ..services import updater
from ..services import video_scanner
from ..services.player import PlayerController, stop_background_measuring
from .context import AppContext
from .theme import stylesheet
from .views.charts import ChartsView
from .views.jukebox import JukeboxView
from .views.library import LibraryView
from .views.nowplaying import NowPlayingView
from .views.playlists import PlaylistsView
from .views.settings import SettingsView
from .views.tuner import TunerView
from .views.videos import VideosView
from ..version import RELEASE_VERSION

#: James, 2026-09-06: "remove the Library [item] as a left sidebar item and
#: make all those sub menus part of the main [nav]" - Artists/Albums/Tracks/
#: Title Details used to nest under one collapsible "Library" button (see
#: LIBRARY_MODE_KEYS below); each now gets its own top-level slot here,
#: in the exact order James asked for.
#: 2026-10-03 - James: "Let's move Jukebox to the top left menu option" -
#: it's also the page MusicMgr opens on, so it leads the rail.
NAV_ITEMS = [
    ("jukebox", "Jukebox"),
    ("artist", "Artists"),
    ("album", "Albums"),
    ("track", "Tracks"),
    ("details", "Title Details"),
    # Videos briefly got a real sidebar entry here (2026-09-15), reverted
    # the very next day (James: "remove the Videos sidebar menu option"
    # now that Title Details has its own "Videos only" checkbox - see
    # track_details_table.py - giving a second, redundant way to reach the
    # same videos). Back to how it started: no sidebar entry, reachable
    # only mid-transit from a search match or the artist page's own video
    # list (ctx.playVideoRequested/focusVideoArtistRequested, both still
    # wired up below) - the "videos" key stays in the views/PANES mapping
    # a few lines down for exactly that, it just isn't one of these
    # buttons any more. See ctx.videosBackRequested's own docstring for
    # what changed in the embedded player's "‹ Back" button to match.
    ("playlists", "Playlists"),
    ("charts", "Charts"),
    # 2026-10-01 - old-time radio shows and internet stations, see
    # ui/views/tuner.py
    ("radio", "Radio"),
    ("settings", "Settings"),
]

#: Artists/Albums/Tracks/Title Details are four presentations of one
#: underlying widget (LibraryView), not four separate views - a NAV_ITEMS
#: key in this set routes to LibraryView via select_mode(key) instead of
#: switching to a view of its own. See navigate() and LibraryView's
#: docstring.
LIBRARY_MODE_KEYS = {"artist", "album", "track", "details"}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{config.APP_NAME} — music library")
        if config.APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(config.APP_ICON)))
        self.resize(1440, 900)
        self.setMinimumSize(1024, 680)

        self.player = PlayerController(self)
        self.ctx = AppContext(self.player, self)
        self.ctx.notified.connect(self._show_toast)
        self.ctx.navigateRequested.connect(self.navigate)
        self.ctx.nowPlayingBackRequested.connect(self._go_back_from_nowplaying)
        self.ctx.videosBackRequested.connect(self._go_back_from_videos)
        self.player.errorOccurred.connect(self._show_toast)
        #: which section (a NAV_ITEMS key) was active right before Now
        #: Playing was opened, so its "‹ Back" button can return there - see
        #: navigate() and _go_back_from_nowplaying(). None until the first
        #: real navigation happens; falls back to "library" if somehow asked
        #: for before that.
        self._current_key: Optional[str] = None
        self._nowplaying_back_key: Optional[str] = None
        #: same idea as _nowplaying_back_key, for the embedded video
        #: player's "‹ Back" button now that Videos isn't a sidebar
        #: destination of its own any more - see navigate() and
        #: _go_back_from_videos(), and ctx.videosBackRequested's docstring
        #: for why this exists.
        self._videos_back_key: Optional[str] = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        content.addWidget(self._build_nav())

        self.stack = QStackedWidget()
        self.views: dict[str, QWidget] = {}
        for key, cls in (
            ("library", LibraryView),
            ("videos", VideosView),
            ("charts", ChartsView),
            ("playlists", PlaylistsView),
            ("jukebox", JukeboxView),
            ("nowplaying", NowPlayingView),
            ("radio", TunerView),
            ("settings", SettingsView),
        ):
            view = cls(self.ctx)
            self.views[key] = view
            self.stack.addWidget(view)
        content.addWidget(self.stack, 1)
        outer.addLayout(content, 1)

        from .widgets.player_bar import PlayerBar

        self.player_bar = PlayerBar(self.player, self.ctx, self)
        self.player_bar.nowPlayingRequested.connect(lambda: self.navigate("nowplaying"))
        self.player_bar.queueRequested.connect(lambda: self.navigate("nowplaying"))
        outer.addWidget(self.player_bar)

        # video playback borrows the same persistent transport bar rather
        # than showing its own - see PlayerBar.enter_video_mode/exit_video_mode
        self.ctx.videoPlaybackStarted.connect(self.player_bar.enter_video_mode)
        self.ctx.videoPlaybackEnded.connect(self.player_bar.exit_video_mode)

        self.toast = QLabel("", self)
        self.toast.setStyleSheet(
            "background: #2a2f39; color: #f2f4f8; border: 1px solid #3a4150;"
            "border-radius: 10px; padding: 14px 22px; font-size: 15px;"
        )
        self.toast.setAlignment(Qt.AlignCenter)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)

        self._install_shortcuts()
        # 2026-09-07 follow-up, James: "When you start the app, default to
        # the Jukebox page." Startup used to always land on Albums ("album"
        # - not "artist", first though it now sits in the rail - kept the
        # screen James saw before the sidebar was flattened); now it opens
        # straight to the jukebox board instead.
        self.navigate("jukebox")
        self._start_web_remote()

    def _start_web_remote(self) -> None:
        """2026-10-13 phone remote (services/web_remote.py): back on at
        launch if it was left on in Settings › Phone remote."""
        from ..services import web_remote

        try:
            with self.ctx.session() as db:
                prefs = web_remote.load_prefs(db)
        except Exception:  # pragma: no cover - never block startup on it
            logging.getLogger(__name__).exception("web remote: couldn't read its settings")
            return
        if prefs.enabled:
            ok, message = self.ctx.web_remote.start(prefs.port, prefs.name)
            if not ok:
                self._show_toast(f"Phone remote: {message}")

    # -- chrome ---------------------------------------------------------------

    #: Displayed size of the brand icon in _build_brand_row(). 2026-09-07,
    #: three rounds in one day: 28 (first pass) -> 56 ("make it a lot
    #: bigger") -> stacked layout + this size + the QIcon fix below ("it's
    #: completely blurry ... still could be bigger"). Stacking the icon
    #: above the wordmark (instead of beside it) uncoupled icon size from
    #: nav_width entirely, so this can go as big as looks good without
    #: fighting the sidebar's width the way a side-by-side layout did.
    _BRAND_ICON_SIZE = 88

    def _build_brand_row(self) -> QWidget:
        """The nav rail's header: MusicMgr's icon, upper-left.
        James, 2026-09-07: "put the ico image in the upper left area of the
        MusicMgr application" - this row is the upper-left area (the rail's
        first row, above every nav button). Replaces the old plain-text-only
        QLabel#NavBrand. Falls back to a QLabel#NavBrandText wordmark if the
        icon asset is missing (same guard used for the splash and window
        icon) - there's otherwise nothing here identifying the app at all,
        since the icon artwork already has "MusicMgr" lettered into it (see
        the next paragraph).

        Icon over text, not beside it, as of a same-day follow-up - a
        side-by-side row forces a tradeoff between icon size and sidebar
        width (a bigger icon needs a wider row to avoid clipping the
        wordmark, which fought the earlier "make the sidebar narrower"
        request); stacked, the icon can be sized independently.

        The blurriness James reported after that was a real bug, not a
        resolution limit: config.APP_ICON is a multi-resolution .ico (16 up
        to 256px), and `QPixmap(path)` - what the first two passes used -
        defaults to that file's *first* frame, which for this Pillow-built
        icon is the smallest one (16x16) - so scaling up to even 56px was
        stretching a 16px source. `QIcon(path).pixmap(size)` instead picks
        whichever embedded frame best matches the requested size.

        Final same-day follow-up: "Looks good, you can remove the MusicMgr
        text now below the icon" - the icon artwork is itself a badge with
        "MusicMgr" lettered across it, so the separate QLabel wordmark
        underneath was redundant once the icon was big enough (88px) to
        read on its own."""
        row = QFrame()
        row.setObjectName("NavBrand")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(16, 16, 16, 16)
        row_layout.setSpacing(10)

        icon_shown = False
        if config.APP_ICON.is_file():
            size = self._BRAND_ICON_SIZE
            icon_pixmap = QIcon(str(config.APP_ICON)).pixmap(size, size)
            if not icon_pixmap.isNull():
                icon_label = QLabel()
                icon_label.setObjectName("NavBrandIcon")
                icon_label.setPixmap(icon_pixmap)
                row_layout.addWidget(icon_label)
                icon_shown = True

        if not icon_shown:
            brand_text = QLabel(config.APP_NAME)
            brand_text.setObjectName("NavBrandText")
            row_layout.addWidget(brand_text)
        return row

    #: extra room added on top of the widest real nav button's own
    #: `sizeHint()` in `_nav_rail_width` below - just enough that the
    #: longest label isn't pressed right up against the button's own
    #: edge, without padding the rail out to an arbitrary fixed width.
    _NAV_WIDTH_BUFFER = 16

    def _build_nav(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("NavRail")
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(2)

        layout.addWidget(self._build_brand_row())

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        # every button that actually sits on the rail - fed to
        # _nav_rail_width below once they've all been built, so the rail
        # sizes itself off whichever one turns out widest rather than
        # guessing "Title Details" is always going to be it.
        rail_buttons: list[QPushButton] = []
        for key, label in NAV_ITEMS:
            btn = QPushButton(label)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self.navigate(k))
            self._nav_group.addButton(btn)
            self._nav_buttons[key] = btn
            rail_buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch(1)
        self.fullscreen_btn = QPushButton("Full screen")
        self.fullscreen_btn.setObjectName("NavButton")
        self.fullscreen_btn.setCursor(Qt.PointingHandCursor)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        rail_buttons.append(self.fullscreen_btn)
        layout.addWidget(self.fullscreen_btn)

        quit_btn = QPushButton("Exit")
        quit_btn.setObjectName("NavQuit")
        quit_btn.setCursor(Qt.PointingHandCursor)
        quit_btn.setToolTip("Close MusicMgr (Ctrl+Q)")
        quit_btn.clicked.connect(self.quit_app)
        rail_buttons.append(quit_btn)
        layout.addWidget(quit_btn)

        rail.setFixedWidth(self._nav_rail_width(rail_buttons))
        return rail

    def _nav_rail_width(self, buttons: list[QPushButton]) -> int:
        """2026-09-13 follow-up (James: this app runs on a small
        touchscreen jukebox panel, so "let's make the rail a bit smaller
        ... where I think space may be saved"): `config.TOUCH["nav_width"]`
        used to be a hand-picked constant, sized by eye against whatever
        the nav labels happened to look like at the time (see that key's
        own removal comment in config.py for its 220 -> 176 -> 200
        history - the last jump only because the brand icon, back when it
        sat *beside* the wordmark, needed the extra room; a constraint
        that no longer even applies now that `_build_brand_row` stacks
        the icon above the text instead).

        Rather than pick yet another fixed number by eye - and risk it
        going stale again the next time a label or `TOUCH["font_base"]`
        changes - this measures every real nav button's own `sizeHint()`.
        That already reflects the live `#NavButton`/`#NavQuit` QSS (the
        same 18px each-side padding, `nav_item_height`, and `font_base`
        every other touch control on the rail uses) because `main()`
        below calls `app.setStyleSheet(...)` before `MainWindow` - and
        therefore these buttons - ever gets constructed. Whichever button
        turns out widest wins, plus `_NAV_WIDTH_BUFFER`, so the rail is
        always exactly as wide as it needs to be for whatever's actually
        on it, on whatever machine it's actually running on - no
        Windows-vs-whatever-built-it font substitution to second-guess."""
        widest = max(btn.sizeHint().width() for btn in buttons)
        return widest + self._NAV_WIDTH_BUFFER

    def _install_shortcuts(self) -> None:
        pairs = [
            ("Space", self.player.toggle),
            ("Right", lambda: self.player.seek_relative(10000)),
            ("Left", lambda: self.player.seek_relative(-10000)),
            ("Ctrl+Right", lambda: self.player.next(user_initiated=True)),
            ("Ctrl+Left", self.player.previous),
            ("F11", self.toggle_fullscreen),
            ("Escape", lambda: self.showNormal() if self.isFullScreen() else None),
            ("Ctrl+Q", self.quit_app),
            ("Ctrl+W", self.quit_app),
        ]
        for sequence, handler in pairs:
            QShortcut(QKeySequence(sequence), self, activated=handler)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.fullscreen_btn.setText(
            "Exit full screen" if self.isFullScreen() else "Full screen"
        )

    def quit_app(self) -> None:
        """Confirm, then shut down. Confirmation matters on a touch panel where
        the Exit button sits one stray tap away from the nav items."""
        if self.player.is_playing():
            answer = QMessageBox.question(
                self,
                "Exit MusicMgr",
                "Music is still playing. Close the app?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.close()

    # -- navigation -----------------------------------------------------------

    def navigate(self, key: str) -> None:
        # Artists/Albums/Tracks/Title Details all route to the one shared
        # LibraryView, by mode, rather than to a view of their own.
        if key in LIBRARY_MODE_KEYS:
            self.views["library"].select_mode(key)
            view = self.views["library"]
        else:
            view = self.views.get(key)
        if view is None:
            return
        # remember where we're navigating *from*, but only the first time we
        # land on Now Playing after being somewhere else - re-entering Now
        # Playing while already there (e.g. the PlayerBar's "Now Playing"
        # button tapped twice) must not overwrite an already-recorded back
        # target with "nowplaying" itself
        if key == "nowplaying" and self._current_key not in (None, "nowplaying"):
            self._nowplaying_back_key = self._current_key
        # same tracking, for the same reason, now that a video search match
        # (LibraryView._activate_video/_activate_video_group) routes here
        # via ctx.navigateRequested.emit("videos") without Videos being a
        # button of its own to highlight/return to - see
        # ctx.videosBackRequested's docstring.
        if key == "videos" and self._current_key not in (None, "videos"):
            self._videos_back_key = self._current_key
        self._current_key = key
        self.stack.setCurrentWidget(view)
        if key == "library":
            # a raw "library" hand-off (Now Playing's tappable artist/album
            # jumping straight here - see ui/views/nowplaying.py) rather than
            # one of Library's own top-level mode buttons - highlight
            # whichever mode select_mode() left it on instead of leaving
            # every one of those buttons unchecked. QButtonGroup's own
            # exclusivity handles unchecking the previous button in every
            # other case, now that all of them (including the four Library
            # modes) share one flat, exclusive group.
            button = self._nav_buttons.get(view.mode)
        else:
            button = self._nav_buttons.get(key)
        if button is not None:
            button.setChecked(True)
        view.refresh()

    def _go_back_from_nowplaying(self) -> None:
        """Now Playing's "‹ Back" button. Falls back to Library on the off
        chance nothing was ever recorded (shouldn't happen in practice -
        navigate("library") already runs once at startup before anything
        else can reach Now Playing)."""
        self.navigate(self._nowplaying_back_key or "library")

    def _go_back_from_videos(self) -> None:
        """The embedded video player's "‹ Back" button (see
        ctx.videosBackRequested's docstring) - same shape as
        _go_back_from_nowplaying above, same fallback reasoning."""
        self.navigate(self._videos_back_key or "library")

    def _show_toast(self, message: str) -> None:
        if not message:
            return
        self.toast.setText(message)
        self.toast.adjustSize()
        self.toast.move(
            (self.width() - self.toast.width()) // 2,
            self.height() - self.player_bar.height() - self.toast.height() - 24,
        )
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(3200)

    def closeEvent(self, event) -> None:
        self.ctx.web_remote.stop()
        self.player.stop()
        # gives any in-flight background spectrum analysis (2026-09-06
        # follow-up - see ui/widgets/player_bar.py's module docstring) a
        # brief window to finish rather than abandoning it mid-decode
        self.player_bar.shutdown()
        # 2026-10-01 volume levelling: a track being measured in the
        # background (services/player.py:LoudnessThread)
        stop_background_measuring()
        from ..services.radio import stop_background_work as stop_radio_work

        stop_radio_work()
        for key in ("settings", "radio"):
            view = self.views.get(key)
            if view is not None and hasattr(view, "shutdown"):
                view.shutdown()
        self.ctx.tuner.shutdown()
        # 2026-10-19: today's library backup, the first time the app closes
        # each day (services/library_backup.py) - after everything above has
        # stopped writing, before the window goes
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            library_backup.make_daily_backup(config.DB_PATH, config.DB_BACKUP_DIR)
        finally:
            QApplication.restoreOverrideCursor()
        super().closeEvent(event)


#: settings key: the one-time compilation merge has run on this library
COMPILATION_MERGE_PREF = "compilation_merge_done"
FOLDER_ARTIST_REPAIR_PREF = "folder_album_artist_repair_done"
SIDE_COVERS_PREF = "side_covers_done"
UNKNOWN_ARTIST_REPAIR_PREF = "unknown_artist_repair_done"
CREDIT_RESPLIT_PREF = "credit_resplit_done"
ARTIST_MERGE_PREF = "artist_merges_v1_done"
#: 2026-10-21: for KING & COUNTRY - the band kept whole in song credits,
#: "for KING + COUNTRY" merged in, guests out of video artists
ARTIST_MERGE_V2_PREF = "artist_merges_v2_done"
CREDIT_RESPLIT_V2_PREF = "credit_resplit_v2_done"
VIDEO_ARTIST_REPAIR_PREF = "video_artist_repair_done"
#: 2026-10-21: albums split in two by a catalog number stored as
#: "88697187522, 88697187522" (Jordin Sparks and 8 others on PC1)
SPLIT_RELEASE_REPAIR_PREF = "split_release_repair_done"


def bootstrap_database() -> None:
    """Create the DB and seed the built-in charts and smart playlists."""
    config.ensure_dirs()
    init_engine()
    # 2026-09-24 (USB sync step 2): rename id-named artwork to MusicBee-style
    # names once per data folder, after backing up library.db - see
    # services/artwork_names.py
    with session_scope() as session:
        artwork_names.migrate_if_needed(session, db_path=config.DB_PATH)
    with session_scope() as session:
        pl_svc.ensure_default_playlists(session)
        pl_svc.ensure_builtin_playback_playlists(session)
        chart_svc.remove_orphaned_playback_charts(session)
        lib_svc.backfill_album_artists(session)
        # fix_artist_sort_keys and fix_single_artist_compilations used to
        # run here too - about 0.27 s of every start on PC1's library, for
        # checks that only find work after a scan. They run at the end of
        # every library scan now (scanner.tidy_after_scan, 2026-10-21).
        # 3.5 s on the real library (reads every file path) - a one-time
        # clean-up for databases scanned before the scanner learned about
        # compilations, so it runs once per data folder, not every start
        # (2026-09-24, "I don't want it taking a long time at startup")
        if updater.get_pref(session, COMPILATION_MERGE_PREF) != "1":
            lib_svc.merge_compilation_duplicates(session)
            updater.set_pref(session, COMPILATION_MERGE_PREF, "1")
        # 2026-10-17: records and singles the old folder-wide album-artist
        # rule filed under one artist (all of Old78Records under Bing
        # Crosby, their covers blank) go back to their own artists and
        # covers - once per data folder (it reads some files' tags)
        if updater.get_pref(session, FOLDER_ARTIST_REPAIR_PREF) != "1":
            try:
                with session.begin_nested():
                    fixed = scanner_svc.repair_folder_album_artists(session)
            except Exception:  # never block startup on it - tried again next start
                logging.getLogger(__name__).exception("album-artist repair failed")
            else:
                if fixed:
                    logging.getLogger(__name__).info(
                        "put %d albums back under their own album artist / cover", fixed)
                updater.set_pref(session, FOLDER_ARTIST_REPAIR_PREF, "1")
        # 2026-10-17: each 78 side's own label, for files already scanned
        # (once per data folder - it reads the 78s' pictures)
        if updater.get_pref(session, SIDE_COVERS_PREF) != "1":
            try:
                saved = scanner_svc.save_side_covers(session)
            except Exception:  # never block startup on it - tried again next start
                logging.getLogger(__name__).exception("side covers failed")
            else:
                if saved:
                    logging.getLogger(__name__).info("saved %d 78 side labels", saved)
                updater.set_pref(session, SIDE_COVERS_PREF, "1")
        # 2026-10-19: "? and the Mysterians" and untagged "Artist - Title"
        # files out from under one artist called "?" (once per data folder -
        # it re-reads only those files; see repair_unknown_artist_merge)
        if updater.get_pref(session, UNKNOWN_ARTIST_REPAIR_PREF) != "1":
            try:
                with session.begin_nested():
                    reread = scanner_svc.repair_unknown_artist_merge(session)
            except Exception:  # never block startup on it - tried again next start
                logging.getLogger(__name__).exception("unknown-artist repair failed")
            else:
                if reread:
                    logging.getLogger(__name__).info(
                        "re-read %d files filed under '?' / Unknown Artist", reread)
                updater.set_pref(session, UNKNOWN_ARTIST_REPAIR_PREF, "1")
        # 2026-10-19: every song's artist credits worked out again with the
        # new splitting rules ("Blood, Sweat & Tears" whole again, big-band
        # singers out of "Glenn Miller (Tex Beneke & chorus, vocal)") - from
        # the stored artist text, no files read; ~25 s once on PC1
        if updater.get_pref(session, CREDIT_RESPLIT_PREF) != "1":
            try:
                with session.begin_nested():
                    changed, removed = scanner_svc.resplit_track_credits(session)
            except Exception:  # never block startup on it - tried again next start
                logging.getLogger(__name__).exception("credit re-split failed")
            else:
                if changed:
                    logging.getLogger(__name__).info(
                        "re-split artist credits on %d tracks, removed %d stray artists",
                        changed, removed)
                updater.set_pref(session, CREDIT_RESPLIT_PREF, "1")
        # 2026-10-19: spelling variants of one artist merged (Tim McGraw /
        # Tim Mc Graw ...) - the reviewed list in services/artist_merge.py
        if updater.get_pref(session, ARTIST_MERGE_PREF) != "1":
            try:
                with session.begin_nested():
                    merged = artist_merge.apply_artist_merges(session)
            except Exception:  # never block startup on it - tried again next start
                logging.getLogger(__name__).exception("artist merges failed")
            else:
                if merged:
                    logging.getLogger(__name__).info("merged %d duplicate artists", merged)
                updater.set_pref(session, ARTIST_MERGE_PREF, "1")
        # 2026-10-21: for KING & COUNTRY under one artist - merge the "+"
        # spelling (the whole reviewed list again; groups already merged
        # just confirm their aliases), then re-split song credits so the
        # band stays whole instead of "for KING" + "COUNTRY" (167 songs on
        # PC1), then re-file videos with guests moved into the title. In
        # that order: the re-split and video pass both resolve names
        # through the aliases the merge records.
        _run_once(session, ARTIST_MERGE_V2_PREF, "artist merges v2",
                  lambda: artist_merge.apply_artist_merges(session), "merged %d duplicate artists")
        _run_once(session, CREDIT_RESPLIT_V2_PREF, "credit re-split v2",
                  lambda: scanner_svc.resplit_track_credits(session)[0],
                  "re-split artist credits on %d tracks")
        _run_once(session, VIDEO_ARTIST_REPAIR_PREF, "video artist repair",
                  lambda: video_scanner.repair_video_artists(session), "re-filed %d videos")
        _run_once(session, SPLIT_RELEASE_REPAIR_PREF, "split album repair",
                  lambda: lib_svc.merge_split_releases(session), "put %d split albums back together")


def _run_once(session, pref: str, label: str, work, message: str) -> None:
    """Run a one-time library repair once per data folder: inside a
    savepoint, logged, and never blocking startup - a failure is logged and
    tried again on the next start (the shape every repair above uses)."""
    if updater.get_pref(session, pref) == "1":
        return
    try:
        with session.begin_nested():
            count = work()
    except Exception:
        logging.getLogger(__name__).exception("%s failed", label)
        return
    if count:
        logging.getLogger(__name__).info(message, count)
    updater.set_pref(session, pref, "1")


def _setup_logging() -> None:
    """Send every module's `logging.getLogger(__name__).exception(...)` call
    (scanner.py's per-bad-file catch is the one that motivated this) to
    config.LOG_PATH, so it lands somewhere readable instead of nowhere.

    Nothing called logging.basicConfig or attached a handler anywhere in
    this app before now, so every log.exception(...) call was going to
    Python's default "last resort" handler - stderr - and the built .exe is
    a windowed (console=False) app with no stderr for anyone to see, so in
    practice these were going nowhere at all (James, 2026-09-08: a scan
    failure with the real cause masked by a generic SQLAlchemy message -
    diagnosing it turned up this as a second bug, since log.exception was
    already meant to capture exactly that kind of per-file failure)."""
    config.ensure_dirs()
    handler = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _prepare_for_update_restart(argv: list[str]) -> None:
    """In-app updates (2026-09-23, services/updater.py). "Restart now"
    launches the new exe with `--wait-pid <old pid>` and then closes the
    old one normally - wait for it to be fully gone before anything here
    opens library.db, so the two never hold the database at once."""
    pid = updater.parse_wait_pid(argv)
    if pid is not None:
        updater.wait_for_pid_exit(pid)


def _pre_database_housekeeping() -> None:
    """Before bootstrap_database() runs any schema migration: tidy up a
    half-finished update download, and copy library.db aside the first
    time a new release opens it (see updater.backup_database_if_version_changed
    - never raises)."""
    try:
        updater.cleanup_leftovers(updater.current_executable())
    except OSError:
        logging.getLogger(__name__).exception("update cleanup failed")
    made = updater.backup_database_if_version_changed(
        config.DB_PATH, config.DB_BACKUP_DIR, config.LAST_RUN_VERSION_FILE, RELEASE_VERSION
    )
    if made is not None:
        logging.getLogger(__name__).info("backed up library.db to %s before first run of %s", made, RELEASE_VERSION)


def main(argv: Optional[list[str]] = None, splash=None) -> int:
    """Start the app. Normally reached through musicmgr.launch.main (run.py),
    which has already created the QApplication and put the splash up before
    this module and all its pages were imported - `splash` is that splash.
    Called directly, it puts one up itself."""
    argv = list(argv if argv is not None else sys.argv)

    _prepare_for_update_restart(argv)
    _setup_logging()

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(config.APP_NAME)
    app.setStyleSheet(stylesheet())
    if config.APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(config.APP_ICON)))
    QGuiApplication.setAttribute(Qt.AA_SynthesizeTouchForUnhandledMouseEvents, False)

    # The splash stays up through the database work and building the
    # window below. launch.py normally shows it before this module is even
    # imported (2026-10-21 - see that module's docstring).
    if splash is None:
        from ..launch import build_splash, close_bootloader_splash

        splash = build_splash()
        if splash is not None:
            app.processEvents()
        close_bootloader_splash()

    restore = library_backup.apply_pending_restore(
        config.DB_PATH, config.DB_BACKUP_DIR, config.RESTORE_MARKER
    )
    _pre_database_housekeeping()
    bootstrap_database()

    window = MainWindow()
    if splash is not None:
        splash.finish(window)
    if "--fullscreen" in argv or "--kiosk" in argv:
        window.showFullScreen()
    elif "--maximized" in argv:
        # 2026-09-08, James: "how can I get the MusicMgr app to automatically
        # run in windows 11 at startup and to be maximized" - a plain
        # windowed launch (showMaximized() keeps the title bar/taskbar,
        # unlike the borderless --fullscreen/--kiosk mode above; a Startup-
        # folder shortcut's own "Run: Maximized" property isn't reliably
        # honored by Qt apps, so this flag is the reliable way to get there).
        window.showMaximized()
    else:
        window.show()

    if restore is not None:
        message = restore.error or (
            f"Library restored from {restore.restored_from.name}"
            + (f" - the library it replaced is saved as {restore.saved_current_as.name}"
               if restore.saved_current_as else "")
        )
        QTimer.singleShot(1500, lambda: window.ctx.notify(message))
    if "--after-update" in argv:
        QTimer.singleShot(1500, lambda: window.ctx.notify(f"Updated to MusicMgr {RELEASE_VERSION}"))
    # 2026-09-24 - James: "I should never be checking for updates or anything
    # that relies on an internet connection at startup" - no update check
    # here any more; Settings → Updates → "Check now" is the only trigger.
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
