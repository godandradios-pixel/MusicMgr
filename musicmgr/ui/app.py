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

#: the nav rail, top to bottom. Artists/Albums/Tracks/Title Details are
#: top-level slots (routed to LibraryView - see LIBRARY_MODE_KEYS below).
#: Jukebox leads the rail; it's also the page MusicMgr opens on.
NAV_ITEMS = [
    ("jukebox", "Jukebox"),
    ("artist", "Artists"),
    ("album", "Albums"),
    ("track", "Tracks"),
    ("details", "Title Details"),
    # Videos has no sidebar entry: it's reached from a search match or
    # Title Details' "Videos only" filter (ctx.playVideoRequested/
    # focusVideoArtistRequested, wired below). The "videos" key stays in
    # the views mapping for that - see ctx.videosBackRequested's docstring
    # for how the embedded player's "‹ Back" works.
    ("playlists", "Playlists"),
    ("charts", "Charts"),
    # old-time radio shows and internet stations, see ui/views/tuner.py
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
        #: player's "‹ Back" button (Videos isn't a sidebar destination) -
        #: see navigate(), _go_back_from_videos() and ctx.videosBackRequested.
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
        # the app opens on the Jukebox page
        self.navigate("jukebox")
        self._start_web_remote()

    def _start_web_remote(self) -> None:
        """Phone remote (services/web_remote.py): back on at launch if it
        was left on in Settings › Phone remote."""
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

    #: Displayed size of the brand icon in _build_brand_row(). The icon
    #: sits above the rail's buttons rather than beside a wordmark, so its
    #: size is independent of the rail's width.
    _BRAND_ICON_SIZE = 88

    def _build_brand_row(self) -> QWidget:
        """The nav rail's header: MusicMgr's icon, upper-left. The icon
        artwork has "MusicMgr" lettered into it, so there's no separate
        wordmark - except a QLabel#NavBrandText fallback if the icon asset
        is missing (same guard as the splash and window icon).

        config.APP_ICON is a multi-resolution .ico; `QPixmap(path)` would
        take its first (16x16) frame and blur when scaled, so
        `QIcon(path).pixmap(size)` picks the embedded frame that best
        matches the requested size."""
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
        # every button on the rail - fed to _nav_rail_width below so the
        # rail sizes itself off whichever turns out widest.
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
        """Size the rail to its widest real button rather than a fixed
        constant (the app runs on a small touchscreen panel, so width
        matters). Each button's `sizeHint()` already reflects the live
        `#NavButton`/`#NavQuit` QSS (padding, `nav_item_height`,
        `font_base`) because `main()` calls `app.setStyleSheet(...)` before
        `MainWindow` is constructed, so this stays right whatever the
        labels, font size or platform font substitution."""
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
        # same tracking for Videos, which is reached via
        # ctx.navigateRequested.emit("videos") (e.g. a video search match)
        # without a button of its own - see ctx.videosBackRequested.
        if key == "videos" and self._current_key not in (None, "videos"):
            self._videos_back_key = self._current_key
        self._current_key = key
        self.stack.setCurrentWidget(view)
        if key == "library":
            # a raw "library" hand-off (e.g. Now Playing's tappable
            # artist/album - see ui/views/nowplaying.py): highlight whichever
            # mode select_mode() left it on. In every other case the one
            # flat, exclusive QButtonGroup unchecks the previous button.
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
        # gives any in-flight background spectrum analysis (see
        # ui/widgets/player_bar.py's module docstring) a brief window to
        # finish rather than abandoning it mid-decode
        self.player_bar.shutdown()
        # volume levelling: a track being measured in the background
        # (services/player.py:LoudnessThread)
        stop_background_measuring()
        from ..services.radio import stop_background_work as stop_radio_work

        stop_radio_work()
        for key in ("settings", "radio"):
            view = self.views.get(key)
            if view is not None and hasattr(view, "shutdown"):
                view.shutdown()
        self.ctx.tuner.shutdown()
        from . import video_details_worker

        video_details_worker.stop(self.ctx)
        # today's library backup, the first time the app closes each day
        # (services/library_backup.py) - after everything above has stopped
        # writing, before the window goes
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
#: for KING & COUNTRY - the band kept whole in song credits,
#: "for KING + COUNTRY" merged in, guests out of video artists
ARTIST_MERGE_V2_PREF = "artist_merges_v2_done"
CREDIT_RESPLIT_V2_PREF = "credit_resplit_v2_done"
VIDEO_ARTIST_REPAIR_PREF = "video_artist_repair_done"
#: albums split in two by a catalog number stored as
#: "88697187522, 88697187522"
SPLIT_RELEASE_REPAIR_PREF = "split_release_repair_done"


def bootstrap_database() -> None:
    """Create the DB and seed the built-in charts and smart playlists."""
    config.ensure_dirs()
    init_engine()
    # USB sync: rename id-named artwork to MusicBee-style names once per
    # data folder, after backing up library.db - see services/artwork_names.py
    with session_scope() as session:
        artwork_names.migrate_if_needed(session, db_path=config.DB_PATH)
    with session_scope() as session:
        pl_svc.ensure_default_playlists(session)
        pl_svc.ensure_builtin_playback_playlists(session)
        chart_svc.remove_orphaned_playback_charts(session)
        lib_svc.backfill_album_artists(session)
        # fix_artist_sort_keys and fix_single_artist_compilations run at
        # the end of every library scan (scanner.tidy_after_scan), not here.
        # Compilation merging is a slow one-time clean-up (reads every file
        # path) for databases scanned before the scanner knew about
        # compilations, so it runs once per data folder, not every start.
        if updater.get_pref(session, COMPILATION_MERGE_PREF) != "1":
            lib_svc.merge_compilation_duplicates(session)
            updater.set_pref(session, COMPILATION_MERGE_PREF, "1")
        # records and singles an old folder-wide album-artist rule filed
        # under one artist go back to their own artists and covers - once per
        # data folder (it reads some files' tags)
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
        # each 78 side's own label, for files already scanned (once per
        # data folder - it reads the 78s' pictures)
        if updater.get_pref(session, SIDE_COVERS_PREF) != "1":
            try:
                saved = scanner_svc.save_side_covers(session)
            except Exception:  # never block startup on it - tried again next start
                logging.getLogger(__name__).exception("side covers failed")
            else:
                if saved:
                    logging.getLogger(__name__).info("saved %d 78 side labels", saved)
                updater.set_pref(session, SIDE_COVERS_PREF, "1")
        # "? and the Mysterians" and untagged "Artist - Title" files out
        # from under one artist called "?" (once per data folder - it re-reads
        # only those files; see repair_unknown_artist_merge)
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
        # every song's artist credits worked out again with the current
        # splitting rules ("Blood, Sweat & Tears" whole, big-band singers out
        # of "Glenn Miller (Tex Beneke & chorus, vocal)") - from the stored
        # artist text, no files read; slow, once per data folder
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
        # spelling variants of one artist merged (Tim McGraw / Tim Mc Graw
        # ...) - the reviewed list in services/artist_merge.py
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
        # for KING & COUNTRY under one artist - merge the "+" spelling (the
        # whole reviewed list again; groups already merged just confirm their
        # aliases), then re-split song credits so the band stays whole instead
        # of "for KING" + "COUNTRY", then re-file videos with guests moved into
        # the title. In that order: the re-split and video pass both resolve
        # names through the aliases the merge records.
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
    (e.g. scanner.py's per-bad-file catch) to config.LOG_PATH. Without a
    handler they'd go to stderr, which the windowed (console=False) .exe
    doesn't have."""
    config.ensure_dirs()
    handler = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _prepare_for_update_restart(argv: list[str]) -> None:
    """In-app updates (services/updater.py). "Restart now" launches the
    new exe with `--wait-pid <old pid>` and then closes the old one normally
    - wait for it to be fully gone before anything here opens library.db,
    so the two never hold the database at once."""
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
    # imported (see that module's docstring).
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
        # for a Windows startup shortcut: showMaximized() keeps the title
        # bar/taskbar (unlike borderless --fullscreen/--kiosk), and a
        # shortcut's own "Run: Maximized" isn't reliably honored by Qt apps.
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
    # video thumbnails and years, in the background a couple of minutes in
    # - only when some video still needs them (one COUNT query), so an
    # ordinary start does no extra work (ui/video_details_worker.py)
    from . import video_details_worker

    QTimer.singleShot(120_000, lambda: video_details_worker.start(window.ctx, only_if_pending=True))
    # nothing that needs the internet runs at startup - Settings → Updates
    # → "Check now" is the only update-check trigger.
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
