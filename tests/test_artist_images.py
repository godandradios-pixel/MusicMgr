"""Tests for services/artist_images.py's `progress` callback (2026-09-22 -
James: "does the progress bar also work on the import of artist images" -
it didn't; SettingsView.import_artist_images now runs this on a background
thread via ArtistImagesImportThread and needs real progress out of it).
Matching/fuzzy-matching behavior itself isn't covered here - this is only
about the new progress plumbing.
"""

from __future__ import annotations

from musicmgr.services import artist_images
from musicmgr.services import library as lib
from musicmgr.db.models import Track


def make_credited_artist(session, name: str):
    artist = lib.get_or_create_artist(session, name)
    release = lib.get_or_create_release(session, f"{name} Album", name)
    release.album_artist_id = artist.id
    track = Track(release_id=release.id, title="Song", title_key="song", artist_display=name)
    session.add(track)
    session.flush()
    lib.add_credit(session, artist, track=track)
    return artist


class TestImportArtistImagesProgress:
    def test_progress_callback_reports_the_final_total(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(artist_images.config, "ARTIST_IMG_DIR", tmp_path / "artist_img_out")
        make_credited_artist(session, "AC/DC")

        src = tmp_path / "images"
        src.mkdir()
        (src / "AC-DC.jpg").write_bytes(b"not a real image")

        calls = []
        artist_images.import_artist_images(
            session, src, progress=lambda done, total, name: calls.append((done, total, name))
        )

        assert calls
        assert calls[-1][:2] == (1, 1)
        assert calls[-1][2] == "AC-DC.jpg"


class TestImportArtistImagesShouldStop:
    """2026-09-22 same-day follow-up - James: "add a real cancel button
    that stops any process running within Settings". `should_stop`,
    checked once per candidate image, must `break` cleanly: this function
    only flushes once at the very end regardless, so a stop here should
    leave every artist matched *before* the stop with its image_path set,
    and every one after it untouched."""

    def test_should_stop_true_from_the_start_matches_nothing(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(artist_images.config, "ARTIST_IMG_DIR", tmp_path / "out")
        artist = make_credited_artist(session, "AC/DC")
        src = tmp_path / "images"
        src.mkdir()
        (src / "AC-DC.jpg").write_bytes(b"not a real image")

        result = artist_images.import_artist_images(session, src, should_stop=lambda: True)

        assert result.matched == 0
        assert artist.image_path is None

    def test_should_stop_partway_through_keeps_the_earlier_match(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(artist_images.config, "ARTIST_IMG_DIR", tmp_path / "out")
        first = make_credited_artist(session, "AC/DC")
        second = make_credited_artist(session, "Rush")
        src = tmp_path / "images"
        src.mkdir()
        (src / "AC-DC.jpg").write_bytes(b"not a real image")
        (src / "Rush.jpg").write_bytes(b"not a real image either")

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let the first candidate through, stop before the second

        result = artist_images.import_artist_images(session, src, should_stop=should_stop)

        assert result.matched == 1
        # iter_candidates sorts by filename, so "AC-DC.jpg" is always first
        assert first.image_path is not None
        assert second.image_path is None
