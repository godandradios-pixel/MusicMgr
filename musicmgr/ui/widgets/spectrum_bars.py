"""Small audio-reactive bar-spectrum visualizer for the persistent
PlayerBar - see services/spectrum.py for how the underlying [0..1] band
data is computed (a whole-track Goertzel pass, done once per track on a
background thread) and architecture.md's "Audio-reactive spectrum bars on
the PlayerBar" for the scope this was built to (James: "is there a pulse
visualizer we could display somewhere when the music plays", 2026-09-05 -
he chose a small bar-spectrum widget in the bottom bar, real audio-reactive
rather than purely decorative).

This widget is purely a display: PlayerBar hands it a track's precomputed
buckets (`set_data`), tells it where playback is right now
(`update_position`), and tells it whether to actually reach for that data
or fade to idle (`set_active`, false while paused/stopped/nothing loaded).
"""

from __future__ import annotations

from typing import List, Sequence

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from ...services.spectrum import BAND_FREQS, BUCKET_MS
from ..theme import COLORS

#: repaint/decay cadence - fast enough to read as smooth motion, slow
#: enough that this idle-but-always-running timer costs nothing worth
#: measuring while a track is loaded
_TICK_MS = 45

#: eases toward the target level faster on the way up than on the way down
#: - the classic asymmetric VU-meter shape, reads as "hit" then "settle"
#: rather than a strobe
_RISE = 0.6
_FALL = 0.25

#: below this, a level is treated as "arrived" - stops the idle timer from
#: triggering endless no-op repaints once bars have settled
_SETTLE_EPS = 0.002


class SpectrumBars(QWidget):
    BAR_COUNT = len(BAND_FREQS)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(34, 56)
        self._buckets: List[List[float]] = []
        self._bucket_ms = BUCKET_MS
        self._target = [0.0] * self.BAR_COUNT
        self._levels = [0.0] * self.BAR_COUNT
        self._active = False

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def clear(self) -> None:
        """Called on a track change (before that track's spectrum has been
        computed, or when nothing's queued at all) - drops any stale data
        so the previous track's shape can't flash up again, and eases the
        bars back down rather than snapping them."""
        self._buckets = []
        self._target = [0.0] * self.BAR_COUNT

    def set_data(self, buckets: List[List[float]], bucket_ms: int) -> None:
        self._buckets = buckets
        self._bucket_ms = bucket_ms or BUCKET_MS

    def update_position(self, ms: int) -> None:
        if not self._active or not self._buckets:
            return
        idx = min(len(self._buckets) - 1, max(0, ms // self._bucket_ms))
        self._target = list(self._buckets[idx])

    def set_active(self, active: bool) -> None:
        """`active` tracks the transport's playing/paused state (see
        PlayerBar.on_state) - paused or stopped fades every bar to zero
        rather than freezing whatever was last shown, so a paused track
        visibly reads as paused here too."""
        self._active = active
        if not active:
            self._target = [0.0] * self.BAR_COUNT

    def _tick(self) -> None:
        changed = False
        for i, target in enumerate(self._target):
            level = self._levels[i]
            step = _RISE if target > level else _FALL
            new_level = level + (target - level) * step
            if abs(new_level - level) > _SETTLE_EPS:
                changed = True
            self._levels[i] = new_level
        if changed:
            self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        w, h = self.width(), self.height()
        n = self.BAR_COUNT
        gap = 3.0
        bar_w = (w - gap * (n - 1)) / n
        # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for this
        # whole sweep) - this class isn't actually used anywhere in the app
        # today (see ui/widgets/visualizer.py's module docstring: PlayerBar
        # moved on to PulseVisualizer after this widget's original outing),
        # but there's no reason to leave a stray red-orange behind in dead
        # code either.
        color = QColor(COLORS["jukebox_key_hi"] if self._active else COLORS["text_dim"])
        painter.setBrush(color)

        for i, level in enumerate(self._levels):
            level = max(0.0, min(1.0, level))
            bar_h = max(2.0, level * h)
            x = i * (bar_w + gap)
            y = h - bar_h
            painter.drawRoundedRect(x, y, bar_w, bar_h, 1.5, 1.5)
