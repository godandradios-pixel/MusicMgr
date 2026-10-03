"""Editing a chart edition's positions by hand (2026-10-03 - James: "I
would like to be able to edit my Chart songs. For example, the 2003
Billboard Top Country chart is missing #40 Trace Adkins - Then They Do")."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from musicmgr.db.models import Chart, ChartEntry, ChartIssue, Track
from musicmgr.services import chart_sync as cs
from musicmgr.services import charts as chart_svc
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize
from musicmgr.ui.views.charts import ChartsView


def _owned(session, title, artist) -> Track:
    release = lib.get_or_create_release(session, f"{title} Album", artist)
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display=artist)
    session.add(track)
    session.flush()
    return track


def _edition(session, ranks=(38, 39, 41, 42)) -> ChartIssue:
    chart = Chart(name="Year End", slug="billboard-country-year-end", size=42)
    session.add(chart)
    session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2003, 12, 31))
    session.add(issue)
    session.flush()
    for rank in ranks:
        session.add(ChartEntry(issue_id=issue.id, rank=rank, title=f"Song {rank}",
                               artist_name=f"Artist {rank}", title_key=normalize(f"Song {rank}"),
                               artist_key=normalize(f"Artist {rank}"), peak_pos=rank))
    session.flush()
    return issue


def _ranks(session, issue_id) -> list[tuple[int, str]]:
    return [(e.rank, e.title) for e in session.scalars(
        select(ChartEntry).where(ChartEntry.issue_id == issue_id).order_by(ChartEntry.rank))]


class TestService:
    def test_fills_the_missing_position_and_matches_it(self, session):
        track = _owned(session, "Then They Do", "Trace Adkins")
        issue = _edition(session)
        assert chart_svc.suggested_rank(session, issue.id) == 1
        entry = chart_svc.add_entry(session, issue.id, 40, " Then They Do ", "Trace Adkins")
        assert (entry.rank, entry.title, entry.peak_pos) == (40, "Then They Do", 40)
        assert entry.track_id == track.id
        assert [r for r, _ in _ranks(session, issue.id)] == [38, 39, 40, 41, 42]

    def test_suggested_rank_is_the_first_gap(self, session):
        issue = _edition(session, ranks=(1, 2, 3, 5))
        assert chart_svc.suggested_rank(session, issue.id) == 4

    def test_taken_position_refused_then_shifted(self, session):
        issue = _edition(session)
        with pytest.raises(chart_svc.RankTaken) as taken:
            chart_svc.add_entry(session, issue.id, 39, "New", "Someone")
        assert taken.value.holder == "Song 39 — Artist 39"
        chart_svc.add_entry(session, issue.id, 39, "New", "Someone", shift=True)
        assert _ranks(session, issue.id) == [
            (38, "Song 38"), (39, "New"), (40, "Song 39"), (42, "Song 41"), (43, "Song 42")]
        assert session.get(Chart, issue.chart_id).size == 43

    def test_edit_retitles_and_rematches(self, session):
        track = _owned(session, "Then They Do", "Trace Adkins")
        issue = _edition(session)
        entry = chart_svc.add_entry(session, issue.id, 40, "Then They Dp", "Trace Adkin")
        chart_svc.update_entry(session, entry.id, 40, "Then They Do", "Trace Adkins",
                               peak_pos=11, weeks_on_chart=20)
        assert (entry.title_key, entry.peak_pos, entry.weeks_on_chart) == ("then they do", 11, 20)
        assert entry.track_id == track.id

    def test_edit_keeps_a_hand_fixed_match(self, session):
        track = _owned(session, "Something Else", "Other")
        issue = _edition(session)
        entry = session.scalar(select(ChartEntry).where(ChartEntry.rank == 38))
        chart_svc.set_entry_track(session, entry.id, track.id)
        chart_svc.update_entry(session, entry.id, 38, "Song 38 (typo fixed)", "Artist 38")
        assert entry.track_id == track.id and entry.match_locked

    def test_move_to_free_position(self, session):
        issue = _edition(session)
        entry = session.scalar(select(ChartEntry).where(ChartEntry.rank == 41))
        chart_svc.update_entry(session, entry.id, 40, "Song 41", "Artist 41")
        assert [r for r, _ in _ranks(session, issue.id)] == [38, 39, 40, 42]

    def test_move_up_and_down_onto_taken_positions(self, session):
        issue = _edition(session)
        e42 = session.scalar(select(ChartEntry).where(ChartEntry.rank == 42))
        with pytest.raises(chart_svc.RankTaken):
            chart_svc.update_entry(session, e42.id, 38, "Song 42", "Artist 42")
        chart_svc.update_entry(session, e42.id, 38, "Song 42", "Artist 42", shift=True)
        assert _ranks(session, issue.id) == [
            (38, "Song 42"), (39, "Song 38"), (40, "Song 39"), (42, "Song 41")]
        # the empty position slides along with the songs, it isn't filled
        chart_svc.update_entry(session, e42.id, 42, "Song 42", "Artist 42", shift=True)
        assert _ranks(session, issue.id) == [
            (38, "Song 38"), (39, "Song 39"), (41, "Song 41"), (42, "Song 42")]

    def test_delete_leaves_a_gap(self, session):
        track = _owned(session, "Song 39", "Artist 39")
        issue = _edition(session)
        entry = session.scalar(select(ChartEntry).where(ChartEntry.rank == 39))
        chart_svc.delete_entry(session, entry.id)
        assert [r for r, _ in _ranks(session, issue.id)] == [38, 41, 42]
        assert session.get(Track, track.id) is not None

    def test_blank_title_refused(self, session):
        issue = _edition(session)
        with pytest.raises(ValueError):
            chart_svc.add_entry(session, issue.id, 40, "  ", "Trace Adkins")

    def test_edit_marks_the_chart_for_the_next_usb_sync(self, session):
        issue = _edition(session)
        cs._save_cache(session, {str(issue.chart_id): ["fp", "sig", "2026-10-01T00:00:00+00:00"]})
        entry = session.scalar(select(ChartEntry).where(ChartEntry.rank == 38))
        # same length title: the cheap fingerprint alone wouldn't notice
        chart_svc.update_entry(session, entry.id, 38, "Song 3X", "Artist 38")
        assert cs._load_cache(session)[str(issue.chart_id)] == [
            "", "sig", "2026-10-01T00:00:00+00:00"]


class TestView:
    def _view(self, ctx, issue_id, answers, confirms=()):
        view = ChartsView(ctx)
        view._selected_type, view._selected_id = "issue", issue_id
        view._load_detail()
        view.asked = []
        answers, confirms = list(answers), list(confirms)
        view._ask_entry = lambda heading, **init: view.asked.append((heading, init)) or answers.pop(0)
        view._confirm = lambda title, text: view.asked.append((title, text)) or confirms.pop(0)
        return view

    @staticmethod
    def _values(rank, title, artist, **more):
        return dict(rank=rank, title=title, artist=artist, last_week=None, peak_pos=None,
                    weeks_on_chart=None) | more

    def test_add_suggests_the_gap_and_selects_the_new_row(self, ctx, session):
        issue = _edition(session, ranks=tuple(r for r in range(1, 43) if r != 40))
        session.commit()
        view = self._view(ctx, issue.id, [self._values(40, "Then They Do", "Trace Adkins")])
        assert view.add_entry_btn.isEnabled()
        view.add_entry()
        assert view.asked == [("Add song", {"rank": 40})]
        assert view.entry_list.current_payload()["primary"] == "Then They Do"
        assert view.entry_title.text() == "42 positions"

    def test_add_onto_taken_position_asks_first(self, ctx, session):
        issue = _edition(session)
        session.commit()
        view = self._view(ctx, issue.id, [self._values(39, "New", "Someone")], confirms=[False])
        view.add_entry()
        assert view.asked[-1][0] == "Position taken" and "Song 39 — Artist 39" in view.asked[-1][1]
        with ctx.session() as s:
            assert [r for r, _ in _ranks(s, issue.id)] == [38, 39, 41, 42]
        view = self._view(ctx, issue.id, [self._values(39, "New", "Someone")], confirms=[True])
        view.add_entry()
        with ctx.session() as s:
            assert _ranks(s, issue.id)[1:3] == [(39, "New"), (40, "Song 39")]

    def test_edit_and_remove_the_selected_row(self, ctx, session):
        issue = _edition(session)
        session.commit()
        view = self._view(ctx, issue.id, [self._values(38, "Fixed", "Artist 38", peak_pos=5)],
                          confirms=[True])
        view.entry_list.setCurrentRow(0)
        view.edit_entry()
        assert view.asked[0] == ("Edit song", {"rank": 38, "title": "Song 38", "artist": "Artist 38",
                                               "last_week": None, "peak_pos": 38,
                                               "weeks_on_chart": None})
        assert view.entry_list.current_payload()["primary"] == "Fixed"
        view.delete_entry()
        with ctx.session() as s:
            assert [r for r, _ in _ranks(s, issue.id)] == [39, 41, 42]

    def test_buttons_off_without_an_edition(self, ctx, session):
        view = ChartsView(ctx)
        view._load_detail()
        assert not view.add_entry_btn.isEnabled() and not view.delete_entry_btn.isEnabled()
