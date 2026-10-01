"""Volume levelling (2026-10-01): services/replaygain.py and
services/playback_prefs.py - tag parsing, the BS.1770 meter, album gains,
the library "Volume levels" job, and the scanner picking tags up.

The meter is checked against BS.1770's own calibration point (a 997 Hz sine
at full scale in one channel reads -3.01 LUFS) rather than another tool, so
these tests need nothing beyond numpy.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db.models import MediaFile, Setting, Track
from musicmgr.services import library as lib
from musicmgr.services import playback_prefs, replaygain, scanner
from musicmgr.services.matching import normalize

np = pytest.importorskip("numpy")


def make_tone_wav(path: Path, seconds: float = 1.0, amp: float = 0.5, freq: float = 440.0,
                  rate: int = 22050, channels: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for n in range(int(rate * seconds)):
        v = int(32767 * amp * math.sin(2 * math.pi * freq * n / rate))
        frames += struct.pack("<h", v) * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def add_id3_gain(path: Path, track_gain: str, album_gain: str = "", peak: str = "") -> None:
    from mutagen.id3 import TXXX
    from mutagen.wave import WAVE

    audio = WAVE(str(path))
    audio.add_tags()
    audio.tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[track_gain]))
    if album_gain:
        audio.tags.add(TXXX(encoding=3, desc="replaygain_album_gain", text=[album_gain]))
    if peak:
        audio.tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=[peak]))
    audio.save()


class FakeTagged:
    """Stands in for a mutagen file whose `.tags.items()` gives (key, value)."""

    def __init__(self, pairs):
        self.tags = self
        self._pairs = pairs

    def items(self):
        return list(self._pairs)

    def __bool__(self):
        return True


# --------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------


class TestReadGainTags:
    def test_vorbis_comments(self):
        info = replaygain.read_gain_tags(FakeTagged([
            ("REPLAYGAIN_TRACK_GAIN", ["-7.21 dB"]),
            ("REPLAYGAIN_TRACK_PEAK", ["0.988"]),
            ("REPLAYGAIN_ALBUM_GAIN", ["-6.50 dB"]),
        ]))
        assert info.track_gain == pytest.approx(-7.21)
        assert info.track_peak == pytest.approx(0.988)
        assert info.album_gain == pytest.approx(-6.5)

    def test_mp4_freeform_bytes(self):
        info = replaygain.read_gain_tags(FakeTagged([
            ("----:com.apple.iTunes:replaygain_track_gain", [b"+2.40 dB"]),
        ]))
        assert info.track_gain == pytest.approx(2.4)

    def test_opus_r128_is_shifted_to_replaygain_reference(self):
        # -1280 / 256 = -5 dB relative to -23 LUFS = 0 dB relative to -18
        info = replaygain.read_gain_tags(FakeTagged([("R128_TRACK_GAIN", ["-1280"])]))
        assert info.track_gain == pytest.approx(0.0)

    def test_garbage_and_missing_values_are_none(self):
        info = replaygain.read_gain_tags(FakeTagged([("REPLAYGAIN_TRACK_GAIN", ["loud"])]))
        assert info.track_gain is None
        assert not replaygain.read_gain_tags(object()).has_track_gain

    def test_id3_txxx_on_a_real_file(self, tmp_path):
        path = tmp_path / "t.wav"
        make_tone_wav(path, seconds=0.2)
        add_id3_gain(path, "-8.00 dB", album_gain="-7.00 dB", peak="0.5")
        info = replaygain.read_file_gain_tags(str(path))
        assert (info.track_gain, info.album_gain, info.track_peak) == (-8.0, -7.0, 0.5)


# --------------------------------------------------------------------------
# meter
# --------------------------------------------------------------------------


def sine(rate, seconds, amp, freq=997.0):
    t = np.arange(int(rate * seconds)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


class TestLoudnessMeter:
    def test_full_scale_997hz_mono_reads_minus_3_lufs(self):
        meter = replaygain.LoudnessMeter(48000, 1)
        meter.feed(sine(48000, 5, 1.0)[:, None])
        assert meter.integrated() == pytest.approx(-3.01, abs=0.1)

    def test_stereo_sums_channel_power(self):
        meter = replaygain.LoudnessMeter(44100, 2)
        x = sine(44100, 5, 0.1)
        meter.feed(np.stack([x, x], axis=1))
        # each channel -23.01, two channels add 3 dB
        assert meter.integrated() == pytest.approx(-20.0, abs=0.1)

    def test_chunk_size_does_not_change_the_answer(self):
        x = sine(44100, 4, 0.3)[:, None]
        whole = replaygain.LoudnessMeter(44100, 1)
        whole.feed(x)
        pieces = replaygain.LoudnessMeter(44100, 1)
        for i in range(0, len(x), 1000):
            pieces.feed(x[i : i + 1000])
        assert pieces.integrated() == pytest.approx(whole.integrated(), abs=0.01)

    def test_silence_is_gated_out(self):
        tone = sine(44100, 3, 0.1)
        meter = replaygain.LoudnessMeter(44100, 1)
        meter.feed(np.concatenate([tone, np.zeros(44100 * 3)])[:, None])
        # ungated it would read -26 (half the time silent); gating drops the
        # silence, and only the few blocks straddling the edge pull it down
        assert meter.integrated() == pytest.approx(-23.0, abs=0.4)
        silent = replaygain.LoudnessMeter(44100, 1)
        silent.feed(np.zeros((44100, 1)))
        assert silent.integrated() is None

    def test_peak_and_length(self):
        meter = replaygain.LoudnessMeter(8000, 1)
        meter.feed(sine(8000, 2, 0.25, 440)[:, None])
        assert meter.peak == pytest.approx(0.25, abs=0.01)
        assert meter.frames == 16000

    def test_measure_file_decodes_a_real_wav(self, qapp, tmp_path):
        path = tmp_path / "tone.wav"
        make_tone_wav(path, seconds=2.0, amp=0.5, freq=997, rate=22050, channels=1)
        m = replaygain.measure_file(str(path))
        assert m is not None
        # -3.01 LUFS at full scale, -6.02 dB for half amplitude
        assert m.lufs == pytest.approx(-9.03, abs=0.3)
        assert m.gain == pytest.approx(-18.0 - m.lufs, abs=0.01)
        assert m.duration_s == pytest.approx(2.0, abs=0.05)


# --------------------------------------------------------------------------
# album gain, database, library job
# --------------------------------------------------------------------------


def test_album_gain_is_duration_weighted_power_average():
    assert replaygain.album_gain_from_tracks([(-6.0, 200), (-6.0, 100)]) == -6.0
    # a long loud track dominates a short quiet one
    gain = replaygain.album_gain_from_tracks([(-10.0, 300), (0.0, 10)])
    assert -10.0 < gain < -9.0
    assert replaygain.album_gain_from_tracks([]) is None


def test_gain_db_for_modes():
    f = replaygain.gain_db_for
    assert f(-8.0, -6.0, False, 0.0, -6.0) == -8.0
    assert f(-8.0, -6.0, True, 0.0, -6.0) == -6.0
    assert f(-8.0, None, True, 0.0, -6.0) == -8.0  # no album gain yet
    assert f(None, None, False, 2.0, -6.0) == -4.0  # fallback + preamp


def make_album(session, tmp_path, n=2, name="LP"):
    release = lib.get_or_create_release(session, name, "Artist")
    session.flush()
    files = []
    for i in range(n):
        path = tmp_path / name / f"{i}.wav"
        make_tone_wav(path, seconds=0.3)
        title = f"{name} {i}"
        track = Track(title=title, title_key=normalize(title), release_id=release.id,
                      duration_ms=300)
        session.add(track)
        session.flush()
        mf = MediaFile(track_id=track.id, path=str(path), duration_ms=60_000 * (i + 1))
        session.add(mf)
        session.flush()
        files.append(mf)
    return release, files


class TestLibraryJob:
    def test_tags_first_then_measure_then_album_gain(self, db, tmp_path):
        with db.session_scope() as s:
            release, files = make_album(s, tmp_path)
            tagged_path = files[0].path
            ids = [f.id for f in files]
        add_id3_gain(Path(tagged_path), "-9.00 dB")
        measured = []

        def fake_measure(path):
            measured.append(path)
            return replaygain.Measurement(lufs=-12.0, peak=0.7, duration_s=120)

        result = replaygain.analyze_library(measure=fake_measure)
        assert (result.from_tags, result.measured, result.failed, result.albums) == (1, 1, 0, 1)
        assert measured == [files[1].path]
        with db.session_scope() as s:
            a, b = (s.get(MediaFile, i) for i in ids)
            assert (a.rg_source, a.rg_track_gain) == ("tag", -9.0)
            assert (b.rg_source, b.rg_track_gain, b.rg_track_peak) == ("scan", -6.0, 0.7)
            # both get the same album gain, between the two track gains
            assert a.rg_album_gain == b.rg_album_gain
            assert -9.0 < a.rg_album_gain < -6.0

    def test_failed_files_are_not_retried_and_cancel_stops(self, db, tmp_path):
        with db.session_scope() as s:
            make_album(s, tmp_path, n=3)
        result = replaygain.analyze_library(measure=lambda p: None)
        assert result.failed == 3
        again = replaygain.analyze_library(measure=lambda p: pytest.fail("retried"))
        assert again.failed == again.measured == 0

        with db.session_scope() as s:
            make_album(s, tmp_path, n=3, name="EP")
        calls = []
        stopped = replaygain.analyze_library(
            measure=lambda p: calls.append(p) or replaygain.Measurement(-18, 0.5, 60),
            cancelled=lambda: len(calls) >= 1,
        )
        assert stopped.cancelled and stopped.measured == 1

    def test_tag_album_gain_is_kept(self, db, tmp_path):
        with db.session_scope() as s:
            _, files = make_album(s, tmp_path)
            for mf in files:
                replaygain.apply_tags(mf, replaygain.GainInfo(track_gain=-5.0, album_gain=-4.0))
            replaygain.fill_album_gains(s)
            assert [mf.rg_album_gain for mf in files] == [-4.0, -4.0]


class TestScannerPicksUpTags:
    def test_import_stores_tag_gain_and_clears_it_when_tags_go(self, db, tmp_path):
        path = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_tone_wav(path, seconds=0.2)
        add_id3_gain(path, "-4.50 dB", album_gain="-4.00 dB")
        with db.session_scope() as s:
            scanner.import_file(s, path, scanner.ScanResult())
        with db.session_scope() as s:
            mf = s.scalar(select(MediaFile))
            assert (mf.rg_track_gain, mf.rg_album_gain, mf.rg_source) == (-4.5, -4.0, "tag")

        from mutagen.wave import WAVE

        audio = WAVE(str(path))
        audio.delete()
        with db.session_scope() as s:
            scanner.import_file(s, path, scanner.ScanResult(), force=True)
        with db.session_scope() as s:
            mf = s.scalar(select(MediaFile))
            assert mf.rg_track_gain is None and mf.rg_source is None


# --------------------------------------------------------------------------
# prefs
# --------------------------------------------------------------------------


class TestPlaybackPrefs:
    def test_defaults_round_trip_and_clamping(self, session):
        prefs = playback_prefs.load(session)
        assert prefs == playback_prefs.PlaybackPrefs()
        playback_prefs.save(session, playback_prefs.PlaybackPrefs(
            crossfade_s=5, crossfade_same_album=True, levelling="track", preamp_db=-3
        ))
        loaded = playback_prefs.load(session)
        assert (loaded.crossfade_s, loaded.crossfade_same_album, loaded.levelling,
                loaded.preamp_db) == (5, True, "track", -3.0)
        session.get(Setting, playback_prefs.KEY_CROSSFADE).value = "99"
        session.get(Setting, playback_prefs.KEY_LEVELLING).value = "loudest"
        loaded = playback_prefs.load(session)
        assert loaded.crossfade_s == playback_prefs.MAX_CROSSFADE_S
        assert loaded.levelling == playback_prefs.LEVELLING_AUTO
