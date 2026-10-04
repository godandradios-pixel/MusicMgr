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

Bands, top to bottom (James's layout, reorganised 2026-10-01):
    FM      internet stations
    AM      internet stations
    POLICE  On the Air, WWII news and sounds, American history
    SW1     drama and comedy
    SW2     crime and mystery
    LW      speeches, Paul Harvey, commercials
Only the selected band's strip is lit; the piano keys below choose it.

Below the glass: a 6E5 "magic eye" that closes as a station comes in,
bakelite VOLUME and TUNING knobs, the band keys and a pilot lamp.

2026-10-03 (James): FM and AM stations with a frequency in their name sit
at that frequency on the scale ("102.5 WDVE" by the 102 mark, "KDKA 1020"
just past 1000), internet-only ones fill the empty stretches
(services/stations.py:dial_positions). The magic eye doubles as a signal
meter once a station is on: shadow closed on a good stream, flickering
while it buffers, wide open and dim when the signal is lost. A station
that's off the air has its name faded on the glass.

2026-10-03 (James: "When you move the dial between stations, I would like
it to be able to play static stations, coming in and out"): as the
pointer leaves the station that's playing, it fades and flutters out
under the static (`receptionChanged`, which the Radio page hands to the
player's volume). Rest the pointer on another station mid-drag for
DWELL_MS and it's tuned in there and then, coming up through the static
as the pointer closes on it, rather than only once you let go.
Interaction: tap a station name or anywhere on the lit band, drag the
pointer, drag the TUNING knob or scroll; let go and it settles on the
nearest station and tunes in. Between stations there's tuning static,
as loud as a station halfway between two (can be switched off; louder
and brighter since 2026-10-04). Everything is painted - no images shipped.
"""

from __future__ import annotations

import logging
import math
import random
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

from ...services import stations as station_svc


@dataclass(frozen=True)
class Band:
    key: str
    label: str
    unit: str
    marks: tuple
    #: printed along the bottom of the strip (shortwave meter bands)
    meters: tuple = ()


BANDS: tuple[Band, ...] = (
    Band("fm", "FM", "MHz", tuple(str(m) for m in station_svc.FM_MARKS)),
    Band("am", "AM", "kc", tuple(str(m) for m in station_svc.AM_MARKS)),
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


log = logging.getLogger(__name__)

#: rest on a station this long mid-drag and it starts playing through the static
DWELL_MS = 400
#: how much a half-tuned station's volume wavers (0 = steady)
FLUTTER = 0.55
#: reception/static refresh while the pointer moves
RECEPTION_TICK_MS = 60

#: what the magic eye shows once something's tuned in (PlayerController.signal)
SIGNAL_NONE, SIGNAL_CONNECTING, SIGNAL_GOOD, SIGNAL_BUFFERING, SIGNAL_LOST = (
    "none", "connecting", "good", "buffering", "lost")


def station_positions(n: int) -> list[float]:
    """Where n stations sit along the 0..1 dial, evenly spaced."""
    return station_svc.even_positions(n)


#: the generated static loop; the name carries a version so a set that
#: kept an older, quieter loop next to its library makes the new one
STATIC_FILE = "tuner_static_v2.wav"
STATIC_RATE = 44100
#: static loudness between stations, relative to the VOLUME knob
STATIC_GAIN = 0.85


def make_static_wav(path: Path, seconds: float = 3.0, rate: int = STATIC_RATE) -> Path:
    """A loop of AM-band tuning static, written once next to the library.

    2026-10-04 (James: "can we put some radio static in there?"): the first
    loop sat around -40 dBFS and was filtered down to a dull rumble, so
    under a station it was all but inaudible. This one is a brighter hiss
    (band-passed roughly 200 Hz to 5 kHz, the way an AM set sounds between
    stations) with a slow breathing swell, crackles and a faint heterodyne
    whistle, levelled to about -16 dBFS RMS - as loud as a station - and
    stereo 16-bit at 44.1 kHz, which every sound device takes. It loops
    without a seam: the filters run over two passes of the same noise and
    the second pass is kept, and the swell and whistle complete whole
    cycles within the loop.
    """
    import numpy as np

    rng = np.random.default_rng(7)
    n = max(1, int(seconds * rate))
    t = np.arange(n) / rate
    out = np.empty((n, 2))
    for ch in range(2):
        white = rng.uniform(-1.0, 1.0, n)
        x = np.concatenate([white, white])
        # one-pole high-pass (~200 Hz) then two one-pole low-passes (~5 kHz)
        a_hp = math.exp(-2 * math.pi * 200 / rate)
        a_lp = math.exp(-2 * math.pi * 5000 / rate)
        y = []
        hp = lp1 = lp2 = 0.0
        prev = 0.0
        b_lp = 1 - a_lp
        for v in x.tolist():
            hp = a_hp * (hp + v - prev)
            prev = v
            lp1 = lp1 * a_lp + hp * b_lp
            lp2 = lp2 * a_lp + lp1 * b_lp
            y.append(lp2)
        out[:, ch] = y[n:]
    # the two channels share most of the hiss, with a little width
    mid = out.mean(axis=1, keepdims=True)
    out = 0.8 * mid + 0.2 * out
    # a slow swell, whole cycles in the loop
    out *= (0.85 + 0.15 * np.sin(2 * math.pi * 2 * t / seconds))[:, None]
    # crackle: short decaying pops
    for start in rng.choice(n, size=int(seconds * 9), replace=False):
        length = min(n - start, int(rate * rng.uniform(0.002, 0.012)))
        pop = rng.uniform(-1, 1, length) * np.exp(-np.arange(length) / (length / 4 + 1))
        out[start:start + length] += (pop * rng.uniform(0.6, 1.4))[:, None] * out.std() * 4
    # a faint heterodyne whistle drifting around 1.8 kHz (phase wraps with the loop)
    wobble = 300 * np.sin(2 * math.pi * 1 * t / seconds)
    phase = 2 * math.pi * np.cumsum(1800 + wobble) / rate
    phase *= round(phase[-1] / (2 * math.pi)) * 2 * math.pi / phase[-1]
    rms = float(np.sqrt(np.mean(out ** 2))) or 1.0
    out = out / rms * 10 ** (-16 / 20)
    out += (0.025 * np.sin(phase))[:, None]
    out = np.clip(out, -0.98, 0.98)
    pcm = (out * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


class StaticPlayer:
    """Plays the static loop through a QMediaPlayer, the same FFmpeg path
    the stations use.

    2026-10-04 (James, after the louder loop: "I can't hear the static
    between stations"): QSoundEffect, used until then, can stay silent on
    Windows without reporting anything, so the hiss never played. The media
    player is proven on his PCs. It's paused rather than stopped when the
    dial comes to rest, so it starts again without a delay.
    """

    def __init__(self, path: Path, parent=None) -> None:
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        self._audio = QAudioOutput(parent)
        self._audio.setVolume(0.0)
        self._player = QMediaPlayer(parent)
        self._player.setAudioOutput(self._audio)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self._player.errorOccurred.connect(
            lambda _e, msg: log.warning("tuning static: %s", msg))
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._playing_state = QMediaPlayer.PlaybackState.PlayingState

    def setVolume(self, value: float) -> None:
        self._audio.setVolume(max(0.0, min(1.0, value)))

    def volume(self) -> float:
        return self._audio.volume()

    def isPlaying(self) -> bool:
        return self._player.playbackState() == self._playing_state

    def play(self) -> None:
        self._player.play()

    def stop(self) -> None:
        self._audio.setVolume(0.0)
        self._player.pause()


class RadioSet(QWidget):
    #: the pointer settled on station `index` of `band` - play it
    tuneRequested = Signal(str, int)
    bandRequested = Signal(str)
    volumeRequested = Signal(float)
    contextRequested = Signal(object)  # global QPoint
    #: how well the playing station comes in at the pointer, 0..1 (1 when
    #: the pointer sits on it) - the Radio page sets the player's volume by it
    receptionChanged = Signal(float)
    #: the listener moved the pointer or chose a band themselves (by mouse,
    #: wheel or keys) - the Radio page stops a station scan on it
    userTuned = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(760, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._band = "am"
        self._labels: dict[str, list[str]] = {k: [] for k in BAND_KEYS}
        self._positions: dict[str, list[float]] = {k: [] for k in BAND_KEYS}
        #: per band, which names are printed faded (stations off the air)
        self._faded: dict[str, set[int]] = {k: set() for k in BAND_KEYS}
        #: the tuned stream's health, for the magic eye (SIGNAL_*)
        self._signal = SIGNAL_NONE
        self._flicker = 0.0
        self._flicker_rng = random.Random()
        self._flicker_timer = QTimer(self)
        self._flicker_timer.setInterval(90)
        self._flicker_timer.timeout.connect(self._on_flicker)
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

        self._reception = 1.0
        self._flutter_phase = 0.0
        self._flutter_rng = random.Random()
        self._reception_timer = QTimer(self)
        self._reception_timer.setInterval(RECEPTION_TICK_MS)
        self._reception_timer.timeout.connect(self._update_reception)
        self._dwell_idx = -1
        self._dwell = QTimer(self)
        self._dwell.setSingleShot(True)
        self._dwell.setInterval(DWELL_MS)
        self._dwell.timeout.connect(self._on_dwell)

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

    def positions(self, band: Optional[str] = None) -> list[float]:
        return list(self._positions[band or self._band])

    def set_entries(self, band: str, labels: Sequence[str], faded: Sequence[int] = ()) -> None:
        self._labels[band] = list(labels)
        self._positions[band] = station_svc.dial_positions(band, labels)
        self._faded[band] = set(faded)
        if self._tuned[band] >= len(labels):
            self._tuned[band] = -1
        self.update()

    def set_tuned(self, band: str, index: int, animate: bool = True) -> None:
        """Show station `index` of `band` as tuned (switching to that band
        and moving the pointer) without asking for it to be played."""
        self._tuned[band] = index
        self._band = band
        if not animate:
            self._set_reception(1.0)
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

    @property
    def signal(self) -> str:
        return self._signal

    def set_signal(self, level: str) -> None:
        """How the tuned station is coming in (SIGNAL_*): the magic eye
        closes on a good stream, flickers while it connects or buffers and
        opens wide, dimmed, when the signal is lost."""
        if level == self._signal:
            return
        self._signal = level
        if level in (SIGNAL_CONNECTING, SIGNAL_BUFFERING):
            self._flicker_timer.start()
        else:
            self._flicker_timer.stop()
            self._flicker = 0.0
        self.update()

    def _on_flicker(self) -> None:
        # a restless shadow, the way a real eye jumps while the set hunts
        # for a fading station
        target = self._flicker_rng.uniform(0.25, 0.85)
        self._flicker = self._flicker * 0.45 + target * 0.55
        self.update()

    def set_static_enabled(self, on: bool) -> None:
        self._static_enabled = on
        if not on:
            self._silence()

    def set_static_path(self, path: Path) -> None:
        self._static_path = path

    def _dial_order(self, band: Optional[str] = None) -> list[int]:
        """The band's station indexes left to right as they sit on the
        glass (on FM/AM that's by frequency, not by list order)."""
        positions = self._positions[band or self._band]
        return sorted(range(len(positions)), key=lambda i: (positions[i], i))

    def step(self, delta: int) -> None:
        positions = self._positions[self._band]
        if not positions:
            return
        order = self._dial_order()
        current = order.index(self._nearest())
        target = order[max(0, min(len(order) - 1, current + delta))]
        self._move_to(positions[target], True, tune=True, debounce=650)

    def scan_to(self, index: int) -> None:
        """Sweep the pointer to station `index` of the lit band and tune it
        in when it gets there - the Scan button's move (2026-10-04). The
        sweep passes through the static like a hand on the knob would."""
        positions = self._positions[self._band]
        if 0 <= index < len(positions):
            self._move_to(positions[index], True, tune=True, debounce=150)

    def next_in_dial_order(self, after: int, skip: Sequence[int] = ()) -> int:
        """The station after index `after` along the lit band, left to right,
        wrapping round at the end and passing over `skip`; -1 if none."""
        order = [i for i in self._dial_order() if i not in skip]
        if not order:
            return -1
        full = self._dial_order()
        start = full.index(after) if after in full else -1
        for i in full[start + 1:] + full[:start + 1]:
            if i in order and i != after:
                return i
        return -1

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
        others = [abs(positions[i] - t) for k, t in enumerate(positions) if k != i]
        spacing = min(others) if others else 0.4
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
            self._set_reception(1.0)
            self.tuneRequested.emit(self._band, index)

    # -- reception (stations coming in and out) --------------------------------

    @property
    def reception(self) -> float:
        return self._reception

    def _moving(self) -> bool:
        return self._drag in ("dial", "tune") or self._anim.state() == QVariantAnimation.Running

    def _closeness_to(self, index: int) -> float:
        """0..1: how near the pointer is to station `index` of the lit band,
        1 on it, 0 halfway to its nearest neighbour or beyond."""
        positions = self._positions[self._band]
        if not 0 <= index < len(positions):
            return 0.0
        others = [abs(positions[index] - t) for k, t in enumerate(positions) if k != index]
        half = (min(others) if others else 0.4) / 2
        return max(0.0, 1.0 - abs(self._pos - positions[index]) / max(1e-6, half))

    def _flutter(self) -> float:
        """0..1, wandering - the fading of a station not quite tuned in."""
        self._flutter_phase += self._flutter_rng.uniform(0.25, 0.9)
        return 0.5 + 0.5 * math.sin(self._flutter_phase) * math.sin(self._flutter_phase * 0.37)

    def _set_reception(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        if abs(value - self._reception) > 1e-3 or value in (0.0, 1.0) and value != self._reception:
            self._reception = value
            self.receptionChanged.emit(value)

    def _update_reception(self) -> None:
        if not self._moving():
            self._reception_timer.stop()
            self._dwell.stop()
            return
        if not self._reception_timer.isActive():
            self._reception_timer.start()
        base = self._closeness_to(self._tuned.get(self._band, -1))
        wobble = self._flutter() if 0.0 < base < 1.0 else 0.0
        self._set_reception((base * (1.0 - FLUTTER * (1.0 - base) * wobble)) ** 1.5)
        self._update_static(wobble)
        # resting on another station mid-drag: bring it in
        if self._drag in ("dial", "tune"):
            near = self._nearest()
            if near >= 0 and near != self._tuned.get(self._band, -1) \
                    and self._closeness_to(near) > 0.55:
                if near != self._dwell_idx or not self._dwell.isActive():
                    if near != self._dwell_idx:
                        self._dwell_idx = near
                        self._dwell.start()
            else:
                self._dwell_idx = -1
                self._dwell.stop()

    def _on_dwell(self) -> None:
        index = self._dwell_idx
        if self._drag not in ("dial", "tune") or index < 0 or self._nearest() != index:
            return
        self._tuned[self._band] = index
        self.update()
        self.tuneRequested.emit(self._band, index)
        self._update_reception()

    def _pos_from_x(self, x: float) -> float:
        return max(0.0, min(1.0, (x - self._geom["scale_left"]) / self._geom["scale_width"]))

    # -- static -------------------------------------------------------------

    def _ensure_static(self):
        if self._static is not None or self._static_path is None:
            return self._static
        try:
            if not self._static_path.exists():
                make_static_wav(self._static_path)
            self._static = StaticPlayer(self._static_path, self)
        except Exception:  # pragma: no cover - no audio backend
            log.warning("tuning static unavailable", exc_info=True)
            self._static = None
        return self._static

    def _update_static(self, wobble: Optional[float] = None) -> None:
        moving = self._moving()
        if wobble is None:
            # called on every pointer movement - reception follows it too
            # (and calls back here with its flutter)
            if moving:
                self._update_reception()
            else:
                self._reception_timer.stop()
                self._dwell.stop()
                self._dwell_idx = -1
                if self._drag is None and self._pending_tune < 0:
                    # at rest (on a station, or after a band change): full
                    # strength; while a new station's about to tune, stay faded
                    self._set_reception(1.0)
            if moving:
                return
        if not self._static_enabled or not moving or not self._positions[self._band]:
            self._static_off.start(120)
            return
        effect = self._ensure_static()
        if effect is None:
            return
        # the hiss rises as the playing station fades, and wavers against it
        level = max((1.0 - self._closeness()) ** 0.8, 1.0 - self._reception)
        if wobble:
            level *= 0.75 + 0.5 * wobble
        effect.setVolume(min(1.0, level * STATIC_GAIN) * self._volume)
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
        if hit and hit != "knob_vol":
            self.userTuned.emit()
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
            self.userTuned.emit()
            self.step(1 if steps < 0 else -1)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: D102
        if event.key() in (Qt.Key_Right, Qt.Key_Left, Qt.Key_Up, Qt.Key_Down):
            self.userTuned.emit()
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
        xs = [left + width * t for t in positions]
        order = sorted(range(len(labels)), key=lambda i: (positions[i], i))
        longest = max(len(label) for label in labels)

        def room(rows: int) -> dict[int, float]:
            """How wide each name may be: up to the next name along in its
            own row (names alternate rows when there are two), so centred
            names never overlap; an end name may also run to the glass
            edge."""
            out = {}
            for k, i in enumerate(order):
                prev = order[k - rows] if k - rows >= 0 else None
                nxt = order[k + rows] if k + rows < len(order) else None
                d_prev = xs[i] - xs[prev] if prev is not None else None
                d_next = xs[nxt] - xs[i] if nxt is not None else None
                if d_prev is None and d_next is None:
                    out[i] = width * 0.96
                elif d_prev is None:
                    out[i] = min(d_next, (xs[i] - left) + d_next / 2)
                elif d_next is None:
                    out[i] = min(d_prev, (left + width - xs[i]) + d_prev / 2)
                else:
                    out[i] = min(d_prev, d_next)
                out[i] = max(8.0, out[i] * 0.96)
            return out

        avail_one = room(1)
        # the size the names would get spread evenly, so a crowded pair
        # doesn't shrink every name on the band
        typical = width * (0.84 / max(1, len(labels) - 1)) if len(labels) > 1 else width
        h = name_area.height()
        px = min(h * 0.62, max(8.0, typical * 0.95 / max(5, longest * 0.55)))
        tuned = self._tuned.get(band.key, -1)
        faded = self._faded.get(band.key, set())

        # Most names print in one row at a common size, shrinking a little
        # if they must. Only names too crowded for that (two stations a few
        # notches apart) are stacked, half height, one above the other -
        # rather than halving every name on the band.
        sizes: dict[int, float] = {}
        widths: dict[int, float] = {}
        stacked: list[int] = []
        for i in order:
            font = _font(px, bold=i == tuned)
            need = QFontMetricsF(font).horizontalAdvance(labels[i].upper())
            if need <= avail_one[i]:
                sizes[i] = px
            elif need * 0.8 <= avail_one[i] or h <= 26 or px * 0.8 <= 9:
                sizes[i] = max(9.0, math.floor(px * avail_one[i] / need))
            else:
                stacked.append(i)
                continue
            f = _font(sizes[i], bold=i == tuned)
            widths[i] = min(avail_one[i], QFontMetricsF(f).horizontalAdvance(labels[i].upper()))
        stack_room = room(2)
        row_of = {i: 0 for i in order}
        for k, i in enumerate(stacked):
            row_of[i] = 1 + (k % 2)
            # stay clear of the full-height names either side
            limit = stack_room[i]
            pos_k = order.index(i)
            for nb in (pos_k - 1, pos_k + 1):
                if 0 <= nb < len(order) and order[nb] not in stacked:
                    j = order[nb]
                    limit = min(limit, 2 * max(4.0, abs(xs[i] - xs[j]) - widths[j] / 2 - 4))
            stack_room[i] = limit
            sizes[i] = min(h / 2 * 0.62, px)

        for i, label in enumerate(labels):
            row = row_of[i]
            is_tuned = i == tuned
            avail = stack_room[i] if row else avail_one[i]
            font = _font(sizes[i], bold=is_tuned)
            fm = QFontMetricsF(font)
            full = fm.horizontalAdvance(label.upper())
            if full > avail and sizes[i] > 9:
                font = _font(max(9.0, math.floor(sizes[i] * avail / full)), bold=is_tuned)
                fm = QFontMetricsF(font)
            p.setFont(font)
            text = fm.elidedText(label.upper(), Qt.ElideRight, avail)
            tw = fm.horizontalAdvance(text)
            cy = name_area.top() + (h * 0.5 if row == 0 else h * (0.25 if row == 1 else 0.75))
            x = xs[i]
            tx = max(left + 4, min(left + width - 4 - tw, x - tw / 2))
            colour = QColor("#ffb19a") if (is_tuned and active) else QColor(name_ink)
            mark = POINTER if (is_tuned and active) else QColor(ink)
            if i in faded:
                # off the air: printed faintly, like a station that's gone dark
                colour.setAlpha(int(colour.alpha() * 0.38))
                mark.setAlpha(int(mark.alpha() * 0.38))
            p.setPen(colour)
            baseline = cy + (fm.ascent() - fm.descent()) / 2 - 1
            p.drawText(QPointF(tx, baseline), text)
            # marker bar under the name, centred on the station's spot
            bar = QRectF(x - 7, min(baseline + fm.descent() + 1.5, r.bottom() - 4), 14, 2.6)
            p.fillRect(bar, mark)

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

    def _eye_shadow(self) -> tuple[float, float]:
        """(shadow wedge in degrees, glow 0..1) for the magic eye. While the
        pointer moves it follows the tuning, as on a real set; once settled
        on a playing stream it shows the signal."""
        moving = self._drag in ("dial", "tune") or self._anim.state() == QVariantAnimation.Running
        lit = 0.55 + 0.45 * (1.0 if self._playing else 0.6)
        wedge = 8 + (1.0 - self._closeness()) * 92
        if moving or self._signal in (SIGNAL_NONE, SIGNAL_GOOD):
            return wedge, lit
        if self._signal == SIGNAL_LOST:
            return 110.0, 0.42
        # connecting / buffering: the shadow jumps about
        return max(wedge, 8 + self._flicker * 92), 0.55 + 0.35 * (1.0 - self._flicker)

    def _paint_eye(self, p: QPainter, center: QPointF, radius: float) -> None:
        """A 6E5 magic eye: green fan with a dark shadow wedge that closes as
        the pointer lands on a station - and, once a stream's on, stays
        closed while the signal's good (see _eye_shadow)."""
        wedge, lit = self._eye_shadow()
        p.setPen(QPen(QColor("#1a1a14"), 3))
        bezel = QRadialGradient(center, radius * 1.3)
        bezel.setColorAt(0, QColor("#8a7a54"))
        bezel.setColorAt(1, QColor("#3a3020"))
        p.setBrush(bezel)
        p.drawEllipse(center, radius * 1.18, radius * 1.18)
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
