"""Volume levelling (ReplayGain) - 2026-10-01.

James picked "volume levelling" and "gapless/crossfade" as the first two
items off the feature list ("proceed with 1 and 2 together"). His library
runs from 78 rpm transfers to modern chart masters, so track-to-track
loudness swings are large. This module supplies the numbers; the player
(`services/player.py`) applies them.

Where a track's gain comes from, in order:

1. **ReplayGain tags already in the file** - read by the scanner
   (`read_gain_tags`, called from `services/scanner.py:import_file`).
   Handles ID3 TXXX frames, FLAC/Ogg Vorbis comments, MP4 freeform atoms,
   APEv2, and Opus R128 tags. Stored with `rg_source = "tag"`.
2. **MusicMgr's own measurement** - `measure_file` decodes the file with
   Qt's own decoder (the same QAudioDecoder `services/spectrum.py` uses, so
   no ffmpeg binary is needed on either PC) and computes ITU-R BS.1770
   integrated loudness with numpy. Stored with `rg_source = "scan"`.

Decision (James, 2026-10-01): measured gains go **only into the library
database - the music files are never written to**. Writing tags would
change thousands of files' mtimes, and USB sync would then want to copy
every one of them.

Gains are relative to the ReplayGain 2.0 reference level, -18 LUFS. The
player can only turn a track *down* below the volume slider, never boost it
above full scale (QAudioOutput's volume tops out at 1.0), so levelled
playback can never clip - quiet tracks just get as much boost as the
volume slider's headroom allows.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

# Imported here, on the main thread at startup, rather than lazily inside a
# measuring thread: PySide6 installs an import hook that inspects every
# module imported after it, and numpy's few hundred modules going through
# it on a background thread - while the GUI thread competes for the GIL -
# took over ten seconds in testing, against a tenth of a second here.
try:
    import numpy as _numpy
except ImportError:  # pragma: no cover - from-source install without it
    _numpy = None

#: ReplayGain 2.0 reference loudness
REFERENCE_LUFS = -18.0
#: R128 tags (Opus) are relative to -23 LUFS; ReplayGain's reference is 5 dB louder
R128_TO_RG_OFFSET_DB = 5.0

SOURCE_TAG = "tag"
SOURCE_SCAN = "scan"
#: tried to measure and couldn't (corrupt/undecodable) - not retried by
#: "Analyze loudness" until the file itself changes
SOURCE_FAILED = "failed"

#: give up on a decode that has produced nothing new for this long
DECODE_IDLE_TIMEOUT_MS = 15_000

_GAIN_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------


@dataclass
class GainInfo:
    track_gain: Optional[float] = None
    track_peak: Optional[float] = None
    album_gain: Optional[float] = None
    album_peak: Optional[float] = None

    @property
    def has_track_gain(self) -> bool:
        return self.track_gain is not None


def _parse_db(text: str) -> Optional[float]:
    match = _GAIN_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(0))
    return value if -60.0 <= value <= 60.0 else None


def _parse_peak(text: str) -> Optional[float]:
    match = _GAIN_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(0))
    return value if 0.0 < value < 100.0 else None


def _text_of(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = getattr(value, "text", None)
    if text is not None:
        return _text_of(text)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _flatten_tags(tags) -> dict[str, str]:
    """Every tag as lower-case key -> first text value, with the TXXX: and
    iTunes freeform prefixes stripped, so one lookup works for every format."""
    out: dict[str, str] = {}
    try:
        items = list(tags.items())
    except Exception:  # pragma: no cover - exotic tag containers
        return out
    for key, value in items:
        k = str(key).lower()
        if k.startswith("txxx:"):
            k = k[5:]
        elif k.startswith("----:"):
            k = k.rsplit(":", 1)[-1]
        if k not in out:
            try:
                out[k] = _text_of(value)
            except Exception:
                continue
    return out


def read_gain_tags(raw) -> GainInfo:
    """ReplayGain values from an already-opened `mutagen.File(path)` (the raw,
    non-easy one). Never raises; missing or malformed values come back None."""
    info = GainInfo()
    tags = getattr(raw, "tags", None)
    if not tags:
        return info
    flat = _flatten_tags(tags)
    info.track_gain = _parse_db(flat.get("replaygain_track_gain", ""))
    info.track_peak = _parse_peak(flat.get("replaygain_track_peak", ""))
    info.album_gain = _parse_db(flat.get("replaygain_album_gain", ""))
    info.album_peak = _parse_peak(flat.get("replaygain_album_peak", ""))
    # Opus: R128_*_GAIN is a Q7.8 integer relative to -23 LUFS
    for key, attr in (("r128_track_gain", "track_gain"), ("r128_album_gain", "album_gain")):
        if getattr(info, attr) is None and key in flat:
            try:
                q78 = int(flat[key].strip())
                setattr(info, attr, q78 / 256.0 + R128_TO_RG_OFFSET_DB)
            except ValueError:
                pass
    return info


# --------------------------------------------------------------------------
# measuring (ITU-R BS.1770-4 integrated loudness)
# --------------------------------------------------------------------------


def _biquad_coeffs(kind: str, fs: float, fc: float, q: float, gain_db: float):
    """RBJ-cookbook biquads at any sample rate - the same parameterisation
    pyloudnorm uses for the K-weighting pre-filter (high shelf +4 dB at
    1.5 kHz, then a 38 Hz high pass)."""
    a_lin = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * fc / fs
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / (2 * q)
    if kind == "high_shelf":
        sq = 2 * math.sqrt(a_lin) * alpha
        b = [
            a_lin * ((a_lin + 1) + (a_lin - 1) * cos_w0 + sq),
            -2 * a_lin * ((a_lin - 1) + (a_lin + 1) * cos_w0),
            a_lin * ((a_lin + 1) + (a_lin - 1) * cos_w0 - sq),
        ]
        a = [
            (a_lin + 1) - (a_lin - 1) * cos_w0 + sq,
            2 * ((a_lin - 1) - (a_lin + 1) * cos_w0),
            (a_lin + 1) - (a_lin - 1) * cos_w0 - sq,
        ]
    else:  # high_pass
        b = [(1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2]
        a = [1 + alpha, -2 * cos_w0, 1 - alpha]
    return [x / a[0] for x in b], [x / a[0] for x in a]


def _k_weighting_ir(fs: float, length: int):
    """Impulse response of the two K-weighting biquads in series, long enough
    (50 ms - the tail beyond that is over 100 dB down) that truncating it
    is far below measurement precision. Lets the meter filter with fast FFT convolution in numpy
    instead of a per-sample IIR loop in Python."""
    import numpy as np

    x = [0.0] * length
    x[0] = 1.0
    for kind, fc, q, g in (
        ("high_shelf", 1500.0, 1 / math.sqrt(2), 4.0),
        ("high_pass", 38.0, 0.5, 0.0),
    ):
        b, a = _biquad_coeffs(kind, fs, fc, q, g)
        y = [0.0] * length
        x1 = x2 = y1 = y2 = 0.0
        for n in range(length):
            xn = x[n]
            yn = b[0] * xn + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
            y[n] = yn
            x2, x1 = x1, xn
            y2, y1 = y1, yn
        x = y
    return np.asarray(x, dtype=np.float64)


class LoudnessMeter:
    """Streaming BS.1770 meter: feed interleaved float frames in any chunk
    size, then call `integrated()`. Memory stays small however long the
    track is - only one energy value per 100 ms is kept."""

    FFT_SIZE = 1 << 16

    def __init__(self, sample_rate: int, channels: int) -> None:
        import numpy as np

        self._np = np
        self.rate = int(sample_rate)
        self.channels = max(1, int(channels))
        ir_len = 1 << max(10, math.ceil(math.log2(self.rate * 0.05)))
        self._ir = _k_weighting_ir(self.rate, ir_len)
        self._nfft = max(self.FFT_SIZE, 4 * ir_len)
        #: overlap-save: each FFT yields nfft - (ir_len - 1) new samples
        self.CHUNK_FRAMES = self._nfft - ir_len + 1
        self._H = np.fft.rfft(self._ir, self._nfft)
        self._tail = np.zeros((self.channels, ir_len - 1))
        self._pending: list = []
        self._pending_frames = 0
        self._sub = max(1, int(round(self.rate * 0.1)))
        self._carry = np.zeros((self.channels, 0))
        self._sub_energy: list[float] = []
        self.peak = 0.0
        self.frames = 0
        # BS.1770 channel weights: L, R, C = 1.0; surrounds 1.41 (LFE is
        # rare in a music library and not worth special-casing here)
        weights = [1.0] * self.channels
        if self.channels >= 5:
            weights[3:] = [1.41] * (self.channels - 3)
        self._weights = np.asarray(weights)[:, None]

    def feed(self, frames) -> None:
        """`frames`: numpy array shaped (n, channels), values in -1..1."""
        np = self._np
        if frames.size == 0:
            return
        self.peak = max(self.peak, float(np.max(np.abs(frames))))
        self.frames += frames.shape[0]
        self._pending.append(frames)
        self._pending_frames += frames.shape[0]
        while self._pending_frames >= self.CHUNK_FRAMES:
            block = np.concatenate(self._pending, axis=0)
            self._pending = [block[self.CHUNK_FRAMES:]]
            self._pending_frames = block.shape[0] - self.CHUNK_FRAMES
            self._process(block[: self.CHUNK_FRAMES].T)

    def _process(self, block) -> None:
        np = self._np
        n = block.shape[1]
        ext = np.concatenate([self._tail, block], axis=1)
        tail_len = self._tail.shape[1]
        filtered = np.fft.irfft(np.fft.rfft(ext, self._nfft, axis=1) * self._H, self._nfft, axis=1)
        filtered = filtered[:, tail_len : tail_len + n]
        self._tail = ext[:, -tail_len:] if tail_len else self._tail
        sq = np.concatenate([self._carry, filtered * filtered], axis=1)
        k = sq.shape[1] // self._sub
        if k:
            ms = sq[:, : k * self._sub].reshape(self.channels, k, self._sub).mean(axis=2)
            self._sub_energy.extend((ms * self._weights).sum(axis=0).tolist())
        self._carry = sq[:, k * self._sub :]

    def integrated(self) -> Optional[float]:
        """Gated integrated loudness in LUFS, or None for silence/no audio."""
        np = self._np
        if self._pending_frames:
            block = np.concatenate(self._pending, axis=0)
            self._pending, self._pending_frames = [], 0
            self._process(block.T)
        energies = np.asarray(self._sub_energy)
        if energies.size == 0:
            if self._carry.shape[1] == 0:
                return None
            z = float((self._carry.mean(axis=1) * self._weights[:, 0]).sum())
            return -0.691 + 10 * math.log10(z) if z > 0 else None
        if energies.size < 4:
            blocks = np.asarray([energies.mean()])
        else:
            # 400 ms blocks, 75 % overlap = 4 consecutive 100 ms sub-blocks
            csum = np.concatenate([[0.0], np.cumsum(energies)])
            blocks = (csum[4:] - csum[:-4]) / 4.0
        with np.errstate(divide="ignore"):
            loud = -0.691 + 10 * np.log10(blocks)
        gated = blocks[loud > -70.0]
        if gated.size == 0:
            return None
        relative = -0.691 + 10 * math.log10(float(gated.mean())) - 10.0
        with np.errstate(divide="ignore"):
            final = gated[(-0.691 + 10 * np.log10(gated)) > relative]
        if final.size == 0:
            return None
        return -0.691 + 10 * math.log10(float(final.mean()))


class MeasureInterrupted(Exception):
    """The calling QThread was asked to stop mid-decode - nothing is stored
    for the file, so it's simply tried again another time."""


