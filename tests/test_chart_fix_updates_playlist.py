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


class TestRefreshAll:
    """Settings › "Chart playlists" › Update (2026-10-29, option 1: rebuild
    each one exactly from its chart) - the catch-up for fixes made before
    they carried over by themselves."""

    def test_rebuilds_a_stale_playlist_and_leaves_a_current_one(self, session):
        a, b, c = (_owned(session, f"Song {n}", f"Artist {n}") for n in "ABC")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        stale = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong),
                                   ("Song C", "Artist C", c)])
        p_stale = chart_svc.snapshot_to_playlist(session, stale.id)
        current = ChartIssue(chart_id=stale.chart_id, chart_date=dt.date(2004, 12, 31))
        session.add(current)
        session.flush()
        session.add(ChartEntry(issue_id=current.id, rank=1, title="Song A", artist_name="Artist A",
                               title_key=normalize("Song A"), artist_key=normalize("Artist A"),
                               track_id=a.id))
        session.flush()
        p_current = chart_svc.snapshot_to_playlist(session, current.id)
        stamp = dt.datetime(2026, 1, 1)
        p_current.updated_at = stamp
        # a fix made the old way: the chart changed, the playlist didn't
        _entry(session, stale, 2).track_id = b.id
        session.flush()

        result = chart_svc.refresh_chart_playlists(session)

        assert (result.checked, result.updated, result.unlinked) == (2, 1, [])
        assert _playlist_tracks(session, p_stale.id) == [a.id, b.id, c.id]
        session.refresh(p_current)
        assert p_current.updated_at == stamp  # nothing for USB sync to merge

    def test_hand_edits_replaced_name_and_folder_kept(self, session):
        from musicmgr.services import playlists as pl_svc

        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        extra = _owned(session, "Bonus", "Someone")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", b)])
        folder = pl_svc.create_folder(session, "Country")
        playlist = chart_svc.snapshot_to_playlist(session, issue.id, folder_id=folder.id)
        playlist.name = "2003"
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=extra.id, position=2))
        session.flush()

        chart_svc.refresh_chart_playlists(session)

        assert _playlist_tracks(session, playlist.id) == [a.id, b.id]
        session.refresh(playlist)
        assert (playlist.name, playlist.folder_id) == ("2003", folder.id)

    def test_old_unlinked_playlist_found_by_name(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", b)])
        old = Playlist(name="Year End - 2003-12-31", kind=Playlist.KIND_CHART)
        session.add(old)
        session.flush()
        session.add(PlaylistItem(playlist_id=old.id, track_id=a.id, position=0))
        session.flush()

        result = chart_svc.refresh_chart_playlists(session)

        assert result.updated == 1
        assert _playlist_tracks(session, old.id) == [a.id, b.id]
        session.refresh(old)
        assert old.source_issue_id == issue.id

    def test_playlist_whose_edition_is_gone_is_skipped(self, session):
        a = _owned(session, "Song A", "Artist A")
        orphan = Playlist(name="Deleted Chart - 1999", kind=Playlist.KIND_CHART)
        session.add(orphan)
        session.flush()
        session.add(PlaylistItem(playlist_id=orphan.id, track_id=a.id, position=0))
        session.flush()

        result = chart_svc.refresh_chart_playlists(session)

        assert result.unlinked == ["Deleted Chart - 1999"]
        assert _playlist_tracks(session, orphan.id) == [a.id]

    def test_manual_playlists_untouched(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        _edition(session, [("Song A", "Artist A", a)])
        mine = Playlist(name="Year End - 2003", kind=Playlist.KIND_MANUAL)
        session.add(mine)
        session.flush()
        session.add(PlaylistItem(playlist_id=mine.id, track_id=b.id, position=0))
        session.flush()

        result = chart_svc.refresh_chart_playlists(session)

        assert result.checked == 0
        assert _playlist_tracks(session, mine.id) == [b.id]


class TestRenamedUnlinked:
    """PC1 on 2026-10-29: 165 of 174 chart playlists were saved before the
    edition link and renamed to just their year ("2020" in a "2020-29"
    folder), so neither the link nor the default name finds them. Update…
    finds each by the year in its name plus the songs it shares, and links
    it."""

    @staticmethod
    def _chart(session, name, folder_names, year, songs):
        from musicmgr.db.models import ChartFolder

        parent = None
        for folder_name in folder_names:
            folder = ChartFolder(name=folder_name, parent_id=parent)
            session.add(folder)
            session.flush()
            parent = folder.id
        chart = Chart(name=name, slug=f"{'-'.join(folder_names)}-{name}-{year}".lower(),
                      folder_id=parent)
        session.add(chart)
        session.flush()
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(year, 12, 31))
        session.add(issue)
        session.flush()
        for rank, (title, artist, track) in enumerate(songs, start=1):
            session.add(ChartEntry(
                issue_id=issue.id, rank=rank, title=title, artist_name=artist,
                title_key=normalize(title), artist_key=normalize(artist), peak_pos=rank,
                track_id=track.id if track is not None else None))
        session.flush()
        return issue

    @staticmethod
    def _playlist(session, name, folder_names, tracks):
        from musicmgr.services import playlists as pl_svc

        parent = None
        for folder_name in folder_names:
            parent = pl_svc.create_folder(session, folder_name, parent).id
        playlist = Playlist(name=name, kind=Playlist.KIND_CHART, folder_id=parent)
        session.add(playlist)
        session.flush()
        for pos, track in enumerate(tracks):
            session.add(PlaylistItem(playlist_id=playlist.id, track_id=track.id, position=pos))
        session.flush()
        return playlist

    def test_found_by_year_and_songs_then_linked(self, session):
        a, b, c, d = (_owned(session, f"Song {n}", f"Artist {n}") for n in "ABCD")
        wrong = _owned(session, "Song C (Live)", "Artist C")
        right_year = self._chart(session, "Year End", ["Billboard", "Country"], 2020,
                                 [("Song A", "Artist A", a), ("Song B", "Artist B", b),
                                  ("Song C", "Artist C", c)])
        # the year before shares most songs, as year-end charts do
        self._chart(session, "Year End", ["Billboard", "Country"], 2019,
                    [("Song A", "Artist A", a), ("Song B", "Artist B", b),
                     ("Song D", "Artist D", d)])
        playlist = self._playlist(session, "2020", ["Billboard Hot Country", "2020-29"],
                                  [a, b, wrong])

        result = chart_svc.refresh_chart_playlists(session)

        assert (result.linked, result.updated, result.unlinked) == (1, 1, [])
        session.refresh(playlist)
        assert playlist.source_issue_id == right_year.id
        assert (playlist.name, _playlist_tracks(session, playlist.id)) == ("2020", [a.id, b.id, c.id])

    def test_source_folder_breaks_a_tie(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        songs = [("Song A", "Artist A", a), ("Song B", "Artist B", b)]
        self._chart(session, "Year End", ["Billboard", "Pop"], 2015, songs)
        playback = self._chart(session, "Year End", ["Playback", "Pop"], 2015, songs)
        playlist = self._playlist(session, "2015", ["Playback.fm Charts", "Top Pop", "2010-19"],
                                  [a, b])

        chart_svc.refresh_chart_playlists(session)

        session.refresh(playlist)
        assert playlist.source_issue_id == playback.id

    def test_unclear_playlist_left_alone(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        other = _owned(session, "Other", "Someone")
        self._chart(session, "Year End", ["Billboard", "Pop"], 2015,
                    [("Song A", "Artist A", a), ("Song B", "Artist B", b)])
        eras = self._playlist(session, "ERAs", ["K-LOVE"], [other])
        stray = self._playlist(session, "2015", ["Mine"], [other, a])

        result = chart_svc.refresh_chart_playlists(session)

        assert sorted(result.unlinked) == ["2015", "ERAs"]
        assert _playlist_tracks(session, eras.id) == [other.id]
        assert _playlist_tracks(session, stray.id) == [other.id, a.id]

    def test_lost_chart_match_taken_from_the_playlist(self, session):
        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        tequila = _owned(session, "Tequila", "Champs")
        miley = _owned(session, "You", "Miley Cyrus")
        issue = self._chart(session, "Year End", ["Playback", "Pop"], 1958,
                            [("Song A", "Artist A", a), ("Tequila", "The Champs", None),
                             ("You", "Ten Sharp", None), ("Song B", "Artist B", b)])
        playlist = self._playlist(session, "1958", ["Playback.fm Charts", "Top Pop"],
                                  [a, tequila, miley, b])

        result = chart_svc.refresh_chart_playlists(session)

        assert result.chart_fixed == 1
        assert _entry(session, issue, 2).track_id == tequila.id     # same artist: trusted
        assert _entry(session, issue, 3).track_id is None           # different artist: not
        assert _playlist_tracks(session, playlist.id) == [a.id, tequila.id, b.id]

    def test_hand_fixed_position_not_overwritten(self, session):
        a = _owned(session, "Song A", "Artist A")
        tequila = _owned(session, "Tequila", "Champs")
        issue = self._chart(session, "Year End", ["Playback", "Pop"], 1958,
                            [("Song A", "Artist A", a), ("Tequila", "The Champs", None)])
        _entry(session, issue, 2).match_locked = True
        session.flush()
        self._playlist(session, "1958", ["Playback.fm Charts"], [a, tequila])

        chart_svc.refresh_chart_playlists(session)

        assert _entry(session, issue, 2).track_id is None


class TestSettingsButton:
    def test_asks_first_then_reports(self, ctx, session, monkeypatch):
        from musicmgr.ui.views.settings import SettingsView

        a, b = (_owned(session, f"Song {n}", f"Artist {n}") for n in "AB")
        wrong = _owned(session, "Song B (Live)", "Artist B")
        issue = _edition(session, [("Song A", "Artist A", a), ("Song B", "Artist B", wrong)])
        playlist = chart_svc.snapshot_to_playlist(session, issue.id)
        _entry(session, issue, 2).track_id = b.id
        session.commit()
        playlist_id, right = playlist.id, b.id

        view = SettingsView(ctx)
        notes, refreshed = [], []
        ctx.notified.connect(notes.append)
        ctx.playlistsChanged.connect(lambda: refreshed.append(True))

        view._confirm_refresh_chart_playlists = lambda: False
        view.refresh_chart_playlists()
        with ctx.session() as s:
            assert _playlist_tracks(s, playlist_id) == [a.id, wrong.id]
        assert notes == []

        view._confirm_refresh_chart_playlists = lambda: True
        view.refresh_chart_playlists()
        with ctx.session() as s:
            assert _playlist_tracks(s, playlist_id) == [a.id, right]
        assert notes == ["Updated 1 of 1 chart playlists"]
        assert refreshed
