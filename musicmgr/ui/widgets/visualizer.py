"""Audio-reactive pulse visualizer across the persistent PlayerBar's top
edge.

**History, in order (see architecture.md for the full write-ups):**

1. **2026-09-05, real audio-reactive spectrum.** James's original ask ("is
   there a pulse visualizer") got a real one: `services/spectrum.py`
   decodes a track's actual bass/mid/treble energy over time, shown as
   five small bars in the PlayerBar. He removed it the same day after
   seeing it live - at that tiny size it didn't read as reacting to the
   music at all.
2. **2026-09-05, purely decorative replacement.** In its place: this
   widget, deliberately touching no audio data whatsoever - each bar eased
   toward a fresh random target on a timer, "may not sync with music but
   just a visualizer." Enhanced the same day with a traveling-wave shape
   (so neighboring bars looked related, not like noise) and an idle
   "breathing" pulse instead of snapping flat on pause, then moved from
   Now Playing to the PlayerBar's full width and reshaped into a
   small-at-the-edges/tall-in-the-middle "mountain" silhouette.
3. **2026-09-06 follow-up, back to real data.** James: "I still don't like
   the wave of the visualizer... colors and pulse just isn't working for
   me. Let's go back to making the visualizer actually use the beat of the
   actual sound." He confirmed the size/shape/location from step 2 were
   fine as they were - only the fake motion and the position-based rainbow
   needed to go. This is that: the random wave generator is gone, replaced
   by `services/spectrum.py`'s real per-track band analysis (the same
   engine step 1 built and left as a still-tested orphan after its own
   widget was removed) feeding this widget's bars instead, plus a beat
   detector (`services.spectrum.detect_beats`) that layers a brighter,
   slightly taller flash on top whenever the bass actually spikes. The
   "mountain" envelope and idle breathing pulse from step 2 are unchanged -
   James never objected to the shape, only to the fake motion and colors
   inside it.

**What's real and what's still decorative.** `PlayerBar` hands this widget
a whole track's precomputed band buckets up front (`set_data`, via a
background `SpectrumThread` exactly like the original spectrum widget
used) and its live playback position on every tick (`update_position`);
between those calls, `_tick()` extrapolates the position forward on its
own 45ms cadence rather than waiting on however often the underlying
`QMediaPlayer` actually emits `positionChanged` (which is coarser than
that), so the motion stays smooth regardless of the real signal's own
cadence - `update_position` simply resyncs that estimate back to ground
truth whenever a fresh reading arrives. The only two things that are still
faked, deliberately: the idle "breathing" pulse while paused/stopped
(there's no audio to analyze then), and the brief window before a newly-
started track's analysis finishes (typically well under a second - see
`services/spectrum.py`) - both fall back to the same gentle ambient sine
motion in the dim idle color, rather than a jarring flat/dark bar.

**Mapping 5 bands onto many more bars.** `services.spectrum.BAND_FREQS`
only carries five band centers (bass through treble) per bucket - `
_spread_bands_across_bars` piecewise-linearly interpolates those five
values across all `BAR_COUNT` bars so the row reads as one continuous
shape rather than five blocky steps, the same way a real equalizer's LED
columns look continuous despite a fixed number of discrete bands
underneath. The result is still multiplied by the fixed `_ENVELOPE`
"mountain" curve from step 2, so the overall silhouette (small at the
edges, tallest in the middle) stays exactly as before - only what fills
that shape moment to moment is now real.

**Color: frequency mix, not bar position.** The old "yellow at top to blue
at bottom" gradient colored every bar by *where it sat*, regardless of
what was actually playing - understandable feedback that it "wasn't
working" once the bars themselves became real, since a fixed position-
based gradient has nothing to do with the audio. `_BAND_COLORS` assigns
one anchor color per band (bass through treble); `_blend_band_color`
mixes them by each band's share of the current bucket's total energy, so
the whole strip's color leans toward the bass anchor during a bass-heavy
passage and the treble anchor during a bright/cymbal-heavy one, shifting
continuously as the music's frequency balance actually shifts - one color
for the whole widget each moment (not per-bar), since that's what "which
frequencies are actually loudest" means for a single mix, not a per-bar
property.

**The beat flash.** `services.spectrum.detect_beats` runs once per track
(alongside the band analysis) and returns which buckets are bass spikes.
`_refresh_from_position` sets `self._flash` to full strength the instant
playback crosses into a flagged bucket; `_tick` decays it every tick
after. While `_flash` is above zero it does two things on top of the
continuous band-following motion: brightens the blended color
(`QColor.lighter`) and adds a small extra height boost to every bar's
target - a felt "hit" layered on top of the ongoing shape, not a
replacement for it, matching what James asked for ("actually use the
beat" alongside continuous frequency motion, not instead of it).

4. **2026-09-06, same day, second follow-up: "not enough motion... we
   need more dramatic movement."** Step 3 made the bars real, but real
   linear band energy reads as far too flat to the eye - `compute_spectrum`
   normalizes every band value against the single loudest instant in the
   *whole track*, so almost every other moment sits well below 1.0, and
   most of a bar's travel was getting spent in a narrow, undramatic low
   range. `_apply_visual_gain` (a small gain nudge plus a `_VISUAL_GAMMA <
   1` power curve, applied to each bar's real level right before the
   mountain envelope multiplies it) fixes this the way real spectrum
   analyzers almost always do: a curve that lifts quiet-to-moderate values
   much more than already-loud ones, so the bars actually use their
   height, instead of a flat 1:1 mapping of "share of the track's single
   peak" straight to pixels. `_RISE`/`_FALL` were also both raised so bars
   snap toward a new target and fall back again faster - punchier, less
   mushy - and the beat flash (`_FLASH_HEIGHT_BOOST`/`_FLASH_BRIGHTEN`)
   was made stronger to match. Still the same real per-bucket band/beat
   data driving everything - this only changes how that data gets mapped
   onto pixel height, the same role a dB/log scale plays in a normal audio
   visualizer.

5. **2026-09-07 follow-up - James: "The pulse visualizer only seem to rise
   up half way in the space, can you almost double the pulse side of the
   bars so it show more dramatic size differences."** Step 4's gain/gamma
   curve helped, but typical (not-loudest-instant) moments were still only
   reaching roughly half the widget's height in practice. `_HEIGHT_BOOST`
   multiplies the already-gained level (post `_apply_visual_gain`, still
   clamped to `[0, 1]`) by roughly 1.9x *before* the mountain envelope
   shapes it - not folded into `_VISUAL_GAIN` itself, so a genuinely
   maxed-out real value still saturates at exactly 1.0 going into the
   envelope and the edges stay capped exactly as before (see
   `test_edge_bars_stay_envelope_capped_even_at_full_band_strength`); only
   the moderate/quiet levels below that ceiling now map noticeably closer
   to it, so ordinary playback actually uses most of the available height
   instead of hovering around the middle.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...services.spectrum import BAND_FREQS, BUCKET_MS
from ..theme import COLORS

#: enough bars to read as dense across a whole window's width (2026-09-06
#: mountain reshape - see module docstring's history)
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
#: settle" shape a real VU-meter/spectrum display uses. Raised from the
#: original 0.35/0.12 in the 2026-09-06 "more dramatic movement"
#: follow-up (see module docstring) - the old fall rate in particular left
#: bars lingering high enough between hits that motion read as mushy
#: rather than punchy.
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
_BAND_COLORS = [
    QColor(COLORS["accent"]),  # 60Hz (bass) - warm red-orange
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
#: real band-following target. Raised from 0.22 in the "more dramatic
#: movement" follow-up (see module docstring) to match the punchier
#: _RISE/_FALL and _VISUAL_GAIN/_VISUAL_GAMMA changes alongside it.
_FLASH_HEIGHT_BOOST = 0.32
#: QColor.lighter() factor added at full flash strength (100 = unchanged).
#: Raised from 55 alongside _FLASH_HEIGHT_BOOST above.
_FLASH_BRIGHTEN = 75

#: perceptual scaling applied to each bar's real band level, right before
#: the mountain envelope multiplies it in - see module docstring's
#: "more dramatic movement" follow-up. `compute_spectrum` normalizes every
#: value against the single loudest instant across the *whole track*, so
#: almost every other moment sits well below 1.0 - fed straight through,
#: that reads as barely any motion at all. `_VISUAL_GAIN` nudges the whole
#: range up slightly first; `_VISUAL_GAMMA` (<1, applied as a power curve)
#: lifts quiet-to-moderate values much more than already-loud ones, the
#: same role a dB/log scale plays in a normal audio visualizer. Both ends
#: stay fixed (0 stays 0, 1 stays ~1) - only what happens in between gets
#: reshaped.
_VISUAL_GAIN = 1.2
_VISUAL_GAMMA = 0.55

#: a further amplification of the already-gained level, applied right
#: after `_apply_visual_gain` and re-clamped to [0, 1] before the mountain
#: envelope shapes it - see the module docstring's 2026-09-07 follow-up
#: ("only seem to rise up half way in the space... almost double the
#: pulse side of the bars"). Kept as its own separate multiplier rather
#: than raised into `_VISUAL_GAIN` so that a genuinely maxed-out real
#: value (which `_apply_visual_gain` already saturates to ~1.0 on its
#: own) still saturates at exactly 1.0 here too - the envelope keeps
#: capping the edges exactly as before regardless of this boost; only
#: values below that ceiling get pushed noticeably higher.
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


class PulseVisualizer(QWidget):
    """A full-width row of bars, tallest in the middle and tapering to a
    sliver at both edges (`_ENVELOPE`), driven by a track's real band
    energy and beat data while `set_active(True)` and a track is loaded -
    see the module docstring for the full history and design. Settles into
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
        bands = self._buckets[idx]
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
        if not self._active or not self._buckets:
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

        if self._active and self._buckets and self._current_bands is not None:
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
