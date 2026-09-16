"""Persistent transport bar pinned to the bottom of the window.

This is the *only* transport surface in the app - Now Playing dropped its
own play/pause/seek row (see ui/views/nowplaying.py) so there'd be one
copy, not two. Music videos follow the same rule: rather than a second,
video-flavoured transport living in ui/widgets/video_panel.py, this bar
temporarily points its buttons and seek bar at whatever VideoController is
playing instead of the audio queue - see enter_video_mode/exit_video_mode,
wired up from ui/app.py via AppContext.videoPlaybackStarted/Ended.

Also owns the `PulseVisualizer` strip across its top edge (2026-09-06,
moved here from Now Playing; see ui/widgets/visualizer.py's module
docstring for the full history, including the same-day follow-up that
made it audio-reactive again). Its active/idle state is wired to the exact
same `on_state` transitions the transport already reacts to, so it tracks
play/pause for music with no separate wiring of its own.

**Feeding it real data (2026-09-06 follow-up).** `_start_spectrum` fires a
background `SpectrumThread` (services/spectrum.py) on every track change,
the same "analyze once per track, off the GUI thread" shape the original
2026-09-05 spectrum feature used before it was removed - see that
module's docstring for why this was brought back. `on_position` (already
firing every position tick for the seek bar) also feeds the visualizer's
`update_position`. `on_track_changed`/`enter_video_mode` both clear its
data - an emptied queue, or a playing video - rather than start a new
analysis. Video briefly got its own real analysis too (2026-09-15,
same-day follow-up - the same decode-the-file-and-index-by-position trick
works whether `path` is an audio file or a video container's audio
track), then James asked for that back out again along with the
visualizer strip itself, wanting the video bigger and no pulse bar while
watching one at all: `enter_video_mode`/`exit_video_mode` now also
`setVisible()` the strip and call `_update_height()` to actually give its
row of height back rather than just leaving it there, hidden and idle.
`SPECTRUM_ENABLED` and the discard-stale-results pattern in
`_on_spectrum_ready`/`_on_spectrum_failed` mirror that same removed
feature's own conventions exactly, including the reason for both (see
their docstrings and tests/conftest.py's autouse fixture).
"""

from __future__ import annotations

import logging
from typing import List, Optional, Union

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...services.library import format_duration
from ...services.player import REPEAT_ALL, REPEAT_ONE, PlayerController, QueueItem
from ...services.spectrum import BUCKET_MS, SpectrumThread, detect_beats
from ...services.video_player import VideoController
from .common import PAUSE_GLYPH, CoverArt, TouchButton
from .visualizer import VISUALIZER_HEIGHT, PulseVisualizer
from ..theme import COLORS

log = logging.getLogger(__name__)

#: gates the real background analysis - see tests/conftest.py's autouse
#: `_disable_spectrum_threads` fixture. Exists for the exact reason the
#: original 2026-09-05 spectrum feature's identical flag did: every test
#: that builds a real PlayerBar and plays back real audio would otherwise
#: spawn a real, un-awaited SpectrumThread as a side effect, which - given
#: enough of them - hung the full suite before (see architecture.md's
#: "Audio-reactive spectrum bars on the PlayerBar"). Tests that actually
#: exercise the real threaded decode opt back in explicitly.
SPECTRUM_ENABLED = True


