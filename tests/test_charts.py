"""Tests for services/charts.py: CSV date parsing (including the documented
bare-year-per-issue fix), forgiving column-header matching, the CSV import
pipeline's bookkeeping (issues/entries/matched/unmatched, re-import updating
rather than duplicating), title/artist matching against owned tracks,
chart-folder tree safety, coverage/shape reporting, and freezing a chart
week into a playlist.
"""

from __future__ import annotations

import csv
import datetime as dt

import pytest
from sqlalchemy import select

from musicmgr.db.models import Chart, ChartEntry, ChartFolder, ChartIssue, Playlist, PlaylistItem
from musicmgr.services import charts
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize


def make_owned_track(session, title, artist):
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, f"{title} Album", artist)
    release.album_artist_id = artist_obj.id
    from musicmgr.db.models import Track

    track = Track(
        release_id=release.id, title=title, title_key=normalize(title), artist_display=artist
    )
    session.add(track)
    session.flush()
    return track


def write_csv(tmp_path, filename, rows, headers):
    path = tmp_path / filename
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return path


class TestSlugify:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Billboard Hot 100", "billboard-hot-100"),
            ("  Weird---Spacing  ", "weird-spacing"),
            # \w is unicode-aware in Python's re module, so accented
            # letters are kept - only punctuation like "!" is stripped
            ("Café Rankings!", "café-rankings"),
            ("", "chart"),
        ],
    )
    def test_slugify(self, name, expected):
        assert charts.slugify(name) == expected


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2020-01-15", dt.date(2020, 1, 15)),
            ("01/15/2020", dt.date(2020, 1, 15)),
            ("15/01/2020", dt.date(2020, 1, 15)),
            ("2020/01/15", dt.date(2020, 1, 15)),
            ("Jan 15, 2020", dt.date(2020, 1, 15)),
            ("15 Jan 2020", dt.date(2020, 1, 15)),
            ("", None),
            ("not a date at all", None),
        ],
    )
    def test_recognized_formats(self, raw, expected):
        assert charts.parse_date(raw) == expected

    def test_a_bare_year_becomes_december_31st(self):
        # the 2026-08-30-ish fix this module's own docstring documents: a
        # bare-year Year-End chart row used to collapse every year into one
        # ChartIssue dated "today" instead of getting its own distinct date.
        assert charts.parse_date("1999") == dt.date(1999, 12, 31)
        assert charts.parse_date("2020") == dt.date(2020, 12, 31)


class TestGetOrCreateChart:
    def test_creates_once_for_the_same_name(self, session):
        c1 = charts.get_or_create_chart(session, "Billboard Hot 100")
        c2 = charts.get_or_create_chart(session, "Billboard Hot 100")

        assert c1.id == c2.id

    def test_matches_by_slug_even_with_different_capitalization(self, session):
        c1 = charts.get_or_create_chart(session, "Billboard Hot 100")
        c2 = charts.get_or_create_chart(session, "billboard hot 100")

        assert c1.id == c2.id


