"""The SXM band (2026-10-21): SiriusXM channels on the radio dial that open
in the web browser - services/stations.py, services/tuner.py, the dial's
tap detection (ui/widgets/radio_dial.py) and the Radio page."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from musicmgr.db.models import RadioStation
from musicmgr.services import stations
from musicmgr.services.player import PlayerController
from musicmgr.services.tuner import TunerController
from musicmgr.ui.widgets import radio_dial

CH7 = "https://www.siriusxm.com/player/channel-linear/entity/70s-on-7"
CH6 = "https://www.siriusxm.com/player/channel-linear/entity/60s-on-6"
#: what the address bar shows while a channel plays (PC1, 2026-10-21)
LIST_PAGE = ("https://www.siriusxm.com/player/curated-grouping/all-channels/"
             "403ab6a5-d3c9-4c2a-a722-a94a6a5fd056/3JoBfOCIwo6FmTpzM1S2H7/all")


@pytest.fixture
def opened(monkeypatch):
    """Links the tuner 'opened in the browser' (none really are)."""
    urls: list[str] = []
    monkeypatch.setattr(TunerController, "open_url", staticmethod(lambda u: urls.append(u) or True))
    return urls


@pytest.fixture
def tuner(ctx, monkeypatch):
    monkeypatch.setattr(PlayerController, "auto_measure", False)
    yield ctx.tuner
    ctx.player.clear_queue()


def _view(ctx):
    from musicmgr.ui.views.tuner import TunerView

    view = TunerView(ctx)
    view.refresh()
    return view


# -- services ---------------------------------------------------------------


@pytest.mark.parametrize("url, ok", [
    (CH7, True),
    ("https://siriusxm.com/player", True),
    ("https://player.siriusxm.ca/channel/x", True),
    ("  " + CH7 + "  ", True),
    ("https://www.notsiriusxm.com/x", False),
    ("https://siriusxm.com.evil.example/x", False),
    ("ftp://www.siriusxm.com/x", False),
    ("70s on 7", False),
    ("", False),
])
def test_is_sxm_link(url, ok):
    assert stations.is_sxm_link(url) is ok


def test_only_a_channels_own_page_is_a_channel_link():
    # The Highway's link, from the All channels page on PC1
    assert stations.is_sxm_channel_link(
        "https://www.siriusxm.com/player/channel-linear/entity/ffd94bb6-5368-67ae-91be-5b2bababeca0")
    assert stations.is_sxm_link(LIST_PAGE) and not stations.is_sxm_channel_link(LIST_PAGE)
    assert not stations.is_sxm_channel_link(stations.SXM_PLAYER_URL)


def test_sxm_channels_have_their_own_band(db):
    with db.session_scope() as s:
        stations.add_manual(s, "WQED", "http://127.0.0.1:9/a")
        ch = stations.add_sxm(s, "70s on 7", CH7)
        assert ch.band == "sxm" and stations.opens_in_browser(ch)
        assert stations.station_band(ch) == "sxm"
        assert [x.name for x in stations.band_stations(s, "sxm")] == ["70s on 7"]
        assert [x.name for x in stations.band_stations(s, "fm")] == ["WQED"]
        assert stations.dial_positions("sxm", ["a", "b"]) == stations.even_positions(2)


@pytest.mark.parametrize("name, channel", [
    ("56 - The Highway", 56), ("Ch 56 The Highway", 56), ("Channel 7: 70s on 7", 7),
    ("150 - Last", 150), ("301 - Past the dial", 150),
    ("70s on 7", None), ("1st Wave", None), ("The Highway", None),
])
def test_channel_number_from_the_name(name, channel):
    got = stations.frequency(name, "sxm")
    assert got == (None if channel is None else float(channel))


@pytest.mark.parametrize("name, shown", [
    ("56 - The Highway", "The Highway"), ("Ch 56: The Highway", "The Highway"),
    ("70s on 7", "70s on 7"), ("150", "150"),
])
def test_glass_leaves_the_channel_number_to_the_scale(name, shown):
    assert stations.glass_label("sxm", name) == shown
    assert stations.glass_label("fm", "102.5 WDVE") == "102.5 WDVE"


def test_channels_sit_at_their_number_on_a_1_to_150_scale():
    """2026-10-21 - James: "make the scale 150 channels equivalent to
    whats in a car"."""
    assert stations.SXM_MARKS[0] == 1 and stations.SXM_MARKS[-1] == 150
    lo, hi = stations.SCALE_LO, stations.SCALE_HI
    pos = stations.dial_positions("sxm", ["1 - First", "150 - Last", "Elvis Radio"])
    assert pos[0] == pytest.approx(lo) and pos[1] == pytest.approx(hi)
    mid = stations.dial_positions("sxm", ["75 - Middle"])[0]
    assert mid == pytest.approx((lo + hi) / 2)


