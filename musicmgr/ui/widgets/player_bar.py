"""Persistent transport bar pinned to the bottom of the window.

This is the *only* transport surface in the app - Now Playing dropped its
own play/pause/seek row (see ui/views/nowplaying.py) so there'd be one
copy, not two. Music videos follow the same rule: rather than a second,
video-flavoured transport living in ui/widgets/video_panel.py, this bar
temporarily points its buttons and seek bar at whatever VideoController is
playing instead of the audio queue - see enter_video_mode/exit_video_mode,
wired up from ui/app.py via AppContext.videoPlaybackStarted/Ended.

Also owns the `PulseVisualizer` strip across its top edge (see
ui/widgets/visualizer.py). Its active/idle state follows the same
`on_state` transitions the transport reacts to.

`_start_spectrum` runs a background `SpectrumThread` (services/spectrum.py)
on every track change - analyze once per track, off the GUI thread - and
`on_position` feeds the visualizer's `update_position`. An emptied queue
clears its data. While a video plays the strip is hidden (no analysis at
all) and `_update_height()` gives its row of height back to the video.
`SPECTRUM_ENABLED` and the discard-stale-results check in
`_on_spectrum_ready`/`_on_spectrum_failed` are explained in their own
docstrings and tests/conftest.py's autouse fixture.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Union

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...services import audio_quality, tracknum
from ...services.library import format_duration
from ...services.player import REPEAT_ALL, REPEAT_ONE, PlayerController, QueueItem
from ...services.spectrum import BUCKET_MS, SpectrumThread, detect_beats
from ...services.video_player import VideoController
from .common import PAUSE_GLYPH, CoverArt, TouchButton
from .visualizer import VISUALIZER_HEIGHT, PulseVisualizer
from ..theme import COLORS

log = logging.getLogger(__name__)

#: gates the real background analysis - see tests/conftest.py's autouse
#: `_disable_spectrum_threads` fixture. Without it every test that plays
#: real audio through a PlayerBar spawns an un-awaited SpectrumThread,
#: and enough of them hang the suite (see architecture.md's
#: "Audio-reactive spectrum bars on the PlayerBar"). Tests of the threaded
#: decode opt back in explicitly.
SPECTRUM_ENABLED = True


#: width of each side of the bar (cover + text on the left, Queue + volume
#: on the right) - equal, so the transport buttons stay centred
SIDE_WIDTH = 420


class ElidingLabel(QLabel):
    """A one-line label that cuts long text short with "…" to fit its width
    (full text in the tooltip) instead of asking for more room."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def text(self) -> str:  # the full text, not the shortened one
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.width() if self.width() > 20 else 10_000
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, width)
        super().setText(shown)


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
        # The left (cover, title, artist/album) and right (Queue, volume)
        # sides are the same fixed width, so the transport row stays in the
        # true centre of the bar whatever the text; a long title or album
        # is cut short with "…" (full text in the tooltip).
        left_box = QWidget()
        left_box.setFixedWidth(SIDE_WIDTH)
        left = QHBoxLayout(left_box)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(16)
        self.cover = CoverArt(88)
        self.cover.setCursor(Qt.PointingHandCursor)
        left.addWidget(self.cover)

        meta = QVBoxLayout()
        meta.setSpacing(2)
        meta.addStretch(1)
        self.title = ElidingLabel("Nothing playing")
        self.title.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.subtitle = ElidingLabel("")
        self.subtitle.setObjectName("Subtitle")
        meta.addWidget(self.title)
        meta.addWidget(self.subtitle)
        meta.addStretch(1)
        holder = QWidget()
        holder.setLayout(meta)
        left.addWidget(holder, 1)
        root.addWidget(left_box)

        # --- transport + seek ----------------------------------------------
        centre = QVBoxLayout()
        centre.setSpacing(4)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.shuffle_btn = self._transport("⇄", "Shuffle", checkable=True)
        self.back_btn = self._transport("−15s", "Back 15 seconds", wide=True)
        # ⏮/⏭ (U+23EE/U+23ED, the "Miscellaneous Technical" media-symbol
        # block) are drawn by Windows with the color emoji font whatever the
        # app's font stack (small blue squares). Doubled triangles from the
        # Geometric Shapes block (as ▶ for play) avoid that fallback.
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
        # While an internet station plays, -15s/+30s, shuffle and repeat
        # (useless on a live stream) are hidden and this LIVE badge takes
        # the seek bar's place; previous/next step along the station's band
        # instead (see _set_live).
        self.live_badge = QLabel("●  LIVE")
        self.live_badge.setObjectName("LiveBadge")
        self.live_badge.setAlignment(Qt.AlignCenter)
        self.live_badge.setStyleSheet(
            f"QLabel#LiveBadge {{ background: {COLORS['jukebox_key']}; color: {COLORS['on_accent']};"
            " border-radius: 11px; padding: 0 16px; font-size: 12px; font-weight: 700;"
            " letter-spacing: 1px; }")
        self.live_badge.setFixedHeight(22)
        self.live_badge.setToolTip("Live internet radio")
        self.live_badge.setVisible(False)
        self._live = False
        # the file's quality: format, bit depth/sample rate
        # (lossless) or bitrate (compressed) and channels, in a pill at the
        # end of the seek row (services/audio_quality.py). Outlined rather
        # than filled like LIVE, warm for lossless, dim for compressed.
        self.quality_badge = QLabel("")
        self.quality_badge.setObjectName("QualityBadge")
        self.quality_badge.setAlignment(Qt.AlignCenter)
        self.quality_badge.setFixedHeight(22)
        self.quality_badge.setVisible(False)
        seek_row.addWidget(self.elapsed)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.live_badge, 0, Qt.AlignCenter)
        seek_row.addWidget(self.remaining)
        seek_row.addWidget(self.quality_badge, 0, Qt.AlignVCenter)
        centre.addLayout(seek_row)
        root.addLayout(centre, 1)

        # --- right side ------------------------------------------------------
        right_box = QWidget()
        right_box.setFixedWidth(SIDE_WIDTH)
        right = QHBoxLayout(right_box)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        right.addStretch(1)
        self.queue_btn = TouchButton("Queue")
        self.queue_btn.setFixedWidth(96)
        self.queue_btn.clicked.connect(self.queueRequested.emit)
        vol_icon = QLabel("Vol")
        vol_icon.setObjectName("Dim")
        self.volume = QSlider(Qt.Horizontal)
        # its own object name so ui/theme.py's QSlider#VolumeSlider rule can
        # tint it brown without retinting the seek bar's shared QSlider rule.
        self.volume.setObjectName("VolumeSlider")
        self.volume.setFixedWidth(140)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(player.volume * 100))
        self.volume.valueChanged.connect(lambda v: self._active().set_volume(v / 100))
        # the radio tuner's VOLUME knob turns this too
        player.volumeChanged.connect(self._sync_volume_slider)
        right.addWidget(self.queue_btn)
        right.addWidget(vol_icon)
        right.addWidget(self.volume)
        root.addWidget(right_box)

        # --- wiring ----------------------------------------------------------
        # play/pause and the ±15s/30s skip route to whichever source is
        # active (see _active) - the audio queue normally, or a playing
        # video for as long as enter_video_mode has it bound. prev/next/
        # shuffle/repeat are queue concepts a single video doesn't have, so
        # they're disabled rather than routed - see enter_video_mode.
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.next_btn.clicked.connect(lambda: self._skip(1))
        self.prev_btn.clicked.connect(lambda: self._skip(-1))
        self.back_btn.clicked.connect(lambda: self._active().seek_relative(-15000))
        self.fwd_btn.clicked.connect(lambda: self._active().seek_relative(30000))
        self.shuffle_btn.toggled.connect(self.player.set_shuffle)
        self.repeat_btn.clicked.connect(self._cycle_repeat)
        self.cover.mousePressEvent = lambda _e: self.nowPlayingRequested.emit()

        player.trackChanged.connect(self.on_track_changed)
        player.positionChanged.connect(self.on_position)
        player.durationChanged.connect(self.on_duration)
        player.playbackStateChanged.connect(self.on_state)
        # a station's sound, analysed as it plays
        player.liveBands.connect(self.visualizer.push_live)

    def _transport(
        self, glyph: str, tip: str, main: bool = False, checkable: bool = False, wide: bool = False
    ):
        btn = TouchButton(glyph)
        btn.setObjectName("TransportMain" if main else "TransportWide" if wide else "Transport")
        btn.setToolTip(tip)
        btn.setCheckable(checkable)
        return btn

    def _skip(self, delta: int) -> None:
        """Previous/next: along the band while a station is on (like the
        tuner's knob), else the queue."""
        tuner = getattr(self.ctx, "tuner", None) if self.ctx is not None else None
        if self._live and tuner is not None and tuner.step_station(delta):
            return
        if delta > 0:
            self.player.next(user_initiated=True)
        else:
            self.player.previous()

    def _set_live(self, live: bool) -> None:
        """Swap the transport row between a normal queue and a live station."""
        self._live = live
        for b in (self.shuffle_btn, self.back_btn, self.fwd_btn, self.repeat_btn):
            b.setVisible(not live)
        for w in (self.elapsed, self.seek, self.remaining):
            w.setVisible(not live)
        self.live_badge.setVisible(live)
        self.prev_btn.setToolTip("Previous station" if live else "Previous")
        self.next_btn.setToolTip("Next station" if live else "Next")

    def _cycle_repeat(self) -> None:
        mode = self.player.cycle_repeat()
        glyph = {"off": "↻", REPEAT_ALL: "↻", REPEAT_ONE: "↻¹"}[mode]
        self.repeat_btn.setText(glyph)
        self.repeat_btn.setToolTip(f"Repeat {mode}")
        # jukebox_key_hi, not jukebox_key, since this is small bold text on
        # the button's own dark background (same reasoning as
        # ui/widgets/common.py's toggle-icon colors).
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
        # No visualizer during a video: the strip is hidden and its height
        # goes back to the picture (see _update_height below), and no decode
        # is attempted. Clearing here keeps a stale music track's bars from
        # lingering in the hidden strip.
        self._start_spectrum(None)
        self._set_live(False)
        self.quality_badge.setVisible(False)
        self.visualizer.set_live(False)
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
        by itself shrink this frame's fixed height, so this must run too.
        A shorter PlayerBar gives more vertical room to the view above it,
        which is the point of hiding the strip during video."""
        extra = 0 if self._video is not None else VISUALIZER_HEIGHT
        self.setFixedHeight(self._content_height + extra)

    # -- slots ---------------------------------------------------------------

    def _sync_volume_slider(self, value: float) -> None:
        target = int(round(value * 100))
        if self.volume.value() != target:
            self.volume.blockSignals(True)
            self.volume.setValue(target)
            self.volume.blockSignals(False)

    def _show_quality(self, item: Optional[QueueItem]) -> None:
        """The quality pill - files only (not a station, not a video)."""
        quality = None
        if item is not None and not item.is_stream and self._video is None:
            quality = audio_quality.describe(item.path)
        if quality is None:
            self.quality_badge.setVisible(False)
            return
        color = COLORS["jukebox_key_hi"] if quality.lossless else COLORS["text_dim"]
        self.quality_badge.setStyleSheet(
            f"QLabel#QualityBadge {{ color: {color}; border: 1px solid {color};"
            " border-radius: 11px; padding: 0 12px; font-size: 12px; font-weight: 600; }")
        self.quality_badge.setText(quality.label)
        self.quality_badge.setToolTip(quality.detail)
        self.quality_badge.setVisible(True)

    def on_track_changed(self, item: Optional[QueueItem]) -> None:
        self._set_live(item is not None and item.is_stream and self._video is None)
        self._show_quality(item)
        if item is None:
            self.visualizer.set_live(False)
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
            " — ".join(x for x in (item.artist, tracknum.with_side(item.album, item.position)) if x)
        )
        self.cover.set_source(item.cover_path, item.album or item.artist)
        # an internet station never ends - nothing to decode up front; its
        # bars come from the sound as it plays instead (PlayerController.liveBands)
        self._start_spectrum(None if item.is_stream else item.path)
        self.visualizer.set_live(item.is_stream and self._video is None)

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

    # -- spectrum analysis (see the module and ui/widgets/visualizer.py
    # docstrings) ------------------------------------------------------------

    def _start_spectrum(self, path: Optional[str]) -> None:
        """Fires a background analysis for `path` (or clears the visualizer's
        data if `path` is None - an emptied queue or a video's own audio).
        Doesn't wait for or cancel whatever computation is still running for
        a track just left - decode is fast enough that there's rarely more
        than one in flight, and `_on_spectrum_ready`/`_on_spectrum_failed`
        check the reported path against `self._spectrum_path` before
        applying a result, so a late-arriving stale one (e.g. from a fast
        double-skip) is simply discarded."""
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
