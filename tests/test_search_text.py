"""Tests for services/search_text.py: searches ignore accents, case and
curly punctuation - "expose" finds Exposé, "motley crue" finds Mötley Crüe,
"don't" finds "Don’t" - in SQL (mm_fold / fold_ilike) and in the Library
filter boxes."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from musicmgr.db.models import Track
from musicmgr.services import charts, library as lib, playlists
from musicmgr.services.search_text import fold, fold_ilike
from tests.test_charts import make_owned_track


@pytest.mark.parametrize("text,folded", [
    ("Exposé", "expose"),
    ("EXPOSÉ", "expose"),
    ("Mötley Crüe", "motley crue"),
    ("Sigur Rós", "sigur ros"),
    ("Mø", "mo"),
    ("Straße", "strasse"),
    ("Don’t Stop", "don't stop"),
    ("Jay–Z", "jay-z"),
    ("", ""),
    (None, ""),
])
def test_fold(text, folded):
    assert fold(text) == folded


def test_sql_function_is_registered(session):
    make_owned_track(session, "Point of No Return", "Exposé")
    for typed in ("expose", "EXPOSE", "Exposé", "xpos"):
        assert session.scalar(
            select(Track.title).where(fold_ilike(Track.artist_display, f"%{typed}%"))
        ) == "Point of No Return", typed


def test_plain_spelling_in_library_found_by_accented_search(session):
    make_owned_track(session, "Seasons Change", "Expose")
    assert session.scalar(
        select(Track.title).where(fold_ilike(Track.artist_display, "%Exposé%"))) == "Seasons Change"


def test_library_search(session):
    make_owned_track(session, "Kickstart My Heart", "Mötley Crüe")
    found = lib.search(session, "motley crue")
    assert [t.title for t in found["tracks"]] == ["Kickstart My Heart"]
    assert [a.name for a in found["artists"]] == ["Mötley Crüe"]


def test_pickers(session):
    make_owned_track(session, "Let Me Be the One", "Exposé")
    assert [r["title"] for r in playlists.search_tracks(session, artist_query="expose")] == [
        "Let Me Be the One"]
    assert [r["title"] for r in charts.search_tracks_for_match(session, artist_query="expose")] == [
        "Let Me Be the One"]


def test_smart_playlist_rule(session):
    make_owned_track(session, "Let Me Be the One", "Exposé")
    for op, value in (("contains", "xpos"), ("startswith", "expo"), ("is", "EXPOSE")):
        cond = playlists._rule_clause("artist", op, value)
        assert session.scalar(select(Track.title).where(cond)) == "Let Me Be the One", op


def test_title_details_filter_matches_the_songs_own_artist():
    from musicmgr.ui.widgets.track_details_table import TrackDetailRow, TrackDetailsTable

    row = TrackDetailRow(track_id=1, genre="Pop", album_artist="Various Artists",
                         album="1999 Billboard Top Hits", track_no=3,
                         title="It's All About Me", duration_ms=270000, year=1998,
                         rating=None, sort_key="its all about me", artist="Mýa ft. Sisqó")
    assert TrackDetailsTable._row_matches(row, fold("mya"))
    assert TrackDetailsTable._row_matches(row, fold("Sisqo"))
    assert not TrackDetailsTable._row_matches(row, fold("shaggy"))
