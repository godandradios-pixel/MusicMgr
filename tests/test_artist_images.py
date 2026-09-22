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
