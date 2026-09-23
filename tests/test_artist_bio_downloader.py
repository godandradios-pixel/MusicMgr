"""Tests for services/artist_bio_downloader.py's `should_stop` plumbing.

2026-09-22 - James: "add a real cancel button that stops any process
running within Settings". `download_bios_for_artists` is network-bound
(one Wikipedia lookup per artist), so these tests monkeypatch
`download_bio_for_artist` itself rather than hitting the network - the
same "swap the one per-item function, keep the real bulk loop" shape
tests/test_video_scanner.py's flaky-import test already uses. Nothing
else about this module (the actual Wikipedia lookup/matching logic) is
covered here.
"""

from __future__ import annotations

from musicmgr.db.models import Artist
from musicmgr.services import artist_bio_downloader as bio_dl


def make_artist(session, name: str) -> Artist:
    artist = Artist(name=name, name_key=name.lower())
    session.add(artist)
    session.flush()
    return artist


class TestDownloadBiosForArtistsShouldStop:
    def test_should_stop_true_from_the_start_downloads_nothing(self, session, monkeypatch):
        artist = make_artist(session, "AC/DC")
        monkeypatch.setattr(
            bio_dl, "download_bio_for_artist",
            lambda http, db, a, **k: bio_dl.ArtistBioOutcome(a.id, a.name, "downloaded"),
        )

        result = bio_dl.download_bios_for_artists([artist.id], should_stop=lambda: True)

        assert result.outcomes == []
        session.refresh(artist)
        assert artist.profile is None

    def test_should_stop_partway_through_keeps_the_earlier_commit(self, session, monkeypatch):
        first = make_artist(session, "AC/DC")
        second = make_artist(session, "Rush")

        def fake_download(http, db, artist, **kwargs):
            artist.profile = f"bio for {artist.name}"
            return bio_dl.ArtistBioOutcome(artist.id, artist.name, "downloaded")

        monkeypatch.setattr(bio_dl, "download_bio_for_artist", fake_download)

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let the first artist through, stop before the second

        result = bio_dl.download_bios_for_artists(
            [first.id, second.id], should_stop=should_stop
        )

        assert len(result.outcomes) == 1
        session.refresh(first)
        session.refresh(second)
        assert first.profile == "bio for AC/DC"
        assert second.profile is None