class TestImportChartCsv:
    def test_matches_a_track_already_in_the_library(self, session, tmp_path):
        make_owned_track(session, "Thriller", "Michael Jackson")
        path = write_csv(
            tmp_path, "chart.csv",
            [
                {"rank": "1", "title": "Thriller", "artist": "Michael Jackson"},
                {"rank": "2", "title": "Some Unowned Song", "artist": "Nobody You Have"},
            ],
            headers=["rank", "title", "artist"],
        )

        result = charts.import_chart_csv(
            session, path, chart_name="Test Chart", chart_date=dt.date(2020, 1, 4)
        )

        assert result.entries == 2
        assert result.matched == 1
        assert result.unmatched == 1
        assert not result.errors

    def test_recognizes_alternate_column_headers(self, session, tmp_path):
        path = write_csv(
            tmp_path, "alt.csv",
            [{"pos": "1", "song": "A Song", "performer": "Someone"}],
            headers=["pos", "song", "performer"],
        )

        result = charts.import_chart_csv(
            session, path, chart_name="Alt", chart_date=dt.date(2020, 1, 1)
        )

        assert result.entries == 1
        assert not result.errors

    def test_missing_required_columns_is_reported_and_nothing_is_created(self, session, tmp_path):
        path = write_csv(tmp_path, "bad.csv", [{"foo": "1"}], headers=["foo"])

        result = charts.import_chart_csv(session, path, chart_name="Bad")

        assert result.entries == 0
        assert result.errors
        assert session.scalar(select(Chart)) is None

    def test_bare_year_rows_get_one_issue_per_year_not_one_collapsed_issue(self, session, tmp_path):
        path = write_csv(
            tmp_path, "yearend.csv",
            [
                {"rank": "1", "title": "Song A", "artist": "Artist A", "year": "1999"},
                {"rank": "1", "title": "Song B", "artist": "Artist B", "year": "2000"},
            ],
            headers=["rank", "title", "artist", "year"],
        )

        result = charts.import_chart_csv(session, path, chart_name="Year End Chart")

        assert result.issues == 2
        chart = session.scalar(select(Chart).where(Chart.slug == "year-end-chart"))
        dates = sorted(i.chart_date for i in charts.list_issues(session, chart.id))
        assert dates == [dt.date(1999, 12, 31), dt.date(2000, 12, 31)]

    def test_reimporting_the_same_rank_updates_the_entry_instead_of_duplicating_it(
        self, session, tmp_path
    ):
        path1 = write_csv(
            tmp_path, "chart1.csv",
            [{"rank": "1", "title": "Old Title", "artist": "Someone"}],
            headers=["rank", "title", "artist"],
        )
        charts.import_chart_csv(session, path1, chart_name="Dup Chart", chart_date=dt.date(2020, 1, 1))

        path2 = write_csv(
            tmp_path, "chart2.csv",
            [{"rank": "1", "title": "New Title", "artist": "Someone Else"}],
            headers=["rank", "title", "artist"],
        )
        charts.import_chart_csv(session, path2, chart_name="Dup Chart", chart_date=dt.date(2020, 1, 1))

        chart = session.scalar(select(Chart).where(Chart.slug == "dup-chart"))
        issue = charts.list_issues(session, chart.id)[0]
        entries = charts.issue_entries(session, issue.id)
        assert len(entries) == 1
        assert entries[0].title == "New Title"

    def test_reimporting_the_same_chart_name_reuses_the_same_chart(self, session, tmp_path):
        path1 = write_csv(
            tmp_path, "c1.csv",
            [{"rank": "1", "title": "A", "artist": "X"}],
            headers=["rank", "title", "artist"],
        )
        charts.import_chart_csv(session, path1, chart_name="Same Chart", chart_date=dt.date(2020, 1, 1))
        path2 = write_csv(
            tmp_path, "c2.csv",
            [{"rank": "1", "title": "B", "artist": "Y"}],
            headers=["rank", "title", "artist"],
        )
        charts.import_chart_csv(session, path2, chart_name="Same Chart", chart_date=dt.date(2020, 1, 8))

        all_charts = session.scalars(select(Chart)).all()
        assert len(all_charts) == 1
        assert charts.chart_edition_count(session, all_charts[0].id) == 2

    def test_a_row_missing_a_rank_or_title_is_skipped_without_erroring(self, session, tmp_path):
        path = write_csv(
            tmp_path, "sparse.csv",
            [
                {"rank": "1", "title": "Real Song", "artist": "Someone"},
                {"rank": "", "title": "No Rank", "artist": "Someone"},
                {"rank": "3", "title": "", "artist": "Someone"},
            ],
            headers=["rank", "title", "artist"],
        )

        result = charts.import_chart_csv(session, path, chart_name="Sparse", chart_date=dt.date(2020, 1, 1))

        assert result.entries == 1
        assert not result.errors


class TestMatchEntry:
    def _entry(self, session, chart_name, title, artist, **extra):
        chart = charts.get_or_create_chart(session, chart_name)
        issue = session.scalar(
            select(ChartIssue).where(ChartIssue.chart_id == chart.id, ChartIssue.chart_date == dt.date(2020, 1, 1))
        )
        if issue is None:
            issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
            session.add(issue)
            session.flush()
        entry = ChartEntry(
            issue_id=issue.id,
            rank=extra.pop("rank", 1),
            title=title,
            artist_name=artist,
            title_key=normalize(title),
            artist_key=normalize(artist),
            **extra,
        )
        session.add(entry)
        session.flush()
        return entry

    def test_matches_a_close_title_and_artist(self, session):
        make_owned_track(session, "Thriller", "Michael Jackson")
        entry = self._entry(session, "Test", "Thriller", "Michael Jackson")

        matched = charts.match_entry(session, entry)

        assert matched is True
        assert entry.track_id is not None

    def test_does_not_match_an_unrelated_title(self, session):
        make_owned_track(session, "Thriller", "Michael Jackson")
        entry = self._entry(session, "Test", "Completely Different Song", "Nobody")

        matched = charts.match_entry(session, entry)

        assert matched is False
        assert entry.track_id is None

    def test_a_locked_match_is_never_recomputed(self, session):
        track = make_owned_track(session, "Thriller", "Michael Jackson")
        entry = self._entry(
            session, "Test", "Some Other Title", "Someone Else",
            track_id=track.id, match_locked=True,
        )

        matched = charts.match_entry(session, entry)

        assert matched is True
        assert entry.track_id == track.id  # unchanged, despite not really matching


