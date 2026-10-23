"""Embedded video player pane: the picture, plus an in-app full-screen
toggle.

Play/pause and seek do NOT live here while embedded in the Videos tab -
the persistent audio PlayerBar takes over those for as long as a video is
playing (see enter_video_mode/exit_video_mode in ui/widgets/player_bar.py,
wired up here via AppContext.videoPlaybackStarted/Ended) so there's only
ever one transport on screen, matching what Now Playing does for music.
services/video_player.py has more on why video keeps its own playback
engine separate from the music queue even though they now share one set of
buttons.

The one exception is full-screen: `self.controls` (play/pause + seek, kept
wired to `self.controller` the whole time) stays hidden while embedded and
is only made visible once full-screen, since the main window - and its
PlayerBar - isn't reachable there. Full screen itself is implemented by
reparenting the same QVideoWidget and control bar into a separate
frameless top-level window rather than building (and wiring) a second copy
of everything - see toggle_fullscreen.

While embedded, the top row holds only "‹ Back" and "⤢ Full screen", at
either edge - no title, since the PlayerBar already shows title and
artist. `self.controls` is hidden outright (Qt's layouts don't reserve
space for a hidden widget), so its row goes to the video; `fullscreen_btn`
moves into `self.controls` for full screen and back again. VideosView
also hides its browse chrome while this pane is up - see
`_set_browse_chrome_visible` there.

Captions: a video with a `.srt` beside it (services/subtitles.py) gets a
caption bar - white text on black, directly under the picture, one or two
lines. It's under rather than over the picture because QVideoWidget draws
into a native window of its own on Windows and Mint, which a widget laid
on top can end up hidden behind. The picture and the bar travel together
as `self.video_screen`, so full screen keeps the captions (larger). A "CC"
button, shown only for a video with subtitles, turns them on and off; the
choice is remembered (CAPTIONS_PREF) for every video and every launch.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from ...services import updater
from ...services.library import format_duration
from ...services.subtitles import Captions, load_captions
from ...services.video_player import VideoController
from .common import PAUSE_GLYPH, ChipButton, TouchButton, dim_label

#: settings key: "1" captions on (the default), "0" off
CAPTIONS_PREF = "video_captions"
#: caption text size, embedded / full screen
CAPTION_PX = 24
CAPTION_PX_FULLSCREEN = 38


class VideoPlayerPanel(QWidget):
    #: the "‹ Back" button was tapped. Target-less by design - its one
    #: connection (VideosView._build_player) re-emits
    #: ctx.videosBackRequested, which MainWindow answers by returning to
    #: whatever section was active before (see that signal's docstring).
    backRequested = Signal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._seeking = False
        self._fullscreen_window: Optional[QWidget] = None
        #: this video's subtitles, None when it has no .srt
        self._captions: Optional[Captions] = None
        self._captions_on = self._load_captions_pref()

        self.controller = VideoController(self)
        self.controller.positionChanged.connect(self._on_position)
        self.controller.durationChanged.connect(self._on_duration)
        self.controller.playbackStateChanged.connect(self._on_state)
        self.controller.errorOccurred.connect(self.ctx.notify)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        # the top row is just two buttons; no reason to hold the picture
        # far off them
        root.setSpacing(6)

        top = QHBoxLayout()
        # "‹ Back", not "‹ Videos" - it returns to wherever the video was
        # opened from (see backRequested), matching NowPlayingView's own
        # "‹ Back" button.
        back = TouchButton("‹ Back")
        back.setFixedWidth(130)
        back.clicked.connect(self.backRequested.emit)
        top.addWidget(back)
        # no title label: the PlayerBar shows title and artist. One
        # stretch, so the buttons sit at the edges.
        top.addStretch(1)
        # captions on/off - only shown for a video with subtitles; travels
        # with fullscreen_btn into the full-screen controls
        self.cc_btn = ChipButton("CC")
        self.cc_btn.setToolTip("Show or hide the captions (from the .srt file beside the video)")
        self.cc_btn.setChecked(self._captions_on)
        self.cc_btn.toggled.connect(self._set_captions_on)
        self.cc_btn.setVisible(False)
        top.addWidget(self.cc_btn)
        # embedded home for the full-screen toggle - moved into
        # self.controls' row while full screen, via toggle_fullscreen.
        self.fullscreen_btn = TouchButton("⤢ Full screen")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        top.addWidget(self.fullscreen_btn)
        self._top_layout = top
        root.addLayout(top)

        # `stage` is the video widget's and control bar's normal (embedded)
        # home; toggle_fullscreen reparents both of them out of it and back.
        self.stage = QWidget()
        self._stage_layout = QVBoxLayout(self.stage)
        self._stage_layout.setContentsMargins(0, 0, 0, 0)
        self._stage_layout.setSpacing(8)

        # the picture with its caption bar under it - moved as one into and
        # out of the full-screen window
        self.video_screen = QWidget()
        self.video_screen.setObjectName("VideoScreen")
        self.video_screen.setStyleSheet("#VideoScreen { background: black; }")
        screen_layout = QVBoxLayout(self.video_screen)
        screen_layout.setContentsMargins(0, 0, 0, 0)
        screen_layout.setSpacing(0)

        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(360)
        self.video_widget.setStyleSheet("background: black;")
        self.controller.set_video_output(self.video_widget)
        screen_layout.addWidget(self.video_widget, 1)

        self.caption = QLabel("")
        self.caption.setObjectName("VideoCaption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        self._style_caption(CAPTION_PX)
        self.caption.setVisible(False)
        screen_layout.addWidget(self.caption)

        self._stage_layout.addWidget(self.video_screen, 1)

        self.controls = self._build_controls()
        self._stage_layout.addWidget(self.controls)
        # nothing inside it shows while embedded - hide the row itself, not
        # just its contents, so Qt gives the space to video_widget.
        self.controls.setVisible(False)

        root.addWidget(self.stage, 1)

    def _build_controls(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Card")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        self.play_btn = TouchButton("▶")
        self.play_btn.setObjectName("TransportMain")
        self.play_btn.clicked.connect(self.controller.toggle)
        row.addWidget(self.play_btn)

        self.elapsed = dim_label("0:00")
        self.elapsed.setFixedWidth(52)
        self.elapsed.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderReleased.connect(self._commit_seek)
        self.remaining = dim_label("--:--")
        self.remaining.setFixedWidth(52)
        row.addWidget(self.elapsed)
        row.addWidget(self.seek, 1)
        row.addWidget(self.remaining)

        # play/pause + seek stay wired but hidden while embedded - the
        # PlayerBar already shows them (enter_video_mode); they're only
        # shown once full-screen, see _enter_fullscreen/_exit_fullscreen.
        self._playback_widgets = [self.play_btn, self.elapsed, self.seek, self.remaining]
        for w in self._playback_widgets:
            w.setVisible(False)
        return bar

    # -- playback ---------------------------------------------------------

    def play(self, video_id: int, path: str, title: str, artist: Optional[str]) -> None:
        # title/artist are shown by the PlayerBar, via
        # videoPlaybackStarted below
        self.ctx.player.pause()  # don't talk over a video with background music
        self._captions = load_captions(path)
        self.caption.setText("")
        self.cc_btn.setVisible(self._captions is not None)
        self._update_caption_visibility()
        self.controller.play_path(path, video_id)
        self.ctx.videoPlaybackStarted.emit(self.controller, title, artist or "")

    def stop(self) -> None:
        self.controller.stop()

    def _commit_seek(self) -> None:
        duration = self.controller.duration()
        if duration:
            self.controller.seek(int(duration * self.seek.value() / 1000))
        self._seeking = False

    def _on_position(self, ms: int) -> None:
        duration = self.controller.duration()
        if duration and not self._seeking:
            self.seek.setValue(int(1000 * ms / duration))
        self.elapsed.setText(format_duration(ms))
        if self._captions is not None and self._captions_on:
            text = self._captions.text_at(ms)
            if text != self.caption.text():
                self.caption.setText(text)
        if duration:
            self.remaining.setText("-" + format_duration(max(0, duration - ms)))

    def _on_duration(self, ms: int) -> None:
        self.remaining.setText(format_duration(ms) if ms else "--:--")

    def _on_state(self, state: str) -> None:
        # see PAUSE_GLYPH in ui/widgets/common.py.
        self.play_btn.setText(PAUSE_GLYPH if state == "playing" else "▶")

    # -- captions -------------------------------------------------------------

    def _style_caption(self, px: int) -> None:
        # room for two lines even between cues, so the picture doesn't
        # jump each time a line comes and goes
        self.caption.setStyleSheet(
            f"#VideoCaption {{ background: black; color: #ffffff; font-size: {px}px;"
            f" font-weight: 600; padding: 6px 24px 10px 24px; }}")
        self.caption.setMinimumHeight(int(px * 2.6) + 16)

    def _update_caption_visibility(self) -> None:
        self.caption.setVisible(self._captions is not None and self._captions_on)

    def _set_captions_on(self, on: bool) -> None:
        self._captions_on = on
        if on and self._captions is not None:
            self.caption.setText(self._captions.text_at(self.controller.position()))
        self._update_caption_visibility()
        try:
            with self.ctx.session() as db:
                updater.set_pref(db, CAPTIONS_PREF, "1" if on else "0")
        except Exception:  # pragma: no cover - never let a setting break playback
            pass

    def _load_captions_pref(self) -> bool:
        try:
            with self.ctx.session() as db:
                return updater.get_pref(db, CAPTIONS_PREF, "1") != "0"
        except Exception:  # pragma: no cover
            return True

    # -- full screen --------------------------------------------------------

    def toggle_fullscreen(self) -> None:
        if self._fullscreen_window is None:
            self._enter_fullscreen()
        else:
            self._exit_fullscreen()

    def _enter_fullscreen(self) -> None:
        window = QWidget(self.window())
        window.setWindowFlag(Qt.Window, True)
        window.setStyleSheet("background: black;")
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._stage_layout.removeWidget(self.video_screen)
        self._stage_layout.removeWidget(self.controls)
        layout.addWidget(self.video_screen, 1)
        layout.addWidget(self.controls)
        self._style_caption(CAPTION_PX_FULLSCREEN)
        # fullscreen_btn moves out of the top bar (which stays behind in the
        # main window) into self.controls' row, so it's reachable full screen
        self._top_layout.removeWidget(self.cc_btn)
        self._top_layout.removeWidget(self.fullscreen_btn)
        self.controls.layout().addWidget(self.cc_btn)
        self.controls.layout().addWidget(self.fullscreen_btn)
        # the PlayerBar (and the rest of the main window) isn't reachable in
        # a separate full-screen top-level window - show play/pause + seek
        # here for the duration, the one case where a second copy earns its
        # keep rather than just duplicating what's already on screen
        for w in self._playback_widgets:
            w.setVisible(True)
        self.controls.setVisible(True)
        self.fullscreen_btn.setText("⤢ Exit full screen")
        QShortcut(QKeySequence("Escape"), window, activated=self.toggle_fullscreen)
        self._fullscreen_window = window
        window.showFullScreen()

    def _exit_fullscreen(self) -> None:
        window = self._fullscreen_window
        if window is None:
            return
        layout = window.layout()
        layout.removeWidget(self.video_screen)
        layout.removeWidget(self.controls)
        self.controls.layout().removeWidget(self.cc_btn)
        self.controls.layout().removeWidget(self.fullscreen_btn)
        self._top_layout.addWidget(self.cc_btn)
        self._top_layout.addWidget(self.fullscreen_btn)
        self._stage_layout.insertWidget(0, self.video_screen, 1)
        self._style_caption(CAPTION_PX)
        self._stage_layout.addWidget(self.controls)
        for w in self._playback_widgets:
            w.setVisible(False)
        self.controls.setVisible(False)
        self.fullscreen_btn.setText("⤢ Full screen")
        self._fullscreen_window = None
        window.close()
        window.deleteLater()

    def hideEvent(self, event) -> None:
        # leaving the Videos tab (or the app closing) should never leave a
        # video quietly playing off-screen, full-screen chrome included
        if self._fullscreen_window is not None:
            self._exit_fullscreen()
        self.controller.pause()
        self.ctx.videoPlaybackEnded.emit()
        super().hideEvent(event)
