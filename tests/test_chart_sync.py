"""services/chart_sync.py - charts shared between PCs through the USB drive
(2026-09-30: "I would like to have Charts be added to the SETTINGS Sync to
USB"). Uses test_library_state's two-PC setup."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from musicmgr.db import session as db_session
from musicmgr.db.models import Chart, ChartEntry, ChartFolder, ChartIssue
from musicmgr.services import chart_sync as cs
from musicmgr.services import charts as chart_svc
from musicmgr.services import library_state as ls
from musicmgr.services.matching import normalize
from tests.test_library_state import SONGS, two_pcs  # noqa: F401 - fixture

ROWS = [("Subdivisions", "Rush"), ("You Really Got Me", "Kinks"), ("Not Owned", "Nobody")]


def _make_chart(s, path: str, name: str, years=(1982, 1983)) -> Chart:
    folders = cs._Folders(s)
    chart = Chart(name=name, slug=chart_svc.slugify(f"{path} {name}"),
                  folder_id=folders.ensure(path), size=3)
    s.add(chart)
    s.flush()
    for year in years:
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(year, 12, 31))
        s.add(issue)
        s.flush()
        for rank, (title, artist) in enumerate(ROWS, start=1):
            entry = ChartEntry(issue_id=issue.id, rank=rank, title=title, artist_name=artist,
                               title_key=normalize(title), artist_key=normalize(artist),
                               peak_pos=rank)
            s.add(entry)
            s.flush()
            chart_svc.match_entry(s, entry)
    return chart


def _charts(s) -> dict[str, tuple]:
    """{key: (editions, rows, matched rows)}"""
    out = {}
    for key, cid in cs.keyed_charts(s).items():
        issues = s.scalar(select(func.count()).select_from(ChartIssue)
                          .where(ChartIssue.chart_id == cid))
        rows = s.execute(select(ChartEntry.track_id).join(ChartIssue)
                         .where(ChartIssue.chart_id == cid)).scalars().all()
        out[key] = (issues, len(rows), sum(1 for t in rows if t))
    return out


def _folders(s) -> set[str]:
    f = cs._Folders(s)
    return {f.path(fid) for fid in f.rows}


class TestChartSync:
    def test_chart_travels_and_is_matched_there(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Billboard/Rock", "Year End")
        sent = pc1.sync()
        assert sent.charts.charts_out == 1
        assert "1 chart" in sent.summary() and sent.status().endswith("1 chart)")
        notes = pc2.sync()
        assert notes.charts.charts_in == ["Billboard/Rock/Year End"] and notes.received()
        with db_session.session_scope() as s:
            # 2 editions x 3 rows; the two owned songs matched on PC2 itself
            assert _charts(s) == {"Billboard/Rock/Year End": (2, 6, 4)}
            assert _folders(s) == {"Billboard", "Billboard/Rock"}
        assert pc1.sync().summary() == "Library data already in sync"
        assert pc2.sync().summary() == "Library data already in sync"

    def test_charts_off_neither_sends_nor_takes(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Pop", "Weekly")
            ls.set_category_enabled(s, ls.CHARTS, False)
        assert pc1.sync().charts is None
        assert not cs.usb_index_path(drive).exists()
        pc1.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.CHARTS, True)
        pc1.sync()
        assert pc2.sync().charts.charts_in == ["Pop/Weekly"]

    def test_move_rename_and_delete_follow(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Pop", "Billboard YE")
            _make_chart(s, "Rock", "Playback YE", years=(1990,))
        pc1.sync()
        pc2.sync()
        pc1.use()
        with db_session.session_scope() as s:
            ids = cs.keyed_charts(s)
            chart = s.get(Chart, ids["Pop/Billboard YE"])
            chart.name = "Year End"
            chart.folder_id = cs._Folders(s).ensure("Billboard/Pop")
            chart_svc.delete_chart(s, ids["Rock/Playback YE"])
        files_before = set(cs.usb_dir(drive).glob("*.json.gz"))
        pc1.sync()
        notes = pc2.sync()
        # the moved chart is moved here, not deleted and rebuilt
        assert notes.charts.charts_in == ["Billboard/Pop/Year End"]
        assert notes.charts.charts_removed == ["Rock/Playback YE"]
        with db_session.session_scope() as s:
            assert _charts(s) == {"Billboard/Pop/Year End": (2, 6, 4)}
            # old folders emptied by the move/delete go too
            assert _folders(s) == {"Billboard", "Billboard/Pop"}
        # a move writes no new contents; the deleted chart's file is gone
        files_after = set(cs.usb_dir(drive).glob("*.json.gz"))
        assert len(files_after) == 2 and files_after < files_before

    def test_new_edition_on_one_pc_arrives_on_the_other(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Rock", "Year End", years=(1980,))
        pc1.sync()
        pc2.sync()
        with db_session.session_scope() as s:   # PC2 adds an edition
            cid = cs.keyed_charts(s)["Rock/Year End"]
            issue = ChartIssue(chart_id=cid, chart_date=dt.date(1981, 12, 31))
            s.add(issue)
            s.flush()
            s.add(ChartEntry(issue_id=issue.id, rank=1, title="X", artist_name="Y",
                             title_key="x", artist_key="y"))
        assert pc2.sync().charts.charts_out == 1
        assert pc1.sync().charts.charts_in == ["Rock/Year End"]
        with db_session.session_scope() as s:
            assert _charts(s)["Rock/Year End"][:2] == (2, 4)

    def test_hand_fixed_match_travels_locked(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            chart = _make_chart(s, "Rock", "Year End", years=(1980,))
            row = s.scalar(select(ChartEntry).join(ChartIssue).where(
                ChartIssue.chart_id == chart.id, ChartEntry.rank == 3))
            chart_svc.set_entry_track(s, row.id, pc1.track(s, SONGS[1]).id)
        pc1.sync()
        pc2.sync()
        with db_session.session_scope() as s:
            row = s.scalar(select(ChartEntry).where(ChartEntry.rank == 3))
            assert row.match_locked and row.track_id == pc2.track(s, SONGS[1]).id

    def test_same_chart_filed_differently_before_first_sync_is_merged(self, two_pcs):
        """PC1 ran organize_charts_by_source, PC2 hasn't: no duplicates."""
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Billboard/Pop", "Year End")
        pc2.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Pop", "Billboard YE")
        pc1.sync()
        notes = pc2.sync()
        assert notes.charts.charts_in == ["Billboard/Pop/Year End"]
        assert notes.charts.charts_out == 0
        with db_session.session_scope() as s:
            assert list(_charts(s)) == ["Billboard/Pop/Year End"]
            assert _folders(s) == {"Billboard", "Billboard/Pop"}
        assert list(cs.load_index(cs.usb_index_path(drive))) == ["Billboard/Pop/Year End"]
        assert pc1.sync().summary() == "Library data already in sync"

    def test_missing_contents_file_is_retried_not_lost(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            _make_chart(s, "Rock", "Year End")
        pc1.sync()
        (only,) = [p for p in cs.usb_dir(drive).glob("*.json.gz") if p.name != cs.INDEX_NAME]
        hidden = only.with_name("hidden")
        only.rename(hidden)
        notes = pc2.sync()
        assert notes.charts.charts_missing == ["Rock/Year End"]
        assert "not on the drive yet" in notes.summary()
        hidden.rename(only)
        assert pc2.sync().charts.charts_in == ["Rock/Year End"]
        pc1.use()
        with db_session.session_scope() as s:
            assert list(_charts(s)) == ["Rock/Year End"]      # PC1 kept it all along
