"""Radio improvements (2026-10-03, James picked three):

- stations at their real frequencies: "102.5 WDVE" sits by the 102 mark,
  internet-only stations fill the empty stretches (services/stations.py
  dial_positions, ui/widgets/radio_dial.py);
- the magic eye as a signal meter, and stations marked off the air when
  their stream stops answering, with a lookup for a new link
  (PlayerController.signal, services/tuner.py, ui/views/tuner.py);
- the player bar in live mode: no seek/skip/shuffle/repeat, a LIVE badge,
  and previous/next stepping along the band (ui/widgets/player_bar.py).
"""

from __future__ import annotations

import pytest

from musicmgr.db.models import RadioStation
from musicmgr.services import stations
from musicmgr.services.player import PlayerController, QueueItem
from musicmgr.ui.widgets import player_bar as player_bar_module
from musicmgr.ui.widgets import radio_dial
from musicmgr.ui.widgets.player_bar import PlayerBar

FM = ["WALM", "20s Gold", "40s Gold", "60s", "WQED Pittsburgh",
      "100.7 WMMS", "102.5 WDVE", "105.9 The X WXDX"]
AM = ["WGN 720 Chicago, IL", "WABC 770 AM - NY", "95.5 WSB UGA", "KDKA 1020", "WTAM 1100"]


class FakeHttp:
    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        payload = next((v for k, v in self.by_path.items() if url.endswith(k)), [])
        if callable(payload):
            payload = payload(params)

        class R:
            status_code = 200

            def json(self_inner):
                return payload
        return R()


def add(s, name, band="fm", url=None):
    return stations.add_manual(s, name, url or f"http://127.0.0.1:9/{name}", band=band)


class TestFrequencies:
    @pytest.mark.parametrize("name, band, freq", [
        ("102.5 WDVE", "fm", 102.5),
        ("105.9 The X WXDX", "fm", 105.9),
        ("100.7 WMMS - CLEVELAND, OHIO", "fm", 100.7),
        ("20s Gold", "fm", None),
        ("WQED Pittsburgh", "fm", None),
        ("KDKA 1020", "am", 1020),
        ("WGN 720 Chicago, IL", "am", 720),
        ("WABC 770 AM - NY", "am", 770),
        ("95.5 WSB UGA", "am", None),      # an FM number on the AM band
        ("1940s Hits", "am", None),
        ("KDKA 1020", "fm", None),
        ("Lone Ranger", "sw1", None),
    ])
    def test_frequency_in_the_name(self, name, band, freq):
        assert stations.frequency(name, band) == freq

    def test_scale_follows_the_printed_marks(self):
        # 102 is the 8th of 11 FM marks; 1100 sits halfway from 1000 to 1200
        assert stations.scale_position("fm", 102) == pytest.approx(0.08 + 0.84 * 7 / 10)
        am = stations.AM_MARKS
        t1000 = 0.08 + 0.84 * am.index(1000) / (len(am) - 1)
        t1200 = 0.08 + 0.84 * am.index(1200) / (len(am) - 1)
        assert stations.scale_position("am", 1100) == pytest.approx((t1000 + t1200) / 2)
        assert stations.scale_position("fm", 87.5) >= stations.DIAL_MIN

    def test_stations_sit_at_their_frequency_the_rest_fill_the_roomiest_stretch(self):
        pos = stations.dial_positions("fm", FM)
        assert pos[6] == pytest.approx(stations.scale_position("fm", 102.5))
        assert pos[5] < pos[6] < pos[7]
        loose = pos[:5]
        # all five internet-only ones go into the empty 88-100 stretch, in order
        assert loose == sorted(loose) and max(loose) < pos[5]
        assert min(b - a for a, b in zip(loose, loose[1:])) > 0.08

    def test_same_frequency_twice_is_nudged_apart(self):
        a, b = stations.dial_positions("fm", ["102.5 One", "102.5 Two"])
        assert b - a >= stations.MIN_GAP - 1e-9

    def test_no_frequencies_or_a_program_band_spread_evenly(self):
        assert stations.dial_positions("fm", ["A", "B", "C"]) == stations.even_positions(3)
        assert stations.dial_positions("sw1", ["102.5 X", "B"]) == stations.even_positions(2)

    def test_previous_next_station_goes_by_the_dial_not_the_list(self, db):
        with db.session_scope() as s:
            ids = {n: add(s, n, "am").id for n in AM}
            add(s, "On FM", "fm")
            ordered = [ids[n] for n in ("WGN 720 Chicago, IL", "WABC 770 AM - NY",
                                       "KDKA 1020", "WTAM 1100", "95.5 WSB UGA")]
            assert stations.neighbour(s, ordered[1], 1) == ordered[2]
            assert stations.neighbour(s, ordered[2], -1) == ordered[1]
            assert stations.neighbour(s, ordered[-1], 1) == ordered[0]  # wraps round
            assert stations.neighbour(s, ordered[-1], 1, wrap=False) is None

    def test_move_only_among_internet_only_stations(self, db):
        with db.session_scope() as s:
            a, f, b = add(s, "Alpha"), add(s, "102.5 WDVE"), add(s, "Beta")
            loose = [a.id, b.id]
            stations.move(s, a.id, 1, among=loose)
            assert [x.name for x in stations.dial(s)] == ["Beta", "102.5 WDVE", "Alpha"]
            stations.move(s, a.id, 1, among=loose)  # already last of them
            assert [x.name for x in stations.dial(s)] == ["Beta", "102.5 WDVE", "Alpha"]


