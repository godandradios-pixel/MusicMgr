"""A record player seen from above, for Now Playing (2026-09-30).

James: "on the Now Playing page, any way we can incorporate a visual that
shows the album cover, spinning as if on a record player from the top
view?"

The album cover is the whole record (2026-09-30: "make the album art the
whole record, it doesn't need to have space for the actual grooves") and
turns.
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
import random
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
    QRadialGradient,
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
LABEL_RATIO = 0.985
#: how far in the arm travels by the end of a song, as a share of the radius
RUN_OUT_RATIO = 0.42


class Turntable(QWidget):
    def __init__(self, size: int = 340, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._base: Optional[QPixmap] = None
        self._overlay: Optional[QPixmap] = None
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
        # the record runs to the very edge of the box (2026-09-30: "a little
        # bit less box - have the spinning record go to the very edges");
        # the deck only shows in the four corners
        r = s * 0.475
        c = QPointF(s * 0.5, s * 0.5)
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)

    def _pivot(self) -> QPointF:
        s = self._size
        return QPointF(s * 0.915, s * 0.125)

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

    @staticmethod
    def _mottle(p: QPainter, rect: QRectF, clip: QPainterPath, seed: int,
                colors: tuple, count: int, size: tuple) -> None:
        """Soft random blotches - the swirl in old bakelite, or felt."""
        rng = random.Random(seed)
        p.save()
        p.setClipPath(clip)
        p.setPen(Qt.NoPen)
        for _ in range(count):
            color = QColor(rng.choice(colors))
            color.setAlpha(rng.randint(10, 38))
            p.setBrush(color)
            w = rng.uniform(*size)
            h = w * rng.uniform(0.4, 1.0)
            x = rng.uniform(rect.left(), rect.right())
            y = rng.uniform(rect.top(), rect.bottom())
            p.drawEllipse(QPointF(x, y), w, h)
        p.restore()

    def _make_base(self) -> QPixmap:
        """Everything that never moves, in the style of a late-1930s /
        1940s console record changer (James's Philco "Beam of Light"):
        walnut cabinet, mottled brown bakelite deck, felt turntable, a
        curved record-support arm on the left, a speed-selector row and
        the arm rest on the right, a lever up front."""
        s = self._size
        out = QPixmap(s, s)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)

        # walnut cabinet, grain running across
        cab = QRectF(0.5, 0.5, s - 1, s - 1)
        wood = QLinearGradient(0, 0, 0, s)
        wood.setColorAt(0.0, QColor("#6b3b1f"))
        wood.setColorAt(0.5, QColor("#5a2f17"))
        wood.setColorAt(1.0, QColor("#4a2512"))
        p.setPen(QPen(QColor("#2a140a"), 1.5))
        p.setBrush(wood)
        p.drawRoundedRect(cab, 14, 14)
        rng = random.Random(7)
        cab_clip = QPainterPath()
        cab_clip.addRoundedRect(cab, 14, 14)
        p.save()
        p.setClipPath(cab_clip)
        for _ in range(int(s * 0.35)):
            y = rng.uniform(0, s)
            p.setPen(QPen(QColor(30, 12, 4, rng.randint(18, 45)), rng.uniform(0.6, 1.6)))
            p.drawLine(QPointF(0, y), QPointF(s, y + rng.uniform(-6, 6)))
        p.restore()

        # bakelite deck plate
        m = s * 0.012
        deck = QRectF(m, m, s - 2 * m, s - 2 * m)
        deck_clip = QPainterPath()
        deck_clip.addRoundedRect(deck, s * 0.07, s * 0.07)
        shade = QLinearGradient(deck.topLeft(), deck.bottomRight())
        shade.setColorAt(0.0, QColor("#4a2a17"))
        shade.setColorAt(0.55, QColor("#2e180c"))
        shade.setColorAt(1.0, QColor("#1f1008"))
        p.setPen(QPen(QColor("#140903"), 2))
        p.setBrush(shade)
        p.drawPath(deck_clip)
        self._mottle(p, deck, deck_clip, 11, ("#6a3d22", "#170a04", "#553018"),
                     int(s * 0.9), (s * 0.01, s * 0.05))
        # raised rim highlight along the top/left edge
        p.setPen(QPen(QColor(255, 220, 180, 40), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(deck.adjusted(3, 3, -3, -3), s * 0.06, s * 0.06)
        # mounting holes
        for fx, fy in ((0.05, 0.05),):
            c = QPointF(s * fx, s * fy)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#120702"))
            p.drawEllipse(c, s * 0.014, s * 0.014)
            p.setBrush(QColor(255, 220, 180, 30))
            p.drawEllipse(c + QPointF(0.8, 0.8), s * 0.008, s * 0.008)

        rec = self._record_rect()
        c = rec.center()
        r = rec.width() / 2

        # felt turntable, a little larger than the record
        felt_r = s * 0.495
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))                   # shadow
        p.drawEllipse(c + QPointF(3, 4), felt_r, felt_r)
        p.setBrush(QColor("#5b5249"))
        p.drawEllipse(c, felt_r, felt_r)
        felt_clip = QPainterPath()
        felt_clip.addEllipse(c, felt_r, felt_r)
        self._mottle(p, QRectF(c.x() - felt_r, c.y() - felt_r, 2 * felt_r, 2 * felt_r),
                     felt_clip, 23, ("#7a7065", "#3a332c"), int(s * 1.4), (1.0, 3.0))

        # the record itself is all album art (drawn per frame, turning);
        # just its dark shellac edge here, under the art
        p.setBrush(QColor("#0e0d0c"))
        p.drawEllipse(c, r, r)

        # front control panel: reject lever and two jewel lamps (lit in paint)
        panel = QRectF(s * 0.86, s * 0.905, s * 0.12, s * 0.075)
        p.setPen(QPen(QColor("#110802"), 1))
        p.setBrush(QColor("#3d2210"))
        p.drawRoundedRect(panel, 4, 4)
        lever = QPainterPath()
        lx, ly = s * 0.07, s * 0.93
        lever.moveTo(lx - s * 0.015, ly - s * 0.025)
        lever.lineTo(lx + s * 0.015, ly - s * 0.025)
        lever.lineTo(lx + s * 0.005, ly + s * 0.028)
        lever.lineTo(lx - s * 0.005, ly + s * 0.028)
        lever.closeSubpath()
        p.setBrush(QColor("#1a0d06"))
        p.drawPath(lever)

        # arm pivot base
        pivot = self._pivot()
        p.setPen(QPen(QColor("#120702"), 1.5))
        p.setBrush(QColor(0, 0, 0, 100))
        p.drawEllipse(pivot + QPointF(3, 4), s * 0.045, s * 0.045)
        base = QLinearGradient(pivot - QPointF(s * 0.045, s * 0.045), pivot + QPointF(s * 0.045, s * 0.045))
        base.setColorAt(0.0, QColor("#8b9096"))
        base.setColorAt(1.0, QColor("#3a3e43"))
        p.setBrush(base)
        p.drawEllipse(pivot, s * 0.045, s * 0.045)
        p.end()
        return out

    def _make_overlay(self) -> QPixmap:
        """Drawn over the turning art every frame but never turns itself: a
        soft lamp reflection, like light on a glossy record - what makes the
        art's turning read as a spinning disc."""
        s = self._size
        out = QPixmap(s, s)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        rec = self._record_rect()
        c = rec.center()
        r = rec.width() / 2
        sheen = QConicalGradient(c, 50)
        for at, alpha in ((0.0, 46), (0.07, 12), (0.18, 0), (0.43, 0), (0.5, 30),
                          (0.57, 8), (0.68, 0), (0.93, 0), (1.0, 46)):
            sheen.setColorAt(at, QColor(255, 240, 215, alpha))
        p.setPen(Qt.NoPen)
        p.setBrush(sheen)
        p.drawEllipse(c, r, r)
        # a hint of grooves near the edge, and the shellac rim
        p.setBrush(Qt.NoBrush)
        for k in range(6):
            p.setPen(QPen(QColor(0, 0, 0, 38), 1))
            rr = r * (0.9 + k * 0.018)
            p.drawEllipse(c, rr, rr)
        p.setPen(QPen(QColor("#0e0d0c"), max(2.0, s * 0.012)))
        p.drawEllipse(c, r - s * 0.006, r - s * 0.006)
        p.end()
        return out

    def _rest_point(self) -> QPointF:
        """Where the stylus sits when the arm is parked."""
        pivot = self._pivot()
        a = math.radians(self._REST_DEG)
        length = self._arm_length()
        return QPointF(pivot.x() + math.sin(a) * length, pivot.y() + math.cos(a) * length)

    _REST_DEG = -3.0

    def _arm_angle(self) -> float:
        """Degrees, measured from pointing straight down (screen coords),
        positive toward the record."""
        rest = self._REST_DEG
        rec = self._record_rect()
        c = rec.center()
        r = rec.width() / 2
        pivot = self._pivot()
        length = self._arm_length()
        # the arm tip lands on a circle of radius `rr` round the record
        # centre; solve the angle that puts it there
        outer = r - 6
        inner = r * RUN_OUT_RATIO
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
        return self._size * 0.46

    @staticmethod
    def _ease(t: float) -> float:
        return t * t * (3 - 2 * t)

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        if self._base is None:
            self._base = self._make_base()
            self._overlay = self._make_overlay()
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
        p.drawPixmap(0, 0, self._overlay)
        # the changer's tall spindle, seen from above: its shadow falls
        # across the record, the post catches the light
        sz = self._size
        p.setPen(QPen(QColor(0, 0, 0, 110), 5, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, c + QPointF(sz * 0.07, sz * 0.09))
        p.setPen(Qt.NoPen)
        post = QRadialGradient(c - QPointF(1.5, 1.5), 6)
        post.setColorAt(0.0, QColor("#f4f4f2"))
        post.setColorAt(1.0, QColor("#7c7f82"))
        p.setBrush(post)
        p.drawEllipse(c, 5, 5)

        # jewel lamps: green while playing, red when stopped with a track
        s = self._size
        for pos, on_color, lit in (
            (QPointF(s * 0.893, s * 0.943), "#58e07a", self._playing),
            (QPointF(s * 0.945, s * 0.943), "#ff5a4a", self._has_track and not self._playing),
        ):
            p.setPen(QPen(QColor("#0d0602"), 1))
            if lit:
                glow = QRadialGradient(pos, s * 0.03)
                glow.setColorAt(0.0, QColor(on_color))
                glow.setColorAt(1.0, QColor(0, 0, 0, 0))
                p.setBrush(glow)
                p.setPen(Qt.NoPen)
                p.drawEllipse(pos, s * 0.03, s * 0.03)
                p.setPen(QPen(QColor("#0d0602"), 1))
                p.setBrush(QColor(on_color))
            else:
                p.setBrush(QColor("#3a2a1e"))
            p.drawEllipse(pos, s * 0.013, s * 0.013)

        # tone arm (2026-09-30: "back off that philco beam of light needle,
        # go back to a more realistic one"): a straight brushed-aluminium
        # tube from a chrome pivot, a counterweight behind, and a headshell
        # with finger lift and a black cartridge over the needle
        pivot = self._pivot()
        angle = math.radians(self._arm_angle())
        length = self._arm_length()
        ux, uy = math.sin(angle), math.cos(angle)       # along the arm
        stylus = QPointF(pivot.x() + ux * length, pivot.y() + uy * length)
        shell_len = s * 0.1
        neck = QPointF(stylus.x() - ux * shell_len * 0.7, stylus.y() - uy * shell_len * 0.7)
        tube_w = max(3.0, s * 0.014)
        # shadow, then the tube with a highlight line along it
        p.setPen(QPen(QColor(0, 0, 0, 95), tube_w + 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pivot + QPointF(4, 5), neck + QPointF(4, 5))
        p.setPen(QPen(QColor("#9aa0a6"), tube_w, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pivot, neck)
        p.setPen(QPen(QColor(255, 255, 255, 150), max(1.0, tube_w * 0.3)))
        p.drawLine(pivot + QPointF(-tube_w * 0.25, -tube_w * 0.25),
                   neck + QPointF(-tube_w * 0.25, -tube_w * 0.25))

        # counterweight behind the pivot
        back = QPointF(pivot.x() - ux * s * 0.065, pivot.y() - uy * s * 0.065)
        p.setPen(QPen(QColor("#9aa0a6"), tube_w, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(pivot, back)
        p.save()
        p.translate(back)
        p.rotate(-math.degrees(angle))
        weight = QLinearGradient(-s * 0.028, 0, s * 0.028, 0)
        weight.setColorAt(0.0, QColor("#5e6368"))
        weight.setColorAt(0.45, QColor("#d9dde1"))
        weight.setColorAt(1.0, QColor("#4a4e53"))
        p.setPen(QPen(QColor("#2b2e31"), 1))
        p.setBrush(weight)
        p.drawRoundedRect(QRectF(-s * 0.028, -s * 0.035, s * 0.056, s * 0.04), 3, 3)
        p.restore()

        # chrome pivot cap over the arm
        cap = QRadialGradient(pivot - QPointF(s * 0.01, s * 0.01), s * 0.03)
        cap.setColorAt(0.0, QColor("#f2f4f6"))
        cap.setColorAt(1.0, QColor("#6f757b"))
        p.setPen(QPen(QColor("#2b2e31"), 1))
        p.setBrush(cap)
        p.drawEllipse(pivot, s * 0.026, s * 0.026)

        # headshell and cartridge
        p.save()
        p.translate(neck)
        p.rotate(-math.degrees(angle) - 12)             # a little offset, like a real arm
        sw = s * 0.05
        shell = QPainterPath()
        shell.addRoundedRect(QRectF(-sw / 2, 0, sw, shell_len), 2.5, 2.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 95))
        p.drawPath(shell.translated(4, 5))
        silver = QLinearGradient(-sw / 2, 0, sw / 2, 0)
        silver.setColorAt(0.0, QColor("#7d838a"))
        silver.setColorAt(0.5, QColor("#dfe3e7"))
        silver.setColorAt(1.0, QColor("#6d737a"))
        p.setPen(QPen(QColor("#2b2e31"), 1))
        p.setBrush(silver)
        p.drawPath(shell)
        # black cartridge body toward the tip, stylus at its front
        p.setBrush(QColor("#141516"))
        p.drawRoundedRect(QRectF(-sw * 0.36, shell_len * 0.42, sw * 0.72, shell_len * 0.5), 1.5, 1.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#c9a227"))
        p.drawEllipse(QPointF(0, shell_len * 0.9), 1.3, 1.3)
        # finger lift off the side
        p.setPen(QPen(QColor("#b9bec3"), max(1.5, s * 0.006), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(sw / 2, shell_len * 0.25), QPointF(sw / 2 + s * 0.028, shell_len * 0.12))
        p.restore()
        p.end()