def test_sxm_is_the_bottom_band_and_shorter(qapp):
    w = radio_dial.RadioSet()
    w.resize(1100, 560)
    w._relayout()
    strips = w._geom["strips"]
    assert radio_dial.BAND_KEYS[-1] == "sxm"
    assert strips["sxm"].top() > strips["lw"].bottom()
    assert strips["sxm"].height() == pytest.approx(strips["fm"].height() * 0.8)
    assert strips["sxm"].height() < strips["fm"].height()
    assert strips["sxm"].bottom() <= w._geom["dial"].bottom()


# -- the tuner ------------------------------------------------------------------


def test_tuning_an_sxm_channel_opens_the_browser_and_plays_nothing(ctx, tuner, db, opened):
    with db.session_scope() as s:
        sid = stations.add_sxm(s, "70s on 7", CH7).id
    seen = []
    tuner.browserStationOpened.connect(seen.append)

    assert tuner.tune_station(sid)

    assert opened == [CH7] and seen == [sid]
    assert ctx.player.queue == [] and tuner.tuned == ("", 0)


def test_a_station_playing_here_stops_for_sxm(ctx, tuner, db, opened):
    with db.session_scope() as s:
        fm = stations.add_manual(s, "Test FM", "http://127.0.0.1:9/stream").id
        sid = stations.add_sxm(s, "70s on 7", CH7).id
    tuner.tune_station(fm)
    assert tuner.tuned == ("station", fm)

    tuner.tune_station(sid)

    assert tuner.tuned == ("", 0) and not ctx.player.is_playing()


def test_music_playing_here_is_paused_for_sxm(ctx, tuner, db, opened, monkeypatch):
    with db.session_scope() as s:
        sid = stations.add_sxm(s, "70s on 7", CH7).id
    paused = []
    monkeypatch.setattr(ctx.player, "is_playing", lambda: True)
    monkeypatch.setattr(ctx.player, "pause", lambda: paused.append(True))
    tuner.tune_station(sid)
    assert paused == [True]


def test_no_browser_says_so(ctx, tuner, db, monkeypatch):
    monkeypatch.setattr(TunerController, "open_url", staticmethod(lambda u: False))
    with db.session_scope() as s:
        sid = stations.add_sxm(s, "70s on 7", CH7).id
    said = []
    tuner.notified.connect(said.append)
    assert not tuner.tune_station(sid)
    assert said == ["Couldn't open the web browser"]


# -- the dial: a tap, not a sweep -------------------------------------------------


def _dial(qapp):
    w = radio_dial.RadioSet()
    w.resize(1100, 560)
    w._relayout()
    w.set_entries("sxm", ["60s on 6", "70s on 7", "80s on 8"])
    w.set_band("sxm", animate=False)
    w.set_tuned("sxm", 0, animate=False)
    return w


def _x_of(w, index: int) -> float:
    return w._x_at(stations.even_positions(3)[index])


def test_tapping_a_channel_is_a_tap(qapp):
    w = _dial(qapp)
    asked = []
    w.tuneRequested.connect(lambda band, i: asked.append((band, i, w.last_tune_was_tap)))
    y = w._geom["strips"]["sxm"].center().y()
    QTest.mouseClick(w, Qt.LeftButton, pos=QPointF(_x_of(w, 2), y).toPoint())
    assert _wait(lambda: asked) and asked[-1] == ("sxm", 2, True)


