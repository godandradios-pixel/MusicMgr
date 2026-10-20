"""A tabletop radio for Now Playing while a station or an old-time radio
show plays (2026-10-19).

James: "when I'm listening to a radio station, it doesn't make sense that a
record spins. It's a station not an album." So instead of the turntable,
Now Playing shows a small set in the same finish as the Radio page's
(Settings › Appearance - walnut, oak, bakelite):

- the station's logo (or the show's artwork) on an ivory badge in the
  speaker grille;
- a lit dial glass with the band's own scale - AM 540-1600, FM 88-108, or a
  program band's - and the red pointer where the Radio page puts that
  station: WTAM 1100 sits halfway between 1000 and 1200
  (services/stations.py:dial_positions). A new station glides the pointer
  across;
- the 6E5 magic eye showing how the stream is coming in, as on the Radio
  page: closed when the signal's good, flickering while it connects or
  buffers, wide open and dim when it's lost (PlayerController.signal);
- the pilot lamp, lit while it plays, and the VOLUME / TUNING knobs.

Painted, not interactive: tap and right-click are handled by Now Playing,
the same as for the turntable and the flat cover.
"""

from __future__ import annotations

import math
import random
import re
from typing import Optional

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...services import stations as station_svc
from ..skins import radio_finish

SIGNAL_NONE, SIGNAL_CONNECTING, SIGNAL_GOOD, SIGNAL_BUFFERING, SIGNAL_LOST = (
    "none", "connecting", "good", "buffering", "lost")

