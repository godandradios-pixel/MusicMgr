"""Tracks search finds featured artists (2026-10-22 - James's deferred
"Tracks search can't find a featured artist")."""

from __future__ import annotations

from musicmgr.db.models import Credit, Release, Track
from musicmgr.services import library as lib
from musicmgr.ui.widgets.album_tracks import KIND_ALBUM, AlbumTracksPanel


def _track(session, album: str, album_artist: str, title: str, credited: list[str]) -> Track:
    release = session.query(Release).filter_by(title=album).one_or_none()
    if release is None:
        release = Release(title=album, title_key=album.lower(), artist_display=album_artist)
        session.add(release)
        session.flush()
    track = Track(release_id=release.id, title=title, title_key=title.lower(),
                  artist_display=" feat. ".join(credited))
    session.add(track)
    session.flush()
    for i, name in enumerate(credited):
        lib.add_credit(session, lib.get_or_create_artist(session, name),
                       Credit.ROLE_MAIN if i == 0 else Credit.ROLE_FEATURING, track=track, position=i)
    return track


def _visible(panel) -> list[tuple[str, list[str]]]:
    return [(b.title, [t.title for t in b.tracks]) for b in panel._blocks if b.kind == KIND_ALBUM]


def _panel(ctx, session):
    session.commit()
    panel = AlbumTracksPanel(ctx)
    panel.load()
    return panel


def test_featured_guest_finds_the_track(ctx, session):
    _track(session, "Confessions", "Usher", "Yeah!", ["Usher", "Lil Jon", "Ludacris"])
    _track(session, "Confessions", "Usher", "Burn", ["Usher"])
    panel = _panel(ctx, session)
    panel.set_filter_text("ludacris")
    assert _visible(panel) == [("Confessions", ["Yeah!"])]


def test_album_artist_is_still_not_a_search_target(ctx, session):
    _track(session, "Confessions", "Usher", "Yeah!", ["Usher", "Ludacris"])
    _track(session, "Confessions", "Usher", "Burn", ["Usher"])
    panel = _panel(ctx, session)
    panel.set_filter_text("usher")
    assert _visible(panel) == []


def test_compilation_performer_is_found(ctx, session):
    _track(session, "Now 12", "Various Artists", "Hot in Herre", ["Nelly"])
    _track(session, "Now 12", "Various Artists", "Angel", ["Shaggy"])
    panel = _panel(ctx, session)
    panel.set_filter_text("nelly")
    assert _visible(panel) == [("Now 12", ["Hot in Herre"])]
    panel.set_filter_text("")
    assert _visible(panel) == [("Now 12", ["Hot in Herre", "Angel"])]
