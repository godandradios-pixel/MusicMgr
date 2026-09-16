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

2026-09-07 follow-up - James, looking at the embedded player: "That full
screen 'button' at the bottom takes up too much space and makes my video
window smaller." Originally `fullscreen_btn` lived inside `self.controls`
alongside the (while embedded, hidden) playback widgets, so that whole
touch-sized bar - built for a full transport row - stayed visible the
entire time embedded just to host one button, stealing a fixed slice of
height from `video_widget`'s own stretch factor. `fullscreen_btn` now
lives in the top title bar instead (`self._top_layout`, next to "‹ Back"
and the title) while embedded, and `self.controls` is hidden outright
there rather than merely emptied - Qt's layouts don't reserve space for a
hidden widget, so hiding it (rather than just hiding what's inside it)
actually gives that reclaimed row back to the video. `toggle_fullscreen`
reparents `fullscreen_btn` into `self.controls`' own row alongside the
playback widgets for the fullscreen window - the same "move it out and
back" idiom already used there for `video_widget`/`self.controls`
themselves - so it still travels into full-screen and back exactly as
before; only its embedded home moved.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from ...services.library import format_duration
from ...services.video_player import VideoController
from .common import PAUSE_GLYPH, TouchButton, dim_label


class VideoPlayerPanel(QWidget):
    #: the "‹ Back" button was tapped. Generic and target-less by design -
    #: this panel doesn't know (or care) what should happen next; its one
    #: connection (VideosView._build_player) just re-emits
    #: ctx.videosBackRequested, which MainWindow answers by returning to
    #: whatever section was active before a search match routed here (see
    #: that signal's own docstring - a brief 2026-09-15 detour had this
    #: drop onto Videos' own table instead, back while Videos had a real
    #: sidebar entry of its own).
    backRequested = Signal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._seeking = False
        self._fullscreen_window: Optional[QWidget] = None

        self.controller = VideoController(self)
        self.controller.positionChanged.connect(self._on_position)
        self.controller.durationChanged.connect(self._on_duration)
        self.controller.playbackStateChanged.connect(self._on_state)
        self.controller.errorOccurred.connect(self.ctx.notify)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        top = QHBoxLayout()
        # Labeled "‹ Back" rather than "‹ Videos" (2026-09-06) - it no longer
        # returns to the Videos table (see backRequested's docstring below
        # and VideosView._build_player), so naming a destination it doesn't
        # go to would be actively misleading. Matches NowPlayingView's own
        # "‹ Back" button, the same fix for the same kind of dead end.
        back = TouchButton("‹ Back")
        back.setFixedWidth(130)
        back.clicked.connect(self.backRequested.emit)
        top.addWidget(back)
        top.addStretch(1)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 19px; font-weight: 600;")
        top.addWidget(self.title_label)
        top.addStretch(1)
        # embedded home for the full-screen toggle (2026-09-07 follow-up -
        # see module docstring) - moved out to self.controls' own row while
        # actually full screen, via toggle_fullscreen.
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

        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(360)
        self.video_widget.setStyleSheet("background: black;")
        self.controller.set_video_output(self.video_widget)
        self._stage_layout.addWidget(self.video_widget, 1)

        self.controls = self._build_controls()
        self._stage_layout.addWidget(self.controls)
        # nothing inside it shows while embedded any more (fullscreen_btn
        # moved to the top bar above; the playback widgets are already
        # hidden individually) - hide the row itself too, rather than just
        # its contents, so Qt reclaims the space for video_widget instead
        # of leaving an empty bar's own padding behind (2026-09-07 follow-
        # up, see module docstring).
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
        self.title_label.setText(" — ".join(x for x in (title, artist) if x))
        self.ctx.player.pause()  # don't talk over a video with background music
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
        if duration:
            self.remaining.setText("-" + format_duration(max(0, duration - ms)))

    def _on_duration(self, ms: int) -> None:
        self.remaining.setText(format_duration(ms) if ms else "--:--")

    def _on_state(self, state: str) -> None:
        # see PAUSE_GLYPH in ui/widgets/common.py.
        self.play_btn.setText(PAUSE_GLYPH if state == "playing" else "▶")

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
        self._stage_layout.removeWidget(self.video_widget)
        self._stage_layout.removeWidget(self.controls)
        layout.addWidget(self.video_widget, 1)
        layout.addWidget(self.controls)
        # fullscreen_btn moves out of the top bar (not part of this new
        # window - it stays behind in the main window) and into self.
        # controls' own row alongside the playback widgets, the same as it
        # sat before the 2026-09-07 "takes up too much space" follow-up -
        # otherwise there'd be no way to see/click it once full screen.
        self._top_layout.removeWidget(self.fullscreen_btn)
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
        layout.removeWidget(self.video_widget)
        layout.removeWidget(self.controls)
        self.controls.layout().removeWidget(self.fullscreen_btn)
        self._top_layout.addWidget(self.fullscreen_btn)
        self._stage_layout.insertWidget(0, self.video_widget, 1)
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
