"""Fixing a chart match also fixes that edition's saved playlist
(2026-10-29 - James: "When I update/fix match on charts, If I already have a
playlist for that chart, I would like the fix matches also be fixed in the
playlist").

"Save as playlist" freezes an edition into a KIND_CHART playlist; before
this, a later "Fix match…" or "Re-match library" only changed the chart, so
the playlist kept playing the old, wrong song until the edition was saved
again. services/charts.py:sync_edition_playlist now carries each changed
match over - swapping the song in place, inserting a newly matched one at
its chart position, or taking a cleared one out - and leaves everything
else about the playlist (name, folder, songs added or reordered by hand)
alone."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from musicmgr.db.models import Chart, ChartEntry, ChartIssue, Playlist, PlaylistItem, Track
from musicmgr.services import charts as chart_svc
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize


def _owned(session, title, artist) -> Track:
    release = lib.get_or_create_release(session, f"{title} Album", artist)
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display=artist)
    session.add(track)
    session.flush()
    return track


def _edition(session, songs) -> ChartIssue:
    """An edition with one entry per (title, artist, track-or-None)."""
    chart = Chart(name="Year End", slug="billboard-country-year-end", size=len(songs))
    session.add(chart)
    session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2003, 12, 31))
    session.add(issue)
    session.flush()
    for rank, (title, artist, track) in enumerate(songs, start=1):
        session.add(ChartEntry(
            issue_id=issue.id, rank=rank, title=title, artist_name=artist,
            title_key=normalize(title), artist_key=normalize(artist), peak_pos=rank,
            track_id=track.id if track is not None else None,
        ))
    session.flush()
    return issue


def _entry(session, issue, rank) -> ChartEntry:
    return session.scalar(select(ChartEntry).where(
        ChartEntry.issue_id == issue.id, ChartEntry.rank == rank))


def _playlist_tracks(session, playlist_id) -> list[int]:
    return list(session.scalars(
        select(PlaylistItem.track_id).where(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position)))


class TestFixMatch:
    def test_wrong_song_swapped_in_place(self, session):
        a, b, c = (_owned(session, f"Song {n}", f"Artist {n}") for n in "ABC")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong),
                                   ("Song C", "Artist C", c)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)
        assert _playlist_tracks(session, playlist.id) == [a.id, wrong.id, c.id]

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, b.id)

        assert _playlist_tracks(session, playlist.id) == [a.id, b.id, c.id]

    def test_unmatched_song_inserted_at_its_chart_position(self, session):
        a, b, c = (_owned(session, f"Song {n}", f"Artist {n}") for n in "ABC")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", None),
                                   ("Song C", "Artist C", c)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)
        assert _playlist_tracks(session, playlist.id) == [a.id, c.id]

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, b.id)

        assert _playlist_tracks(session, playlist.id) == [a.id, b.id, c.id]

    def test_cleared_match_taken_out(self, session):
        a, b, c = (_owned(session, f"Song {n}", f"Artist {n}") for n in "ABC")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", b),
                                   ("Song C", "Artist C", c)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, None)

        assert _playlist_tracks(session, playlist.id) == [a.id, c.id]

    def test_keeps_name_folder_and_hand_edits(self, session):
        from musicmgr.services import playlists as pl_svc

        a, b, c = (_owned(session, f"Song {n}", f"Artist {n}") for n in "ABC")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        extra = _owned(session, "Bonus", "Someone")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong),
                                   ("Song C", "Artist C", c)])
        folder = pl_svc.create_folder(session, "Country")
        playlist = chart_svc.snapshot_to_playlist(session, issue.id, folder_id=folder.id)
        playlist.name = "2003"
        # reordered by hand (C first) and a bonus song added at the end
        items = session.scalars(select(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist.id).order_by(PlaylistItem.position)).all()
        items[0].position, items[1].position, items[2].position = 1, 2, 0
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=extra.id, position=3))
        session.flush()

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, b.id)

        assert _playlist_tracks(session, playlist.id) == [c.id, a.id, b.id, extra.id]
        session.refresh(playlist)
        assert (playlist.name, playlist.folder_id) == ("2003", folder.id)

    def test_song_already_in_playlist_not_doubled(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=b.id, position=2))
        session.flush()

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, b.id)

        assert _playlist_tracks(session, playlist.id) == [a.id, b.id]

    def test_no_playlist_none_created(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", None)])

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, b.id)

        assert session.scalar(select(Playlist).where(Playlist.kind == Playlist.KIND_CHART)) is None

    def test_other_editions_playlists_untouched(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong)])
        other = ChartIssue(chart_id=issue.chart_id, chart_date=dt.date(2004, 12, 31))
        session.add(other)
        session.flush()
        session.add(ChartEntry(issue_id=other.id, rank=1, title="Song B", artist_name="Artist B",
                               title_key=normalize("Song B"), artist_key=normalize("Artist B"),
                               track_id=wrong.id))
        session.flush()
        mine = chart_svc.snapshot_to_playlist(session, issue.id)
        theirs = chart_svc.snapshot_to_playlist(session, other.id)

        chart_svc.set_entry_track(session, _entry(session, issue, 2).id, b.id)

        assert _playlist_tracks(session, mine.id) == [a.id, b.id]
        assert _playlist_tracks(session, theirs.id) == [wrong.id]


class TestRematch:
    def test_rematch_adds_newly_owned_songs_to_the_playlist(self, session):
        a = _owned(session, "Song A", "Artist A")
        c = _owned(session, "Song C", "Artist C")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", None),
                                   ("Song C", "Artist C", c)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)
        b = _owned(session, "Song B", "Artist B")  # added to the library later

        chart_svc.rematch_issue(session, issue.id)

        assert _playlist_tracks(session, playlist.id) == [a.id, b.id, c.id]

    def test_edit_that_rematches_updates_the_playlist(self, session):
        a = _owned(session, "Song A", "Artist A")
        then = _owned(session, "Then They Do", "Trace Adkins")
        issue = _edition(session, [("Song A", "Artist A", a), ("Then They Dp", "Trace Adkin", None)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)

        chart_svc.update_entry(session, _entry(session, issue, 2).id, 2,
                               "Then They Do", "Trace Adkins")

        assert _playlist_tracks(session, playlist.id) == [a.id, then.id]


class TestView:
    def test_fix_match_says_the_playlist_was_fixed_too(self, ctx, session, monkeypatch):
        from PySide6.QtWidgets import QDialog

        from musicmgr.ui.views import charts as charts_view

        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)
        playlist.name = "2003"
        session.commit()
        issue_id, playlist_id, right = issue.id, playlist.id, b.id

        class PickRightSong:
            clear_requested = False
            chosen_track_id = right

            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return QDialog.Accepted

        monkeypatch.setattr(charts_view, "MatchTrackDialog", PickRightSong)
        notes, refreshed = [], []
        ctx.notified.connect(notes.append)
        ctx.playlistsChanged.connect(lambda: refreshed.append(True))

        view = charts_view.ChartsView(ctx)
        view._selected_type, view._selected_id = "issue", issue_id
        view._load_detail()
        view.entry_list.setCurrentRow(1)
        view._fix_match()

        assert "Also fixed in playlist “2003”" in notes
        assert refreshed
        with ctx.session() as s:
            assert _playlist_tracks(s, playlist_id) == [a.id, right]
