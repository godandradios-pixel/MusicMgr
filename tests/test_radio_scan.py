"""The Radio page's Scan button (2026-10-04 - James: "let's add that
station scan feature", from the radio ideas: "Plays each station on the
band for about 8 seconds, like the scan button on a car radio. Press it
again to stay on the current station.").

ui/views/tuner.py sweeps the pointer to the next station along the lit
band (RadioSet.scan_to, through the static), holds it SCAN_HOLD_MS once
its stream comes in, passes over a station that won't come in after
SCAN_GIVE_UP_MS and stations that are off the air, wraps round at the end
of the band, and stops when the button is pressed again, when the
listener moves the dial or changes band, or when something else is tuned.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from musicmgr.db.models import RadioStation
from musicmgr.services import stations
from musicmgr.services.player import PlayerController
from musicmgr.ui.views import tuner as tuner_view
from musicmgr.ui.widgets import radio_dial

NAMES = ["102.5 WDVE", "100.7 WMMS", "105.9 The X", "93.7 KDKA"]


@pytest.fixture
def page(ctx, db, monkeypatch):
    monkeypatch.setattr(PlayerController, "auto_measure", False)
    ids = {}
    with db.session_scope() as s:
        for i, name in enumerate(NAMES):
            ids[name] = stations.add_manual(s, name, f"http://127.0.0.1:9/{i}", band="fm").id
    view = tuner_view.TunerView(ctx)
    view.resize(1200, 700)
    view.refresh()
    view.radio.set_band("fm", animate=False)
    view.ids = ids
    yield view
    view.stop_scan()


def settle(view):
    """Let the pointer's sweep finish and the station tune in."""
    radio = view.radio
    radio._anim.setCurrentTime(radio._anim.duration())
    radio._tune_timer.stop()
    radio._emit_tune()


def tuned_name(view):
    kind, ident = view.ctx.tuner.tuned
    assert kind == "station"
    return next(n for n, i in view.ids.items() if i == ident)


def hold_and_move_on(view):
    view.ctx.player.signalChanged.emit("good")
    assert view._scan_timer.interval() == tuner_view.SCAN_HOLD_MS
    view._scan_timer.stop()
    view._scan_next()
    settle(view)


class TestScan:
    def test_button_only_on_a_station_band_with_two_or_more(self, page):
        assert page.can_scan()
        page._update_info()
        assert not page.btn_scan.isHidden()
        page.set_band("sw1")
        assert not page.can_scan()
        assert page.btn_scan.isHidden()

    def test_plays_each_station_in_dial_order_and_wraps(self, page):
        # dial order by frequency: 93.7, 100.7, 102.5, 105.9
        page.ctx.tuner.tune_station(page.ids["100.7 WMMS"])
        page.start_scan()
        assert page.scanning and page.btn_scan.text() == tuner_view.SCAN_STOP_LABEL
        settle(page)
        assert tuned_name(page) == "102.5 WDVE"
        hold_and_move_on(page)
        assert tuned_name(page) == "105.9 The X"
        hold_and_move_on(page)
        assert tuned_name(page) == "93.7 KDKA"           # round from the top end
        assert page.scanning

    def test_holds_only_once_the_stream_comes_in(self, page):
        page.start_scan()
        settle(page)
        assert page._scan_waiting
        assert page._scan_timer.interval() == tuner_view.SCAN_GIVE_UP_MS
        page.ctx.player.signalChanged.emit("buffering")
        assert page._scan_timer.interval() == tuner_view.SCAN_GIVE_UP_MS
        page.ctx.player.signalChanged.emit("good")
        assert not page._scan_waiting
        assert page._scan_timer.interval() == tuner_view.SCAN_HOLD_MS

    def test_a_station_that_wont_come_in_is_passed_over(self, page):
        page.start_scan()
        settle(page)
        first = tuned_name(page)
        page._scan_timer.stop()
        page._scan_next()                                # gave up waiting
        settle(page)
        assert tuned_name(page) != first and page.scanning

    def test_a_refused_stream_moves_on_quickly(self, page):
        page.start_scan()
        settle(page)
        page.ctx.player.signalChanged.emit("lost")
        assert page._scan_timer.interval() == tuner_view.SCAN_LOST_MS

    def test_skips_stations_off_the_air(self, page, db):
        with db.session_scope() as s:
            s.get(RadioStation, page.ids["102.5 WDVE"]).off_air_since = datetime.now()
            s.commit()
        page._changed()
        page.ctx.tuner.tune_station(page.ids["100.7 WMMS"])
        page.start_scan()
        settle(page)
        assert tuned_name(page) == "105.9 The X"

    def test_press_again_stays_on_the_station(self, page):
        page.start_scan()
        settle(page)
        here = tuned_name(page)
        page.toggle_scan()
        assert not page.scanning and not page._scan_timer.isActive()
        assert page.btn_scan.text() == tuner_view.SCAN_LABEL
        assert tuned_name(page) == here

    def test_moving_the_dial_by_hand_stops_it(self, page):
        page.start_scan()
        settle(page)
        page.radio.userTuned.emit()
        assert not page.scanning

    def test_changing_band_stops_it(self, page):
        page.start_scan()
        page.set_band("am")
        assert not page.scanning

    def test_tuning_something_else_stops_it(self, page):
        page.start_scan()
        settle(page)
        others = [i for i in page.ids.values() if (("station", i) != page._scan_expect)]
        page.ctx.tuner.tune_station(others[0])           # e.g. the player bar's next
        assert not page.scanning

    def test_pausing_stops_it(self, page):
        page.start_scan()
        page._on_state("paused")
        assert not page.scanning

    def test_status_says_scanning_with_the_station_name(self, page):
        page.start_scan()
        settle(page)
        assert page.status.text().startswith("Scanning — ")
        assert tuned_name(page) in page.status.text()


class TestDialHelpers:
    def test_next_in_dial_order_wraps_and_skips(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("fm", NAMES)                       # dial order: 3, 1, 0, 2
        w.set_band("fm", animate=False)
        assert w.next_in_dial_order(3) == 1
        assert w.next_in_dial_order(2) == 3              # wraps round
        assert w.next_in_dial_order(1, skip=[0]) == 2
        assert w.next_in_dial_order(-1) == 3             # nothing tuned: from the left
        assert w.next_in_dial_order(0, skip=[1, 2, 3]) == -1

    def test_user_tuning_is_reported(self, qapp):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        w = radio_dial.RadioSet()
        w.resize(1100, 560)
        w.set_entries("fm", NAMES)
        w.set_band("fm", animate=False)
        got = []
        w.userTuned.connect(lambda: got.append(1))
        w.show()
        QTest.keyClick(w, Qt.Key_Right)
        assert got
        got.clear()
        w._relayout()
        QTest.mouseClick(w, Qt.LeftButton, pos=w._geom["knob_vol"][0].toPoint())
        assert not got                                   # the volume knob isn't tuning
