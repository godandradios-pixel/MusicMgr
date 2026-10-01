"""Radio tuner (2026-10-01): services/tuner.py, services/stations.py, the
player's episode/stream support, the painted set (ui/widgets/radio_dial.py)
and the Radio page (ui/views/tuner.py)."""

from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from sqlalchemy import select

from musicmgr.db.models import RadioEpisode, RadioShow
from musicmgr.services import otr, stations
from musicmgr.services.player import PlayerController, QueueItem
from musicmgr.ui.widgets import radio_dial


def make_episode(path: Path, seconds: float = 0.4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


@pytest.fixture
def library(db, tmp_path):
    root = tmp_path / "Radio"
    for stem in ("1937-09-26 - Death House Rescue", "1937-10-17 - Murder By The Dead",
                 "1937-10-24 - The Temple Bells"):
        make_episode(root / "The Shadow" / f"{stem}.wav")
    for stem in ("Whistler 44-11-20 (130) Death Sees Double", "Whistler 45-04-16 (151) To Rent Danger"):
        make_episode(root / "The Whistler" / f"{stem}.wav")
    make_episode(root / "Commercials" / "Anacin.wav", 0.2)
    make_episode(root / "Commercials" / "Blue Coal.wav", 0.2)
    otr.scan(db.new_session, str(root), read_tags=lambda p: {"length": 0.4})
    with db.session_scope() as s:
        otr.set_root(s, str(root))
        ids = {sh.name: sh.id for sh in otr.list_shows(s)}
        ids["shadow_eps"] = [e.id for e in otr.episodes(s, ids["The Shadow"])]
    return ids


@pytest.fixture
def tuner(ctx, monkeypatch):
    monkeypatch.setattr(PlayerController, "auto_measure", False)
    monkeypatch.setattr(otr, "MIN_PROGRAM_MS", 100)
    yield ctx.tuner
    ctx.player.clear_queue()


def wait_until(cond, ms=5000):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        if cond():
            return True
        QTest.qWait(20)
    return cond()


class TestTunerController:
    def test_tune_show_queues_broadcasts_in_order(self, ctx, tuner, library):
        assert tuner.tune_show(library["The Shadow"])
        assert tuner.tuned == ("show", library["The Shadow"])
        assert [i.episode_id for i in ctx.player.queue] == library["shadow_eps"]
        assert ctx.player.source.startswith("otr:show")
        assert all(i.kind == "episode" for i in ctx.player.queue)

    def test_resume_position_is_passed_to_the_player(self, ctx, tuner, library, db):
        eps = library["shadow_eps"]
        with db.session_scope() as s:
            otr.save_position(s, eps[1], 120_000, 1_800_000)
        tuner.tune_show(library["The Shadow"])
        assert ctx.player.queue[0].episode_id == eps[1]

    def test_episodes_played_through_are_marked_and_show_moves_on(self, ctx, tuner, library, db):
        tuner.tune_show(library["The Shadow"])
        eps = library["shadow_eps"]
        assert wait_until(lambda: ctx.player.current is not None
                          and ctx.player.current.episode_id == eps[1], 4000)
        with db.session_scope() as s:
            assert s.get(RadioEpisode, eps[0]).played
            show = s.get(RadioShow, library["The Shadow"])
            assert show.current_episode_id == eps[1]

    def test_playing_music_elsewhere_untunes(self, ctx, tuner, library, tmp_path):
        tuner.tune_show(library["The Shadow"])
        path = tmp_path / "song.wav"
        make_episode(path)
        ctx.player.play_tracks([QueueItem(track_id=1, title="Song", artist="A", album="B",
                                          path=str(path), hydrated=True)])
        assert tuner.tuned == ("", 0)

    def test_on_air_mixes_commercials_and_tops_up(self, ctx, tuner, library):
        assert tuner.tune_onair()
        queue = ctx.player.queue
        artists = [i.artist for i in queue]
        assert "Commercials" in artists
        assert {"The Shadow", "The Whistler"} & set(artists)
        before = len(queue)
        ctx.player.jump_to(before - 1)
        assert len(ctx.player.queue) >= before

    def test_on_air_doesnt_touch_resume_points(self, ctx, tuner, library, db):
        tuner.tune_onair()
        wait_until(lambda: False, 900)
        with db.session_scope() as s:
            assert not any(e.played for e in s.scalars(select(RadioEpisode)))

    def test_last_tuned_is_remembered(self, ctx, tuner, library):
        tuner.tune_show(library["The Whistler"])
        assert tuner.last_tuned() == ("show", library["The Whistler"])


class TestPlayerKinds:
    def test_streams_play_by_url_without_preload_or_history(self, ctx, tuner, db):
        with db.session_scope() as s:
            st = stations.add_manual(s, "Test FM", "http://127.0.0.1:9/stream")
            sid = st.id
        tuner.tune_station(sid)
        player = ctx.player
        item = player.current
        assert item.is_stream and item.path.startswith("http")
        assert player._standby.item is None
        assert player._active.player.source().toString().startswith("http")
        assert player._crossfade_ms_for_next() == 0

    def test_episodes_write_no_play_events(self, ctx, tuner, library, db):
        from musicmgr.db.models import PlayEvent

        tuner.tune_show(library["The Shadow"])
        assert wait_until(lambda: ctx.player.current is not None
                          and ctx.player.current.episode_id == library["shadow_eps"][1], 4000)
        with db.session_scope() as s:
            assert s.scalar(select(PlayEvent)) is None

    def test_resume_seek_is_applied_once_loaded(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setattr(PlayerController, "auto_measure", False)
        path = tmp_path / "long.wav"
        make_episode(path, 3.0)
        item = QueueItem(track_id=0, title="Ep", artist="Show", album="", path=str(path),
                         kind="episode", episode_id=1, start_ms=1500, hydrated=True)
        ctx.player.play_tracks([item], source="otr:show:1")
        assert wait_until(lambda: ctx.player.position() >= 1500, 3000)
        assert item.start_ms == 0
        ctx.player.clear_queue()

    def test_volume_changes_are_announced(self, ctx):
        seen = []
        ctx.player.volumeChanged.connect(seen.append)
        ctx.player.set_volume(0.3)
        ctx.player.set_volume(0.3)
        assert seen == [pytest.approx(0.3)]


class TestStations:
    class FakeHttp:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def get(self, url, params=None, timeout=None, headers=None):
            self.calls.append((url, params, headers))
            payload = self.payload

            class R:
                status_code = 200

                def json(self_inner):
                    return payload
            return R()

    RAW = [
        {"stationuuid": "u1", "name": "Old Time Radio Net", "url_resolved": "http://otr/stream",
         "countrycode": "US", "codec": "MP3", "bitrate": 128, "votes": 50, "lastcheckok": 1,
         "tags": "old time radio,drama", "favicon": "http://otr/logo.png"},
        {"stationuuid": "u2", "name": "Broken FM", "url_resolved": "http://broken",
         "votes": 900, "lastcheckok": 0},
        {"stationuuid": "u3", "name": "Swing Street", "url": "http://swing", "votes": 80,
         "lastcheckok": 1},
    ]

    def test_search_parses_filters_and_sorts(self):
        http = self.FakeHttp(self.RAW)
        found = stations.search(params={"tag": "old time radio"}, http=http)
        assert [f.name for f in found] == ["Swing Street", "Old Time Radio Net"]
        url, params, headers = http.calls[0]
        assert url.endswith("/json/stations/search")
        assert params["tag"] == "old time radio" and params["hidebroken"] == "true"
        assert headers["User-Agent"].startswith("MusicMgr/")
        assert found[1].detail == "US · MP3 · 128 kbps"

    def test_add_remove_move_on_the_dial(self, db):
        found = stations.search(http=self.FakeHttp(self.RAW))
        with db.session_scope() as s:
            a = stations.add(s, found[0])
            b = stations.add(s, found[1])
            assert stations.add(s, found[0]).id == a.id  # no duplicates
            assert [x.name for x in stations.dial(s)] == ["Swing Street", "Old Time Radio Net"]
            stations.move(s, b.id, -1)
            assert [x.name for x in stations.dial(s)] == ["Old Time Radio Net", "Swing Street"]
            stations.remove(s, a.id)
            assert [x.name for x in stations.dial(s)] == ["Old Time Radio Net"]

    def test_all_mirrors_down_raises(self):
        class Down:
            def get(self, *a, **k):
                raise OSError("offline")
        with pytest.raises(stations.DirectoryError):
            stations.search(http=Down())


class TestRadioSet:
    def test_positions_spread_across_the_dial(self):
        assert radio_dial.station_positions(1) == [0.5]
        pos = radio_dial.station_positions(5)
        assert pos[0] == pytest.approx(0.08) and pos[-1] == pytest.approx(0.92)

    def test_six_bands_in_order(self):
        assert radio_dial.BAND_KEYS == ("fm", "am", "police", "sw1", "sw2", "lw")

    def test_pointer_maps_from_x(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w._relayout()
        left, width = w._geom["scale_left"], w._geom["scale_width"]
        assert w._pos_from_x(left + width * 0.25) == pytest.approx(0.25)
        assert w._pos_from_x(0) == 0.0

    def test_step_settles_and_asks_to_tune_on_the_lit_band(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("police", ["Green Hornet", "Shadow", "Whistler"])
        w.set_entries("am", ["Lone Ranger"])
        w.set_tuned("police", 0, animate=False)
        asked = []
        w.tuneRequested.connect(lambda band, i: asked.append((band, i)))
        w.step(1)
        assert wait_until(lambda: asked == [("police", 1)], 3000)
        assert w.pointer == pytest.approx(radio_dial.station_positions(3)[1])

    def test_switching_band_moves_pointer_to_that_bands_station(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("am", ["A", "B", "C", "D"])
        w.set_entries("lw", ["On the Air", "Commercials"])
        w.set_tuned("am", 3, animate=False)
        w.set_tuned("lw", 0, animate=False)
        w.set_band("am", animate=False)
        assert w.band == "am" and w.pointer == pytest.approx(radio_dial.station_positions(4)[3])

    def test_tapping_a_key_or_strip_asks_for_the_band(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w._relayout()
        asked = []
        w.bandRequested.connect(asked.append)
        QTest.mouseClick(w, Qt.LeftButton, pos=w._geom["keys"]["sw2"].center().toPoint())
        QTest.mouseClick(w, Qt.LeftButton, pos=w._geom["strips"]["lw"].center().toPoint())
        assert asked == ["sw2", "lw"]

    def test_static_loop_is_generated(self, tmp_path):
        path = radio_dial.make_static_wav(tmp_path / "static.wav", seconds=0.2)
        with wave.open(str(path)) as w:
            assert w.getnframes() == int(0.2 * 22050)

    def test_paints(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(900, 460)
        w.set_entries("am", ["Lone Ranger", "Ozzie & Harriet"])
        assert not w.grab().isNull()


class TestBands:
    def test_shows_are_banded_by_type(self, library, db):
        with db.session_scope() as s:
            bands = {sh.name: sh.band for sh in otr.list_shows(s)}
        assert bands == {"The Shadow": "police", "The Whistler": "police", "Commercials": "lw"}

    @pytest.mark.parametrize("name, band", [
        ("Lone Ranger", "am"), ("Ozzie And Harriet", "am"), ("The Green Hornet", "police"),
        ("Sherlock Holmes", "police"), ("WWII News and Sounds", "sw1"),
        ("FDR Fireside Chats", "sw2"), ("Paul Harvey", "sw2"), ("American History", "sw2"),
        ("Commercials", "lw"),
    ])
    def test_guess_band(self, name, band):
        assert otr.guess_band(name, otr.guess_kind(name)) == band


class TestTunerPage:
    def view(self, ctx):
        from musicmgr.ui.views.tuner import TunerView

        view = TunerView(ctx)
        view.refresh()
        return view

    def test_bands_list_on_air_shows_and_stations(self, ctx, tuner, library, db):
        with db.session_scope() as s:
            stations.add_manual(s, "Test FM", "http://127.0.0.1:9/stream")
        view = self.view(ctx)
        assert view.radio.labels("lw") == ["On the Air", "Commercials"]
        assert view.radio.labels("police") == ["Shadow", "Whistler"]
        assert view.radio.labels("fm") == ["Test FM"]

    def test_tuning_from_the_dial_and_info_card(self, ctx, tuner, library):
        view = self.view(ctx)
        view.set_band("police")
        view._on_tune_requested("police", view.radio.labels("police").index("Shadow"))
        assert tuner.tuned == ("show", library["The Shadow"])
        assert view.radio.band == "police"
        assert view.info_name.text() == "The Shadow"
        assert view.info_title.text() == "Death House Rescue"
        assert "September 26, 1937" in view.info_sub.text()
        assert view.btn_episodes.isVisibleTo(view)

    def test_moving_a_show_to_another_band(self, ctx, tuner, library, db):
        view = self.view(ctx)
        tuner.tune_show(library["The Whistler"])
        view.move_to_band("am")
        assert "Whistler" in view.radio.labels("am")
        assert view.radio.band == "am"
        with db.session_scope() as s:
            assert s.get(RadioShow, library["The Whistler"]).band == "am"

    def test_empty_fm_band_offers_stations(self, ctx, tuner, library):
        view = self.view(ctx)
        view.set_band("fm")
        assert view.btn_starters.isVisibleTo(view) and view.btn_find.isVisibleTo(view)

    def test_no_folder_yet_asks_for_it(self, ctx, tuner):
        view = self.view(ctx)
        view.set_band("am")
        assert view.btn_folder.isVisibleTo(view)

    def test_episodes_dialog_lists_and_toggles(self, ctx, tuner, library, db):
        from musicmgr.ui.views.tuner import EpisodesDialog

        dialog = EpisodesDialog(ctx, library["The Shadow"])
        assert dialog.list.count() == 3
        dialog.list.setCurrentRow(0)
        dialog._toggle_played()
        with db.session_scope() as s:
            assert s.get(RadioEpisode, library["shadow_eps"][0]).played