class TestRematchChart:
    def test_rematches_every_entry_and_returns_the_matched_count(self, session):
        make_owned_track(session, "Thriller", "Michael Jackson")
        chart = charts.get_or_create_chart(session, "Test")
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
        session.add(issue)
        session.flush()
        session.add(ChartEntry(
            issue_id=issue.id, rank=1, title="Thriller", artist_name="Michael Jackson",
            title_key=normalize("Thriller"), artist_key=normalize("Michael Jackson"),
        ))
        session.add(ChartEntry(
            issue_id=issue.id, rank=2, title="Nothing Like It", artist_name="Nobody",
            title_key=normalize("Nothing Like It"), artist_key=normalize("Nobody"),
        ))
        session.flush()

        matched = charts.rematch_chart(session, chart.id)

        assert matched == 1


class TestChartShape:
    def test_no_issues_is_a_dash(self, session):
        chart = charts.get_or_create_chart(session, "Empty")

        assert charts.chart_shape(chart) == "—"

    def test_one_issue_per_year_is_year_end(self, session):
        chart = charts.get_or_create_chart(session, "YE")
        session.add(ChartIssue(chart_id=chart.id, chart_date=dt.date(1999, 12, 31)))
        session.add(ChartIssue(chart_id=chart.id, chart_date=dt.date(2000, 12, 31)))
        session.flush()

        assert charts.chart_shape(chart) == "Year End"

    def test_more_than_one_issue_in_a_year_is_weekly(self, session):
        chart = charts.get_or_create_chart(session, "Weekly")
        session.add(ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 4)))
        session.add(ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 11)))
        session.flush()

        assert charts.chart_shape(chart) == "Weekly"


class TestCoverage:
    def _chart_with_entries(self, session, owned_titles, unowned_titles, name="Coverage Test"):
        chart = charts.get_or_create_chart(session, name)
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
        session.add(issue)
        session.flush()
        rank = 1
        for title in owned_titles:
            track = make_owned_track(session, title, "Artist")
            session.add(ChartEntry(
                issue_id=issue.id, rank=rank, title=title, artist_name="Artist",
                title_key=normalize(title), artist_key=normalize("Artist"), track_id=track.id,
            ))
            rank += 1
        for title in unowned_titles:
            session.add(ChartEntry(
                issue_id=issue.id, rank=rank, title=title, artist_name="Nobody",
                title_key=normalize(title), artist_key=normalize("Nobody"),
            ))
            rank += 1
        session.flush()
        return chart, issue

    def test_chart_and_issue_coverage_count_owned_versus_total(self, session):
        chart, issue = self._chart_with_entries(session, ["Owned One", "Owned Two"], ["Missing One"])

        assert charts.chart_coverage(session, chart.id) == (2, 3)
        assert charts.issue_coverage(session, issue.id) == (2, 3)

    def test_edition_count_counts_every_issue(self, session):
        chart, _ = self._chart_with_entries(session, [], [], name="Editions")
        session.add(ChartIssue(chart_id=chart.id, chart_date=dt.date(2021, 1, 1)))
        session.flush()

        assert charts.chart_edition_count(session, chart.id) == 2

    def test_issue_coverage_by_chart_is_keyed_per_issue(self, session):
        chart, issue1 = self._chart_with_entries(session, ["Owned"], ["Missing"], name="Batch")
        issue2 = ChartIssue(chart_id=chart.id, chart_date=dt.date(2021, 1, 1))
        session.add(issue2)
        session.flush()
        session.add(ChartEntry(
            issue_id=issue2.id, rank=1, title="Another Missing", artist_name="X",
            title_key=normalize("Another Missing"), artist_key=normalize("X"),
        ))
        session.flush()

        result = charts.issue_coverage_by_chart(session, chart.id)

        assert result[issue1.id] == (1, 2)
        assert result[issue2.id] == (0, 1)


