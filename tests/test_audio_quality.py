"""Tests for services/audio_quality.py and the player bar's quality pill
(2026-10-13 - James: "For other songs, can we have a Mono Stereo or bit
rate or some other indicator that can be used to describe the quality of
the file")."""

from __future__ import annotations

import wave
from types import SimpleNamespace

import pytest

from musicmgr.services import audio_quality
from musicmgr.services.player import QueueItem


def info(module: str, **attrs):
    cls = type("Info", (), {"__module__": f"mutagen.{module}"})
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


@pytest.mark.parametrize("i, label, lossless", [
    (info("flac", bits_per_sample=16, sample_rate=44100, channels=2, bitrate=900_000),
     "FLAC · 16/44.1 · Stereo", True),
    (info("flac", bits_per_sample=24, sample_rate=96000, channels=2, bitrate=3_000_000),
     "FLAC · 24/96 · Stereo", True),
    (info("mp3", sample_rate=44100, channels=2, bitrate=320_000),
     "MP3 · 320 kbps · Stereo", False),
    (info("mp3", sample_rate=22050, channels=1, bitrate=64_000),
     "MP3 · 64 kbps · Mono", False),
    (info("mp3", sample_rate=44100, channels=2, bitrate=245_123, bitrate_mode="BitrateMode.VBR"),
     "MP3 · 245 kbps VBR · Stereo", False),
    (info("mp4", codec="mp4a.40.2", sample_rate=44100, channels=2, bitrate=256_000),
     "AAC · 256 kbps · Stereo", False),
    (info("mp4", codec="alac", bits_per_sample=16, sample_rate=44100, channels=2),
     "ALAC · 16/44.1 · Stereo", True),
    (info("wave", bits_per_sample=16, sample_rate=48000, channels=6),
     "WAV · 16/48 · 5.1", True),
])
def test_labels(i, label, lossless):
    q = audio_quality.describe_info(i)
    assert q.label == label
    assert q.lossless is lossless


def test_reads_a_real_file(tmp_path):
    path = tmp_path / "side-a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 2205)
    q = audio_quality.describe(str(path))
    assert q.label == "WAV · 16/22.05 · Mono"
    assert "lossless" in q.detail


def test_unreadable_or_missing_files_give_nothing(tmp_path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio")
    assert audio_quality.describe(str(junk)) is None
    assert audio_quality.describe(str(tmp_path / "gone.flac")) is None
    assert audio_quality.describe(None) is None


def test_player_bar_pill(ctx, tmp_path, monkeypatch):
    from musicmgr.ui.widgets import player_bar as player_bar_module
    from musicmgr.ui.widgets.player_bar import PlayerBar

    monkeypatch.setattr(player_bar_module, "SPECTRUM_ENABLED", False)

    path = tmp_path / "t.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 441)
    bar = PlayerBar(ctx.player, ctx)
    track = QueueItem(track_id=1, title="Song", artist="A", album="B", path=str(path))
    bar.on_track_changed(track)
    assert not bar.quality_badge.isHidden()
    assert bar.quality_badge.text() == "WAV · 16/44.1 · Stereo"

    station = QueueItem(track_id=0, title="WDVE", artist="Internet radio", album="",
                        path="http://example/stream", kind="stream")
    bar.on_track_changed(station)
    assert bar.quality_badge.isHidden()
    assert not bar.live_badge.isHidden()

    bar.on_track_changed(None)
    assert bar.quality_badge.isHidden()
