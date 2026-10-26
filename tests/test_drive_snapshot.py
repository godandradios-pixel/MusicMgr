"""USB sync leaves a copy of the library on the drive (2026-10-25).

services/library_backup.py:save_drive_snapshot - the folder the drive's
Install-MusicMgr.bat and Copy-USB-to-D.bat pick up (newest data* with a
library.db)."""

import datetime as dt
import json
import sqlite3

from musicmgr.services import library_backup as lb


def _library(data_dir, tracks=3):
    data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data_dir / "library.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table tracks (id integer primary key, title text)")
    con.executemany("insert into tracks (title) values (?)", [(f"Song {i}",) for i in range(tracks)])
    con.commit()
    return con  # left open: the -wal holds the rows, as in the running app


def _setup(tmp_path):
    data = tmp_path / "pc" / "data"
    con = _library(data)
    (data / "sync").mkdir()
    (data / "sync" / "library-state-base-abc.json.gz").write_bytes(b"base")
    (data / "stations").mkdir()
    (data / "stations" / "12.png").write_bytes(b"png")
    (data / "video_thumbs").mkdir()
    (data / "video_thumbs" / "x.jpg").write_bytes(b"jpg")
    (data / "artwork").mkdir()
    (data / "artwork" / "cover.jpg").write_bytes(b"big")
    (data / "appearance.json").write_text("{}")
    (data / "last_run_version.txt").write_text("2.4.3")
    (data / "musicmgr.log").write_text("log")
    app = tmp_path / "usb" / "MusicMgr"
    app.mkdir(parents=True)
    return data, app, con


def test_snapshot_holds_the_library_and_its_sync_bases(tmp_path):
    data, app, con = _setup(tmp_path)

    res = lb.save_drive_snapshot(data, app, pc_name="PC1", version="2.4.3",
                                 today=dt.date(2026, 10, 25))
    con.close()

    assert res.error == ""
    snap = app / "data-20261025"
    assert res.path == snap
    # one self-contained file with the rows that were only in the -wal
    assert not (snap / "library.db-wal").exists()
    copy = sqlite3.connect(snap / "library.db")
    assert copy.execute("select count(*) from tracks").fetchone()[0] == 3
    copy.close()
    assert (snap / "sync" / "library-state-base-abc.json.gz").read_bytes() == b"base"
    assert (snap / "stations" / "12.png").exists()
    assert (snap / "video_thumbs" / "x.jpg").exists()
    assert (snap / "appearance.json").exists()
    assert (snap / "last_run_version.txt").read_text() == "2.4.3"
    # covers already live on the drive; the log belongs to this PC
    assert not (snap / "artwork").exists()
    assert not (snap / "musicmgr.log").exists()
    marker = json.loads((snap / lb.SNAPSHOT_MARKER).read_text())
    assert marker["pc"] == "PC1" and marker["version"] == "2.4.3"
    assert not (app / ".snapshot-in-progress").exists()


def test_same_day_replaces_and_only_the_newest_two_are_kept(tmp_path):
    data, app, con = _setup(tmp_path)
    by_hand = app / "data-20261016"   # copied by hand: not ours, never pruned
    by_hand.mkdir()
    (by_hand / "library.db").write_bytes(b"old")

    for day in (20, 21, 22, 22):
        assert lb.save_drive_snapshot(data, app, today=dt.date(2026, 10, day)).error == ""
    con.close()

    assert [p.name for p in lb.snapshot_folders(app)] == ["data-20261022", "data-20261021"]
    assert by_hand.exists()


def test_a_hand_made_folder_with_todays_name_is_left_alone(tmp_path):
    data, app, con = _setup(tmp_path)
    by_hand = app / "data-20261025"
    by_hand.mkdir()
    (by_hand / "library.db").write_bytes(b"old")

    res = lb.save_drive_snapshot(data, app, today=dt.date(2026, 10, 25))
    con.close()

    assert res.path == app / "data-20261025-1"
    assert (by_hand / "library.db").read_bytes() == b"old"


def test_a_failed_copy_leaves_no_half_snapshot(tmp_path, monkeypatch):
    data, app, con = _setup(tmp_path)

    def boom(*a, **k):
        raise OSError("drive pulled")
    monkeypatch.setattr(lb.shutil, "copytree", boom)

    res = lb.save_drive_snapshot(data, app, today=dt.date(2026, 10, 25))
    con.close()

    assert "drive pulled" in res.error
    assert res.path is None
    assert list(app.iterdir()) == []


def test_not_enough_space(tmp_path, monkeypatch):
    data, app, con = _setup(tmp_path)
    monkeypatch.setattr(lb.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())

    res = lb.save_drive_snapshot(data, app)
    con.close()

    assert "not enough space" in res.error
    assert list(app.iterdir()) == []


def test_usb_sync_saves_the_copy_after_the_library_data(ctx, tmp_path, monkeypatch):
    """UsbSyncThread step 4: on by default, off by the Settings box, and
    never for a test's in-memory database."""
    from types import SimpleNamespace

    from musicmgr.db import session as dbs
    from musicmgr.services import updater, usb_sync as us
    from musicmgr.ui.widgets.usb_sync import UsbSyncThread

    drive = us.Drive(tmp_path / "USB", "id", "Test USB")
    thread = UsbSyncThread(SimpleNamespace(drive=drive))
    calls = []
    monkeypatch.setattr(lb, "save_drive_snapshot", lambda data, app, **kw: calls.append(
        (app, kw["db_path"])) or lb.SnapshotResult(path=app / "data-20261025"))

    assert thread._save_snapshot("Files copied") == "Files copied"   # :memory:
    assert calls == []

    fake = SimpleNamespace(url=SimpleNamespace(database=str(tmp_path / "library.db")))
    monkeypatch.setattr(dbs, "get_engine", lambda: fake)
    assert thread._save_snapshot("Files copied") == \
        "Files copied · Library copy saved on the drive (data-20261025)"
    assert calls == [(tmp_path / "USB" / "MusicMgr", tmp_path / "library.db")]

    with ctx.session() as db:
        updater.set_pref(db, lb.PREF_DRIVE_SNAPSHOT, "0")
        db.commit()
    assert thread._save_snapshot("Files copied") == "Files copied"
    assert len(calls) == 1
