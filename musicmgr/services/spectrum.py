"""Spectrum data for the PlayerBar's audio-reactive pulse visualizer.

James asked for a "pulse visualizer" while music plays (2026-09-05) - see
architecture.md's "Audio-reactive spectrum bars on the PlayerBar" for the
original scope decisions (real audio-reactive rather than purely
decorative). That first version was removed the same day (its small
5-bar widget didn't read as reacting at its original size/place) in favor
of a purely-decorative fake wave - but James later asked for the fake wave
itself to go, wanting the visualizer to "actually use the beat of the
actual sound" again (2026-09-06 follow-up), so this module's real analysis
is back in use, now feeding the bigger full-width bar row across the
PlayerBar's top edge (`ui/widgets/visualizer.py:PulseVisualizer`) instead
of the original small dedicated widget. Qt6 removed QAudioProbe, the old
way to tap a *live* playback stream, so this instead pre-analyzes the
whole file once per track and indexes into the result by playback
position - the same "compute once, look up by position" shape
services/lyrics.py already uses for .lrc timing, just computed here instead
of read from a sidecar file.

No new dependency: rather than pull in numpy/scipy for an FFT, each
bucket's band magnitudes are estimated with the Goertzel algorithm - a
single-frequency DFT-bin magnitude in O(n) time, no allocation, cheap
enough in pure Python to analyze a whole track (a few hundred thousand
samples) in well under a second - confirmed in the sandbox against a
synthetic 4-minute track (~0.7s for 5 bands).

`detect_beats()` (2026-09-06 follow-up) runs a simple, dependency-free
onset detector over the already-computed bass band - a beat is a bucket
where bass spikes well above its own recent rolling average - so the
visualizer can layer a sharper flash on top of its continuous band-
following motion, on top of the real analysis rather than as a second,
separate feature.

**2026-09-07 follow-up: MP3s showed no movement at all.** James: "the
pulse visualizer works better with FLAC files compared to MP3. It just
doesn't seem like there is any movement when playing MP3s." `compute_
spectrum` used to hand `QAudioDecoder` a fixed target `QAudioFormat`
(mono, Int16, `DECODE_SAMPLE_RATE`) so the bucketing math below never had
to branch on a file's own sample rate/channel layout/codec. That's a real
bug in Qt6's FFmpeg-backed `QAudioDecoder`, confirmed directly in this
sandbox against a battery of real MP3s at several bit rates and channel
counts: asking it to decode an MP3 *and* resample the result to a
different sample rate than the file's own native rate makes it hang
forever - no `bufferReady` after the first (near-empty) one, no `error`,
no `finished` - until `DECODE_TIMEOUT_MS` gives up and this returns `[]`.
FLAC hits the exact same resample request without any trouble, and an
MP3 already at the target rate decodes fine too - it's specifically
"MP3 decode + resample" that Qt's pipeline chokes on. Since the previous
code always requested a fixed 22050Hz target and the overwhelming
majority of real MP3s are natively 44100Hz, this fired on nearly every
MP3 a real library would have, every time, while FLACs (also usually
44100Hz, but immune to the bug) kept working - exactly the "FLAC works,
MP3 doesn't" split James was seeing.

The fix: stop requesting a specific `QAudioFormat` at all, and decode
each file at whatever sample rate/channel count/sample format it
natively is - sidestepping the buggy resampler entirely rather than
working around it. `_buffer_to_mono` reads a decoded buffer in its own
native sample format (`UInt8`/`Int16`/`Int32`/`Float` - whatever `Qt
AudioFormat.SampleFormat` reports) and, for a multi-channel file, just
keeps the first channel of each frame rather than averaging every
channel together - cheap slicing instead of a per-frame Python loop, and
a real stereo mix's bass/mid/treble balance barely differs channel to
channel anyway, so the visual result is indistinguishable for what's a
decorative feature to begin with. The bucket size and each `_goertzel_
mag` call now use whatever sample rate the first buffer actually reports
(`native_rate`, discovered once per track) instead of the fixed
`DECODE_SAMPLE_RATE` constant, which now only exists as the harmless
fallback if a buffer somehow reports no rate at all. One consequence
worth knowing: decoding at a file's own native rate instead of always
resampling down to 22050 means a very high native rate (96/192kHz,
uncommon but real for some FLACs) analyzes proportionally slower than
before - still comfortably bounded by `DECODE_TIMEOUT_MS`, just no longer
always the sub-second case the original benchmark below described for
every file regardless of its own rate.
"""

from __future__ import annotations

import logging
import math
import struct
from typing import List, Sequence

from PySide6.QtCore import QEventLoop, QThread, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

log = logging.getLogger(__name__)

#: five classic EQ-style band centers (Hz), bass through treble - all
#: comfortably under DECODE_SAMPLE_RATE's Nyquist frequency (11025 Hz)
BAND_FREQS: Sequence[int] = (60, 250, 1000, 3500, 9000)

#: one bar-height reading per this many milliseconds of audio
BUCKET_MS = 60

#: 2026-09-07 follow-up: no longer the sample rate every file gets
#: resampled to (see the module docstring's "MP3s showed no movement at
#: all" note - asking Qt's FFmpeg-backed QAudioDecoder to resample an MP3
#: to any other rate than its own hangs indefinitely). `compute_spectrum`
#: now decodes each file at its own native rate and only falls back to
#: this constant in the never-normally-happens case where a decoded buffer
#: reports no sample rate at all.
DECODE_SAMPLE_RATE = 22050

#: safety ceiling on one track's decode, in the (real, sandbox-confirmed)
#: case where QAudioDecoder's `error` signal fires but its connected
#: `loop.quit()` doesn't actually stop a QEventLoop running on a
#: background QThread the way it reliably does on the GUI thread - decoding
#: a whole track normally finishes in well under a second (see the module
#: docstring), so this only ever matters as a backstop against a decoder
#: that's gone stuck, not as a normal exit path.
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
    mono list in whatever the file's own native sample format is (2026-09-07
    follow-up - see the module docstring's "MP3s showed no movement at all"
    note for why this is no longer always Int16 mono). For a multi-channel
    buffer, keeps only the first channel of each frame rather than
    averaging every channel together: cheap slicing instead of a per-frame
    Python loop, and a real mix's bass/mid/treble balance barely differs
    channel to channel anyway - not worth the cost for what's a decorative
    feature. Values are left in their native scale (an Int16 sample isn't
    normalized to +-1 the way a Float one already is) since `compute_
    spectrum` only ever compares magnitudes *within* one track and
    normalizes by that track's own peak at the end - the absolute scale
    never matters. Returns [] for a buffer whose sample format isn't one of
    the four `QAudioFormat.SampleFormat` values Qt6 has (shouldn't happen
    in practice) or that reports zero samples."""
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

    Deliberately doesn't ask QAudioDecoder for a specific output
    QAudioFormat any more (2026-09-07 follow-up - see the module
    docstring): decodes at whatever sample rate/channel count/sample
    format the file itself natively is, discovered from the first decoded
    buffer, rather than requesting a fixed resample that hangs Qt6's
    FFmpeg-backed decoder on most real MP3s.

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
    the bars simply stay at their idle baseline until this finishes, which
    the sandbox timing above puts comfortably under a second even for a
    full-length track."""

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
