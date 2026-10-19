"""services/library_backup.py - daily backups and restore (2026-10-19)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from musicmgr.services import library_backup as lb


def make_library(path: Path, marker: str = "original", wal_extra: str | None = None):
    """A tiny WAL-mode "library" with a tracks table holding `marker`. With
    `wal_extra`, returns an open connection whose last insert is still only
    in the -wal file (the app's own state while running)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE IF NOT EXISTS tracks (title TEXT)")
    con.execute("INSERT INTO tracks VALUES (?)", (marker,))
    con.commit()
    if wal_extra is None:
        con.close()
        return None
    con.execute("INSERT INTO tracks VALUES (?)", (wal_extra,))
    con.commit()
    return con


def titles(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT title FROM tracks ORDER BY rowid")]
    finally:
        con.close()


class TestDailyBackup:
    def test_once_a_day_and_includes_changes_still_in_the_wal(self, tmp_path):
        db = tmp_path / "library.db"
        live = make_library(db, wal_extra="unsaved")
        backups = tmp_path / "backups"
        day = dt.date(2026, 10, 19)

        first = lb.make_daily_backup(db, backups, today=day)
        second = lb.make_daily_backup(db, backups, today=day)
        live.close()

        assert first.path == backups / "library-daily-2026-10-19.db" and not first.error
        assert second.skipped and second.path == first.path
        assert titles(first.path) == ["original", "unsaved"]
        assert not Path(str(first.path) + "-wal").exists()
        assert sorted(p.name for p in backups.iterdir()) == ["library-daily-2026-10-19.db"]

    def test_back_up_now_replaces_todays_copy(self, tmp_path):
        db = tmp_path / "library.db"
        make_library(db)
        backups = tmp_path / "backups"
        day = dt.date(2026, 10, 19)
        lb.make_daily_backup(db, backups, today=day)
        make_library(db, marker="later")

        result = lb.make_daily_backup(db, backups, today=day, force=True)

        assert titles(result.path) == ["original", "later"]

    def test_keeps_the_last_seven(self, tmp_path):
        db = tmp_path / "library.db"
        make_library(db)
        backups = tmp_path / "backups"
        (backups).mkdir()
        (backups / "library-before-2.2.7.db").write_bytes(b"x")  # not a daily copy
        for i in range(10):
            lb.make_daily_backup(db, backups, today=dt.date(2026, 10, 1) + dt.timedelta(days=i))

        daily = sorted(p.name for p in backups.glob("library-daily-*.db"))

        assert daily == [f"library-daily-2026-10-{d:02d}.db" for d in range(4, 11)]
        assert (backups / "library-before-2.2.7.db").exists()

    def test_a_damaged_copy_is_not_kept_and_nothing_is_pruned(self, tmp_path, monkeypatch):
        db = tmp_path / "library.db"
        make_library(db)
        backups = tmp_path / "backups"
        for i in range(7):
            lb.make_daily_backup(db, backups, today=dt.date(2026, 10, 1) + dt.timedelta(days=i))
        monkeypatch.setattr(lb, "check_database", lambda path: "the copy failed its check (bad page)")

        result = lb.make_daily_backup(db, backups, today=dt.date(2026, 10, 19))

        assert result.path is None and "earlier backups were kept" in result.error
        assert len(list(backups.glob("library-daily-*.db"))) == 7
        assert not list(backups.glob("*.new")) and not list(backups.glob("*.partial"))

    def test_no_library_yet_is_reported_not_raised(self, tmp_path):
        result = lb.make_daily_backup(tmp_path / "library.db", tmp_path / "backups")

        assert result.path is None and result.error


class TestListBackups:
    def test_newest_first_with_kinds(self, tmp_path):
        import os

        backups = tmp_path / "backups"
        backups.mkdir()
        for i, name in enumerate([
            "library-before-2.2.6.db", "library-daily-2026-10-18.db",
            "library-pre-restore-20261019-090000.db", "library-before-orphan-cleanup.db",
        ]):
            path = backups / name
            path.write_bytes(b"x" * 10)
            os.utime(path, (1_790_000_000 + i * 60, 1_790_000_000 + i * 60))
        (backups / "library-before-1.7.4.db-wal").write_bytes(b"x")
        (backups / "empty.db").write_bytes(b"")

        found = lb.list_backups(backups)

        assert [(b.path.name, b.kind) for b in found] == [
            ("library-before-orphan-cleanup.db", "Before update"),
            ("library-pre-restore-20261019-090000.db", "Before restore"),
            ("library-daily-2026-10-18.db", "Daily"),
            ("library-before-2.2.6.db", "Before update"),
        ]


class TestRestore:
    def test_restore_swaps_the_library_and_saves_the_current_one(self, tmp_path):
        import shutil

        work = tmp_path / "running"
        db = tmp_path / "data" / "library.db"
        backups = tmp_path / "data" / "backups"
        marker = tmp_path / "data" / "restore_pending.txt"
        make_library(work / "library.db", marker="old")
        saved = lb.make_daily_backup(work / "library.db", backups, today=dt.date(2026, 10, 18)).path
        # the library as a closed (or crashed) app leaves it: changes still
        # sitting in -wal next to library.db
        live = make_library(work / "library.db", marker="new", wal_extra="still in wal")
        for name in ("library.db", "library.db-wal", "library.db-shm"):
            shutil.copyfile(work / name, db.parent / name)
        live.close()
        assert Path(str(db) + "-wal").exists()
        assert lb.schedule_restore(saved, marker) == ""
        assert lb.pending_restore(marker) == saved

        outcome = lb.apply_pending_restore(db, backups, marker)

        assert not outcome.error and outcome.restored_from == saved
        assert not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists()
        assert titles(db) == ["old"]
        assert titles(outcome.saved_current_as) == ["old", "new", "still in wal"]
        assert outcome.saved_current_as.name.startswith("library-pre-restore-")
        assert not marker.exists()

    def test_nothing_pending(self, tmp_path):
        assert lb.apply_pending_restore(tmp_path / "library.db", tmp_path, tmp_path / "m.txt") is None

    def test_a_backup_that_vanished_leaves_the_library_alone(self, tmp_path):
        db = tmp_path / "library.db"
        make_library(db)
        marker = tmp_path / "restore_pending.txt"
        marker.write_text(str(tmp_path / "gone.db"))

        outcome = lb.apply_pending_restore(db, tmp_path / "backups", marker)

        assert "wasn't changed" in outcome.error
        assert titles(db) == ["original"]
        assert not marker.exists()

    def test_schedule_refuses_a_file_that_isnt_a_library(self, tmp_path):
        junk = tmp_path / "junk.db"
        sqlite3.connect(junk).execute("CREATE TABLE other (x)").connection.close()
        marker = tmp_path / "restore_pending.txt"

        assert "isn't a MusicMgr library" in lb.schedule_restore(junk, marker)
        assert not marker.exists()
