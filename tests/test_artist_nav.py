"""Artist page ‹ › arrows (2026-10-17 - James: "When I'm on an artist page,
I see the breadcrumb Artist > Big Country. I would like a way to click a
right arrow for next artist and < for a previous artist").

The arrows step through artists in the Artists grid's own on-screen order
(sorted by name, narrowed by a live search), grey out at either end, and
keep whatever leading crumb the page was opened with.
"""

from __future__ import annotations

import pytest

from musicmgr.services import library as lib
from musicmgr.ui.views.library import LibraryView


@pytest.fixture
def view(ctx):
    return LibraryView(ctx)


def make_artist(session, name):
    artist = lib.get_or_create_artist(session, name)
    release = lib.get_or_create_release(session, f"{name} album", name)
    release.album_artist_id = artist.id
    session.flush()
    return artist


@pytest.fixture
def artists(session):
    # created out of name order on purpose - the grid sorts by name
    names = ["Rush", "Big Country", "ABBA"]
    made = {n: make_artist(session, n) for n in names}
    session.commit()
    return made


def test_next_and_previous_follow_grid_name_order(view, artists):
    view._open_artist(artists["Big Country"].id)
    panel = view.artist_detail
    assert panel.prev_artist_btn.isEnabled()
    assert panel.next_artist_btn.isEnabled()
    assert panel.next_artist_btn.toolTip() == "Next artist: Rush"
    assert panel.prev_artist_btn.toolTip() == "Previous artist: ABBA"

    panel.next_artist_btn.click()
    assert panel.artist_id == artists["Rush"].id
    assert panel.breadcrumb.path == ["Artists", "Rush"]

    panel.prev_artist_btn.click()
    panel.prev_artist_btn.click()
    assert panel.artist_id == artists["ABBA"].id


def test_arrows_disable_at_the_ends(view, artists):
    view._open_artist(artists["ABBA"].id)
    assert not view.artist_detail.prev_artist_btn.isEnabled()
    assert view.artist_detail.next_artist_btn.isEnabled()

    view._open_artist(artists["Rush"].id)
    assert view.artist_detail.prev_artist_btn.isEnabled()
    assert not view.artist_detail.next_artist_btn.isEnabled()


def test_live_search_narrows_the_stepping_order(view, artists):
    view._load_artists()
    view.artist_grid.set_filter_text("r")  # Rush, Big Country - not ABBA
    view._open_artist(artists["Big Country"].id)
    assert not view.artist_detail.prev_artist_btn.isEnabled()
    view.artist_detail.next_artist_btn.click()
    assert view.artist_detail.artist_id == artists["Rush"].id


def test_stepping_keeps_the_missing_metadata_root(view, artists):
    view._open_artist_from_missing_metadata(artists["ABBA"].id)
    view.artist_detail.next_artist_btn.click()
    assert view.artist_detail.artist_id == artists["Big Country"].id
    assert view.artist_detail.breadcrumb.path == ["Missing metadata", "Big Country"]


# -- album page (2026-10-17 - James: "should we do the same on the Album
# < and >") ---------------------------------------------------------------


@pytest.fixture
def albums(session):
    """Two AC/DC albums and one by Rush - the Albums grid sorts by artist,
    then year, then title."""
    made = {}
    for artist_name, title, year in [
        ("Rush", "Moving Pictures", 1981),
        ("AC/DC", "Powerage", 1978),
        ("AC/DC", "Let There Be Rock", 1977),
    ]:
        artist = lib.get_or_create_artist(session, artist_name)
        release = lib.get_or_create_release(session, title, artist_name, year=year)
        release.album_artist_id = artist.id
        made[title] = release
    session.flush()
    session.commit()
    return made


def test_album_arrows_follow_albums_grid_order(view, albums):
    view.select_mode("album")
    view.album_grid.tileActivated.emit(albums["Let There Be Rock"].id)
    page = view.album_detail
    arrows = page.neighbor_arrows
    assert not arrows.isHidden()
    assert not arrows.prev_btn.isEnabled()
    assert arrows.next_btn.toolTip() == "Next album: Powerage"

    arrows.next_btn.click()
    assert page.release_id == albums["Powerage"].id
    assert page.breadcrumb.path == ["Albums", "Powerage"]
    arrows.next_btn.click()
    assert page.release_id == albums["Moving Pictures"].id
    assert not arrows.next_btn.isEnabled()


def test_album_arrows_from_artist_page_stay_within_that_artist(view, albums):
    acdc = albums["Powerage"].album_artist_id
    view._open_artist(acdc)
    view.artist_detail.releaseActivated.emit(albums["Let There Be Rock"].id)
    arrows = view.album_detail.neighbor_arrows
    arrows.next_btn.click()
    assert view.album_detail.release_id == albums["Powerage"].id
    assert view.album_detail.breadcrumb.path == ["Artists", "AC/DC", "Powerage"]
    # Moving Pictures is Rush's - not next in AC/DC's list
    assert not arrows.next_btn.isEnabled()


def test_album_arrows_hidden_when_opened_from_now_playing(view, albums):
    view._open_release_from_elsewhere(albums["Powerage"].id)
    assert view.album_detail.neighbor_arrows.isHidden()
