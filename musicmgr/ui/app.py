"""Main window: nav rail, stacked views, persistent player bar."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplashScreen,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..db.session import init_engine, session_scope
from ..services import charts as chart_svc
from ..services import library as lib_svc
from ..services import playlists as pl_svc
from ..services.player import PlayerController
from .context import AppContext
from .theme import stylesheet
from .views.charts import ChartsView
from .views.jukebox import JukeboxView
from .views.library import LibraryView
from .views.nowplaying import NowPlayingView
from .views.playlists import PlaylistsView
from .views.settings import SettingsView
from .views.videos import VideosView

#: James, 2026-09-06: "remove the Library [item] as a left sidebar item and
#: make all those sub menus part of the main [nav]" - Artists/Albums/Tracks/
#: Title Details used to nest under one collapsible "Library" button (see
#: LIBRARY_MODE_KEYS below); each now gets its own top-level slot here,
#: in the exact order James asked for.
NAV_ITEMS = [
    ("artist", "Artists"),
    ("album", "Albums"),
    ("jukebox", "Jukebox"),
    ("track", "Tracks"),
    ("details", "Title Details"),
    # 2026-09-15, James: "introduce a Video sidebar menu option" - Videos
    # used to have no sidebar entry at all, reachable only mid-transit from
    # a search match or the artist page's own video list (see
    # ctx.playVideoRequested/focusVideoArtistRequested and
    # views/videos.py). Now a real destination, so its embedded player's
    # "‹ Back" button returns to its own table instead of leaving Videos
    # entirely - see VideosView._build_player.
    ("videos", "Videos"),
    ("charts", "Charts"),
    ("playlists", "Playlists"),
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
        self.player.errorOccurred.connect(self._show_toast)
        #: which section (a NAV_ITEMS key) was active right before Now
        #: Playing was opened, so its "‹ Back" button can return there - see
        #: navigate() and _go_back_from_nowplaying(). None until the first
        #: real navigation happens; falls back to "library" if somehow asked
        #: for before that.
        self._current_key: Optional[str] = None
        self._nowplaying_back_key: Optional[str] = None

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
        self.player.stop()
        # gives any in-flight background spectrum analysis (2026-09-06
        # follow-up - see ui/widgets/player_bar.py's module docstring) a
        # brief window to finish rather than abandoning it mid-decode
        self.player_bar.shutdown()
        super().closeEvent(event)


def bootstrap_database() -> None:
    """Create the DB and seed the built-in charts and smart playlists."""
    config.ensure_dirs()
    init_engine()
    with session_scope() as session:
        pl_svc.ensure_default_playlists(session)
        pl_svc.ensure_builtin_playback_playlists(session)
        chart_svc.remove_orphaned_playback_charts(session)
        lib_svc.fix_artist_sort_keys(session)
        lib_svc.backfill_album_artists(session)
        lib_svc.merge_compilation_duplicates(session)


def _build_splash() -> Optional[QSplashScreen]:
    """A launch splash showing James's MusicMgr banner - see main()'s
    docstring-length comment below for why it's shown where it is. Returns
    None (rather than raising) if the banner asset is missing, so a
    from-source checkout that hasn't pulled musicmgr/assets/ yet - or any
    future trimmed-down build - still starts normally without it."""
    if not config.SPLASH_IMAGE.is_file():
        return None
    pixmap = QPixmap(str(config.SPLASH_IMAGE))
    if pixmap.isNull():
        return None
    # Full-res source is 2170x725 - a splash that size would dwarf the
    # 1440x900 main window on most screens, so it's scaled down for display
    # only; the bundled asset stays full resolution for any other future use.
    pixmap = pixmap.scaledToWidth(640, Qt.SmoothTransformation)
    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
    splash.show()
    return splash


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


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv)

    _setup_logging()

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(config.APP_NAME)
    app.setStyleSheet(stylesheet())
    if config.APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(config.APP_ICON)))
    QGuiApplication.setAttribute(Qt.AA_SynthesizeTouchForUnhandledMouseEvents, False)

    # The splash needs a QApplication to paint into, which is why app
    # creation moved ahead of bootstrap_database() below - that's the one
    # part of startup with real, library-size-dependent duration (schema
    # creation plus three library-wide fix-up passes), and previously ran
    # with no window on screen at all. processEvents() forces the splash to
    # actually paint before that blocking call starts.
    splash = _build_splash()
    if splash is not None:
        app.processEvents()

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
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