class PlayerBar(QFrame):
    nowPlayingRequested = Signal()
    queueRequested = Signal()

    def __init__(self, player: PlayerController, ctx=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PlayerBar")
        self.player = player
        #: AppContext, used only so the play button can fall back to
        #: ctx.play_or_resume() (start whatever release/artist page is on
        #: screen) when nothing's queued yet - optional so this widget can
        #: still be built standalone (e.g. in tests) without one; falls back
        #: to a plain player.toggle() in that case.
        self.ctx = ctx
        #: set while a video is playing (see enter_video_mode) - transport
        #: and seek route here instead of to `player` for as long as it's set
        self._video: Optional[VideoController] = None
        self._seeking = False
        #: which track the visualizer's data currently belongs (or is being
        #: computed for) to - see _start_spectrum/_on_spectrum_ready. None
        #: means "nothing to analyze" (empty queue, or a playing video).
        self._spectrum_path: Optional[str] = None
        #: keeps every in-flight SpectrumThread referenced so Python doesn't
        #: garbage-collect a still-running QThread out from under itself;
        #: pruned as each one finishes (see _start_spectrum)
        self._spectrum_threads: List[SpectrumThread] = []
        #: referenced again by _update_height() below, once a video needs
        #: to drop back to just this (no visualizer strip) - kept on self
        #: rather than a local, unlike before this needed revisiting after
        #: construction
        self._content_height = 120
        self.setFixedHeight(self._content_height + VISUALIZER_HEIGHT)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.visualizer = PulseVisualizer()
        outer.addWidget(self.visualizer)

        content = QWidget()
        content.setFixedHeight(self._content_height)
        outer.addWidget(content)

        root = QHBoxLayout(content)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(16)

        # --- current track -------------------------------------------------
        self.cover = CoverArt(88)
        self.cover.setCursor(Qt.PointingHandCursor)
        root.addWidget(self.cover)

        meta = QVBoxLayout()
        meta.setSpacing(2)
        meta.addStretch(1)
        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("Subtitle")
        meta.addWidget(self.title)
        meta.addWidget(self.subtitle)
        meta.addStretch(1)
        holder = QWidget()
        holder.setLayout(meta)
        holder.setMinimumWidth(180)
        holder.setMaximumWidth(340)
        root.addWidget(holder)

        # --- transport + seek ----------------------------------------------
        centre = QVBoxLayout()
        centre.setSpacing(4)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.shuffle_btn = self._transport("⇄", "Shuffle", checkable=True)
        self.back_btn = self._transport("−15s", "Back 15 seconds", wide=True)
        # ⏮/⏭ (U+23EE/U+23ED, the "Miscellaneous Technical" media-symbol
        # block) are exactly the range Windows hands off to the color emoji
        # font instead of the plain UI font, regardless of the app's own
        # font-family stack - that's what was showing up as small solid-blue
        # squares instead of a flat glyph matching shuffle/repeat. Doubled
        # triangles from the Geometric Shapes block (the same block ▶ already
        # uses safely for play) read the same way visually without tripping
        # that fallback.
        self.prev_btn = self._transport("◀◀", "Previous")
        self.play_btn = self._transport("▶", "Play / pause", main=True)
        self.next_btn = self._transport("▶▶", "Next")
        self.fwd_btn = self._transport("+30s", "Forward 30 seconds", wide=True)
        self.repeat_btn = self._transport("↻", "Repeat off")
        for b in (
            self.shuffle_btn,
            self.back_btn,
            self.prev_btn,
            self.play_btn,
            self.next_btn,
            self.fwd_btn,
            self.repeat_btn,
        ):
            buttons.addWidget(b)
        buttons.addStretch(1)
        centre.addLayout(buttons)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(10)
        self.elapsed = QLabel("0:00")
        self.elapsed.setObjectName("Dim")
        self.elapsed.setFixedWidth(52)
        self.elapsed.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.remaining = QLabel("--:--")
        self.remaining.setObjectName("Dim")
        self.remaining.setFixedWidth(52)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderReleased.connect(self._commit_seek)
        seek_row.addWidget(self.elapsed)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.remaining)
        centre.addLayout(seek_row)
        root.addLayout(centre, 1)

        # --- right side ------------------------------------------------------
        right = QHBoxLayout()
        right.setSpacing(10)
        self.queue_btn = TouchButton("Queue")
        self.queue_btn.setFixedWidth(96)
        self.queue_btn.clicked.connect(self.queueRequested.emit)
        vol_icon = QLabel("Vol")
        vol_icon.setObjectName("Dim")
        self.volume = QSlider(Qt.Horizontal)
        # 2026-09-08 follow-up - James: "make that volume control bar brown
        # to fit pallete" - see ui/theme.py's QSlider#VolumeSlider rule for
        # why this needed its own object name rather than retinting QSlider
        # generally (the seek bar right above shares that same base rule).
        self.volume.setObjectName("VolumeSlider")
        self.volume.setFixedWidth(140)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(player.volume * 100))
        self.volume.valueChanged.connect(lambda v: self._active().set_volume(v / 100))
        right.addWidget(self.queue_btn)
        right.addWidget(vol_icon)
        right.addWidget(self.volume)
        root.addLayout(right)

        # --- wiring ----------------------------------------------------------
        # play/pause and the ±15s/30s skip route to whichever source is
        # active (see _active) - the audio queue normally, or a playing
        # video for as long as enter_video_mode has it bound. prev/next/
        # shuffle/repeat are queue concepts a single video doesn't have, so
        # they're disabled rather than routed - see enter_video_mode.
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.next_btn.clicked.connect(lambda: self.player.next(user_initiated=True))
        self.prev_btn.clicked.connect(self.player.previous)
        self.back_btn.clicked.connect(lambda: self._active().seek_relative(-15000))
        self.fwd_btn.clicked.connect(lambda: self._active().seek_relative(30000))
        self.shuffle_btn.toggled.connect(self.player.set_shuffle)
        self.repeat_btn.clicked.connect(self._cycle_repeat)
        self.cover.mousePressEvent = lambda _e: self.nowPlayingRequested.emit()

        player.trackChanged.connect(self.on_track_changed)
        player.positionChanged.connect(self.on_position)
        player.durationChanged.connect(self.on_duration)
        player.playbackStateChanged.connect(self.on_state)

    def _transport(
        self, glyph: str, tip: str, main: bool = False, checkable: bool = False, wide: bool = False
    ):
        btn = TouchButton(glyph)
        btn.setObjectName("TransportMain" if main else "TransportWide" if wide else "Transport")
        btn.setToolTip(tip)
        btn.setCheckable(checkable)
        return btn

    def _cycle_repeat(self) -> None:
        mode = self.player.cycle_repeat()
        glyph = {"off": "↻", REPEAT_ALL: "↻", REPEAT_ONE: "↻¹"}[mode]
        self.repeat_btn.setText(glyph)
        self.repeat_btn.setToolTip(f"Repeat {mode}")
        # 2026-09-13 follow-up (James: "clean up the remaining red/orange
        # lines and text in the app ... brown pallete" - see ui/theme.py's
        # #Primary comment for the rest of this sweep): this one was a raw
        # "#e8563f" literal rather than a COLORS["accent"] lookup, so it
        # slipped past every earlier retint that searched for the token by
        # name. jukebox_key_hi, not jukebox_key, since this is small bold
        # text on the button's own dark background (same reasoning as
        # ui/widgets/common.py's toggle-icon colors below).
        self.repeat_btn.setStyleSheet(
            "" if mode == "off" else f"color: {COLORS['jukebox_key_hi']}; font-weight: 700;"
        )

    def _commit_seek(self) -> None:
        active = self._active()
        duration = active.duration()
        if duration:
            active.seek(int(duration * self.seek.value() / 1000))
        self._seeking = False

    def _active(self) -> Union[PlayerController, VideoController]:
        return self._video or self.player

    def _on_play_clicked(self) -> None:
        # a playing video always just toggles (no "nothing queued yet"
        # fallback makes sense for a single video - see enter_video_mode).
        # For music, an empty queue routes through ctx.play_or_resume()
        # instead of a bare toggle() so the button can start whatever
        # release/artist page is currently on screen - see AppContext.
        if self._video is not None:
            self._video.toggle()
        elif self.ctx is not None:
            self.ctx.play_or_resume()
        else:
            self.player.toggle()

    # -- video hand-off -------------------------------------------------------

    def enter_video_mode(
        self, controller: VideoController, title: str, artist: str
    ) -> None:
        """Point the transport row at a playing video instead of the audio
        queue, called from ui/app.py on AppContext.videoPlaybackStarted (the
        audio queue is paused by the caller before this fires, so nothing
        talks over the video). No queue concept for a single video, so
        prev/next/shuffle/repeat are disabled for as long as this lasts."""
        # 2026-09-15, two follow-ups same day: a video's own audio was
        # briefly analyzed here too (same decode a music track gets, so the
        # visualizer would react to it), then James asked for that back out
        # entirely - "When watching a music video, I really don't need the
        # pulse visualizer. I want to look at the video" - along with the
        # visualizer strip itself hidden during video, not just idle, so
        # its height goes back to the picture (see _update_height below).
        # No decode even attempted now, not just an unused one - clearing
        # here is also what keeps a stale music track's bars from lingering
        # once the strip is hidden and no longer overwriting them itself.
        self._start_spectrum(None)
        self.visualizer.setVisible(False)
        already_bound = self._video is controller
        self._video = controller
        # _update_height() reads self._video to decide whether the strip's
        # row is reserved right now - has to run after the assignment
        # above, or it'd still see the pre-video state and compute no
        # change at all
        self._update_height()
        self.title.setText(title)
        self.subtitle.setText(artist)
        self.cover.set_source(None, title)
        self.seek.setValue(0)
        self.elapsed.setText("0:00")
        if not already_bound:
            # a different video started without an intervening
            # exit_video_mode (e.g. hand-off from an artist page while
            # another video's pane was still current) - connect once, since
            # VideoPlayerPanel reuses the same VideoController for every
            # video it plays and connecting twice would double up updates
            for b in (self.shuffle_btn, self.prev_btn, self.next_btn, self.repeat_btn):
                b.setEnabled(False)
            controller.positionChanged.connect(self.on_position)
            controller.durationChanged.connect(self.on_duration)
            controller.playbackStateChanged.connect(self.on_state)
        controller.set_volume(self.volume.value() / 100)
        self.on_duration(controller.duration())
        self.on_state("playing" if controller.is_playing() else "paused")

    def exit_video_mode(self) -> None:
        """Hand the transport row back to the audio queue - called on
        AppContext.videoPlaybackEnded (leaving the video, whether by the
        back button, navigating away, or the app closing)."""
        controller = self._video
        if controller is None:
            return
        controller.positionChanged.disconnect(self.on_position)
        controller.durationChanged.disconnect(self.on_duration)
        controller.playbackStateChanged.disconnect(self.on_state)
        self._video = None
        self.visualizer.setVisible(True)
        self._update_height()
        for b in (self.shuffle_btn, self.prev_btn, self.next_btn, self.repeat_btn):
            b.setEnabled(True)
        self.on_track_changed(self.player.current)
        self.on_duration(self.player.duration())
        self.on_state("playing" if self.player.is_playing() else "paused")

    def _update_height(self) -> None:
        """The visualizer strip's height is only ever "there" or "not" -
        hiding the widget (enter_video_mode/exit_video_mode above) doesn't
        by itself shrink this frame's own fixed height, so the room it used
        to take stays reserved as dead space unless this also runs. Giving
        that room back is the actual point of hiding it during video
        (2026-09-15, James: "so that video size can be bigger") - this bar
        sits below the stacked view content in MainWindow's layout, so a
        shorter PlayerBar directly means more vertical room for whatever's
        above it, the embedded video player included."""
        extra = 0 if self._video is not None else VISUALIZER_HEIGHT
        self.setFixedHeight(self._content_height + extra)

    # -- slots ---------------------------------------------------------------

    def on_track_changed(self, item: Optional[QueueItem]) -> None:
        if item is None:
            self.title.setText("Nothing playing")
            self.subtitle.setText("")
            self.cover.set_source(None, "")
            self.seek.setValue(0)
            self.elapsed.setText("0:00")
            self.remaining.setText("--:--")
            self.visualizer.set_active(False)
            self._start_spectrum(None)
            return
        self.title.setText(item.title)
        self.subtitle.setText(
            " — ".join(x for x in (item.artist, item.album) if x)
        )
        self.cover.set_source(item.cover_path, item.album or item.artist)
        self._start_spectrum(item.path)

    def on_position(self, ms: int) -> None:
        duration = self._active().duration()
        if duration and not self._seeking:
            self.seek.setValue(int(1000 * ms / duration))
        self.elapsed.setText(format_duration(ms))
        if duration:
            self.remaining.setText("-" + format_duration(max(0, duration - ms)))
        # the video-audio case never has data loaded (see enter_video_mode),
        # so this is a no-op then - see PulseVisualizer.update_position
        self.visualizer.update_position(ms)

    def on_duration(self, ms: int) -> None:
        self.remaining.setText(format_duration(ms) if ms else "--:--")

    def on_state(self, state: str) -> None:
        # see PAUSE_GLYPH in ui/widgets/common.py for why this isn't "⏸" or
        # the old single "‖" glyph.
        self.play_btn.setText(PAUSE_GLYPH if state == "playing" else "▶")
        # covers both the audio queue and a hand-off video - enter_video_mode
        # and exit_video_mode both funnel through this same method (see
        # their own bodies), so the visualizer never needs its own separate
        # wiring for the video case.
        self.visualizer.set_active(state == "playing")

    # -- spectrum analysis (2026-09-06 follow-up - see the module and
    # ui/widgets/visualizer.py docstrings for why this is back) --------------

    def _start_spectrum(self, path: Optional[str]) -> None:
        """Fires a background analysis for `path` (or clears the visualizer's
        data if `path` is None - an emptied queue or a video's own audio).
        Doesn't wait for or cancel whatever computation is still running for
        a track just left - decode is fast enough that there's rarely more
        than one in flight, and `_on_spectrum_ready`/`_on_spectrum_failed`
        check the reported path against `self._spectrum_path` before
        applying a result, so a late-arriving stale one (e.g. from a fast
        double-skip) is simply discarded - the same shape this project uses
        elsewhere for "ignore a result that's no longer current"."""
        self._spectrum_path = path
        if path is None or not SPECTRUM_ENABLED:
            self.visualizer.clear_data()
            return
        thread = SpectrumThread(path)
        thread.spectrumReady.connect(self._on_spectrum_ready)
        thread.failed.connect(self._on_spectrum_failed)
        thread.finished.connect(lambda: self._spectrum_threads.remove(thread))
        self._spectrum_threads.append(thread)
        thread.start()

    def _on_spectrum_ready(self, path: str, buckets: list) -> None:
        if path != self._spectrum_path:
            return  # stale - a later track change already moved on
        self.visualizer.set_data(buckets, BUCKET_MS, detect_beats(buckets, BUCKET_MS))

    def _on_spectrum_failed(self, path: str) -> None:
        if path != self._spectrum_path:
            return
        self.visualizer.clear_data()

    def shutdown(self) -> None:
        """Called from MainWindow.closeEvent - gives any in-flight analysis
        a brief window to finish rather than abandoning it mid-decode."""
        for thread in list(self._spectrum_threads):
            thread.wait(500)
