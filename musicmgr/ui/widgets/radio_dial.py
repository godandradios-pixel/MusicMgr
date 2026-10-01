"""The radio tuner's set (2026-10-01): a painted antique multi-band radio.

James collects and restores antique radios. The tuner page is drawn as a
walnut cabinet around a wide glass dial in the style of a 1950s European
multi-band set: a dark glass face with gold and cream printing, one strip
per band, each with its own scale and its stations printed along it, and a
red pointer that runs down through every band. (A round cathedral face was
built first, then dropped at James's request - "let's just do the slider.
But I would like to incorporate a dial face with the various bands ... like
the attached", a Grundig-style UKW/KW/MW/LW glass; "not exactly like the
attached but the general idea".)

Bands, top to bottom (James's choice, "by type of program"):
    FM      internet stations
    AM      drama and comedy
    POLICE  crime and mystery
    SW1     WWII and world news
    SW2     American history, speeches, Paul Harvey
    LW      On the Air, commercials
Only the selected band's strip is lit; the piano keys below choose it.

Below the glass: a 6E5 "magic eye" that closes as a station comes in,
bakelite VOLUME and TUNING knobs, the band keys and a pilot lamp.
Interaction: tap a station name or anywhere on the lit band, drag the
pointer, drag the TUNING knob or scroll; let go and it settles on the
nearest station and tunes in. Between stations there's a little tuning
static (can be switched off). Everything is painted - no images shipped.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
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


@dataclass(frozen=True)
class Band:
    key: str
    label: str
    unit: str
    marks: tuple
    #: printed along the bottom of the strip (shortwave meter bands)
    meters: tuple = ()


BANDS: tuple[Band, ...] = (
    Band("fm", "FM", "MHz", ("88", "90", "92", "94", "96", "98", "100", "102", "104", "106", "108")),
    Band("am", "AM", "kc", ("540", "600", "700", "800", "900", "1000", "1200", "1400", "1600")),
    Band("police", "POLICE", "Mc", ("1.6", "1.8", "2.0", "2.2", "2.4", "2.6", "2.8", "3.0")),
    Band("sw1", "SW1", "Mc", ("5.9", "6.5", "7.1", "7.7", "8.3", "8.9", "9.5", "10.1"),
         ("49m", "41m", "31m")),
    Band("sw2", "SW2", "Mc", ("11", "12", "13", "15", "17", "19", "21", "22"),
         ("25m", "19m", "16m", "13m")),
    Band("lw", "LW", "kc", ("150", "175", "200", "225", "250", "275", "300", "350")),
)
BAND_KEYS = tuple(b.key for b in BANDS)
BY_KEY = {b.key: b for b in BANDS}

GOLD = QColor("#d9b56a")
CREAM = QColor("#f1e3bd")
POINTER = QColor("#d1281e")
GLASS_DARK = QColor("#160e07")
IVORY = QColor("#efe4c8")


def _font(px: float, bold: bool = False, condensed: bool = True) -> QFont:
    f = QFont("Arial Narrow" if condensed else "Georgia")
    f.setStyleHint(QFont.SansSerif if condensed else QFont.Serif)
    f.setPixelSize(max(6, int(px)))
    f.setBold(bold)
    if condensed:
        f.setStretch(QFont.SemiCondensed)
    return f


def station_positions(n: int) -> list[float]:
    """Where n stations sit along the 0..1 dial."""
    if n <= 0:
        return []
    if n == 1:
        return [0.5]
    lo, hi = 0.08, 0.92
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def make_static_wav(path: Path, seconds: float = 2.0, rate: int = 22050) -> Path:
    """A loop of AM-band tuning static: filtered noise with crackle and a
    faint heterodyne whistle, written once next to the library."""
    rng = random.Random(7)
    frames = bytearray()
    lp = 0.0
    n = int(seconds * rate)
    for i in range(n):
        white = rng.uniform(-1, 1)
        lp = lp * 0.82 + white * 0.18
        sample = lp * 0.55
        if rng.random() < 0.0015:
            sample += rng.choice((-1, 1)) * rng.uniform(0.3, 0.8)
        sample += 0.05 * math.sin(2 * math.pi * (1800 + 300 * math.sin(i / rate * 3)) * i / rate)
        edge = min(1.0, i / 600, (n - i) / 600)
        frames += struct.pack("<h", int(max(-1, min(1, sample * edge)) * 12000))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class RadioSet(QWidget):
    #: the pointer settled on station `index` of `band` - play it
    tuneRequested = Signal(str, int)
    bandRequested = Signal(str)
    volumeRequested = Signal(float)
    contextRequested = Signal(object)  # global QPoint

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(760, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._band = "am"
        self._labels: dict[str, list[str]] = {k: [] for k in BAND_KEYS}
        self._positions: dict[str, list[float]] = {k: [] for k in BAND_KEYS}
        #: per band, the station tuned/last tuned there
        self._tuned: dict[str, int] = {k: -1 for k in BAND_KEYS}
        self._pos = 0.5
        self._volume = 0.8
        self._playing = False
        self._drag: Optional[str] = None
        self._drag_angle = 0.0
        self._drag_moved = False
        self._cabinet: Optional[QPixmap] = None
        self._geom: dict = {}

        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._anim.finished.connect(self._on_anim_done)
        self._pending_tune = -1
        self._debounce = 250
        self._tune_timer = QTimer(self)
        self._tune_timer.setSingleShot(True)
        self._tune_timer.timeout.connect(self._emit_tune)

        self._static = None
        self._static_enabled = True
        self._static_path: Optional[Path] = None
        self._static_off = QTimer(self)
        self._static_off.setSingleShot(True)
        self._static_off.timeout.connect(self._silence)

    # -- public ------------------------------------------------------------

    @property
    def band(self) -> str:
        return self._band

    @property
    def pointer(self) -> float:
        return self._pos

    @property
    def tuned_index(self) -> int:
        return self._tuned.get(self._band, -1)

    def labels(self, band: Optional[str] = None) -> list[str]:
        return list(self._labels[band or self._band])

    def set_band(self, band: str, animate: bool = True) -> None:
        if band not in BY_KEY:
            return
        changed = band != self._band
        self._band = band
        idx = self._tuned.get(band, -1)
        if changed and 0 <= idx < len(self._positions[band]):
            self._move_to(self._positions[band][idx], animate, tune=False)
        self.update()

    def set_entries(self, band: str, labels: Sequence[str]) -> None:
        self._labels[band] = list(labels)
        self._positions[band] = station_positions(len(labels))
        if self._tuned[band] >= len(labels):
            self._tuned[band] = -1
        self.update()

    def set_tuned(self, band: str, index: int, animate: bool = True) -> None:
        """Show station `index` of `band` as tuned (switching to that band
        and moving the pointer) without asking for it to be played."""
        self._tuned[band] = index
        self._band = band
        if 0 <= index < len(self._positions[band]):
            self._move_to(self._positions[band][index], animate, tune=False)
        self.update()

    def clear_tuned(self, band: str) -> None:
        self._tuned[band] = -1
        self.update()

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))
        self.update()

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        self.update()

    def set_static_enabled(self, on: bool) -> None:
        self._static_enabled = on
        if not on:
            self._silence()

    def set_static_path(self, path: Path) -> None:
        self._static_path = path

    def step(self, delta: int) -> None:
        positions = self._positions[self._band]
        if not positions:
            return
        current = self._nearest()
        target = max(0, min(len(positions) - 1, current + delta))
        self._move_to(positions[target], True, tune=True, debounce=650)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(1150, 560)

    # -- geometry ----------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: D102
        self._cabinet = None
        self._relayout()
        super().resizeEvent(event)

    def _relayout(self) -> None:
        w, h = self.width(), self.height()
        g: dict = {}
        g["cabinet"] = QRectF(4, 4, w - 8, h - 8)
        controls_h = h * 0.24
        dial = QRectF(w * 0.04, h * 0.05, w * 0.92, h - controls_h - h * 0.08)
        g["dial"] = dial
        pad = dial.height() * 0.03
        inner = dial.adjusted(dial.width() * 0.012, pad, -dial.width() * 0.012, -pad)
        gap = inner.height() * 0.018
        strip_h = (inner.height() - gap * (len(BANDS) - 1)) / len(BANDS)
        label_w = inner.width() * 0.075
        g["strips"] = {}
        g["scale_left"] = inner.left() + label_w
        g["scale_width"] = inner.width() - 2 * label_w
        for i, band in enumerate(BANDS):
            top = inner.top() + i * (strip_h + gap)
            g["strips"][band.key] = QRectF(inner.left(), top, inner.width(), strip_h)
        cy = h - controls_h / 2 - h * 0.02
        kr = min(controls_h * 0.40, w * 0.05)
        g["knob_vol"] = (QPointF(w * 0.085, cy), kr)
        g["knob_tune"] = (QPointF(w * 0.915, cy), kr)
        er = min(controls_h * 0.26, w * 0.028)
        g["eye"] = (QPointF(w * 0.19, cy), er)
        g["lamp"] = (QPointF(w * 0.81, cy), er * 0.45)
        keys_left, keys_right = w * 0.245, w * 0.755
        n = len(BANDS)
        key_gap = 8
        key_w = (keys_right - keys_left - key_gap * (n - 1)) / n
        key_h = controls_h * 0.52
        g["keys"] = {
            band.key: QRectF(keys_left + i * (key_w + key_gap), cy - key_h / 2, key_w, key_h)
            for i, band in enumerate(BANDS)
        }
        self._geom = g

    def _x_at(self, t: float) -> float:
        return self._geom["scale_left"] + self._geom["scale_width"] * t

    # -- pointer ------------------------------------------------------------

    def _nearest(self, pos: Optional[float] = None) -> int:
        pos = self._pos if pos is None else pos
        positions = self._positions[self._band]
        if not positions:
            return -1
        return min(range(len(positions)), key=lambda i: abs(positions[i] - pos))

    def _closeness(self) -> float:
        positions = self._positions[self._band]
        if not positions:
            return 0.0
        i = self._nearest()
        spacing = (positions[1] - positions[0]) if len(positions) > 1 else 0.4
        return max(0.0, 1.0 - abs(self._pos - positions[i]) / max(1e-6, spacing / 2))

    def _move_to(self, target: float, animate: bool, tune: bool, debounce: int = 250) -> None:
        self._anim.stop()
        self._pending_tune = self._nearest(target) if tune else -1
        self._debounce = debounce
        if not animate or abs(target - self._pos) < 1e-4:
            self._pos = target
            self._on_anim_done()
            self.update()
            return
        self._anim.setDuration(int(250 + 900 * min(1.0, abs(target - self._pos))))
        self._anim.setStartValue(float(self._pos))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._pos = float(value)
        self._update_static()
        self.update()

    def _on_anim_done(self) -> None:
        self._update_static()
        if self._pending_tune >= 0:
            self._tune_timer.start(self._debounce)

    def _emit_tune(self) -> None:
        index, self._pending_tune = self._pending_tune, -1
        if index >= 0:
            self._tuned[self._band] = index
            self.update()
            self.tuneRequested.emit(self._band, index)

    def _pos_from_x(self, x: float) -> float:
        return max(0.0, min(1.0, (x - self._geom["scale_left"]) / self._geom["scale_width"]))

    # -- static -------------------------------------------------------------

    def _ensure_static(self):
        if self._static is not None or self._static_path is None:
            return self._static
        try:
            from PySide6.QtMultimedia import QSoundEffect

            if not self._static_path.exists():
                make_static_wav(self._static_path)
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(str(self._static_path)))
            effect.setLoopCount(QSoundEffect.Infinite)
            effect.setVolume(0.0)
            self._static = effect
        except Exception:  # pragma: no cover - no audio backend
            self._static = None
        return self._static

    def _update_static(self) -> None:
        moving = self._drag in ("dial", "tune") or self._anim.state() == QVariantAnimation.Running
        if not self._static_enabled or not moving or not self._positions[self._band]:
            self._static_off.start(120)
            return
        effect = self._ensure_static()
        if effect is None:
            return
        effect.setVolume((1.0 - self._closeness()) ** 0.8 * 0.35 * self._volume)
        if not effect.isPlaying():
            effect.play()
        self._static_off.stop()

    def _silence(self) -> None:
        if self._static is not None:
            self._static.stop()

    # -- input --------------------------------------------------------------

    def _hit(self, pt: QPointF) -> Optional[str]:
        g = self._geom
        for name in ("knob_vol", "knob_tune"):
            c, r = g[name]
            if math.hypot(pt.x() - c.x(), pt.y() - c.y()) <= r * 1.15:
                return name
        for key, rect in g["keys"].items():
            if rect.contains(pt):
                return "key:" + key
        for key, rect in g["strips"].items():
            if rect.contains(pt):
                return "strip:" + key
        return None

    def _angle(self, name: str, pt: QPointF) -> float:
        c, _ = self._geom[name]
        return math.degrees(math.atan2(pt.y() - c.y(), pt.x() - c.x()))

    def mousePressEvent(self, event) -> None:  # noqa: D102
        if event.button() == Qt.RightButton:
            return super().mousePressEvent(event)
        pt = event.position()
        hit = self._hit(pt) or ""
        if hit.startswith("key:"):
            self.bandRequested.emit(hit[4:])
        elif hit.startswith("strip:"):
            band = hit[6:]
            if band != self._band:
                # tapping another band's strip selects that band first
                self.bandRequested.emit(band)
                return
            if self._positions[band]:
                self._anim.stop()
                self._tune_timer.stop()
                self._drag = "dial"
                self._pos = self._pos_from_x(pt.x())
                self._update_static()
                self.update()
        elif hit in ("knob_tune", "knob_vol"):
            self._anim.stop()
            self._drag = "tune" if hit == "knob_tune" else "volume"
            self._drag_angle = self._angle(hit, pt)
            self._drag_moved = False
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: D102
        pt = event.position()
        if self._drag == "dial":
            self._pos = self._pos_from_x(pt.x())
            self._update_static()
            self.update()
        elif self._drag in ("tune", "volume"):
            name = "knob_tune" if self._drag == "tune" else "knob_vol"
            a = self._angle(name, pt)
            delta = (a - self._drag_angle + 540) % 360 - 180
            self._drag_angle = a
            if abs(delta) > 0.5:
                self._drag_moved = True
            if self._drag == "tune":
                self._pos = max(0.0, min(1.0, self._pos + delta / 720.0))
                self._update_static()
            else:
                self._volume = max(0.0, min(1.0, self._volume + delta / 270.0))
                self.volumeRequested.emit(self._volume)
            self.update()
        else:
            self.setCursor(Qt.PointingHandCursor if self._hit(pt) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: D102
        drag, self._drag = self._drag, None
        positions = self._positions[self._band]
        if drag in ("dial", "tune") and positions:
            if drag == "tune" and not self._drag_moved:
                c, _ = self._geom["knob_tune"]
                self.step(-1 if event.position().x() < c.x() else 1)
                return
            self._move_to(positions[self._nearest()], True, tune=True)
        self._update_static()

    def wheelEvent(self, event) -> None:  # noqa: D102
        steps = event.angleDelta().y() / 120 or event.angleDelta().x() / 120
        if not steps:
            return
        if self._hit(event.position()) == "knob_vol":
            self._volume = max(0.0, min(1.0, self._volume + 0.05 * steps))
            self.volumeRequested.emit(self._volume)
            self.update()
        else:
            self.step(1 if steps < 0 else -1)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: D102
        if event.key() == Qt.Key_Right:
            self.step(1)
        elif event.key() == Qt.Key_Left:
            self.step(-1)
        elif event.key() in (Qt.Key_Up, Qt.Key_Down):
            i = BAND_KEYS.index(self._band) + (-1 if event.key() == Qt.Key_Up else 1)
            self.bandRequested.emit(BAND_KEYS[i % len(BAND_KEYS)])
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: D102
        self.contextRequested.emit(event.globalPos())

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: D102
        if not self._geom:
            self._relayout()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        if self._cabinet is None or self._cabinet.size() != self.size():
            self._cabinet = self._paint_cabinet()
        p.drawPixmap(0, 0, self._cabinet)
        self._paint_glass(p)
        self._paint_controls(p)
        p.end()

    def _paint_cabinet(self) -> QPixmap:
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._geom["cabinet"]
        path = QPainterPath()
        path.addRoundedRect(r, 26, 26)
        wood = QLinearGradient(r.topLeft(), r.bottomRight())
        wood.setColorAt(0.0, QColor("#6e3c1e"))
        wood.setColorAt(0.45, QColor("#5a2f16"))
        wood.setColorAt(1.0, QColor("#431f0d"))
        p.fillPath(path, wood)
        rng = random.Random(42)
        p.save()
        p.setClipPath(path)
        for _ in range(int(r.height() / 3)):
            y = rng.uniform(r.top(), r.bottom())
            p.setPen(QPen(QColor(25, 10, 3, rng.randint(14, 40)), rng.uniform(0.5, 1.6)))
            amp, wl, ph = rng.uniform(2, 9), rng.uniform(120, 420), rng.uniform(0, 6.28)
            grain = QPainterPath(QPointF(r.left(), y))
            x = r.left()
            while x < r.right():
                x += 12
                grain.lineTo(x, y + amp * math.sin(x / wl * 6.28 + ph))
            p.drawPath(grain)
        p.restore()
        p.setPen(QPen(QColor("#1d0c04"), 2))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 220, 170, 38), 2))
        p.drawRoundedRect(r.adjusted(7, 7, -7, -7), 21, 21)
        p.end()
        return pm

    def _paint_glass(self, p: QPainter) -> None:
        d = self._geom["dial"]
        path = QPainterPath()
        path.addRoundedRect(d, 12, 12)
        glass = QLinearGradient(d.topLeft(), d.bottomLeft())
        glass.setColorAt(0, QColor("#22160b"))
        glass.setColorAt(1, GLASS_DARK)
        p.fillPath(path, glass)
        p.save()
        p.setClipPath(path)
        for band in BANDS:
            self._paint_strip(p, band)
        # the pointer runs down through every band
        x = self._x_at(self._pos)
        p.setPen(QPen(QColor(0, 0, 0, 120), 6))
        p.drawLine(QPointF(x + 2, d.top() + 6), QPointF(x + 2, d.bottom() - 6))
        p.setPen(QPen(POINTER, 3.2))
        p.drawLine(QPointF(x, d.top() + 4), QPointF(x, d.bottom() - 4))
        glow = QLinearGradient(QPointF(x - 9, 0), QPointF(x + 9, 0))
        glow.setColorAt(0, QColor(255, 60, 40, 0))
        glow.setColorAt(0.5, QColor(255, 80, 50, 55))
        glow.setColorAt(1, QColor(255, 60, 40, 0))
        p.fillRect(QRectF(x - 9, d.top(), 18, d.height()), glow)
        shine = QLinearGradient(d.topLeft(), d.bottomLeft())
        shine.setColorAt(0.0, QColor(255, 255, 255, 34))
        shine.setColorAt(0.22, QColor(255, 255, 255, 6))
        shine.setColorAt(0.23, QColor(255, 255, 255, 0))
        p.fillPath(path, shine)
        p.restore()
        p.setPen(QPen(QColor("#2a1a0a"), 8))
        p.drawPath(path)
        p.setPen(QPen(QColor("#b08a4a"), 5))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 236, 190, 110), 1))
        p.drawPath(path)

    def _paint_strip(self, p: QPainter, band: Band) -> None:
        r = self._geom["strips"][band.key]
        active = band.key == self._band
        ink = GOLD if active else QColor(GOLD.red(), GOLD.green(), GOLD.blue(), 105)
        name_ink = CREAM if active else QColor(CREAM.red(), CREAM.green(), CREAM.blue(), 95)
        if active:
            lit = QLinearGradient(r.topLeft(), r.bottomLeft())
            lit.setColorAt(0, QColor(255, 196, 110, 46))
            lit.setColorAt(1, QColor(255, 170, 80, 22))
            p.fillRect(r, lit)
        p.setPen(QPen(ink, 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
        left, width = self._geom["scale_left"], self._geom["scale_width"]
        # band name at both ends, like the old glass
        side_w = left - r.left()
        p.setFont(_font(min(r.height() * 0.42, side_w * 0.85 / max(2, len(band.label) * 0.55)), bold=True))
        p.setPen(name_ink if active else ink)
        p.drawText(QRectF(r.left(), r.top(), left - r.left(), r.height()), Qt.AlignCenter, band.label)
        p.drawText(QRectF(left + width, r.top(), r.right() - left - width, r.height()),
                   Qt.AlignCenter, band.label)
        p.setPen(QPen(ink, 1))
        p.drawLine(QPointF(left, r.top() + 3), QPointF(left, r.bottom() - 3))
        p.drawLine(QPointF(left + width, r.top() + 3), QPointF(left + width, r.bottom() - 3))
        # scale row: unit, numbers and ticks along the top of the strip
        scale_h = r.height() * 0.36
        base = r.top() + scale_h
        p.setFont(_font(scale_h * 0.62, bold=True))
        fm = QFontMetricsF(p.font())
        p.setPen(ink)
        p.drawText(QPointF(left + 6, base - scale_h * 0.22), band.unit)
        n = len(band.marks)
        for i, mark in enumerate(band.marks):
            t = 0.08 + 0.84 * i / (n - 1)
            x = left + width * t
            p.drawText(QPointF(x - fm.horizontalAdvance(mark) / 2, base - scale_h * 0.22), mark)
        if band.meters:
            # shortwave meter bands, small and italic, between the numbers
            meter_font = _font(scale_h * 0.46)
            meter_font.setItalic(True)
            p.setFont(meter_font)
            fm2 = QFontMetricsF(meter_font)
            for i, meter in enumerate(band.meters):
                t = 0.08 + 0.84 * (i * 2 + 1) / (2 * len(band.meters))
                t = 0.08 + 0.84 * (round(t * (n - 1) - 0.5) + 0.5) / (n - 1) if n > 1 else t
                x = left + width * t
                p.drawText(QPointF(x - fm2.horizontalAdvance(meter) / 2, base - scale_h * 0.22), meter)
        p.setPen(QPen(ink, 1))
        p.drawLine(QPointF(left + 4, base), QPointF(left + width - 4, base))
        for i in range(61):
            t = 0.08 + 0.84 * i / 60
            x = left + width * t
            major = i % 10 == 0
            p.drawLine(QPointF(x, base), QPointF(x, base + (scale_h * 0.3 if major else scale_h * 0.16)))
        # station names, one or two rows, each over a little marker bar
        labels = self._labels[band.key]
        positions = self._positions[band.key]
        if not labels:
            p.setFont(_font(r.height() * 0.24))
            p.setPen(QColor(ink.red(), ink.green(), ink.blue(), int(ink.alpha() * 0.7)))
            p.drawText(QRectF(left, base, width, r.bottom() - base), Qt.AlignCenter, "· · ·")
            return
        name_area = QRectF(left, base + scale_h * 0.32, width, r.bottom() - base - scale_h * 0.32)
        spacing = width * (0.84 / max(1, len(labels) - 1)) if len(labels) > 1 else width
        longest = max(len(label) for label in labels)
        two_rows = spacing < longest * name_area.height() * 0.30 and name_area.height() > 26
        rows = 2 if two_rows else 1
        px = min(name_area.height() / rows * 0.62,
                 max(8.0, spacing * rows * 0.95 / max(5, longest * 0.55)))
        tuned = self._tuned.get(band.key, -1)
        for i, (label, t) in enumerate(zip(labels, positions)):
            row = i % rows
            is_tuned = i == tuned
            font = _font(px, bold=is_tuned)
            fm = QFontMetricsF(font)
            avail = spacing * rows * 0.96
            full = fm.horizontalAdvance(label.upper())
            if full > avail and px > 9:
                # shrink a long name to fit before resorting to "..."
                font = _font(max(9.0, math.floor(font.pixelSize() * avail / full)), bold=is_tuned)
                fm = QFontMetricsF(font)
            p.setFont(font)
            text = fm.elidedText(label.upper(), Qt.ElideRight, avail)
            tw = fm.horizontalAdvance(text)
            cy = name_area.top() + name_area.height() * (row + 0.5) / rows
            x = left + width * t
            tx = max(left + 4, min(left + width - 4 - tw, x - tw / 2))
            p.setPen(QColor("#ffb19a") if (is_tuned and active) else name_ink)
            baseline = cy + (fm.ascent() - fm.descent()) / 2 - 1
            p.drawText(QPointF(tx, baseline), text)
            # marker bar under the name, centred on the station's spot
            bar = QRectF(x - 7, min(baseline + fm.descent() + 1.5, r.bottom() - 4), 14, 2.6)
            p.fillRect(bar, POINTER if (is_tuned and active) else ink)

    def _paint_knob(self, p: QPainter, center: QPointF, radius: float, angle_deg: float, label: str) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawEllipse(center + QPointF(3, 4), radius, radius)
        body = QRadialGradient(center - QPointF(radius * 0.35, radius * 0.4), radius * 1.3)
        body.setColorAt(0, QColor("#6a3a1c"))
        body.setColorAt(0.6, QColor("#2c1308"))
        body.setColorAt(1, QColor("#120602"))
        p.setBrush(body)
        p.setPen(QPen(QColor("#0a0301"), 1.5))
        p.drawEllipse(center, radius, radius)
        p.setPen(QPen(QColor(255, 210, 160, 45), 1.2))
        for k in range(36):
            a = math.radians(k * 10)
            p.drawLine(center + QPointF(math.cos(a) * radius * 0.86, math.sin(a) * radius * 0.86),
                       center + QPointF(math.cos(a) * radius * 0.98, math.sin(a) * radius * 0.98))
        cap = QRadialGradient(center - QPointF(radius * 0.2, radius * 0.25), radius * 0.7)
        cap.setColorAt(0, QColor("#7a4524"))
        cap.setColorAt(1, QColor("#2a1207"))
        p.setBrush(cap)
        p.setPen(Qt.NoPen)
        p.drawEllipse(center, radius * 0.66, radius * 0.66)
        a = math.radians(angle_deg - 90)
        p.setPen(QPen(CREAM, max(2.0, radius * 0.07), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(center + QPointF(math.cos(a) * radius * 0.25, math.sin(a) * radius * 0.25),
                   center + QPointF(math.cos(a) * radius * 0.62, math.sin(a) * radius * 0.62))
        p.setFont(_font(radius * 0.30, bold=True, condensed=False))
        p.setPen(CREAM)
        fm = QFontMetricsF(p.font())
        p.drawText(QPointF(center.x() - fm.horizontalAdvance(label) / 2,
                           center.y() + radius + fm.ascent() + 3), label)

    def _paint_key(self, p: QPainter, rect: QRectF, label: str, down: bool) -> None:
        r = rect.translated(0, 4 if down else 0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 100))
        p.drawRoundedRect(rect.translated(2, 6), 7, 7)
        face = QLinearGradient(r.topLeft(), r.bottomLeft())
        top = IVORY if not down else IVORY.darker(112)
        face.setColorAt(0, top.lighter(105))
        face.setColorAt(1, top.darker(118))
        p.setBrush(face)
        p.setPen(QPen(QColor("#5c4a2c"), 1.2))
        p.drawRoundedRect(r, 7, 7)
        p.setFont(_font(min(r.height() * 0.32, r.width() / max(3, len(label)) * 1.35), bold=True))
        p.setPen(QColor("#4a2a12") if not down else POINTER.darker(140))
        p.drawText(r, Qt.AlignCenter, label)

    def _paint_eye(self, p: QPainter, center: QPointF, radius: float) -> None:
        """A 6E5 magic eye: green fan with a dark shadow wedge that closes as
        the pointer lands on a station."""
        p.setPen(QPen(QColor("#1a1a14"), 3))
        bezel = QRadialGradient(center, radius * 1.3)
        bezel.setColorAt(0, QColor("#8a7a54"))
        bezel.setColorAt(1, QColor("#3a3020"))
        p.setBrush(bezel)
        p.drawEllipse(center, radius * 1.18, radius * 1.18)
        glow = QConicalGradient(center, 90)
        lit = 0.55 + 0.45 * (1.0 if self._playing else 0.6)
        glow.setColorAt(0.0, QColor.fromRgbF(0.30 * lit, 1.0 * lit, 0.48 * lit))
        glow.setColorAt(0.5, QColor.fromRgbF(0.18 * lit, 0.75 * lit, 0.32 * lit))
        glow.setColorAt(1.0, QColor.fromRgbF(0.30 * lit, 1.0 * lit, 0.48 * lit))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#06140a"))
        p.drawEllipse(center, radius, radius)
        p.setBrush(QBrush(glow))
        p.drawEllipse(center, radius * 0.94, radius * 0.94)
        wedge = 8 + (1.0 - self._closeness()) * 92
        p.setBrush(QColor("#041008"))
        rect = QRectF(center.x() - radius * 0.95, center.y() - radius * 0.95, radius * 1.9, radius * 1.9)
        p.drawPie(rect, int((90 - wedge / 2) * 16), int(wedge * 16))
        cap = QRadialGradient(center, radius * 0.35)
        cap.setColorAt(0, QColor("#2b2b26"))
        cap.setColorAt(1, QColor("#0b0b09"))
        p.setBrush(cap)
        p.drawEllipse(center, radius * 0.32, radius * 0.32)

    def _paint_lamp(self, p: QPainter, center: QPointF, radius: float) -> None:
        glow = QRadialGradient(center, radius * 2.6)
        glow.setColorAt(0, QColor(255, 170, 60, 150 if self._playing else 50))
        glow.setColorAt(1, QColor(255, 170, 60, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, radius * 2.6, radius * 2.6)
        jewel = QRadialGradient(center - QPointF(radius * 0.3, radius * 0.3), radius * 1.2)
        jewel.setColorAt(0, QColor("#ffe2a8") if self._playing else QColor("#a8743a"))
        jewel.setColorAt(1, QColor("#c2561a") if self._playing else QColor("#4a2610"))
        p.setBrush(jewel)
        p.setPen(QPen(QColor("#2a1206"), 1.5))
        p.drawEllipse(center, radius, radius)

    def _paint_controls(self, p: QPainter) -> None:
        g = self._geom
        c, r = g["knob_vol"]
        self._paint_knob(p, c, r, -135 + 270 * self._volume, "VOLUME")
        c, r = g["knob_tune"]
        self._paint_knob(p, c, r, self._pos * 720.0, "TUNING")
        for band in BANDS:
            self._paint_key(p, g["keys"][band.key], band.label, band.key == self._band)
        c, r = g["eye"]
        self._paint_eye(p, c, r)
        c, r = g["lamp"]
        self._paint_lamp(p, c, r)
