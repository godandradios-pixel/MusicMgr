"""Spectrum data for the PlayerBar's audio-reactive pulse visualizer.

Feeds the full-width bar row across the PlayerBar's top edge
(`ui/widgets/visualizer.py:PulseVisualizer`) - see architecture.md's
"Audio-reactive spectrum bars on the PlayerBar". Qt6 removed QAudioProbe,
the old way to tap a live playback stream, so for a track this
pre-analyzes the whole file once and indexes into the result by playback
position - the same "compute once, look up by position" shape
services/lyrics.py uses for .lrc timing. Live radio is analysed as it
plays instead (`LiveSpectrum`).

No numpy/scipy: each bucket's band magnitudes are estimated with the
Goertzel algorithm - a single-frequency DFT-bin magnitude in O(n) time, no
allocation, cheap enough in pure Python to analyze a 4-minute track in
under a second at 22050 Hz.

`detect_beats()` runs a simple onset detector over the bass band - a beat
is a bucket where bass spikes well above its own recent rolling average -
so the visualizer can layer a sharper flash on top of its band-following
motion.

Files are decoded at their own native sample rate/channel count/sample
format; no output `QAudioFormat` is requested. Qt6's FFmpeg-backed
`QAudioDecoder` hangs (no `bufferReady`, `error` or `finished`) when asked
to decode an MP3 *and* resample it to a rate other than its own, which
hit nearly every 44.1 kHz MP3. `_buffer_to_mono` reads each buffer in its
native format and keeps only the first channel; bucket size and Goertzel
math use the rate the first buffer reports. A very high native rate
(96/192 kHz) analyzes proportionally slower, still well within
`DECODE_TIMEOUT_MS`.
"""

from __future__ import annotations

import logging
import math
import struct
from typing import List, Sequence

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

log = logging.getLogger(__name__)

#: five classic EQ-style band centers (Hz), bass through treble - all
#: comfortably under DECODE_SAMPLE_RATE's Nyquist frequency (11025 Hz)
BAND_FREQS: Sequence[int] = (60, 250, 1000, 3500, 9000)

#: one bar-height reading per this many milliseconds of audio
BUCKET_MS = 60

#: fallback sample rate, only used if a decoded buffer reports no rate at
#: all - files are decoded at their native rate (see the module docstring
#: for why no resample is requested).
DECODE_SAMPLE_RATE = 22050

#: safety ceiling on one track's decode: on a background QThread, the
#: decoder's `error` signal can fire without its connected `loop.quit()`
#: actually stopping the QEventLoop. A normal decode finishes in well under
#: a second, so this is only a backstop against a stuck decoder.
DECODE_TIMEOUT_MS = 20_000


