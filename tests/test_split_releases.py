"""Albums split in two by their catalog number (2026-10-21 - James: "why
does it think there are 2 different albums for Jordin Sparks"): the first
copy stored "88697187522, 88697187522" from before the scanner dropped a
repeated number (10-16); the same files read again gave "88697187522", no
longer matched, and made a second album. library.same_catalog compares
the numbers inside instead, and merge_split_releases puts the 9 albums
already split on PC1 back together."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from musicmgr.db.models import MediaFile, Release, Track
from musicmgr.services import library as lib


@pytest.mark.parametrize("a, b, same", [
    ("88697187522, 88697187522", "88697187522", True),
    ("CDP-7243-8-29689-2-4", "CDP-7243-8-29689-2-4, D 106878", True),
    ("822 552‐2 M‐1", "822552-2 m-1", True),
    (None, "ANR-1-603", True),
    ("", "ANR-1-603", True),
    ("ANR-1-603", "314 534 625-2", False),
])
def test_same_catalog(a, b, same):
    assert lib.same_catalog(a, b) is same


def _album(session, title, artist, catalog, folder, songs):
    artist_row = lib.get_or_create_artist(session, artist)
    release = Release(title=title, title_key=title.lower(), artist_display=artist,
                      catalog_number=catalog, album_artist_id=artist_row.id)
    session.add(release)
    session.flush()
    for n, song in enumerate(songs, 1):
        track = Track(release_id=release.id, title=song, title_key=song.lower(),
                      artist_display=artist, track_no=n)
        session.add(track)
        session.flush()
        session.add(MediaFile(track_id=track.id, path=f"{folder}/{song}.mp3"))
    session.flush()
    return release


def test_a_repeated_catalog_number_finds_the_album_already_there(session):
    first = _album(session, "Jordin Sparks", "Jordin Sparks", "88697187522, 88697187522",
                   "D:/Music/Jordin Sparks/Jordin Sparks", ["Save Me"])
    again = lib.get_or_create_release(session, "Jordin Sparks", "Jordin Sparks",
                                      catalog_number="88697187522")
    assert again.id == first.id
    other = lib.get_or_create_release(session, "Jordin Sparks", "Jordin Sparks",
                                      catalog_number="12345")
    assert other.id != first.id


def test_split_album_goes_back_together(session):
    folder = "D:/Music/Jordin Sparks/Jordin Sparks"
    old = _album(session, "Jordin Sparks", "Jordin Sparks", "88697187522, 88697187522", folder,
                 ["Virginia Is For Lovers", "Save Me", "Worth The Wait"])
    old.cover_path = "jordin.jpg"
    new = _album(session, "Jordin Sparks", "Jordin Sparks", "88697187522", folder,
                 ["Tattoo", "No Air"])
    old_id, new_id = old.id, new.id

    assert lib.merge_split_releases(session) == 1

    assert session.get(Release, new_id) is None
    kept = session.get(Release, old_id)
    assert sorted(t.title for t in kept.tracks) == [
        "No Air", "Save Me", "Tattoo", "Virginia Is For Lovers", "Worth The Wait"]
    assert kept.catalog_number == "88697187522" and kept.cover_path == "jordin.jpg"
    assert lib.merge_split_releases(session) == 0


def test_different_pressings_and_different_folders_stay_apart(session):
    _album(session, "Caress of Steel", "Rush", "ANR-1-603", "D:/Music/Rush/Caress of Steel", ["Bastille Day"])
    _album(session, "Caress of Steel", "Rush", "314 534 625-2", "D:/Music/Rush/Caress of Steel", ["Lakeside Park"])
    _album(session, "Greatest Hits", "Rush", "X1", "D:/Music/Rush/Greatest Hits", ["Limelight"])
    _album(session, "Greatest Hits", "Rush", "X1", "D:/Music/Rush/Greatest Hits (2)", ["Tom Sawyer"])
    assert lib.merge_split_releases(session) == 0
    assert len(session.scalars(select(Release)).all()) == 4
