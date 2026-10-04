"""The pulse visualizer on internet radio (2026-10-03 - James: "Any way the
visualizer at the bottom can pulse with radio broadcasts").

A station can't be decoded up front like a track, so PlayerController taps
the deck playing it (QAudioBufferOutput), services/spectrum.py:LiveSpectrum
turns the sound into the same five bands as it plays, and the
PulseVisualizer's live mode plays those readings out.
"""

from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtTest import QTest

from musicmgr.services import player as player_module
from musicmgr.services.player import PlayerController, QueueItem
from musicmgr.services.spectrum import BUCKET_MS, LiveSpectrum
from musicmgr.ui.widgets import player_bar as player_bar_module
from musicmgr.ui.widgets import visualizer as viz
from musicmgr.ui.widgets.player_bar import PlayerBar


def tone(freq: float, ms: int, rate: int = 48000, amp: float = 8000) -> list[float]:
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(rate * ms // 1000)]


def wait_until(cond, ms=4000):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        if cond():
            return True
        QTest.qWait(20)
    return cond()


class TestLiveSpectrum:
    def test_one_reading_per_bucket_with_the_right_band_loudest(self, qapp):
        live = LiveSpectrum()
        got = []
        live.bucketReady.connect(lambda bands, beat: got.append(bands))
        live.feed(tone(60, 600), 48000)
        assert len(got) == 600 // BUCKET_MS
        assert all(b[0] == max(b) for b in got)          # 60 Hz -> the bass band
        got.clear()
        live.feed(tone(3500, 300), 48000)
        assert got and all(b[3] == max(b) for b in got)  # 3.5 kHz -> upper mids
        assert all(0.0 <= v <= 1.0 for b in got for v in b)

    def test_partial_buffers_carry_over(self, qapp):
        live = LiveSpectrum()
        got = []
        live.bucketReady.connect(lambda bands, beat: got.append(bands))
        samples = tone(60, 120)
        live.feed(samples[:1000], 48000)
        live.feed(samples[1000:], 48000)
        assert len(got) == 2

    def test_quiet_hiss_isnt_blown_up(self, qapp):
        live = LiveSpectrum()
        got = []
        live.bucketReady.connect(lambda bands, beat: got.append(bands))
        live.feed(tone(60, 300, amp=20), 48000)
        assert max(max(b) for b in got) < 0.5

    def test_bass_thumps_are_beats(self, qapp):
        live = LiveSpectrum()
        beats = []
        live.bucketReady.connect(lambda bands, beat: beats.append(beat))
        quiet, thump = tone(60, 480, amp=400), tone(60, 120, amp=9000)
        for _ in range(4):
            live.feed(quiet, 48000)
            live.feed(thump, 48000)
        assert sum(beats) >= 3
        assert not beats[0]


class TestVisualizerLiveMode:
    def test_live_readings_drive_the_bars_and_go_idle_when_they_stop(self, qapp):
        w = viz.PulseVisualizer()
        w.set_active(True)
        w.set_live(True)
        assert w.live and not w._live_has_data()
        for _ in range(4):
            w.push_live([1.0, 0.8, 0.6, 0.4, 0.2], beat=True)
        for _ in range(6):
            w._tick()
        middle = viz.BAR_COUNT // 2
        assert w._levels[middle] > 0.4
        assert w._current_bands == [1.0, 0.8, 0.6, 0.4, 0.2]
        assert not w.grab().isNull()
        for _ in range(int(viz._LIVE_STALE_MS / viz._TICK_MS) + 2):
            w._tick()
        assert not w._live_has_data()      # stream stalled: back to breathing

    def test_queue_doesnt_run_away(self, qapp):
        w = viz.PulseVisualizer()
        w.set_live(True)
        for _ in range(20):
            w.push_live([0.5] * 5)
        assert len(w._live_queue) <= viz._LIVE_MAX_QUEUE

    def test_readings_ignored_unless_live(self, qapp):
        w = viz.PulseVisualizer()
        w.push_live([1.0] * 5)
        assert w._live_queue == []


class TestPlayerTap:
    @pytest.fixture
    def player(self, ctx, monkeypatch):
        monkeypatch.setattr(PlayerController, "auto_measure", False)
        yield ctx.player
        ctx.player.clear_queue()

    def stream(self, url: str) -> QueueItem:
        return QueueItem(track_id=0, title="Test FM", artist="Internet radio", album="",
                         path=url, kind="stream", station_id=1, hydrated=True)

    @pytest.mark.skipif(player_module.QAudioBufferOutput is None, reason="Qt < 6.8")
    def test_only_a_station_is_tapped(self, player, tmp_path):
        player.play_tracks([self.stream("http://127.0.0.1:9/x")])
        assert player._active.tap is player._tap
        path = tmp_path / "song.wav"
        write_tone(path, 0.3)
        player.play_tracks([QueueItem(track_id=1, title="Song", artist="A", album="B",
                                      path=str(path), hydrated=True)])
        assert all(d.tap is None for d in player._decks)

    @pytest.mark.skipif(player_module.QAudioBufferOutput is None, reason="Qt < 6.8")
    def test_a_playing_station_sends_live_bands(self, player, tmp_path):
        path = tmp_path / "station.wav"
        write_tone(path, 2.0)
        got = []
        player.liveBands.connect(lambda bands, beat: got.append(bands))
        player.play_tracks([self.stream(QUrl.fromLocalFile(str(path)).toString())])
        assert wait_until(lambda: len(got) >= 5)
        assert all(len(b) == 5 for b in got)


class TestPlayerBar:
    def test_station_puts_the_visualizer_in_live_mode(self, ctx, monkeypatch):
        monkeypatch.setattr(player_bar_module, "SPECTRUM_ENABLED", False)
        bar = PlayerBar(ctx.player, ctx)
        bar.on_track_changed(QueueItem(track_id=0, title="WXDX", artist="Internet radio",
                                       album="", path="http://x", kind="stream",
                                       station_id=1, hydrated=True))
        assert bar.visualizer.live
        ctx.player.liveBands.emit([0.9] * 5, False)
        assert bar.visualizer._live_queue
        bar.on_track_changed(QueueItem(track_id=1, title="Song", artist="A", album="B",
                                       path="x.mp3", hydrated=True))
        assert not bar.visualizer.live
        bar.on_track_changed(None)
        assert not bar.visualizer.live


def write_tone(path: Path, seconds: float) -> None:
    rate = 44100
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            v = int(8000 * math.sin(2 * math.pi * 60 * i / rate))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