def test_dragging_or_stepping_is_not_a_tap(qapp):
    w = _dial(qapp)
    asked = []
    w.tuneRequested.connect(lambda band, i: asked.append((band, i, w.last_tune_was_tap)))
    y = w._geom["strips"]["sxm"].center().y()
    # let go well away from where the press started: a drag, whichever
    # channel the pointer settles on (headless, no move events carry it)
    QTest.mousePress(w, Qt.LeftButton, pos=QPointF(_x_of(w, 0) + 4, y).toPoint())
    QTest.mouseRelease(w, Qt.LeftButton, pos=QPointF(_x_of(w, 1), y).toPoint())
    assert _wait(lambda: asked) and asked[-1][2] is False
    w.step(1)
    assert _wait(lambda: len(asked) == 2) and asked[-1][2] is False


def _wait(cond, ms=3000):
    import time

    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        if cond():
            return True
        QTest.qWait(20)
    return cond()


# -- the Radio page ------------------------------------------------------------------


def test_empty_sxm_band_explains_how_to_add(ctx, tuner):
    view = _view(ctx)
    view.set_band("sxm")
    assert view.status.text().startswith("No SiriusXM channels yet")
    assert view.btn_sxm_add.isVisibleTo(view) and view.btn_sxm_site.isVisibleTo(view)
    assert view.find_btn.text() == "Add a channel…"
    assert not view.btn_scan.isVisibleTo(view)
    view.set_band("fm")
    assert view.find_btn.text() == "Find stations…"


def test_adding_a_channel(ctx, tuner, db):
    view = _view(ctx)
    view.set_band("sxm")
    said = []
    ctx.notified.connect(said.append)
    assert not view.add_sxm("https://example.com/stream", "Nope")
    assert "isn't a SiriusXM link" in said[-1]
    assert not view.add_sxm(LIST_PAGE, "All channels")
    assert "not a channel" in said[-1]
    assert view.add_sxm(CH7, "  70s on 7 ")
    assert view.radio.labels("sxm") == ["70s on 7"]
    with db.session_scope() as s:
        st = s.query(RadioStation).one()
        assert (st.band, st.stream_url) == ("sxm", CH7)


def test_pointing_at_a_channel_opens_nothing_tapping_it_does(ctx, tuner, db, opened):
    with db.session_scope() as s:
        stations.add_sxm(s, "60s on 6", CH6)
        stations.add_sxm(s, "70s on 7", CH7)
    view = _view(ctx)
    view.set_band("sxm")
    view.radio.set_tuned("sxm", 1, animate=False)

    view.radio.last_tune_was_tap = False          # swept / stepped there
    view._on_tune_requested("sxm", 1)
    assert opened == []
    assert view.status.text().startswith("70s on 7")
    assert view.btn_open_browser.isVisibleTo(view)
    assert not view.btn_band.isVisibleTo(view) and not view.btn_listen.isVisibleTo(view)

    view.radio.last_tune_was_tap = True           # tapped
    view._on_tune_requested("sxm", 1)
    assert opened == [CH7]
    assert "playing in your web browser" in view.status.text()


def test_play_in_browser_button_opens_the_pointed_channel(ctx, tuner, db, opened):
    with db.session_scope() as s:
        stations.add_sxm(s, "60s on 6", CH6)
    view = _view(ctx)
    view.set_band("sxm")
    view.radio.set_tuned("sxm", 0, animate=False)
    view._update_info()
    view.btn_open_browser.click()
    assert opened == [CH6]


def test_changing_a_channels_link(ctx, tuner, db):
    with db.session_scope() as s:
        sid = stations.add_sxm(s, "70s on 7", CH7).id
    view = _view(ctx)
    view.set_band("sxm")
    view.radio.set_tuned("sxm", 0, animate=False)
    assert not view.set_sxm_link("http://127.0.0.1:9/x")
    new = CH7 + "?v=2"
    assert view.set_sxm_link(new)
    with db.session_scope() as s:
        assert s.get(RadioStation, sid).stream_url == new


def test_phone_remote_says_where_it_opened(ctx, tuner, db, opened):
    from musicmgr.services.web_remote import RemoteApi

    with db.session_scope() as s:
        sid = stations.add_sxm(s, "70s on 7", CH7).id
    api = RemoteApi(ctx, lambda: "PC1")
    assert api._cmd_tune_station({"id": sid}) == "Opened in the PC's web browser"
    assert opened == [CH7]
