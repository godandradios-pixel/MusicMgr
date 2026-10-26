"""Settings › Library health (2026-10-25) - services/library_health.py."""

import datetime as dt
from pathlib import Path

import pytest

from sqlalchemy import func, select

from musicmgr.db.models import (
    Chart, ChartEntry, ChartIssue, Playlist, PlaylistItem, Release, Track,
    WatchedFolder,
)
from musicmgr.services import charts, library_health as lh, scanner
from musicmgr.services.matching import normalize
from tests.test_scanner import make_tagged_mp3
from tests.test_video_scanner import make_placeholder_video


def _library(session, root: Path):
    folder = root / "Rush" / "Moving Pictures"
    make_tagged_mp3(folder / "01 Tom Sawyer.mp3", "Tom Sawyer", "Rush", "Moving Pictures")
    make_tagged_mp3(folder / "02 Red Barchetta.mp3", "Red Barchetta", "Rush", "Moving Pictures")
    session.add(WatchedFolder(path=str(root)))
    session.flush()
    scanner.scan_folder(session, root)
    session.flush()


def _chart(session, rows, date=dt.date(1981, 12, 31)):
    chart = session.scalar(select(Chart).where(Chart.slug == "ye"))
    if chart is None:
        chart = Chart(name="Year End", slug="ye")
        session.add(chart)
        session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=date)
    session.add(issue)
    session.flush()
    out = []
    for rank, title, artist, track_id, locked in rows:
        e = ChartEntry(issue_id=issue.id, rank=rank, title=title, artist_name=artist,
                       title_key=normalize(title), artist_key=normalize(artist),
                       track_id=track_id, match_locked=locked)
        session.add(e)
        out.append(e)
    session.flush()
    return issue, out


def _track(session, title):
    return session.scalar(select(Track).where(Track.title == title))


def test_a_healthy_library_has_no_problems(session, tmp_path):
    _library(session, tmp_path)

    report = lh.check_library(session)

    assert report.problems() == 0, [(f.key, f.examples) for f in report.findings if not f.ok]


def test_files_gone_and_files_never_imported(session, tmp_path):
    _library(session, tmp_path)
    (tmp_path / "Rush" / "Moving Pictures" / "02 Red Barchetta.mp3").unlink()
    make_tagged_mp3(tmp_path / ".38 Special" / "Hold On Loosely.mp3", "Hold On Loosely",
                    ".38 Special", "Wild-Eyed Southern Boys")
    make_placeholder_video(tmp_path / "Videos" / ".38 Special - Second Chance.mp4")

    report = lh.check_library(session)

    missing = report.get("missing_files")
    assert missing.count == 1 and "Red Barchetta" in missing.examples[0]
    assert missing.fix_label == "" and "Purge" in missing.hint
    new = report.get("not_imported")
    assert new.count == 2
    assert {kind for kind, _ in new.data} == {"audio", "video"}

    msg = lh.apply_fix(session, new)

    assert "Imported 2 files" == msg
    assert _track(session, "Hold On Loosely") is not None
    assert lh.check_library(session).get("not_imported").count == 0


def test_loose_chart_matches_are_unlinked_but_hand_fixes_kept(session, tmp_path):
    _library(session, tmp_path)
    sawyer = _track(session, "Tom Sawyer")
    _issue, (good, wrong, weekly, by_hand) = _chart(session, [
        (1, "Tom Sawyer", "Rush", sawyer.id, False),
        (2, "Sawyer", "Rush", sawyer.id, False),          # another song by the act
        (3, "Sawyer", "Rush", sawyer.id, False),          # same pair again
        (4, "Something Else", "Rush", sawyer.id, True),   # fixed by hand
    ])

    f = lh.check_library(session).get("loose_chart_matches")

    assert f.count == 2 and len(f.examples) == 1   # one line per song pair
    lh.apply_fix(session, f)
    assert good.track_id == sawyer.id and by_hand.track_id == sawyer.id
    assert wrong.track_id is None and weekly.track_id is None


def test_broken_links_are_cleaned_up(session, tmp_path):
    _library(session, tmp_path)
    sawyer = _track(session, "Tom Sawyer")
    pl = Playlist(name="Mix")
    session.add(pl)
    session.flush()
    session.add(PlaylistItem(playlist_id=pl.id, track_id=sawyer.id, position=0))
    _issue, (entry,) = _chart(session, [(1, "Tom Sawyer", "Rush", sawyer.id, False)])
    session.commit()
    # a song deleted behind the database's back - foreign keys off, as an
    # old copy of the library or another tool might have done
    raw = session.connection().connection.driver_connection
    raw.commit()
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM media_files WHERE track_id = ?", (sawyer.id,))
    raw.execute("DELETE FROM tracks WHERE id = ?", (sawyer.id,))
    raw.commit()
    raw.execute("PRAGMA foreign_keys=ON")
    session.expire_all()

    f = lh.check_library(session).get("broken_links")

    assert f.count >= 2
    lh.apply_fix(session, f)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(PlaylistItem)) == 0
    assert session.get(ChartEntry, entry.id).track_id is None
    assert lh.check_library(session).get("broken_links").count == 0


