"""Now Playing's turntable (2026-09-30 - James: "any way we can incorporate
a visual that shows the album cover, spinning as if on a record player from
the top view?")."""

from __future__ import annotations

import pytest

from musicmgr.ui.widgets.turntable import RPM_78, RPM_LP, SPIN_UP_S, Turntable


def test_spins_up_to_speed_and_winds_down(qapp):
    t = Turntable(200)
    t.set_source(None, "Album")
    t.set_playing(True)
    t.advance(SPIN_UP_S / 2)
    assert 0 < t._speed < RPM_LP * 6
    t.advance(SPIN_UP_S)
    assert t._speed == pytest.approx(RPM_LP * 6)          # 200 degrees a second
    assert t._arm == 1                                     # arm on the record
    t.set_playing(False)
    t.advance(SPIN_UP_S)
    assert t._speed == 0                                   # wound down
    t.advance(2)
    assert t._settled() and t._arm == 0                    # arm back at rest


def test_78s_turn_faster(qapp):
    t = Turntable(200)
    t.set_source(None, "Bluebird", rpm=RPM_78)
    t.set_playing(True)
    t.advance(SPIN_UP_S * 2)
    assert t._speed == pytest.approx(RPM_78 * 6)


def test_arm_moves_inward_as_the_song_plays(qapp):
    t = Turntable(300)
    t.set_source(None, "Album")
    t.set_playing(True)
    t.advance(2)
    t.set_progress(0, 1000)
    start = t._arm_angle()
    t.set_progress(1000, 1000)
    end = t._arm_angle()
    assert end != start
    t.grab()                                              # paints without error


def test_now_playing_toggle_is_remembered(ctx, qapp):
    from musicmgr.ui.views.nowplaying import NowPlayingView

    view = NowPlayingView(ctx)
    assert view.art_stack.currentWidget() is view.turntable        # default
    view.set_turntable_shown(False)
    again = NowPlayingView(ctx)
    assert again.art_stack.currentWidget() is again.cover
    assert [a.text() for a in again._art_menu().actions()] == ["Show turntable"]