def _goertzel_mag(samples: Sequence[int], sample_rate: int, target_freq: float) -> float:
    """Magnitude of the frequency bin nearest target_freq, via the Goertzel
    algorithm - the single-frequency equivalent of a DFT bin. O(n) and
    allocation-free, so estimating a handful of band energies per bucket
    never needs a full FFT (or a new numpy/scipy dependency)."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + n * target_freq / sample_rate)
    w = 2 * math.pi * k / n
    cosine = math.cos(w)
    coeff = 2 * cosine
    q1 = q2 = 0.0
    for s in samples:
        q0 = coeff * q1 - q2 + s
        q2 = q1
        q1 = q0
    real = q1 - q2 * cosine
    imag = q2 * math.sin(w)
    return math.sqrt(real * real + imag * imag) / n


#: struct format code for each QAudioFormat sample format Qt6 actually uses
#: (see `_buffer_to_mono`) - there is no Int8/Int24 variant, just these four.
_SAMPLE_STRUCT_CODE = {
    QAudioFormat.SampleFormat.UInt8: "B",
    QAudioFormat.SampleFormat.Int16: "h",
    QAudioFormat.SampleFormat.Int32: "i",
    QAudioFormat.SampleFormat.Float: "f",
}


def _buffer_to_mono(buf) -> List[float]:
    """A decoded `QAudioBuffer`'s interleaved samples, reduced to a flat
    mono list in the file's native sample format (see the module
    docstring). For a multi-channel buffer, keeps only the first channel of
    each frame: cheap slicing instead of a per-frame Python loop, and a real
    mix's band balance barely differs channel to channel - not worth the
    cost for a decorative feature. Values stay in their native scale since
    `compute_spectrum` normalizes by the track's own peak at the end.
    Returns [] for an unknown sample format or a buffer with no samples."""
    fmt = buf.format()
    code = _SAMPLE_STRUCT_CODE.get(fmt.sampleFormat())
    count = buf.sampleCount()
    if not code or count <= 0:
        return []
    size = struct.calcsize(code)
    data = bytes(buf.constData())[: count * size]
    values = struct.unpack(f"<{count}{code}", data)
    channels = max(1, fmt.channelCount())
    return list(values) if channels == 1 else list(values[::channels])


def compute_spectrum(
    path: str, bucket_ms: int = BUCKET_MS, bands: Sequence[int] = BAND_FREQS
) -> List[List[float]]:
    """Decode `path` in full and return one list of [0..1] band magnitudes
    per `bucket_ms` of audio, covering the whole track up front - the same
    "hand back everything, indexed by time, right away" shape
    services/lyrics.py:parse_lrc uses for .lrc line timing.

    Blocking - always call this off the GUI thread (see SpectrumThread
    below). It drives its own QAudioDecoder with a locally-nested
    QEventLoop, which only works because the decoder is created (and so
    lives) on whatever thread calls this function.

    Doesn't ask QAudioDecoder for a specific output QAudioFormat: decodes
    at the file's native format, discovered from the first decoded buffer
    (see the module docstring for the MP3 resample hang this avoids).

    Returns [] for anything that fails to decode (a missing/corrupt file,
    an unsupported container) - the visualizer is purely decorative, never
    worth surfacing an error over, so a caller just gets nothing to show.
    """
    decoder = QAudioDecoder()
    decoder.setSource(QUrl.fromLocalFile(path))

    #: discovered from the first decoded buffer - see the module docstring.
    #: A plain dict (not two separate `nonlocal`s) so the nested callback
    #: below can update both in one place.
    state = {"rate": 0, "bucket_n": 0}
    buckets: List[List[float]] = []
    pending: List[float] = []
    failed = False

    def on_buffer_ready() -> None:
        buf = decoder.read()
        if state["rate"] == 0:
            state["rate"] = buf.format().sampleRate() or DECODE_SAMPLE_RATE
            state["bucket_n"] = max(1, int(state["rate"] * bucket_ms / 1000))
        mono = _buffer_to_mono(buf)
        if not mono:
            return
        pending.extend(mono)
        bucket_n = state["bucket_n"]
        while len(pending) >= bucket_n:
            chunk = pending[:bucket_n]
            del pending[:bucket_n]
            buckets.append([_goertzel_mag(chunk, state["rate"], f) for f in bands])

    def on_error(*_args) -> None:
        nonlocal failed
        failed = True
        loop.quit()

    def on_finished() -> None:
        loop.quit()

    def on_watchdog() -> None:
        nonlocal failed
        failed = True
        loop.quit()

    decoder.bufferReady.connect(on_buffer_ready)
    decoder.finished.connect(on_finished)
    # QAudioDecoder's error signal, unlike QMediaPlayer's errorOccurred (see
    # services/player.py), kept its Qt5 name - "error" - in Qt6.
    decoder.error.connect(on_error)

    loop = QEventLoop()
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(on_watchdog)
    watchdog.start(DECODE_TIMEOUT_MS)
    decoder.start()
    loop.exec()

    if failed:
        log.warning(
            "spectrum decode failed or timed out for %s: %s", path, decoder.errorString()
        )
        return []
    # a short trailing partial bucket is worth keeping - better one lower-
    # confidence reading at the very end of the track than silently
    # dropping the last fraction of a second's worth of bars
    bucket_n = state["bucket_n"] or max(1, int(DECODE_SAMPLE_RATE * bucket_ms / 1000))
    rate = state["rate"] or DECODE_SAMPLE_RATE
    if len(pending) >= bucket_n // 2:
        buckets.append([_goertzel_mag(pending, rate, f) for f in bands])
    if not buckets:
        return []
    peak = max(m for bucket in buckets for m in bucket)
    if peak <= 0:
        return [[0.0] * len(bands) for _ in buckets]
    return [[m / peak for m in bucket] for bucket in buckets]


#: how far back (in ms) `detect_beats` looks to judge whether the current
#: bucket's bass is a spike worth flagging, versus just normal loudness -
#: a couple of beats' worth at a typical tempo, so the threshold adapts to
#: how loud the track has been *recently* rather than one fixed number
#: that would misfire across a quiet verse vs. a loud chorus
BEAT_WINDOW_MS = 1200

#: a bucket's bass has to exceed its own recent rolling average by this
#: multiple to count as a beat - the classic "sound energy history" beat
#: detector shape (compare-to-local-average, not a fixed threshold)
BEAT_SENSITIVITY = 1.3

#: below this absolute bass level, nothing counts as a beat even if it's a
#: local spike - otherwise near-silence (an intro, a fade-out) flags tiny,
#: meaningless relative bumps as "beats"
BEAT_MIN_LEVEL = 0.12

#: minimum gap between two flagged beats - without this, one loud transient
#: spanning several buckets would fire repeatedly instead of once; 160ms
#: comfortably allows anything up to ~375bpm without collapsing two real,
#: distinct hits into one
BEAT_REFRACTORY_MS = 160


def detect_beats(
    buckets: Sequence[Sequence[float]],
    bucket_ms: int = BUCKET_MS,
    band_index: int = 0,
    window_ms: int = BEAT_WINDOW_MS,
    sensitivity: float = BEAT_SENSITIVITY,
    min_level: float = BEAT_MIN_LEVEL,
    refractory_ms: int = BEAT_REFRACTORY_MS,
) -> List[int]:
    """Bucket indices where `band_index` (bass, by default - `BAND_FREQS[0]`
    is 60Hz) spikes well above its own recent local average - a simple,
    dependency-free onset/beat detector run once over `compute_spectrum`'s
    already-decoded bucket list, the same "analyze once up front" shape as
    the band data itself.

    Deliberately not a fixed absolute threshold: a rolling average over the
    last `window_ms` of bass readings adapts the bar a beat has to clear to
    however loud the track has actually been lately, so a quiet verse's
    beats and a loud chorus's beats both register rather than one drowning
    out the other. `min_level` is a floor under that, so near-silence
    doesn't flag its own tiny relative wobbles.

    Edge-triggered, not level-triggered: a beat only fires on the bucket
    where the bass first crosses above the threshold, not on every
    consecutive bucket that stays above it - otherwise one sustained loud
    passage (a held bass note, a wash of cymbal) would register as a rapid
    string of "beats" instead of the single onset it actually is.
    `refractory_ms` is a backstop on top of that, for the rarer case of the
    level oscillating right at the threshold's edge for a tick or two.
    """
    window_n = max(1, window_ms // bucket_ms)
    refractory_n = max(1, refractory_ms // bucket_ms)
    beats: List[int] = []
    last_beat = -refractory_n
    above_threshold = False
    history: List[float] = []
    for i, bucket in enumerate(buckets):
        level = bucket[band_index] if band_index < len(bucket) else 0.0
        avg = sum(history) / len(history) if history else level
        is_hit = level >= min_level and level > avg * sensitivity
        if is_hit and not above_threshold and (i - last_beat) >= refractory_n:
            beats.append(i)
            last_beat = i
        above_threshold = is_hit
        history.append(level)
        if len(history) > window_n:
            history.pop(0)
    return beats


class SpectrumThread(QThread):
    """Computes one track's spectrum off the GUI thread. See PlayerBar's
    `_start_spectrum`/`_on_spectrum_ready` for how the result is consumed -
    the bars stay at their idle baseline until this finishes."""

    spectrumReady = Signal(str, list)  # path, list[list[float]]
    failed = Signal(str)  # path

    def __init__(self, path: str, bucket_ms: int = BUCKET_MS, parent=None) -> None:
        super().__init__(parent)
        self._path = path
        self._bucket_ms = bucket_ms

    @property
    def path(self) -> str:
        return self._path

    def run(self) -> None:  # pragma: no cover - exercised via the real Qt event loop
        try:
            buckets = compute_spectrum(self._path, self._bucket_ms)
        except Exception:  # noqa: BLE001 - decorative feature, never fatal
            log.exception("spectrum computation crashed for %s", self._path)
            self.failed.emit(self._path)
            return
        if buckets:
            self.spectrumReady.emit(self._path, buckets)
        else:
            self.failed.emit(self._path)


# --------------------------------------------------------------------------
# live: internet radio
# --------------------------------------------------------------------------

#: the rolling peak a live stream's bands are scaled against falls by this
#: much per bucket, so a loud passage doesn't flatten everything after it
LIVE_PEAK_DECAY = 0.996
#: never scale up quieter than this (raw Goertzel units, Int16 audio) - keeps
#: hiss between programs from being blown up into a full-height show
LIVE_PEAK_FLOOR = 40.0


class LiveSpectrum(QObject):
    """Spectrum for a live radio stream. A station can't be decoded up
    front the way a track is (`compute_spectrum`) - it never ends - so this
    analyses the audio as it plays: PlayerController hands it each decoded
    buffer from a QAudioBufferOutput tapped onto the deck playing the
    stream, and it emits one reading per `bucket_ms`, the same five
    bands as a track's buckets plus whether the bass just spiked
    (the incremental form of `detect_beats`).

    Unlike a track's buckets, which are scaled by the whole track's peak,
    a live stream is scaled by a slowly falling rolling peak. To keep
    it cheap on the GUI thread, every other sample is skipped at 32 kHz
    and up (the 9 kHz treble band still sits under the reduced rate's
    Nyquist limit)."""

    #: five band magnitudes 0..1, and whether this bucket is a beat
    bucketReady = Signal(list, bool)

    def __init__(self, bucket_ms: int = BUCKET_MS, bands: Sequence[int] = BAND_FREQS,
                 parent=None) -> None:
        super().__init__(parent)
        self.bucket_ms = bucket_ms
        self.bands = tuple(bands)
        self.reset()

    def reset(self) -> None:
        self._pending: List[float] = []
        self._rate = 0
        self._peak = LIVE_PEAK_FLOOR
        self._history: List[float] = []
        self._above = False
        self._since_beat = 10_000

    def feed_buffer(self, buf) -> None:
        """A QAudioBuffer from QAudioBufferOutput.audioBufferReceived."""
        try:
            rate = buf.format().sampleRate()
            mono = _buffer_to_mono(buf)
            if buf.format().sampleFormat() == QAudioFormat.SampleFormat.Float:
                mono = [v * 32767.0 for v in mono]
            elif buf.format().sampleFormat() == QAudioFormat.SampleFormat.Int32:
                mono = [v / 65536.0 for v in mono]
            elif buf.format().sampleFormat() == QAudioFormat.SampleFormat.UInt8:
                mono = [(v - 128) * 256.0 for v in mono]
        except Exception:  # pragma: no cover - decorative, never worth an error
            return
        self.feed(mono, rate)

    def feed(self, mono: Sequence[float], rate: int) -> None:
        """Mono samples at `rate`, scaled like Int16."""
        if not mono or rate <= 0:
            return
        step = 2 if rate >= 32000 else 1
        if step > 1:
            mono = mono[::step]
        rate //= step
        if rate != self._rate:
            self._pending = []
            self._rate = rate
        self._pending.extend(mono)
        n = max(1, int(rate * self.bucket_ms / 1000))
        while len(self._pending) >= n:
            chunk = self._pending[:n]
            del self._pending[:n]
            raw = [_goertzel_mag(chunk, rate, f) for f in self.bands]
            self._peak = max(LIVE_PEAK_FLOOR, self._peak * LIVE_PEAK_DECAY, max(raw))
            bands = [min(1.0, m / self._peak) for m in raw]
            self.bucketReady.emit(bands, self._beat(bands[0]))

    def _beat(self, level: float) -> bool:
        window_n = max(1, BEAT_WINDOW_MS // self.bucket_ms)
        refractory_n = max(1, BEAT_REFRACTORY_MS // self.bucket_ms)
        avg = sum(self._history) / len(self._history) if self._history else level
        hit = level >= BEAT_MIN_LEVEL and level > avg * BEAT_SENSITIVITY
        beat = hit and not self._above and self._since_beat >= refractory_n
        self._since_beat = 0 if beat else self._since_beat + 1
        self._above = hit
        self._history.append(level)
        if len(self._history) > window_n:
            self._history.pop(0)
        return beat
