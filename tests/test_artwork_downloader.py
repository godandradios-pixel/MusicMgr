"""Tests for services/artwork_downloader.py's `should_stop` plumbing.

2026-09-22 - James: "add a real cancel button that stops any process
running within Settings". `search_artwork_for_releases` is network-bound
(one Discogs lookup per release), so these tests monkeypatch
`search_artwork_candidates`/`apply_artwork_candidate` rather than hitting
the network - same shape tests/test_artist_bio_downloader.py's own
should_stop tests use for the sibling bulk downloaders. Nothing else about
this module (the actual Discogs lookup/apply logic) is covered here.
"""

from __future__ import annotations

from musicmgr.services import artwork_downloader as artwork_dl
from musicmgr.services import library as lib


class TestSearchArtworkForReleasesShouldStop:
    def test_should_stop_true_from_the_start_searches_nothing(self, session, monkeypatch):
        release = lib.get_or_create_release(session, "Album", "AC/DC")
        session.flush()
        artwork_dl.set_api_token(session, "fake-token")
        session.flush()
        monkeypatch.setattr(
            artwork_dl, "search_artwork_candidates", lambda *a, **k: ["candidate"]
        )
        monkeypatch.setattr(
            artwork_dl, "apply_artwork_candidate",
            lambda http, db, r, candidate: artwork_dl.ArtworkOutcome(r.id, r.title, "applied"),
        )

        result = artwork_dl.search_artwork_for_releases(
            [release.id], should_stop=lambda: True
        )

        assert result.outcomes == []
        session.refresh(release)
        assert release.cover_path is None

    def test_should_stop_partway_through_keeps_the_earlier_apply(self, session, monkeypatch):
        first = lib.get_or_create_release(session, "Album One", "AC/DC")
        second = lib.get_or_create_release(session, "Album Two", "Rush")
        session.flush()
        artwork_dl.set_api_token(session, "fake-token")
        session.flush()

        monkeypatch.setattr(
            artwork_dl, "search_artwork_candidates", lambda *a, **k: ["candidate"]
        )

        def fake_apply(http, db, release, candidate):
            release.cover_path = f"/covers/{release.id}.jpg"
            return artwork_dl.ArtworkOutcome(release.id, release.title, "applied")

        monkeypatch.setattr(artwork_dl, "apply_artwork_candidate", fake_apply)

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let the first release through, stop before the second

        result = artwork_dl.search_artwork_for_releases(
            [first.id, second.id], should_stop=should_stop
        )

        assert len(result.outcomes) == 1
        session.refresh(first)
        session.refresh(second)
        assert first.cover_path == f"/covers/{first.id}.jpg"
        assert second.cover_path is None
