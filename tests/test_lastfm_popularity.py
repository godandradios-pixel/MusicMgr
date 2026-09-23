"""Tests for services/lastfm_popularity.py's `should_stop` plumbing.

2026-09-22 - James: "add a real cancel button that stops any process
running within Settings". `update_popularity_for_artists` is network-bound
(one Last.fm lookup per artist), so these tests monkeypatch
`update_popularity_for_artist` itself rather than hitting the network -
same shape tests/test_artist_bio_downloader.py's own should_stop tests
use for the sibling bulk downloader. Nothing else about this module (the
actual Last.fm lookup/storage logic) is covered here.
"""

from __future__ import annotations

from musicmgr.db.models import Artist
from musicmgr.services import lastfm_popularity as popularity_dl


def make_artist(session, name: str) -> Artist:
    artist = Artist(name=name, name_key=name.lower())
    session.add(artist)
    session.flush()
    return artist


class TestUpdatePopularityForArtistsShouldStop:
    def test_should_stop_true_from_the_start_updates_nothing(self, session, monkeypatch):
        artist = make_artist(session, "AC/DC")
        popularity_dl.set_api_key(session, "fake-key")
        session.flush()
        monkeypatch.setattr(
            popularity_dl, "update_popularity_for_artist",
            lambda http, key, db, a: popularity_dl.ArtistPopularityOutcome(
                a.id, a.name, "updated", stored=5
            ),
        )

        result = popularity_dl.update_popularity_for_artists(
            [artist.id], should_stop=lambda: True
        )

        assert result.outcomes == []

    def test_should_stop_partway_through_keeps_the_earlier_update(self, session, monkeypatch):
        first = make_artist(session, "AC/DC")
        second = make_artist(session, "Rush")
        popularity_dl.set_api_key(session, "fake-key")
        session.flush()

        updated_ids: list[int] = []

        def fake_update(http, key, db, artist):
            updated_ids.append(artist.id)
            return popularity_dl.ArtistPopularityOutcome(
                artist.id, artist.name, "updated", stored=1
            )

        monkeypatch.setattr(popularity_dl, "update_popularity_for_artist", fake_update)

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let the first artist through, stop before the second

        result = popularity_dl.update_popularity_for_artists(
            [first.id, second.id], should_stop=should_stop
        )

        assert len(result.outcomes) == 1
        assert updated_ids == [first.id]
