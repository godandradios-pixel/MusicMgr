"""Right-click "Remove from library" for songs with no file left
(2026-10-30 - James couldn't get rid of the artist "2000-09": its one song's
file was renamed, and the old song had plays, so the missing-file cleanup
kept it and nothing in the app could remove it). services/track_removal.py.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import (
    Artist, Chart, ChartEntry, ChartIssue, JukeboxSlot, PlayEvent, Playlist, PlaylistItem,
    Release, Track,
)
from musicmgr.services import scanner, track_removal
from musicmgr.ui.views.library import LibraryView, PANE_ARTIST_DETAIL, PANE_ARTIST_GRID

from tests.test_scanner import make_silent_wav


def _scan(session, tmp_path, *rel):
    for r in rel:
        make_silent_wav(tmp_path / r)
    scanner.scan_folder(session, tmp_path)


def _gone(session, tmp_path, rel):
    (tmp_path / rel).unlink()
    scanner.mark_missing_files(session)


def _track(session, title):
    return session.scalar(select(Track).where(Track.title == title))


def test_removes_a_fileless_song_with_all_its_history(session, tmp_path):
    _scan(session, tmp_path, "2000-09/2008/Old Song.wav", "Keeper/Album/01 Stays.wav")
    old = _track(session, "Old Song")
    stays = _track(session, "Stays")
    artist_id = session.scalar(select(Artist.id).where(Artist.name == "2000-09"))
    playlist = Playlist(name="Favorites", updated_at=dt.datetime(2020, 1, 1))
    session.add(playlist)
    session.flush()
    session.add_all([
        PlayEvent(track_id=old.id), PlayEvent(track_id=old.id),
        PlaylistItem(playlist_id=playlist.id, track_id=old.id, position=0),
        PlaylistItem(playlist_id=playlist.id, track_id=stays.id, position=1),
        JukeboxSlot(slot_number=1, artist_id=artist_id, side_a_track_id=old.id),
    ])
    chart = Chart(name="C", slug="c")
    session.add(chart)
    session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2008, 1, 1))
    session.add(issue)
    session.flush()
    session.add(ChartEntry(issue_id=issue.id, rank=1, title="Old Song", artist_name="x",
                           title_key="old song", artist_key="x", track_id=old.id,
                           match_locked=True))
    session.flush()
    _gone(session, tmp_path, "2000-09/2008/Old Song.wav")
    old_id, release_id = old.id, old.release_id
    assert track_removal.is_removable(session, old_id)

    result = track_removal.remove_fileless_tracks(session, [old_id])

    assert (result.removed, result.moved, result.skipped) == (1, 0, 0)
    assert result.albums == 1 and result.artists == 1
    assert session.get(Track, old_id) is None
    assert session.get(Release, release_id) is None
    assert session.get(Artist, artist_id) is None
    assert session.scalar(select(func.count(PlayEvent.id))) == 0
    assert [i.track_id for i in session.scalars(select(PlaylistItem))] == [stays.id]
    assert session.get(Playlist, playlist.id).updated_at.year > 2020
    assert session.scalar(select(ChartEntry.track_id)) is None
    assert session.scalar(select(func.count(JukeboxSlot.id))) == 0
    assert session.get(Track, stays.id) is not None
    assert "Removed 1 song" in result.summary()


def test_a_song_with_a_file_is_never_removed(session, tmp_path):
    _scan(session, tmp_path, "Artist/Album/01 Song.wav")
    track = _track(session, "Song")
    assert not track_removal.is_removable(session, track.id)

    result = track_removal.remove_fileless_tracks(session, [track.id])

    assert (result.removed, result.skipped) == (0, 1)
    assert session.get(Track, track.id) is not None


def test_history_moves_to_a_plain_live_copy(session, tmp_path):
    _scan(session, tmp_path, "Artist/Album/01 Song.wav", "Artist/Album/02 Song.wav")
    old, live = session.scalars(select(Track).order_by(Track.id)).all()
    session.add(PlayEvent(track_id=old.id))
    old.play_count = 1
    session.flush()
    _gone(session, tmp_path, "Artist/Album/01 Song.wav")

    result = track_removal.remove_fileless_tracks(session, [old.id])

    assert (result.removed, result.moved) == (1, 1)
    assert session.scalar(select(PlayEvent.track_id)) == live.id
    assert session.get(Track, live.id).play_count == 1
    assert result.albums == 0 and result.artists == 0


def test_title_details_offers_remove_only_for_fileless_rows(ctx, session, tmp_path):
    _scan(session, tmp_path, "A/Album/01 Here.wav", "A/Album/02 Gone.wav")
    _gone(session, tmp_path, "A/Album/02 Gone.wav")
    session.commit()
    view = LibraryView(ctx)
    view._load_details()
    table = view.details_table
    gone = _track(session, "Gone").id
    for r in range(table.model.rowCount()):
        row = table.model.row_at(r)
        if row is not None and row.track_id == gone:
            table.view.selectRow(r)
    assert table.selected_fileless_track_ids() == [gone]
    table.view.selectAll()
    assert table.selected_fileless_track_ids() == [gone]   # the present one isn't offered
    assert len(table.selected_track_ids()) == 2


def test_removing_the_last_song_of_the_open_artist_goes_back_to_the_grid(ctx, session, tmp_path):
    _scan(session, tmp_path, "2000-09/2008/Old Song.wav", "Keeper/Album/01 Stays.wav")
    old = _track(session, "Old Song")
    old.play_count = 2
    session.add(PlayEvent(track_id=old.id))
    artist_id = session.scalar(select(Artist.id).where(Artist.name == "2000-09"))
    _gone(session, tmp_path, "2000-09/2008/Old Song.wav")
    session.commit()
    view = LibraryView(ctx)
    view._open_artist(artist_id)
    assert view.artist_detail.top_track_id_at(0) == old.id
    notes = []
    ctx.notify = notes.append
    view.confirm_remove = lambda n: True

    view.remove_from_library([old.id])

    assert view.panes.currentIndex() == PANE_ARTIST_GRID
    with ctx.session() as s:
        assert s.get(Artist, artist_id) is None
    assert notes and notes[0].startswith("Removed 1 song")


def test_saying_no_changes_nothing(ctx, session, tmp_path):
    _scan(session, tmp_path, "A/Album/01 Gone.wav")
    _gone(session, tmp_path, "A/Album/01 Gone.wav")
    track_id = _track(session, "Gone").id
    session.commit()
    view = LibraryView(ctx)
    view.confirm_remove = lambda n: False

    view.remove_from_library([track_id])

    with ctx.session() as s:
        assert s.get(Track, track_id) is not None
