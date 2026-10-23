"""Audio-reactive pulse visualizer across the persistent PlayerBar's top
edge. (Design history: architecture.md.)

**Data.** `PlayerBar` hands this widget a whole track's precomputed band
buckets up front (`set_data`, via a background `SpectrumThread` - see
`services/spectrum.py`) and its live playback position on every tick
(`update_position`). Between those calls `_tick()` extrapolates the
position forward on its own 45ms cadence, since `QMediaPlayer` emits
`positionChanged` more coarsely; `update_position` resyncs the estimate.
Two things are deliberately decorative: the idle "breathing" pulse while
paused/stopped, and the brief window before a new track's analysis
finishes - both use a gentle ambient sine motion in the dim idle color
rather than a flat/dark bar. An internet station uses live mode instead
(`set_live` / `push_live`).

**Mapping 5 bands onto many more bars.** `services.spectrum.BAND_FREQS`
carries five band centers (bass through treble) per bucket;
`_spread_bands_across_bars` interpolates them across all `BAR_COUNT` bars
so the row reads as one continuous shape. The result is multiplied by the
fixed `_ENVELOPE` "mountain" curve (small at the edges, tallest in the
middle).

**Perceptual scaling.** `compute_spectrum` normalizes every band against
the loudest instant of the whole track, so most moments sit well below
1.0. `_apply_visual_gain` (a gain plus a `_VISUAL_GAMMA < 1` power curve)
lifts quiet-to-moderate values much more than loud ones, the role a dB/log
scale plays in a normal visualizer, and `_HEIGHT_BOOST` pushes them closer
to full height while a maxed-out value still saturates at 1.0 before the
envelope (see
`test_edge_bars_stay_envelope_capped_even_at_full_band_strength`).

**Color: frequency mix, not bar position.** `_BAND_COLORS` assigns one
anchor color per band; `_blend_band_color` mixes them by each band's share
of the current bucket's energy, so the whole strip (one color at a time,
not per bar) leans toward bass during a bass-heavy passage and toward
treble during a bright one.

**The beat flash.** `services.spectrum.detect_beats` runs once per track
and returns which buckets are bass spikes. `_refresh_from_position` sets
`self._flash` to full strength when playback crosses into a flagged
bucket; `_tick` decays it. While above zero it brightens the blended color
(`QColor.lighter`) and adds a small height boost to every bar - a "hit"
layered on top of the continuous band-following motion.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...services.spectrum import BAND_FREQS, BUCKET_MS
from ..theme import COLORS

#: enough bars to read as dense across a whole window's width
BAR_COUNT = 72

#: the "mountain" envelope's floor at both edges - not fully 0 so the
#: outermost bars still show a visible sliver rather than vanishing flush
#: with the row's ends
_ENVELOPE_FLOOR = 0.16


def _build_envelope(n: int) -> list[float]:
    """A raised-cosine (Hann) curve from `_ENVELOPE_FLOOR` at both ends up
    to 1.0 at the centre bar - see the module docstring's "mapping 5 bands"
    note. `n <= 1` (never happens with the real `BAR_COUNT`, but keeps this
    safe for a hypothetical tiny widget) just returns full height for
    whatever bars exist rather than dividing by zero."""
    if n <= 1:
        return [1.0] * n
    span = 1.0 - _ENVELOPE_FLOOR
    return [
        _ENVELOPE_FLOOR + span * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)))
        for i in range(n)
    ]


_ENVELOPE = _build_envelope(BAR_COUNT)

#: exported so PlayerBar can size the space it reserves above its own
#: transport row without hardcoding a second copy of this number
VISUALIZER_HEIGHT = 40

#: repaint cadence - fast enough to read as smooth motion, and the interval
#: `_tick` extrapolates playback position by between real `update_position`
#: calls (see module docstring)
_TICK_MS = 45
#: eases toward the target faster on the way up than down, the "hit then
#: settle" shape a real VU-meter/spectrum display uses; a fast fall keeps
#: the motion punchy rather than lingering between hits
_RISE = 0.45
_FALL = 0.22
_SETTLE_EPS = 0.002

#: idle "breathing" - how high the ambient idle pulse rises above its floor.
#: Also used, briefly, while a just-started track's analysis hasn't
#: finished yet (see module docstring) - same gentle placeholder motion
#: either way, never a flat/dark bar.
_IDLE_AMPLITUDE = 0.13
_IDLE_BASE = 0.04
#: radians advanced per tick - deliberately slow, a resting pulse rather
#: than active motion
_IDLE_SPEED = 0.05
#: a slight per-bar phase offset so the idle pulse itself reads as a very
#: gentle wave rather than every bar blinking in lockstep
_IDLE_SPATIAL = 2 * math.pi / BAR_COUNT

#: one anchor color per `services.spectrum.BAND_FREQS` entry, bass through
#: treble - `_blend_band_color` mixes these by each band's live share of
#: the current bucket's energy (see module docstring's "color: frequency
#: mix" note). Bass/mid reuse this app's own accent/"good" tokens rather
#: than inventing unrelated hues; low-mid/high-mid/treble fill in the rest
#: of a warm-to-cool run so the blend has somewhere to travel between them.
#: The bass anchor is the brown palette's jukebox_key_hi walnut (see
#: ui/theme.py's #Primary); the others stay distinct hues so the strip
#: reads as a spectrum rather than one color.
_BAND_COLORS = [
    QColor(COLORS["jukebox_key_hi"]),  # 60Hz (bass) - warm walnut brown
    QColor("#f2994a"),  # 250Hz (low-mid) - orange
    QColor(COLORS["good"]),  # 1000Hz (mid) - teal-green
    QColor("#3b82f6"),  # 3500Hz (high-mid) - blue
    QColor("#9b59b6"),  # 9000Hz (treble) - violet
]
assert len(_BAND_COLORS) == len(BAND_FREQS), (
    "one anchor color per real band - see services.spectrum.BAND_FREQS"
)

#: how much of a beat's flash decays away each 45ms tick - a quick, snappy
#: hit rather than a slow fade, so distinct beats stay visually distinct
#: even at a fast tempo
_FLASH_DECAY = 0.75
#: extra normalized bar height added at full flash strength, on top of the
#: real band-following target
_FLASH_HEIGHT_BOOST = 0.32
#: QColor.lighter() factor added at full flash strength (100 = unchanged)
_FLASH_BRIGHTEN = 75

#: perceptual scaling applied to each bar's real band level, right before
#: the mountain envelope multiplies it in - see the module docstring's
#: "perceptual scaling". `_VISUAL_GAIN` nudges the whole range up slightly;
#: `_VISUAL_GAMMA` (<1, a power curve) lifts quiet-to-moderate values much
#: more than loud ones. Both ends stay fixed (0 stays 0, 1 stays ~1).
_VISUAL_GAIN = 1.2
_VISUAL_GAMMA = 0.55

#: a further amplification of the already-gained level, applied right
#: after `_apply_visual_gain` and re-clamped to [0, 1] before the mountain
#: envelope shapes it, so ordinary playback uses most of the height. Kept
#: separate from `_VISUAL_GAIN` so a maxed-out value still saturates at
#: exactly 1.0 and the envelope keeps capping the edges.
_HEIGHT_BOOST = 1.9


def _spread_bands_across_bars(bands: Sequence[float], n: int) -> List[float]:
    """Piecewise-linear interpolation of `bands` (five band magnitudes,
    ordinarily) across `n` bars, so a coarse handful of real data points
    reads as one continuous shape rather than `len(bands)` blocky steps -
    see the module docstring's "mapping 5 bands onto many more bars" note.
    """
    m = len(bands)
    if m == 0:
        return [0.0] * n
    if m == 1 or n <= 1:
        return [bands[0]] * n
    out = []
    for i in range(n):
        pos = i * (m - 1) / (n - 1)
        lo = int(pos)
        hi = min(m - 1, lo + 1)
        frac = pos - lo
        out.append(bands[lo] * (1 - frac) + bands[hi] * frac)
    return out


def _apply_visual_gain(value: float) -> float:
    """Perceptual boost for a single already-[0..1] real band/bar level -
    see `_VISUAL_GAIN`/`_VISUAL_GAMMA` above for why. Monotonic and fixes
    both ends (0 stays 0, 1 stays ~1, clamped), so it only reshapes what
    happens in between - a quiet/moderate real reading comes out looking
    like meaningfully more than a sliver, without a loud one being pushed
    past full height. `value` is clamped to 0 first rather than raising on
    a fractional power of a negative number, though real band data is
    never negative in practice."""
    v = max(0.0, value) * _VISUAL_GAIN
    if v <= 0.0:
        return 0.0
    return min(1.0, v**_VISUAL_GAMMA)


def _blend_band_color(bands: Sequence[float]) -> QColor:
    """Weighted-average `_BAND_COLORS` by each band's share of `bands`'
    total energy - a bass-heavy bucket lands close to the bass anchor
    color, a treble-heavy one close to the treble anchor, and a mix lands
    somewhere between. Falls back to the idle dim color for a silent
    bucket (nothing to weight by) rather than dividing by zero."""
    total = sum(bands)
    if total <= 0:
        return QColor(COLORS["text_dim"])
    r = g = b = 0.0
    for weight, color in zip(bands, _BAND_COLORS):
        w = weight / total
        r += w * color.red()
        g += w * color.green()
        b += w * color.blue()
    return QColor(int(round(r)), int(round(g)), int(round(b)))


#: live mode (an internet station): readings older than this mean the
#: stream has stalled, and the bars ease back to the idle pulse
_LIVE_STALE_MS = 600
#: and more than this many queued up means playback is ahead of us - skip on
_LIVE_MAX_QUEUE = 6


class PulseVisualizer(QWidget):
    """A full-width row of bars, tallest in the middle and tapering to a
    sliver at both edges (`_ENVELOPE`), driven by a track's real band
    energy and beat data while `set_active(True)` and a track is loaded -
    see the module docstring for the design. Settles into
    a slow ambient breathing pulse whenever paused/stopped, or briefly
    while a just-started track's analysis is still running."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(VISUALIZER_HEIGHT)
        # stretches to fill however wide the PlayerBar is
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._levels = [0.0] * BAR_COUNT
        self._targets = [0.0] * BAR_COUNT
        self._active = False
        self._idle_phase = 0.0

        #: the current track's precomputed band data - see set_data()
        self._buckets: List[Sequence[float]] = []
        self._bucket_ms = BUCKET_MS
        self._beats: set[int] = set()
        self._last_beat_idx = -1
        #: the most recent bucket's raw band values, for color blending -
        #: None while there's nothing to show yet (see paintEvent)
        self._current_bands: Optional[Sequence[float]] = None
        self._flash = 0.0

        #: playback position tracking: `update_position` resyncs
        #: `_known_ms` to ground truth; `_tick` extrapolates forward by
        #: `_TICK_MS` every tick in between, so motion stays smooth
        #: regardless of how often the real position signal actually fires
        #: (see module docstring)
        self._known_ms = 0.0
        self._elapsed_since_sync = 0.0

        #: live mode (an internet station - see set_live): band
        #: readings arrive as the stream plays rather than up front, queued
        #: here and played out one per bucket_ms
        self._live = False
        self._live_queue: List[tuple] = []
        self._live_clock = 0.0
        self._live_idle_ms = 0.0

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(_TICK_MS)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

    # -- data / position (mirrors the original small SpectrumBars widget's
    # contract - see services/spectrum.py) ----------------------------------

    def set_data(
        self,
        buckets: List[Sequence[float]],
        bucket_ms: int,
        beats: Sequence[int] = (),
    ) -> None:
        """Called once a background `SpectrumThread` finishes analyzing the
        current track (see PlayerBar._on_spectrum_ready). Resets the
        position tracking too, since `buckets`/`beats` are meaningless
        against whatever position the *previous* track had reached."""
        self._buckets = buckets
        self._bucket_ms = bucket_ms or BUCKET_MS
        self._beats = set(beats)
        self._last_beat_idx = -1
        self._known_ms = 0.0
        self._elapsed_since_sync = 0.0

    def clear_data(self) -> None:
        """Called on a track change (before its analysis finishes, or when
        there's nothing queued at all) - drops any stale data so the
        previous track's shape/color can't flash up again. Falls back to
        the idle placeholder motion (see `_tick`) rather than a dead bar."""
        self._buckets = []
        self._beats = set()
        self._last_beat_idx = -1
        self._current_bands = None
        self._flash = 0.0

    @property
    def live(self) -> bool:
        return self._live

    def set_live(self, live: bool) -> None:
        """An internet station is playing: there's
        no whole-track analysis to follow by position, so readings come in
        as the stream plays (`push_live`, from PlayerController.liveBands)
        instead."""
        if live == self._live:
            return
        self.clear_data()
        self._live = live
        self._live_queue = []
        self._live_clock = 0.0
        self._live_idle_ms = _LIVE_STALE_MS

    def push_live(self, bands: Sequence[float], beat: bool = False) -> None:
        if not self._live:
            return
        self._live_queue.append((list(bands), bool(beat)))
        if len(self._live_queue) > _LIVE_MAX_QUEUE:
            del self._live_queue[: len(self._live_queue) - 2]
        self._live_idle_ms = 0.0

    def _live_has_data(self) -> bool:
        return self._live and self._live_idle_ms < _LIVE_STALE_MS

    def update_position(self, ms: int) -> None:
        """Resyncs the internal playback-position estimate to a real
        reading (see module docstring) and, if a track's data is already
        loaded, immediately refreshes the bar targets from it rather than
        waiting for the next tick."""
        self._known_ms = float(ms)
        self._elapsed_since_sync = 0.0
        if self._buckets:
            self._refresh_from_position(self._known_ms)

    def set_active(self, active: bool) -> None:
        """Tied to play/pause (see PlayerBar.on_state) - bars follow the
        real band/beat data while `active` and data is loaded, and ease
        into the slow idle breathing pulse otherwise (see `_tick`), so a
        paused/stopped track reads as resting here too, not simply off."""
        self._active = active
        if not active:
            self._flash = 0.0

    # -- internals -------------------------------------------------------

    def _refresh_from_position(self, ms: float) -> None:
        idx = min(len(self._buckets) - 1, max(0, int(ms // self._bucket_ms)))
        if idx in self._beats and idx != self._last_beat_idx:
            self._flash = 1.0
            self._last_beat_idx = idx
        self._show_bands(self._buckets[idx])

    def _advance_live(self) -> None:
        """Play out the queued live readings at their own pace: one per
        bucket_ms of ticks, holding the last one if the next is late."""
        self._live_idle_ms += _TICK_MS
        self._live_clock += _TICK_MS
        while self._live_queue and self._live_clock >= self._bucket_ms:
            self._live_clock -= self._bucket_ms
            bands, beat = self._live_queue.pop(0)
            if beat:
                self._flash = 1.0
            self._show_bands(bands)
        if not self._live_queue:
            self._live_clock = min(self._live_clock, float(self._bucket_ms))
        if self._current_bands is not None:
            self._show_bands(self._current_bands)   # keep the flash boost current

    def _show_bands(self, bands: Sequence[float]) -> None:
        self._current_bands = bands
        spread = _spread_bands_across_bars(bands, BAR_COUNT)
        flash_boost = self._flash * _FLASH_HEIGHT_BOOST
        self._targets = [
            min(
                1.0,
                _ENVELOPE[i] * min(1.0, _apply_visual_gain(spread[i]) * _HEIGHT_BOOST)
                + flash_boost,
            )
            for i in range(BAR_COUNT)
        ]

    def _tick(self) -> None:
        changed = False
        if self._live and self._active:
            self._advance_live()
        has_data = bool(self._buckets) or self._live_has_data()
        if not self._active or not has_data:
            # paused/stopped, or a just-started track's analysis hasn't
            # finished yet - same gentle placeholder motion either way
            # (see module docstring)
            self._idle_phase += _IDLE_SPEED
            for i in range(BAR_COUNT):
                target = _ENVELOPE[i] * (
                    _IDLE_BASE + _IDLE_AMPLITUDE * (
                        0.5 + 0.5 * math.sin(self._idle_phase + i * _IDLE_SPATIAL)
                    )
                )
                level = self._levels[i]
                new_level = level + (target - level) * _FALL
                if abs(new_level - level) > _SETTLE_EPS:
                    changed = True
                self._levels[i] = new_level
            self._flash = 0.0
        else:
            if not self._live:
                self._elapsed_since_sync += _TICK_MS
                self._refresh_from_position(self._known_ms + self._elapsed_since_sync)
            for i, target in enumerate(self._targets):
                level = self._levels[i]
                step = _RISE if target > level else _FALL
                new_level = level + (target - level) * step
                if abs(new_level - level) > _SETTLE_EPS:
                    changed = True
                self._levels[i] = new_level
            if self._flash > _SETTLE_EPS:
                self._flash *= _FLASH_DECAY
                changed = True
            else:
                self._flash = 0.0
        if changed:
            self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        w, h = self.width(), self.height()
        n = BAR_COUNT
        gap = 2.0
        bar_w = (w - gap * (n - 1)) / n

        has_data = bool(self._buckets) or self._live_has_data()
        if self._active and has_data and self._current_bands is not None:
            color = _blend_band_color(self._current_bands)
            if self._flash > _SETTLE_EPS:
                color = color.lighter(100 + int(self._flash * _FLASH_BRIGHTEN))
            painter.setBrush(color)
        else:
            painter.setBrush(QColor(COLORS["text_dim"]))

        for i, level in enumerate(self._levels):
            level = max(0.0, min(1.0, level))
            bar_h = max(3.0, level * h)
            x = i * (bar_w + gap)
            y = h - bar_h
            painter.drawRoundedRect(x, y, bar_w, bar_h, 2.0, 2.0)