class TestChartRun:
    def test_returns_every_week_a_track_charted_oldest_first(self, session):
        track = make_owned_track(session, "Recurring Hit", "Artist")
        chart = charts.get_or_create_chart(session, "Run Test")
        later = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 2, 1))
        earlier = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
        session.add_all([later, earlier])
        session.flush()
        session.add(ChartEntry(
            issue_id=later.id, rank=5, title="x", artist_name="a",
            title_key="x", artist_key="a", track_id=track.id,
        ))
        session.add(ChartEntry(
            issue_id=earlier.id, rank=10, title="x", artist_name="a",
            title_key="x", artist_key="a", track_id=track.id,
        ))
        session.flush()

        run = charts.chart_run(session, chart.id, track.id)

        assert [e.issue.chart_date for e in run] == [dt.date(2020, 1, 1), dt.date(2020, 2, 1)]


class TestSnapshotToPlaylist:
    def test_freezes_owned_entries_into_a_new_playlist(self, session):
        track = make_owned_track(session, "Owned", "Artist")
        chart = charts.get_or_create_chart(session, "Snap Test")
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
        session.add(issue)
        session.flush()
        session.add(ChartEntry(
            issue_id=issue.id, rank=1, title="Owned", artist_name="Artist",
            title_key=normalize("Owned"), artist_key=normalize("Artist"), track_id=track.id,
        ))
        session.add(ChartEntry(
            issue_id=issue.id, rank=2, title="Unowned", artist_name="Nobody",
            title_key=normalize("Unowned"), artist_key=normalize("Nobody"),
        ))
        session.flush()

        playlist = charts.snapshot_to_playlist(session, issue.id)

        assert playlist.kind == Playlist.KIND_CHART
        items = session.scalars(
            select(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id)
        ).all()
        assert [i.track_id for i in items] == [track.id]

    def test_resnapshotting_replaces_items_rather_than_duplicating(self, session):
        track = make_owned_track(session, "Owned", "Artist")
        chart = charts.get_or_create_chart(session, "Snap Test 2")
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
        session.add(issue)
        session.flush()
        session.add(ChartEntry(
            issue_id=issue.id, rank=1, title="Owned", artist_name="Artist",
            title_key=normalize("Owned"), artist_key=normalize("Artist"), track_id=track.id,
        ))
        session.flush()

        p1 = charts.snapshot_to_playlist(session, issue.id)
        p2 = charts.snapshot_to_playlist(session, issue.id)

        assert p1.id == p2.id
        items = session.scalars(
            select(PlaylistItem).where(PlaylistItem.playlist_id == p1.id)
        ).all()
        assert len(items) == 1


class TestChartFolders:
    def test_create_rename_and_list(self, session):
        folder = charts.create_folder(session, "  Weekly Charts  ")

        assert folder.name == "Weekly Charts"
        charts.rename_folder(session, folder.id, "Renamed")
        assert [f.name for f in charts.list_folders(session)] == ["Renamed"]

    def test_moving_a_folder_into_its_own_descendant_is_rejected(self, session):
        top = charts.create_folder(session, "Top")
        child = charts.create_folder(session, "Child", parent_id=top.id)

        with pytest.raises(ValueError):
            charts.move_folder(session, top.id, child.id)

    def test_deleting_a_folder_reparents_its_children_and_charts(self, session):
        top = charts.create_folder(session, "Top")
        child_folder = charts.create_folder(session, "Child", parent_id=top.id)
        chart = charts.get_or_create_chart(session, "Inside", folder_id=top.id)

        charts.delete_folder(session, top.id)

        assert session.get(ChartFolder, top.id) is None
        assert child_folder.parent_id is None
        assert chart.folder_id is None

    def test_folder_counts_are_direct_children_only(self, session):
        top = charts.create_folder(session, "Top")
        charts.create_folder(session, "Child", parent_id=top.id)
        charts.get_or_create_chart(session, "C1", folder_id=top.id)
        charts.get_or_create_chart(session, "C2", folder_id=top.id)

        chart_count, subfolders = charts.folder_counts(session, top.id)

        assert chart_count == 2
        assert subfolders == 1


class TestRemoveOrphanedPlaybackCharts:
    def test_deletes_charts_still_carrying_the_old_playback_kind(self, session):
        stale = Chart(name="Old Playback", slug="old-playback", kind="playback")
        keep = Chart(name="External", slug="external", kind=Chart.KIND_EXTERNAL)
        session.add_all([stale, keep])
        session.flush()

        removed = charts.remove_orphaned_playback_charts(session)

        assert removed == 1
        assert session.get(Chart, stale.id) is None
        assert session.get(Chart, keep.id) is not None