@dataclass
class Measurement:
    lufs: float
    peak: float
    duration_s: float

    @property
    def gain(self) -> float:
        return round(REFERENCE_LUFS - self.lufs, 2)


def _buffer_to_frames(buf, np):
    from PySide6.QtMultimedia import QAudioFormat

    fmt = buf.format()
    sf = fmt.sampleFormat()
    channels = max(1, fmt.channelCount())
    raw = bytes(buf.constData())
    if sf == QAudioFormat.SampleFormat.Float:
        data = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    elif sf == QAudioFormat.SampleFormat.Int16:
        data = np.frombuffer(raw, dtype="<i2") / 32768.0
    elif sf == QAudioFormat.SampleFormat.Int32:
        data = np.frombuffer(raw, dtype="<i4") / 2147483648.0
    elif sf == QAudioFormat.SampleFormat.UInt8:
        data = (np.frombuffer(raw, dtype="u1").astype(np.float64) - 128.0) / 128.0
    else:
        return None, fmt.sampleRate(), channels
    usable = (data.size // channels) * channels
    return data[:usable].reshape(-1, channels), fmt.sampleRate(), channels


def measure_file(path: str, should_stop: Optional[Callable[[], bool]] = None) -> Optional[Measurement]:
    """Decode `path` and measure it. Blocking - call off the GUI thread
    (`LoudnessThread`, the Settings "Analyze loudness" job). Like
    `services/spectrum.py:compute_spectrum` it decodes at the file's own
    native format (asking Qt's decoder to resample MP3s hangs it) and runs a
    nested event loop on the calling thread. Returns None if the file can't
    be decoded or is silent."""
    import numpy as np
    from PySide6.QtCore import QEventLoop, QThread, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder

    if should_stop is None:
        should_stop = QThread.currentThread().isInterruptionRequested
    decoder = QAudioDecoder()
    decoder.setSource(QUrl.fromLocalFile(path))
    state: dict = {"meter": None, "failed": False, "stopped": False}
    loop = QEventLoop()
    watchdog = QTimer()
    watchdog.setSingleShot(True)

    def on_buffer_ready() -> None:
        if should_stop():
            state["stopped"] = True
            loop.quit()
            return
        buf = decoder.read()
        if not buf.isValid():
            return
        frames, rate, channels = _buffer_to_frames(buf, np)
        if frames is None or not rate:
            return
        meter = state["meter"]
        if meter is None:
            meter = state["meter"] = LoudnessMeter(rate, channels)
        if frames.shape[1] == meter.channels:
            meter.feed(frames)
        watchdog.start(DECODE_IDLE_TIMEOUT_MS)

    def on_fail(*_args) -> None:
        state["failed"] = True
        loop.quit()

    decoder.bufferReady.connect(on_buffer_ready)
    decoder.finished.connect(loop.quit)
    decoder.error.connect(on_fail)
    watchdog.timeout.connect(on_fail)
    watchdog.start(DECODE_IDLE_TIMEOUT_MS)
    decoder.start()
    loop.exec()
    watchdog.stop()
    decoder.stop()

    if state["stopped"]:
        raise MeasureInterrupted(path)
    meter: Optional[LoudnessMeter] = state["meter"]
    if state["failed"] or meter is None:
        log.info("loudness: could not decode %s: %s", path, decoder.errorString())
        return None
    lufs = meter.integrated()
    if lufs is None:
        return None
    return Measurement(lufs=lufs, peak=meter.peak, duration_s=meter.frames / meter.rate)


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------


def apply_tags(media_file, info: GainInfo) -> None:
    """Scanner hook: tag values win over a previous measurement; a file
    whose tags were removed loses its old tag-sourced values (a measured
    value is kept - the audio didn't necessarily change)."""
    if info.has_track_gain:
        media_file.rg_track_gain = info.track_gain
        media_file.rg_track_peak = info.track_peak
        media_file.rg_album_gain = info.album_gain
        media_file.rg_album_peak = info.album_peak
        media_file.rg_source = SOURCE_TAG
    elif getattr(media_file, "rg_source", None) == SOURCE_TAG:
        media_file.rg_track_gain = None
        media_file.rg_track_peak = None
        media_file.rg_album_gain = None
        media_file.rg_album_peak = None
        media_file.rg_source = None


def store_measurement(media_file, m: Optional[Measurement]) -> None:
    if m is None:
        media_file.rg_source = SOURCE_FAILED
        return
    media_file.rg_track_gain = m.gain
    media_file.rg_track_peak = round(m.peak, 6)
    media_file.rg_source = SOURCE_SCAN


def album_gain_from_tracks(rows: Sequence[tuple[float, float]]) -> Optional[float]:
    """Album gain from (track_gain, duration_s) pairs: the duration-weighted
    power average of the tracks' loudness. The standard approximation of
    measuring the whole album as one stream - differs from it only through
    gating at track boundaries, well under 0.5 dB on real albums."""
    total = sum(d for _, d in rows if d and d > 0)
    if not rows or total <= 0:
        return None
    power = sum(d * 10 ** ((REFERENCE_LUFS - g) / 10.0) for g, d in rows if d and d > 0)
    return round(REFERENCE_LUFS - 10 * math.log10(power / total), 2)


def fill_album_gains(session, release_ids: Optional[Iterable[int]] = None) -> int:
    """Compute album gain for releases whose files all have a track gain.
    Tag-sourced album gains are left alone. Returns releases updated."""
    from sqlalchemy import select

    from ..db.models import MediaFile, Track

    stmt = (
        select(Track.release_id, MediaFile)
        .join(MediaFile, MediaFile.track_id == Track.id)
        .where(MediaFile.is_missing.is_(False), Track.release_id.is_not(None))
    )
    if release_ids is not None:
        ids = list(release_ids)
        if not ids:
            return 0
        stmt = stmt.where(Track.release_id.in_(ids))
    by_release: dict[int, list] = {}
    for release_id, mf in session.execute(stmt):
        by_release.setdefault(release_id, []).append(mf)
    updated = 0
    for files in by_release.values():
        if any(f.rg_track_gain is None for f in files):
            continue
        if all(f.rg_source == SOURCE_TAG and f.rg_album_gain is not None for f in files):
            continue
        rows = [(f.rg_track_gain, (f.duration_ms or 0) / 1000.0) for f in files]
        gain = album_gain_from_tracks(rows)
        if gain is None:
            continue
        peaks = [f.rg_track_peak for f in files if f.rg_track_peak]
        peak = max(peaks) if peaks else None
        for f in files:
            if f.rg_source == SOURCE_TAG and f.rg_album_gain is not None:
                continue
            f.rg_album_gain = gain
            f.rg_album_peak = peak
        updated += 1
    session.flush()
    return updated


def files_needing_analysis(session) -> list[tuple[int, str]]:
    """(media_file id, path) for every present file with no gain yet."""
    from sqlalchemy import or_, select

    from ..db.models import MediaFile

    stmt = (
        select(MediaFile.id, MediaFile.path)
        .where(
            MediaFile.is_missing.is_(False),
            MediaFile.rg_track_gain.is_(None),
            or_(MediaFile.rg_source.is_(None), MediaFile.rg_source != SOURCE_FAILED),
        )
        .order_by(MediaFile.path)
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]


def measuring_available() -> bool:
    """Measuring needs numpy (added to requirements.txt 2026-10-01). A
    from-source install that hasn't re-run `pip install -r requirements.txt`
    can still read ReplayGain tags, just not measure."""
    return _numpy is not None


def read_file_gain_tags(path: str) -> GainInfo:
    try:
        import mutagen

        raw = mutagen.File(path)
    except Exception:
        return GainInfo()
    return read_gain_tags(raw) if raw is not None else GainInfo()


@dataclass
class AnalyzeResult:
    from_tags: int = 0
    measured: int = 0
    failed: int = 0
    #: no tags, and measuring isn't available (numpy missing)
    skipped: int = 0
    albums: int = 0
    cancelled: bool = False

    def summary(self) -> str:
        parts = []
        if self.from_tags:
            parts.append(f"{self.from_tags} read from ReplayGain tags")
        if self.measured:
            parts.append(f"{self.measured} measured")
        if self.failed:
            parts.append(f"{self.failed} couldn't be read")
        if self.skipped:
            parts.append(
                f"{self.skipped} need measuring - run pip install -r requirements.txt first"
            )
        text = ", ".join(parts) if parts else "Every track already has a level"
        if self.cancelled:
            text += " (stopped early)"
        return text


def analyze_paths_one(mf_id: int, path: str, measure=None) -> Optional[int]:
    """Tags first, else measure; store on media file `mf_id`. Returns the
    file's release id (for album gain) or None. Used both by the library
    job below and by the player for a track it's about to play."""
    import os

    from ..db.models import MediaFile, Track
    from ..db.session import session_scope

    if measure is None:
        measure = measure_file if measuring_available() else None
    exists = os.path.exists(path)
    info = read_file_gain_tags(path) if exists else GainInfo()
    m = None
    if not info.has_track_gain and exists and measure is not None:
        m = measure(path)
    with session_scope() as session:
        mf = session.get(MediaFile, mf_id)
        if mf is None:
            return None
        if info.has_track_gain:
            apply_tags(mf, info)
        elif measure is not None or not exists:
            store_measurement(mf, m)
        track = session.get(Track, mf.track_id)
        return track.release_id if track is not None else None


def analyze_library(
    progress: Optional[Callable[[int, int, str], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
    measure: Optional[Callable[[str], Optional[Measurement]]] = None,
) -> AnalyzeResult:
    """Give every file still missing a level one: its ReplayGain tags if it
    has them (a library scanned before 2026-10-01 never read them), else a
    measurement. Then fill album gains. Commits per file, so a cancelled run
    keeps what it finished."""
    import os

    from ..db.models import MediaFile
    from ..db.session import session_scope

    result = AnalyzeResult()
    with session_scope() as session:
        todo = files_needing_analysis(session)
    touched: set[int] = set()
    total = len(todo)
    for idx, (mf_id, path) in enumerate(todo):
        if cancelled():
            result.cancelled = True
            break
        if progress:
            progress(idx, total, os.path.basename(path))
        try:
            release_id = analyze_paths_one(mf_id, path, measure)
        except MeasureInterrupted:
            result.cancelled = True
            break
        if release_id is not None:
            touched.add(release_id)
        with session_scope() as session:
            mf = session.get(MediaFile, mf_id)
            source = mf.rg_source if mf is not None else None
        if source == SOURCE_TAG:
            result.from_tags += 1
        elif source == SOURCE_SCAN:
            result.measured += 1
        elif source == SOURCE_FAILED:
            result.failed += 1
        else:
            result.skipped += 1
    with session_scope() as session:
        result.albums = fill_album_gains(session, touched)
    if progress:
        progress(total, total, "")
    return result


def gain_db_for(
    track_gain: Optional[float],
    album_gain: Optional[float],
    use_album: bool,
    preamp_db: float,
    fallback_db: float,
) -> float:
    """The dB adjustment the player applies to one track. `fallback_db` is
    the gain assumed for a track not measured yet."""
    gain = album_gain if (use_album and album_gain is not None) else track_gain
    if gain is None:
        gain = fallback_db
    return gain + preamp_db


def db_to_factor(db: float) -> float:
    return 10 ** (db / 20.0)
