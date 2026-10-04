"""Stations coming in and out through the static as the dial moves
(2026-10-03 - James: "When you move the dial between stations, I would
like it to be able to play static stations, coming in and out").

ui/widgets/radio_dial.py works out the playing station's reception from
the pointer (with a flutter while it's half tuned), raises the static
against it, and tunes a station in mid-drag when the pointer rests on it;
the Radio page passes reception to PlayerController.set_reception.
"""

from __future__ import annotations

import wave

import numpy as np
import pytest
from PySide6.QtTest import QTest

from musicmgr.services import stations
from musicmgr.services.player import PlayerController
from musicmgr.ui.widgets import radio_dial


def dial(qapp):
    w = radio_dial.RadioSet()
    w.resize(1100, 560)
    w.set_entries("fm", ["100.7 WMMS", "102.5 WDVE", "105.9 The X"])
    w.set_tuned("fm", 1, animate=False)
    return w


def drag_to(w, pos):
    w._drag = "dial"
    w._pos = pos
    w._update_static()


class TestReception:
    def test_fades_as_the_pointer_leaves_and_returns_on_the_station(self, qapp):
        w = dial(qapp)
        got = []
        w.receptionChanged.connect(got.append)
        on = w.positions()[1]
        half = (w.positions()[1] - w.positions()[0]) / 2
        assert w.reception == 1.0
        drag_to(w, on + half * 0.2)
        near = w.reception
        drag_to(w, on + half * 0.7)
        far = w.reception
        assert 0.0 < far < near < 1.0
        drag_to(w, (w.positions()[1] + w.positions()[2]) / 2)
        assert w.reception == 0.0                  # nothing but static between them
        drag_to(w, on)
        assert w.reception == pytest.approx(1.0)
        assert got and got[-1] == pytest.approx(1.0)
        w._drag = None

    def test_half_tuned_station_wavers(self, qapp):
        w = dial(qapp)
        drag_to(w, w.positions()[1] + (w.positions()[1] - w.positions()[0]) / 4)
        readings = set()
        for _ in range(8):
            w._update_reception()
            readings.add(round(w.reception, 4))
        assert len(readings) > 3                   # coming in and out, not steady
        w._drag = None

    def test_resting_on_another_station_mid_drag_brings_it_in(self, qapp):
        w = dial(qapp)
        asked = []
        w.tuneRequested.connect(lambda band, i: asked.append(i))
        drag_to(w, w.positions()[2])
        assert w._dwell.isActive()
        QTest.qWait(radio_dial.DWELL_MS + 150)
        w._update_static()
        assert asked == [2] and w._drag == "dial"  # still dragging
        assert w.reception == pytest.approx(1.0)   # now relative to the new station
        w._drag = None

    def test_sweeping_past_doesnt_tune(self, qapp):
        w = dial(qapp)
        asked = []
        w.tuneRequested.connect(lambda band, i: asked.append(i))
        drag_to(w, w.positions()[2])
        drag_to(w, (w.positions()[1] + w.positions()[2]) / 2)
        QTest.qWait(radio_dial.DWELL_MS + 150)
        assert asked == []
        w._drag = None

    def test_letting_go_settles_back_to_full_reception(self, qapp):
        w = dial(qapp)
        drag_to(w, w.positions()[1] + 0.012)
        assert w.reception < 1.0
        w.mouseReleaseEvent(type("E", (), {"position": lambda self: None})())
        w._anim.setCurrentTime(w._anim.duration())
        w._tune_timer.stop()
        w._emit_tune()
        assert w.reception == pytest.approx(1.0)


    def test_band_change_never_leaves_the_station_faded(self, qapp):
        w = dial(qapp)
        w.set_entries("am", ["KDKA 1020"])        # nothing tuned on AM yet
        w.set_band("am", animate=True)
        w._anim.setCurrentTime(w._anim.duration() // 2)
        w._anim.setCurrentTime(w._anim.duration())
        w._update_static()
        assert w.reception == pytest.approx(1.0)


class TestPlayerAndPage:
    def test_reception_scales_the_output_not_the_volume_setting(self, ctx):
        player = ctx.player
        player.set_volume(0.8)
        player.set_reception(0.25)
        assert player.volume == pytest.approx(0.8)
        assert player._active.audio.volume() == pytest.approx(0.8 * player._active.gain * 0.25,
                                                              abs=0.01)
        player.set_reception(1.0)
        assert player._active.audio.volume() == pytest.approx(0.8 * player._active.gain, abs=0.01)

    def test_page_passes_reception_only_while_the_radio_plays(self, ctx, db, monkeypatch):
        from musicmgr.ui.views.tuner import TunerView

        monkeypatch.setattr(PlayerController, "auto_measure", False)
        with db.session_scope() as s:
            sid = stations.add_manual(s, "102.5 WDVE", "http://127.0.0.1:9/x").id
        view = TunerView(ctx)
        view.refresh()
        view.radio.receptionChanged.emit(0.3)
        assert ctx.player.reception == 1.0         # radio not on: leave it alone
        ctx.tuner._set_tuned("station", sid)
        view.radio.receptionChanged.emit(0.3)
        assert ctx.player.reception == pytest.approx(0.3)
        ctx.tuner._set_tuned("", 0)                # something else took over
        assert ctx.player.reception == 1.0


class FakeEffect:
    def __init__(self):
        self.volume, self.playing = 0.0, False

    def setVolume(self, v):
        self.volume = v

    def isPlaying(self):
        return self.playing

    def play(self):
        self.playing = True

    def stop(self):
        self.playing = False


class TestStaticSound:
    """2026-10-04 (James: "can we put some radio static in there?"): the
    first loop was about -40 dBFS and muffled, so it was hard to hear."""

    def read(self, path):
        with wave.open(str(path)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, w.getnchannels())
        return pcm / 32767

    def test_loop_is_as_loud_as_a_station_and_seamless(self, tmp_path):
        a = self.read(radio_dial.make_static_wav(tmp_path / "s.wav", seconds=1.0))
        rms_db = 20 * np.log10(np.sqrt(np.mean(a ** 2)))
        assert -19 < rms_db < -13
        typical = np.abs(np.diff(a[:, 0])).mean()
        assert abs(a[-1, 0] - a[0, 0]) < 4 * typical   # no click at the loop point

    def test_hiss_is_bright_not_a_rumble(self, tmp_path):
        a = self.read(radio_dial.make_static_wav(tmp_path / "s.wav", seconds=1.0))[:, 0]
        spec = np.abs(np.fft.rfft(a))
        freq = np.fft.rfftfreq(len(a), 1 / radio_dial.STATIC_RATE)
        low = spec[freq < 150].mean()
        mid = spec[(freq > 1000) & (freq < 4000)].mean()
        assert mid > 2 * low

    def test_new_file_name_so_the_old_quiet_loop_is_not_reused(self):
        assert radio_dial.STATIC_FILE != "tuner_static.wav"

    def test_static_between_stations_follows_the_volume_knob(self, qapp):
        w = dial(qapp)
        w.set_static_path(radio_dial.Path("unused.wav"))
        w._static = FakeEffect()
        w.set_volume(0.8)
        drag_to(w, (w.positions()[1] + w.positions()[2]) / 2)
        between = w._static.volume
        assert w._static.playing
        assert between >= 0.8 * radio_dial.STATIC_GAIN * 0.7   # (flutter takes some off)
        drag_to(w, w.positions()[1] + 0.002)
        assert w._static.volume < between / 2                 # almost gone on a station
        w._drag = None
