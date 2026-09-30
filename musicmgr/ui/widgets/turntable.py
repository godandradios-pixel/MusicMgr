"""A record player seen from above, for Now Playing (2026-09-30).

James: "on the Now Playing page, any way we can incorporate a visual that
shows the album cover, spinning as if on a record player from the top
view?"

The album cover is the record's centre label and turns with the record.
Everything else stays still, so the only per-frame work is rotating one
small pixmap:

- the plinth, platter and vinyl (grooves plus a fixed light sheen - real
  vinyl's reflection doesn't turn with the record, which is also what makes
  the label's turning read as motion) are rendered once per size;
- the label is rendered once per track;
- the tone arm is drawn each frame: it swings onto the record when playback
  starts, tracks inward across the grooves as the song goes on (from the
  lead-in at the outer edge to the run-out near the label), and swings back
  to its rest when stopped.

Speed is 33⅓ rpm, or 78 rpm for a track whose position is a record side
("A"/"B" - James's 78 rpm rips, see services/tracknum.py). Play and pause
spin up and wind down over about a second instead of jumping. The frame
timer only runs while something is moving and the widget is visible.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QConicalGradient,
)
from PySide6.QtWidgets import QWidget

from .common import cover_pixmap

RPM_LP = 100.0 / 3.0
RPM_78 = 78.0
#: seconds to reach full speed from rest (and to stop again)
SPIN_UP_S = 0.9
#: seconds for the arm to swing between rest and the record
ARM_SWING_S = 0.7
FRAME_MS = 33

#: label diameter as a share of the record's
LABEL_RATIO = 0.46


class Turntable(QWidget):
    def __init__(self, size: int = 340, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._base: Optional[QPixmap] = None
        self._label: Optional[QPixmap] = None
        self._label_key: tuple = ()
        self._angle = 0.0           # label rotation, degrees
        self._speed = 0.0           # current degrees per second
        self._target_rpm = RPM_LP
        self._playing = False
        self._has_track = False
        self._arm = 0.0             # 0 = at rest, 1 = on the record
        self._progress = 0.0        # 0 = lead-in, 1 = run-out
        self._last = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self.set_source(None, "")

    # -- geometry ---------------------------------------------------------------

    def _record_rect(self) -> QRectF:
        s = self._size
        d = s * 0.80
        return QRectF(s * 0.06, (s - d) / 2, d, d)

    def _pivot(self) -> QPointF:
        s = self._size
        return QPointF(s * 0.925, s * 0.14)

    # -- inputs -----------------------------------------------------------------

    def set_source(self, path: Optional[str], seed_text: str = "", rpm: float = RPM_LP) -> None:
        """A new track: its cover becomes the label."""
        self._target_rpm = rpm
        self._has_track = bool(path or seed_text)
        key = (path, seed_text)
        if key != self._label_key:
            self._label_key = key
            self._label = self._make_label(path, seed_text)
            self._progress = 0.0
        self._kick()
        self.update()

    def set_playing(self, playing: bool) -> None:
        self._playing = playing and self._has_track
        self._kick()

    def set_progress(self, position_ms: int, duration_ms: int) -> None:
        if duration_ms > 0:
            self._progress = max(0.0, min(1.0, position_ms / duration_ms))
            if not self._timer.isActive():
                self.update()

    # -- animation --------------------------------------------------------------

    def _kick(self) -> None:
        if not self._timer.isActive() and self.isVisible():
            self._last = time.monotonic()
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: D102 - Qt override
        super().showEvent(event)
        self._kick()

    def hideEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.2, now - self._last)
        self._last = now
        self.advance(dt)
        self.update()
        if self._settled():
            self._timer.stop()

    def advance(self, dt: float) -> None:
        """Move the animation on by `dt` seconds (split out for tests)."""
        full = self._target_rpm * 6.0            # rpm -> degrees per second
        target = full if self._playing else 0.0
        step = full / SPIN_UP_S * dt
        if self._speed < target:
            self._speed = min(target, self._speed + step)
        else:
            self._speed = max(target, self._speed - step)
        self._angle = (self._angle + self._speed * dt) % 360.0
        arm_target = 1.0 if (self._playing or self._speed > 0) and self._has_track else 0.0
        arm_step = dt / ARM_SWING_S
        if self._arm < arm_target:
            self._arm = min(arm_target, self._arm + arm_step)
        else:
            self._arm = max(arm_target, self._arm - arm_step)

    def _settled(self) -> bool:
        spinning = self._speed > 0 or self._playing
        arm_done = self._arm in (0.0, 1.0)
        return not spinning and arm_done

    # -- rendering --------------------------------------------------------------

    def _make_label(self, path: Optional[str], seed_text: str) -> QPixmap:
        d = int(self._record_rect().width() * LABEL_RATIO)
        art = cover_pixmap(path, d, seed_text)
        out = QPixmap(d, d)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addEllipse(0, 0, d, d)
        p.setClipPath(clip)
        p.drawPixmap(0, 0, art)
        p.setClipping(False)
        p.setPen(QPen(QColor(0, 0, 0, 120), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(0.75, 0.75, d - 1.5, d - 1.5))
        p.end()
        return out

    def _make_base(self) -> QPixmap:
        s = self._size
        out = QPixmap(s, s)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)

        # plinth: dark walnut
        plinth = QRectF(1, 1, s - 2, s - 2)
        wood = QLinearGradient(0, 0, s, s)
        wood.setColorAt(0.0, QColor("#4a3322"))
        wood.setColorAt(1.0, QColor("#2c1d13"))
        p.setPen(QPen(QColor("#1a110b"), 2))
        p.setBrush(wood)
        p.drawRoundedRect(plinth, 18, 18)

        rec = self._record_rect()
        c = rec.center()
        r = rec.width() / 2

        # platter rim
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#8a8f98"))
        p.drawEllipse(c, r + 5, r + 5)
        p.setBrush(QColor("#5c6069"))
        p.drawEllipse(c, r + 2, r + 2)

        # vinyl
        p.setBrush(QColor("#111214"))
        p.drawEllipse(c, r, r)
        # grooves: fine rings between lead-in and run-out
        label_r = r * LABEL_RATIO
        groove = QPen(QColor(255, 255, 255, 14), 1)
        p.setBrush(Qt.NoBrush)
        p.setPen(groove)
        ring = label_r + 6
        while ring < r - 4:
            p.drawEllipse(c, ring, ring)
            ring += 2.6
        # a few wider gaps between "tracks"
        p.setPen(QPen(QColor(0, 0, 0, 200), 2))
        for frac in (0.62, 0.78, 0.9):
            gap = label_r + (r - label_r) * frac
            p.drawEllipse(c, gap, gap)
        # fixed light sheen (doesn't turn with the record): two soft
        # highlights opposite each other, like a lamp overhead
        sheen = QConicalGradient(c, 45)
        for at, alpha in ((0.0, 30), (0.07, 8), (0.18, 0), (0.43, 0), (0.5, 20),
                          (0.57, 6), (0.68, 0), (0.93, 0), (1.0, 30)):
            sheen.setColorAt(at, QColor(255, 255, 255, alpha))
        p.setPen(Qt.NoPen)
        p.setBrush(sheen)
        p.drawEllipse(c, r - 1, r - 1)
        # the label area stays unlit (the label is drawn over it anyway)

        # arm base
        pivot = self._pivot()
        p.setBrush(QColor("#9aa0a8"))
        p.setPen(QPen(QColor("#2a2d33"), 1.5))
        p.drawEllipse(pivot, s * 0.055, s * 0.055)
        p.setBrush(QColor("#3a3e45"))
        p.drawEllipse(pivot, s * 0.025, s * 0.025)
        p.end()
        return out

    def _arm_angle(self) -> float:
        """Degrees, measured from pointing straight down (screen coords),
        positive toward the record."""
        rest = 2.0
        rec = self._record_rect()
        c = rec.center()
        r = rec.width() / 2
        pivot = self._pivot()
        length = self._arm_length()
        # the arm tip lands on a circle of radius `rr` round the record
        # centre; solve the angle that puts it there
        outer = r - 6
        inner = r * LABEL_RATIO + 10
        rr = outer + (inner - outer) * self._progress
        dx, dy = c.x() - pivot.x(), c.y() - pivot.y()
        dist = math.hypot(dx, dy)
        # law of cosines: angle between pivot->centre and pivot->tip
        cos_a = (length ** 2 + dist ** 2 - rr ** 2) / (2 * length * dist)
        cos_a = max(-1.0, min(1.0, cos_a))
        to_centre = math.degrees(math.atan2(dx, dy))   # from straight down
        playing = to_centre + math.degrees(math.acos(cos_a))
        return rest + (playing - rest) * self._ease(self._arm)

    def _arm_length(self) -> float:
        return self._size * 0.56

    @staticmethod
    def _ease(t: float) -> float:
        return t * t * (3 - 2 * t)

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        if self._base is None:
            self._base = self._make_base()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(0, 0, self._base)

        rec = self._record_rect()
        c = rec.center()
        if self._label is not None:
            p.save()
            p.translate(c)
            p.rotate(self._angle)
            half = self._label.width() / 2
            p.drawPixmap(QPointF(-half, -half), self._label)
            p.restore()
        # spindle
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#d7dbe0"))
        p.drawEllipse(c, 4.5, 4.5)
        p.setBrush(QColor("#6b7078"))
        p.drawEllipse(c, 2, 2)

        # tone arm
        pivot = self._pivot()
        angle = math.radians(self._arm_angle())
        length = self._arm_length()
        tip = QPointF(pivot.x() + math.sin(angle) * length, pivot.y() + math.cos(angle) * length)
        # shadow
        p.setPen(QPen(QColor(0, 0, 0, 90), 7, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pivot + QPointF(4, 5), tip + QPointF(4, 5))
        p.setPen(QPen(QColor("#c9ced5"), 5, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pivot, tip)
        # headshell
        p.save()
        p.translate(tip)
        p.rotate(-math.degrees(angle) + 18)
        p.setPen(QPen(QColor("#2a2d33"), 1))
        p.setBrush(QBrush(QColor("#e4e7eb")))
        p.drawRoundedRect(QRectF(-6, -2, 12, 20), 2, 2)
        p.restore()
        # counterweight
        back = QPointF(pivot.x() - math.sin(angle) * 22, pivot.y() - math.cos(angle) * 22)
        p.setPen(QPen(QColor("#2a2d33"), 1))
        p.setBrush(QColor("#60656d"))
        p.drawEllipse(back, 9, 9)
        p.end()