#: printed scale and label per band - the Radio page's (ui/widgets/radio_dial.py)
BAND_SCALES = {
    "fm": ("FM", tuple(str(m) for m in station_svc.FM_MARKS)),
    # in tens of kHz, the way AM dials are printed ("54 … 160")
    "am": ("AM", tuple(str(m // 10) for m in station_svc.AM_MARKS)),
    "police": ("POLICE", ("1.6", "1.8", "2.0", "2.2", "2.4", "2.6", "2.8", "3.0")),
    "sw1": ("SW1", ("5.9", "6.5", "7.1", "7.7", "8.3", "8.9", "9.5", "10.1")),
    "sw2": ("SW2", ("11", "12", "13", "15", "17", "19", "21", "22")),
    "lw": ("LW", ("150", "175", "200", "225", "250", "275", "300", "350")),
}

POINTER = QColor("#d1281e")
GOLD = QColor("#d9b56a")
CREAM = QColor("#f1e3bd")
IVORY = QColor("#efe4c8")


def badge_text(label: str) -> str:
    """What the badge shows when a station has no logo: its call sign
    ("WTAM 1100" -> "WTAM"), else the initials of its words ("Radio
    Paradise" -> "RP")."""
    words = (label or "").split()
    if words and re.fullmatch(r"[A-Z]{3,4}(-[A-Z]{2})?", words[0]):
        return words[0].split("-")[0]
    initials = "".join(w[0] for w in words if w[:1].isalpha())[:3].upper()
    return initials or "\u266a"


def _fc(value) -> QColor:
    return QColor(value) if isinstance(value, str) else QColor(*value)


def _font(px: float, bold: bool = False, serif: bool = False) -> QFont:
    f = QFont("Georgia" if serif else "Arial Narrow")
    f.setStyleHint(QFont.Serif if serif else QFont.SansSerif)
    f.setPixelSize(max(6, int(px)))
    f.setBold(bold)
    if not serif:
        f.setStretch(QFont.SemiCondensed)
    return f


class TableRadio(QWidget):
    def __init__(self, size: int = 340, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._finish = radio_finish()
        self._band = "am"
        self._pos = 0.5
        self._label = ""
        self._logo: Optional[QPixmap] = None
        self._playing = False
        self._signal = SIGNAL_NONE
        self._flicker = 0.0
        self._rng = random.Random()
        self._cabinet: Optional[QPixmap] = None

        self._flicker_timer = QTimer(self)
        self._flicker_timer.setInterval(90)
        self._flicker_timer.timeout.connect(self._on_flicker)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(700)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # -- public ------------------------------------------------------------

    @property
    def band(self) -> str:
        return self._band

    @property
    def pointer(self) -> float:
        return self._pos

    @property
    def label(self) -> str:
        return self._label

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def signal(self) -> str:
        return self._signal

    def set_station(self, band: str, position: float, label: str = "",
                    logo_path: Optional[str] = None, animate: bool = True) -> None:
        """What's tuned in: `band` ("am", "fm", "police" ...), where on the
        0..1 dial, its name (drawn on the badge when there's no logo) and
        its logo or artwork."""
        band = band if band in BAND_SCALES else "fm"
        position = max(0.0, min(1.0, position))
        logo = QPixmap(logo_path) if logo_path else QPixmap()
        self._logo = None if logo.isNull() else logo
        self._label = label or ""
        same_band = band == self._band
        self._band = band
        self._anim.stop()
        if animate and same_band and self.isVisible() and abs(position - self._pos) > 0.002:
            self._anim.setStartValue(float(self._pos))
            self._anim.setEndValue(float(position))
            self._anim.start()
        else:
            self._pos = position
        self.update()

    def set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self._sync_flicker()
            self.update()

    def set_signal(self, level: str) -> None:
        if level != self._signal:
            self._signal = level
            self._sync_flicker()
            self.update()

    # -- animation -----------------------------------------------------------

    def _on_anim(self, value) -> None:
        self._pos = float(value)
        self.update()

    def _sync_flicker(self) -> None:
        if self._signal in (SIGNAL_CONNECTING, SIGNAL_BUFFERING) and self.isVisible():
            self._flicker_timer.start()
        else:
            self._flicker_timer.stop()

    def _on_flicker(self) -> None:
        self._flicker = self._rng.random()
        self.update()

    def showEvent(self, event) -> None:  # noqa: D102
        self._sync_flicker()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: D102
        self._flicker_timer.stop()
        super().hideEvent(event)

    # -- geometry --------------------------------------------------------------

    def _rects(self) -> dict:
        s = float(self.width())
        return {
            "cabinet": QRectF(s * 0.02, s * 0.03, s * 0.96, s * 0.94),
            "grille": QRectF(s * 0.10, s * 0.10, s * 0.80, s * 0.44),
            "dial": QRectF(s * 0.10, s * 0.60, s * 0.80, s * 0.14),
            "lamp": (QPointF(s * 0.50, s * 0.570), s * 0.016),
            "eye": (QPointF(s * 0.50, s * 0.845), s * 0.055),
            "vol": (QPointF(s * 0.23, s * 0.835), s * 0.066),
            "tune": (QPointF(s * 0.77, s * 0.835), s * 0.066),
        }

    def _x_at(self, dial: QRectF, t: float) -> float:
        return dial.left() + dial.width() * t

    # -- painting ----------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: D102
        r = self._rects()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._cabinet is None or self._cabinet.size() != self.size():
            self._cabinet = self._paint_cabinet(r)
        p.drawPixmap(0, 0, self._cabinet)
        self._paint_badge(p, r["grille"])
        self._paint_dial(p, r["dial"])
        self._paint_knob(p, *r["vol"], -60.0, "VOLUME")
        self._paint_knob(p, *r["tune"], self._pos * 300.0 - 150.0, "TUNING")
        self._paint_eye(p, *r["eye"])
        self._paint_lamp(p, *r["lamp"])
        p.end()

    def _paint_cabinet(self, r: dict) -> QPixmap:
        """Wood case with an arched top and the grille cloth - fixed per size."""
        f = self._finish
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        c = r["cabinet"]
        arch = c.height() * 0.16
        path = QPainterPath(QPointF(c.left(), c.bottom() - 18))
        path.lineTo(c.left(), c.top() + arch)
        path.quadTo(QPointF(c.left(), c.top()), QPointF(c.left() + c.width() * 0.22, c.top()))
        path.lineTo(c.right() - c.width() * 0.22, c.top())
        path.quadTo(QPointF(c.right(), c.top()), QPointF(c.right(), c.top() + arch))
        path.lineTo(c.right(), c.bottom() - 18)
        path.quadTo(QPointF(c.right(), c.bottom()), QPointF(c.right() - 18, c.bottom()))
        path.lineTo(c.left() + 18, c.bottom())
        path.quadTo(QPointF(c.left(), c.bottom()), QPointF(c.left(), c.bottom() - 18))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawPath(path.translated(3, 5))
        wood = QLinearGradient(c.topLeft(), c.bottomRight())
        w0, w1, w2 = f["wood"]
        wood.setColorAt(0.0, _fc(w0))
        wood.setColorAt(0.45, _fc(w1))
        wood.setColorAt(1.0, _fc(w2))
        p.fillPath(path, wood)
        rng = random.Random(7)
        p.save()
        p.setClipPath(path)
        for _ in range(int(c.height() / 3)):
            y = rng.uniform(c.top(), c.bottom())
            p.setPen(QPen(QColor(*f["grain"], rng.randint(14, 40)), rng.uniform(0.5, 1.4)))
            amp, wl, ph = rng.uniform(2, 7), rng.uniform(90, 300), rng.uniform(0, 6.28)
            grain = QPainterPath(QPointF(c.left(), y))
            x = c.left()
            while x < c.right():
                x += 10
                grain.lineTo(x, y + amp * math.sin(x / wl * 6.28 + ph))
            p.drawPath(grain)
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_fc(f["edge"]), 2))
        p.drawPath(path)

        # grille: a raised wood frame round the speaker cloth
        g = r["grille"]
        frame = g.adjusted(-6, -6, 6, 6)
        p.setPen(QPen(_fc(f["edge_hi"]), 2))
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawRoundedRect(frame, 14, 14)
        cloth = QLinearGradient(g.topLeft(), g.bottomLeft())
        cloth.setColorAt(0, QColor("#4a3820"))
        cloth.setColorAt(1, QColor("#2c2012"))
        p.setPen(Qt.NoPen)
        p.setBrush(cloth)
        p.drawRoundedRect(g, 10, 10)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(g, 10, 10)
        p.setClipPath(clip)
        p.setPen(QPen(QColor(214, 180, 110, 26), 1))
        step = 3.0
        x = g.left()
        while x < g.right():
            p.drawLine(QPointF(x, g.top()), QPointF(x, g.bottom()))
            x += step
        y = g.top()
        while y < g.bottom():
            p.drawLine(QPointF(g.left(), y), QPointF(g.right(), y))
            y += step
        # three wooden louvres across the cloth, behind the badge
        p.setPen(Qt.NoPen)
        for k in (0.22, 0.5, 0.78):
            bar = QRectF(g.left(), g.top() + g.height() * k - 3, g.width(), 6)
            grad = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            grad.setColorAt(0, _fc(w1).lighter(115))
            grad.setColorAt(1, _fc(w2))
            p.setBrush(grad)
            p.drawRect(bar)
        p.restore()

        # the dial's escutcheon
        d = r["dial"].adjusted(-5, -5, 5, 5)
        p.setPen(QPen(GOLD.darker(130), 2))
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(d, 8, 8)
        p.end()
        return pm

    def _paint_badge(self, p: QPainter, g: QRectF) -> None:
        """The station's logo (or the show's artwork) on an ivory badge."""
        side = min(g.height() * 0.86, g.width() * 0.62)
        badge = QRectF(g.center().x() - side / 2, g.center().y() - side / 2, side, side)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawRoundedRect(badge.translated(2, 4), 12, 12)
        face = QLinearGradient(badge.topLeft(), badge.bottomLeft())
        face.setColorAt(0, IVORY.lighter(104))
        face.setColorAt(1, IVORY.darker(108))
        p.setBrush(face)
        p.setPen(QPen(GOLD.darker(115), 2))
        p.drawRoundedRect(badge, 12, 12)
        inner = badge.adjusted(side * 0.07, side * 0.07, -side * 0.07, -side * 0.07)
        if self._logo is not None:
            scaled = self._logo.scaled(int(inner.width()), int(inner.height()),
                                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap(QPointF(inner.center().x() - scaled.width() / 2,
                                 inner.center().y() - scaled.height() / 2), scaled)
        else:
            text = badge_text(self._label)
            p.setFont(_font(inner.height() * {1: 0.5, 2: 0.42, 3: 0.32}.get(len(text), 0.25), bold=True, serif=True))
            p.setPen(QColor("#5a3c1c"))
            p.drawText(inner, Qt.AlignCenter, text)

    def _paint_dial(self, p: QPainter, d: QRectF) -> None:
        label, marks = BAND_SCALES.get(self._band, BAND_SCALES["fm"])
        glass = QLinearGradient(d.topLeft(), d.bottomLeft())
        if self._playing:
            glass.setColorAt(0, QColor("#3a2610"))
            glass.setColorAt(0.5, QColor("#5a3a14"))
            glass.setColorAt(1, QColor("#2a1a0a"))
        else:
            glass.setColorAt(0, QColor("#1c140a"))
            glass.setColorAt(1, QColor("#120c06"))
        p.setPen(Qt.NoPen)
        p.setBrush(glass)
        p.drawRoundedRect(d, 6, 6)
        if self._playing:
            glow = QRadialGradient(QPointF(self._x_at(d, self._pos), d.center().y()), d.width() * 0.35)
            glow.setColorAt(0, QColor(255, 196, 110, 70))
            glow.setColorAt(1, QColor(255, 196, 110, 0))
            p.setBrush(glow)
            p.drawRoundedRect(d, 6, 6)

        lo, hi = station_svc.SCALE_LO, station_svc.SCALE_HI
        n = len(marks)
        # shrink the numbers until the widest fits between two marks
        gap = (self._x_at(d, hi) - self._x_at(d, lo)) / (n - 1)
        size = d.height() * 0.30
        while size > 6:
            p.setFont(_font(size))
            fm = QFontMetricsF(p.font())
            if max(fm.horizontalAdvance(m) for m in marks) <= gap * 0.82:
                break
            size -= 0.5
        ink = CREAM if self._playing else CREAM.darker(150)
        tick = GOLD if self._playing else GOLD.darker(150)
        base = d.top() + d.height() * 0.62
        for i in range(n * 4 - 3):
            t = lo + (hi - lo) * i / ((n - 1) * 4)
            x = self._x_at(d, t)
            major = i % 4 == 0
            p.setPen(QPen(tick, 1.4 if major else 0.8))
            p.drawLine(QPointF(x, base), QPointF(x, base + d.height() * (0.24 if major else 0.13)))
        p.setPen(ink)
        for i, mark in enumerate(marks):
            x = self._x_at(d, lo + (hi - lo) * i / (n - 1))
            p.drawText(QPointF(x - fm.horizontalAdvance(mark) / 2, base - d.height() * 0.08), mark)
        p.setFont(_font(d.height() * 0.24, bold=True))
        p.setPen(tick)
        p.drawText(QPointF(d.left() + 6, d.bottom() - d.height() * 0.08), label)

        x = self._x_at(d, self._pos)
        p.setPen(QPen(QColor(0, 0, 0, 120), 3.2))
        p.drawLine(QPointF(x + 1.5, d.top() + 3), QPointF(x + 1.5, d.bottom() - 3))
        p.setPen(QPen(POINTER, 2.2))
        p.drawLine(QPointF(x, d.top() + 2), QPointF(x, d.bottom() - 2))

        sheen = QLinearGradient(d.topLeft(), d.bottomLeft())
        sheen.setColorAt(0, QColor(255, 255, 255, 40))
        sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(sheen)
        p.drawRoundedRect(d, 6, 6)

    def _paint_knob(self, p: QPainter, center: QPointF, radius: float, angle_deg: float, label: str) -> None:
        f = self._finish
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawEllipse(center + QPointF(2, 3), radius, radius)
        body = QRadialGradient(center - QPointF(radius * 0.35, radius * 0.4), radius * 1.3)
        k0, k1, k2 = f["knob"]
        body.setColorAt(0, _fc(k0))
        body.setColorAt(0.6, _fc(k1))
        body.setColorAt(1, _fc(k2))
        p.setBrush(body)
        p.setPen(QPen(_fc(f["knob_edge"]), 1.2))
        p.drawEllipse(center, radius, radius)
        p.setPen(QPen(_fc(f["knob_hi"]), 1.0))
        for k in range(24):
            a = math.radians(k * 15)
            p.drawLine(center + QPointF(math.cos(a) * radius * 0.84, math.sin(a) * radius * 0.84),
                       center + QPointF(math.cos(a) * radius * 0.98, math.sin(a) * radius * 0.98))
        cap = QRadialGradient(center - QPointF(radius * 0.2, radius * 0.25), radius * 0.7)
        c0, c1 = f["knob_cap"]
        cap.setColorAt(0, _fc(c0))
        cap.setColorAt(1, _fc(c1))
        p.setBrush(cap)
        p.setPen(Qt.NoPen)
        p.drawEllipse(center, radius * 0.64, radius * 0.64)
        a = math.radians(angle_deg - 90)
        p.setPen(QPen(_fc(f["knob_mark"]), max(1.6, radius * 0.08), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(center + QPointF(math.cos(a) * radius * 0.22, math.sin(a) * radius * 0.22),
                   center + QPointF(math.cos(a) * radius * 0.6, math.sin(a) * radius * 0.6))
        p.setFont(_font(radius * 0.36, bold=True, serif=True))
        p.setPen(_fc(f["knob_label"]))
        fm = QFontMetricsF(p.font())
        p.drawText(QPointF(center.x() - fm.horizontalAdvance(label) / 2,
                           center.y() + radius + fm.ascent() + 2), label)

    def _eye_shadow(self) -> tuple[float, float]:
        """(shadow wedge in degrees, glow 0..1) - see RadioSet._eye_shadow."""
        if not self._playing and self._signal in (SIGNAL_NONE, SIGNAL_GOOD):
            return 70.0, 0.5
        if self._signal == SIGNAL_LOST:
            return 110.0, 0.42
        if self._signal in (SIGNAL_CONNECTING, SIGNAL_BUFFERING):
            return 8 + self._flicker * 92, 0.55 + 0.35 * (1.0 - self._flicker)
        return 8.0, 1.0

    def _paint_eye(self, p: QPainter, center: QPointF, radius: float) -> None:
        wedge, lit = self._eye_shadow()
        p.setPen(QPen(QColor("#1a1a14"), 2))
        bezel = QRadialGradient(center, radius * 1.3)
        bezel.setColorAt(0, QColor("#8a7a54"))
        bezel.setColorAt(1, QColor("#3a3020"))
        p.setBrush(bezel)
        p.drawEllipse(center, radius * 1.2, radius * 1.2)
        glow = QConicalGradient(center, 90)
        glow.setColorAt(0.0, QColor.fromRgbF(0.30 * lit, 1.0 * lit, 0.48 * lit))
        glow.setColorAt(0.5, QColor.fromRgbF(0.18 * lit, 0.75 * lit, 0.32 * lit))
        glow.setColorAt(1.0, QColor.fromRgbF(0.30 * lit, 1.0 * lit, 0.48 * lit))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#06140a"))
        p.drawEllipse(center, radius, radius)
        p.setBrush(QBrush(glow))
        p.drawEllipse(center, radius * 0.94, radius * 0.94)
        p.setBrush(QColor("#041008"))
        rect = QRectF(center.x() - radius * 0.95, center.y() - radius * 0.95, radius * 1.9, radius * 1.9)
        p.drawPie(rect, int((90 - wedge / 2) * 16), int(wedge * 16))
        cap = QRadialGradient(center, radius * 0.35)
        cap.setColorAt(0, QColor("#2b2b26"))
        cap.setColorAt(1, QColor("#0b0b09"))
        p.setBrush(cap)
        p.drawEllipse(center, radius * 0.32, radius * 0.32)

    def _paint_lamp(self, p: QPainter, center: QPointF, radius: float) -> None:
        glow = QRadialGradient(center, radius * 3.0)
        glow.setColorAt(0, QColor(255, 170, 60, 160 if self._playing else 40))
        glow.setColorAt(1, QColor(255, 170, 60, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, radius * 3.0, radius * 3.0)
        jewel = QRadialGradient(center - QPointF(radius * 0.3, radius * 0.3), radius * 1.2)
        jewel.setColorAt(0, QColor("#ffe2a8") if self._playing else QColor("#a8743a"))
        jewel.setColorAt(1, QColor("#c2561a") if self._playing else QColor("#4a2610"))
        p.setBrush(jewel)
        p.setPen(QPen(QColor("#2a1206"), 1.2))
        p.drawEllipse(center, radius, radius)
