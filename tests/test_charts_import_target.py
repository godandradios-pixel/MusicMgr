"""Charts page "Import chart CSV…" adds to the selected chart (2026-09-30 -
James: "change Import chart CSV… to add to whichever chart you have
selected instead of asking for a name", after a weekly Hot 100 file went
into the Pop Year End chart through the old name box)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox
from sqlalchemy import func, select

from musicmgr.db.models import Chart, ChartIssue
from musicmgr.services import charts as chart_svc
from musicmgr.ui.views.charts import ChartsView


def _csv(path: Path, dates: list[str]) -> Path:
    rows = ["date,rank,title,artist"]
    for d in dates:
        rows += [f"{d},1,Song A,Artist A", f"{d},2,Song B,Artist B"]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _view(ctx, monkeypatch, files, confirm=True, name=None) -> ChartsView:
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    view = ChartsView(ctx)
    view._ask_csv_files = lambda: [str(f) for f in files]
    view.asked = []
    view._confirm_add = lambda label, n: view.asked.append(("confirm", label, n)) or confirm
    view._ask_new_chart_name = lambda s: view.asked.append(("name", s)) or name
    return view


def _editions(session, chart_id) -> list[str]:
    return sorted(d.isoformat() for d in session.scalars(
        select(ChartIssue.chart_date).where(ChartIssue.chart_id == chart_id)))


class TestImportTarget:
    def _setup(self, session):
        billboard = chart_svc.create_folder(session, "Billboard")
        pop = chart_svc.create_folder(session, "Pop", billboard.id)
        year_end = Chart(name="Year End", slug="billboard-hot-100", folder_id=pop.id)
        weekly = Chart(name="Weekly", slug="billboard-hot-100-weekly", folder_id=pop.id)
        session.add_all([year_end, weekly])
        session.flush()
        return pop, year_end, weekly

    def test_adds_to_the_selected_chart(self, ctx, session, tmp_path, monkeypatch):
        pop, year_end, weekly = self._setup(session)
        session.commit()
        view = _view(ctx, monkeypatch, [_csv(tmp_path / "Billboard Hot 100.csv", ["2023-08-22"])])
        view._selected_type, view._selected_id = "chart", weekly.id
        view.import_csv()
        assert view.asked == [("confirm", "Billboard › Pop › Weekly", 1)]
        with ctx.session() as s:
            assert _editions(s, weekly.id) == ["2023-08-22"]
            assert _editions(s, year_end.id) == []          # the old trap
            assert s.scalar(select(func.count()).select_from(Chart)) == 2

    def test_edition_selected_means_its_chart(self, ctx, session, tmp_path, monkeypatch):
        _pop, _ye, weekly = self._setup(session)
        issue = ChartIssue(chart_id=weekly.id, chart_date=__import__("datetime").date(2023, 8, 15))
        session.add(issue)
        session.commit()
        view = _view(ctx, monkeypatch, [_csv(tmp_path / "w.csv", ["2023-08-22"])])
        view._selected_type, view._selected_id = "issue", issue.id
        view.import_csv()
        with ctx.session() as s:
            assert _editions(s, weekly.id) == ["2023-08-15", "2023-08-22"]

    def test_cancelled_confirm_imports_nothing(self, ctx, session, tmp_path, monkeypatch):
        _pop, _ye, weekly = self._setup(session)
        session.commit()
        view = _view(ctx, monkeypatch, [_csv(tmp_path / "w.csv", ["2023-08-22"])], confirm=False)
        view._selected_type, view._selected_id = "chart", weekly.id
        view.import_csv()
        with ctx.session() as s:
            assert _editions(s, weekly.id) == []

    def test_folder_selected_makes_a_new_chart_even_if_the_name_is_taken(
        self, ctx, session, tmp_path, monkeypatch
    ):
        pop, year_end, _weekly = self._setup(session)
        session.commit()
        view = _view(ctx, monkeypatch, [_csv(tmp_path / "x.csv", ["2024-01-02"])],
                     name="Billboard Hot 100")
        view._selected_type, view._selected_id = "folder", pop.id
        view.import_csv()
        with ctx.session() as s:
            new = s.scalar(select(Chart).where(Chart.name == "Billboard Hot 100"))
            assert new is not None and new.id != year_end.id and new.folder_id == pop.id
            assert new.slug == "billboard-hot-100-2"
            assert _editions(s, new.id) == ["2024-01-02"] and _editions(s, year_end.id) == []

    def test_blank_name_makes_one_chart_per_file(self, ctx, session, tmp_path, monkeypatch):
        files = [_csv(tmp_path / "rock_1990.csv", ["1990-12-31"]),
                 _csv(tmp_path / "pop_1990.csv", ["1990-12-31"])]
        view = _view(ctx, monkeypatch, files, name="")
        view.import_csv()
        with ctx.session() as s:
            assert sorted(s.scalars(select(Chart.name))) == ["Pop 1990", "Rock 1990"]


class TestRematchEdition:
    def test_rematch_touches_only_the_edition_on_screen(self, session, tmp_path):
        import datetime as dt
        from musicmgr.db.models import ChartEntry

        chart = chart_svc.create_chart(session, "Weekly")
        issues = []
        for d in (dt.date(2026, 9, 22), dt.date(2026, 9, 29)):
            issue = ChartIssue(chart_id=chart.id, chart_date=d)
            session.add(issue)
            session.flush()
            session.add(ChartEntry(issue_id=issue.id, rank=1, title="Song", artist_name="Band",
                                   title_key="song", artist_key="band", track_id=None))
            issues.append(issue)
        session.flush()
        calls = []
        orig = chart_svc.match_entry
        chart_svc.match_entry = lambda s, e, t=0.72: calls.append(e.issue_id) or orig(s, e, t)
        try:
            assert chart_svc.rematch_issue(session, issues[1].id) == (0, 1)
        finally:
            chart_svc.match_entry = orig
        assert calls == [issues[1].id]

    def test_whole_chart_rematch_looks_each_song_up_once(self, session):
        import datetime as dt
        from musicmgr.db.models import ChartEntry

        chart = chart_svc.create_chart(session, "Weekly")
        for w in range(5):
            issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2026, 1, 6) + dt.timedelta(weeks=w))
            session.add(issue)
            session.flush()
            session.add(ChartEntry(issue_id=issue.id, rank=1, title="Song", artist_name="Band",
                                   title_key="song", artist_key="band"))
        session.flush()
        calls = []
        orig = chart_svc.match_entry
        chart_svc.match_entry = lambda s, e, t=0.72: calls.append(1) or orig(s, e, t)
        try:
            chart_svc.rematch_chart(session, chart.id)
        finally:
            chart_svc.match_entry = orig
        assert len(calls) == 1