class TestDial:
    def test_entries_use_frequency_positions_and_step_in_dial_order(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("fm", ["105.9 The X", "Gold", "100.7 WMMS"])
        pos = w.positions("fm")
        assert pos[2] < pos[0]
        w.set_tuned("fm", 2, animate=False)        # 100.7
        asked = []
        w.tuneRequested.connect(lambda band, i: asked.append(i))
        w.step(1)
        w._anim.setCurrentTime(w._anim.duration())
        w._emit_tune()
        assert asked == [0]                        # on to 105.9, not back to "Gold"

    def test_crowded_names_paint_and_off_air_fades(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("am", AM, faded=[2])
        w.set_band("am", animate=False)
        assert w._faded["am"] == {2}
        assert not w.grab().isNull()

    def test_eye_reads_the_signal_once_settled(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("fm", ["A"])
        w.set_tuned("fm", 0, animate=False)
        w.set_playing(True)
        w.set_signal(radio_dial.SIGNAL_GOOD)
        good, lit_good = w._eye_shadow()
        assert good == pytest.approx(8) and not w._flicker_timer.isActive()
        w.set_signal(radio_dial.SIGNAL_BUFFERING)
        assert w._flicker_timer.isActive()
        w._on_flicker()
        assert w._eye_shadow()[0] > good
        w.set_signal(radio_dial.SIGNAL_LOST)
        lost, lit_lost = w._eye_shadow()
        assert lost > 100 and lit_lost < lit_good and not w._flicker_timer.isActive()
        assert not w.grab().isNull()


class TestSignalAndOffAir:
    @pytest.fixture
    def tuner(self, ctx, monkeypatch):
        monkeypatch.setattr(PlayerController, "auto_measure", False)
        yield ctx.tuner
        ctx.player.clear_queue()

    def station(self, db, name="105.9 The X WXDX"):
        with db.session_scope() as s:
            return add(s, name).id

    def test_stream_starts_connecting(self, ctx, tuner, db):
        sid = self.station(db)
        tuner.tune_station(sid)
        assert ctx.player.signal in ("connecting", "lost")

    def test_dropped_twice_goes_off_the_air_and_comes_back(self, ctx, tuner, db, monkeypatch):
        sid = self.station(db)
        tuner._set_tuned("station", sid)
        retunes, notes, air = [], [], []
        monkeypatch.setattr(tuner, "tune_station", lambda i, _retune=False: retunes.append(i))
        tuner.notified.connect(notes.append)
        tuner.stationAirChanged.connect(lambda i, off: air.append((i, off)))

        tuner._on_signal("lost")                       # first drop: retune once
        assert tuner._retune_timer.isActive()
        tuner._retune_timer.stop()
        tuner._retune()
        assert retunes == [sid]
        tuner._on_signal("lost")                       # still nothing: off the air
        assert air == [(sid, True)]
        assert "off the air" in notes[-1]
        with db.session_scope() as s:
            assert s.get(RadioStation, sid).off_air_since is not None

        tuner._on_signal("good")                       # back on
        assert air[-1] == (sid, False)
        with db.session_scope() as s:
            assert s.get(RadioStation, sid).off_air_since is None

    def test_stuck_connecting_counts_as_a_drop(self, ctx, tuner, db):
        sid = self.station(db)
        tuner._set_tuned("station", sid)
        tuner._on_signal("buffering")
        assert tuner._stall.isActive()
        tuner._on_signal("good")
        assert not tuner._stall.isActive()

    def test_shows_never_go_off_the_air(self, ctx, tuner):
        tuner._set_tuned("show", 1)
        tuner._on_signal("lost")
        assert not tuner._retune_timer.isActive()

    def test_step_station_tunes_the_neighbour(self, ctx, tuner, db, monkeypatch):
        with db.session_scope() as s:
            a, b = add(s, "100.7 WMMS").id, add(s, "102.5 WDVE").id
        tuner._set_tuned("station", a)
        tuned = []
        monkeypatch.setattr(tuner, "tune_station", lambda i, _retune=False: tuned.append(i) or True)
        assert tuner.step_station(1) and tuned == [b]
        tuner._set_tuned("show", 1)
        assert not tuner.step_station(1)


class TestNewLink:
    RAW = {"stationuuid": "u9", "name": "105.9 The X", "url_resolved": "http://new/x",
           "codec": "AAC", "bitrate": 64, "votes": 12, "lastcheckok": 1,
           "homepage": "https://1059thex.iheart.com/"}

    def test_queries_from_call_letters_and_name(self):
        assert stations.replacement_queries("105.9 The X WXDX") == ["WXDX", "The X"]
        assert stations.replacement_queries("20s Gold") == ["20s Gold"]
        assert stations.replacement_queries("WABC 770 AM - NY") == ["WABC"]

    def test_lookup_tries_the_same_entry_then_searches_and_skips_the_dead_link(self):
        dead = dict(self.RAW, stationuuid="u1", url_resolved="http://dead/x")
        http = FakeHttp({"/json/stations/byuuid": [dead],
                         "/json/stations/search": [self.RAW, dead]})
        found = stations.find_replacements("105.9 The X WXDX", "u1", "http://dead/x", http=http)
        assert [f.url for f in found] == ["http://new/x"]
        assert http.calls[0][1] == {"uuids": "u1"}

    def test_replacing_keeps_name_band_and_place(self, db):
        with db.session_scope() as s:
            st = add(s, "105.9 The X WXDX", url="http://dead/x")
            st.off_air_since = st.created_at
            st.dial_order = 7
            found = stations._found(self.RAW)
            stations.replace_link(s, st.id, found)
            assert (st.name, st.band, st.dial_order) == ("105.9 The X WXDX", "fm", 7)
            assert st.stream_url == "http://new/x" and st.rb_uuid == "u9"
            assert st.off_air_since is None and st.codec == "AAC"


class TestRadioPage:
    def view(self, ctx):
        from musicmgr.ui.views.tuner import TunerView

        view = TunerView(ctx)
        view.refresh()
        return view

    def test_station_card_shows_frequency_and_hides_move_for_it(self, ctx, db):
        with db.session_scope() as s:
            f, loose = add(s, "102.5 WDVE").id, add(s, "Gold").id
        view = self.view(ctx)
        view.set_band("fm")
        band, idx = view._locate("station", f)
        view.radio.set_tuned(band, idx, animate=False)
        view._update_info()
        assert view.info_sub.text().startswith("102.5 MHz")
        assert not view.btn_move_left.isVisibleTo(view)
        band, idx = view._locate("station", loose)
        view.radio.set_tuned(band, idx, animate=False)
        view._update_info()
        assert view.btn_move_left.isVisibleTo(view)

    def test_off_air_station_is_faded_and_offers_a_new_link(self, ctx, db):
        with db.session_scope() as s:
            sid = add(s, "105.9 The X WXDX").id
            stations.set_off_air(s, sid, True)
        view = self.view(ctx)
        band, idx = view._locate("station", sid)
        assert view.radio._faded[band] == {idx}
        view.set_band(band)
        view.radio.set_tuned(band, idx, animate=False)
        view._update_info()
        assert view.info_title.text().startswith("Off the air")
        assert view.btn_relink.isVisibleTo(view) and view.btn_listen.isVisibleTo(view)

    def test_replacement_dialog_saves_the_chosen_link(self, ctx, db, monkeypatch):
        from musicmgr.ui.views.tuner import ReplacementDialog

        monkeypatch.setattr(ReplacementDialog, "auto_search", False)
        with db.session_scope() as s:
            sid = add(s, "105.9 The X WXDX", url="http://dead/x").id
        dialog = ReplacementDialog(ctx, sid)
        replaced = []
        dialog.replaced.connect(replaced.append)
        dialog.show_results([stations._found(TestNewLink.RAW)])
        assert dialog.use_btn.isEnabled()
        dialog._use()
        assert replaced == [sid]
        with db.session_scope() as s:
            assert s.get(RadioStation, sid).stream_url == "http://new/x"

    def test_eye_follows_the_player_only_for_stations(self, ctx, db):
        view = self.view(ctx)
        ctx.tuner._set_tuned("station", 1)
        ctx.player._set_signal("buffering")
        assert view.radio.signal == radio_dial.SIGNAL_BUFFERING
        ctx.tuner._set_tuned("show", 1)
        assert view.radio.signal == radio_dial.SIGNAL_NONE
        ctx.player._set_signal("none")


class TestPlayerBarLive:
    @pytest.fixture
    def bar(self, ctx, monkeypatch):
        monkeypatch.setattr(player_bar_module, "SPECTRUM_ENABLED", False)
        return PlayerBar(ctx.player, ctx)

    def stream(self):
        return QueueItem(track_id=0, title="105.9 The X WXDX", artist="Internet radio",
                         album="US · AAC", path="http://127.0.0.1:9/x", kind="stream",
                         station_id=1, hydrated=True)

    def test_live_station_hides_seek_and_skips_and_shows_the_badge(self, bar):
        bar.on_track_changed(self.stream())
        for w in (bar.shuffle_btn, bar.back_btn, bar.fwd_btn, bar.repeat_btn,
                  bar.seek, bar.elapsed, bar.remaining):
            assert not w.isVisibleTo(bar)
        assert bar.live_badge.isVisibleTo(bar)
        assert bar.prev_btn.isVisibleTo(bar) and bar.next_btn.toolTip() == "Next station"
        track = QueueItem(track_id=1, title="Song", artist="A", album="B", path="x.mp3",
                          hydrated=True)
        bar.on_track_changed(track)
        assert bar.seek.isVisibleTo(bar) and bar.shuffle_btn.isVisibleTo(bar)
        assert not bar.live_badge.isVisibleTo(bar)

    def test_next_steps_along_the_band_while_live(self, ctx, bar, monkeypatch):
        steps, nexts = [], []
        monkeypatch.setattr(ctx.tuner, "step_station", lambda d: steps.append(d) or True)
        monkeypatch.setattr(ctx.player, "next", lambda user_initiated=False: nexts.append(1))
        bar.on_track_changed(self.stream())
        bar.next_btn.click()
        bar.prev_btn.click()
        assert steps == [1, -1] and nexts == []
        bar.on_track_changed(None)
        bar.next_btn.click()
        assert nexts == [1]