def test_stale_chart_playlists_are_saved_again_in_place(session, tmp_path):
    _library(session, tmp_path)
    sawyer = _track(session, "Tom Sawyer")
    barchetta = _track(session, "Red Barchetta")
    issue, (one, two) = _chart(session, [
        (1, "Tom Sawyer", "Rush", sawyer.id, False),
        (2, "Red Barchetta", "Rush", None, False),
    ])
    pl = charts.snapshot_to_playlist(session, issue.id)
    pl.name = "1981"
    assert [i.track_id for i in pl.items] == [sawyer.id]
    two.track_id = barchetta.id      # matched since
    session.flush()

    f = lh.check_library(session).get("stale_chart_playlists")

    assert f.count == 1 and "1981" in f.examples[0]
    lh.apply_fix(session, f)
    session.expire_all()
    pl = session.get(Playlist, pl.id)
    assert pl.name == "1981"
    assert [i.track_id for i in sorted(pl.items, key=lambda i: i.position)] == [sawyer.id, barchetta.id]


def test_edition_gaps_are_listed(session, tmp_path):
    _library(session, tmp_path)
    _chart(session, [(1, "A", "B", None, False), (3, "C", "D", None, False)],
           date=dt.date(2025, 12, 31))

    f = lh.check_library(session).get("edition_gaps")

    assert f.count == 1 and f.fix_label == ""
    assert "Year End  2025: 2 of 3 positions" in f.examples[0]


def test_empty_albums_are_removed(session, tmp_path):
    _library(session, tmp_path)
    some = session.scalar(select(Release))
    session.add(Release(title="Nothing Here", title_key="nothing here",
                        album_artist_id=some.album_artist_id))
    session.flush()

    f = lh.check_library(session).get("empty_albums")

    assert f.examples == ["Nothing Here"]
    assert lh.apply_fix(session, f) == "Removed 1 empty album"


def test_backup_before_fix(tmp_path):
    import sqlite3

    db = tmp_path / "library.db"
    con = sqlite3.connect(db)
    con.execute("create table tracks (id integer primary key)")
    con.commit()
    con.close()

    dest = lh.backup_before_fix(db, tmp_path / "backups")

    assert dest.name.startswith("library-before-health-fix-") and dest.is_file()


# -- the Settings row and the results dialog ----------------------------------


def test_settings_has_the_tool(ctx):
    from musicmgr.ui.views.settings import SettingsView

    view = SettingsView(ctx)
    assert hasattr(view, "check_library_health")
    assert view._health_thread is None


def test_dialog_lists_problems_first_and_runs_a_fix(ctx, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from musicmgr.ui.widgets import library_health as ui

    ok = lh.Finding("empty_albums", "Albums with no songs", "…", fix_label="Remove them")
    bad = lh.Finding("loose_chart_matches", "Wrong song", "…", count=2,
                     examples=["A - B → C - B"], fix_label="Unlink them", data=[1, 2])
    gaps = lh.Finding("edition_gaps", "Gaps", "…", count=1, examples=["x"], hint="Add them")
    dialog = ui.LibraryHealthDialog(lh.HealthReport([ok, bad, gaps]))

    assert [c.finding.key for c in dialog.cards] == ["loose_chart_matches", "edition_gaps",
                                                     "empty_albums"]
    card = dialog.cards[0]
    assert card.status.text() == "2 found" and not card.fix_btn.isHidden()
    assert dialog.cards[1].fix_btn is None and not dialog.cards[1].hint.isHidden()
    assert dialog.cards[2].status.text() == "✓ None" and dialog.cards[2].fix_btn.isHidden()

    ran, got = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(lh, "backup_before_fix", lambda *a, **k: ran.append("backup"))
    monkeypatch.setattr(lh, "apply_fix", lambda s, f: ran.append(f.key) or "Unlinked 2")
    dialog.fixed.connect(lambda key, msg: got.append((key, msg)))
    # run the fix thread on this thread (the test database is per thread)
    monkeypatch.setattr(ui.HealthFixThread, "start", lambda self: self.run())

    dialog._fix(card)

    assert ran == ["backup", "loose_chart_matches"]
    assert got == [("loose_chart_matches", "Unlinked 2")]
    assert card.status.text() == "Fixed" and card.fix_btn.isHidden()


def test_dialog_fix_stops_when_the_backup_fails(ctx, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from musicmgr.ui.widgets import library_health as ui

    bad = lh.Finding("empty_albums", "Albums", "…", count=1, examples=["x"],
                     fix_label="Remove them")
    dialog = ui.LibraryHealthDialog(lh.HealthReport([bad]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))

    def no_backup(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(lh, "backup_before_fix", no_backup)
    monkeypatch.setattr(lh, "apply_fix", lambda *a: pytest.fail("fixed without a backup"))
    monkeypatch.setattr(ui.HealthFixThread, "start", lambda self: self.run())

    dialog._fix(dialog.cards[0])

    assert "couldn't be backed up" in warned[0]
    assert dialog.cards[0].status.text() == "1 found"
