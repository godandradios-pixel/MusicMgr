"""tools/import_windows_library.py - installing the Windows library.db on
Linux with every Windows path repointed. The "Windows" database is built with
the app's own schema; its paths are D:\\ strings, and the "Linux" music tree
and data folders are real files under tmp_path."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from musicmgr.db import session as db_session
from musicmgr.db.models import Artist, MediaFile, Release, Track, WatchedFolder
from musicmgr.services import jukebox
from musicmgr.services.matching import normalize

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("import_windows_library", TOOLS / "import_windows_library.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


@pytest.fixture
def setup(tmp_path, qapp):
    usb = tmp_path / "USB" / "MusicMgr" / "data"
    (usb / "artwork").mkdir(parents=True)
    (usb / "artists").mkdir()
    (usb / "artwork" / "7.jpg").write_bytes(b"cover")
    (usb / "artists" / "boston.jpg").write_bytes(b"portrait")

    db_session.init_engine(db_path=usb / "library.db")
    with db_session.session_scope() as s:
        boston = Artist(name="Boston", name_key="boston",
                        image_path="D:\\MusicMgr\\data\\artists\\boston.jpg")
        s.add(boston)
        s.flush()
        rel = Release(title="Boston", title_key="boston", album_artist_id=boston.id,
                      cover_path="D:\\MusicMgr\\data\\artwork\\7.jpg")
        s.add(rel)
        s.flush()
        tracks = {}
        for title, path in (
            ("More Than a Feeling", "D:\\Music\\Boston\\Boston\\01 More Than a Feeling.mp3"),
            ("Peace of Mind", "D:\\Music\\Boston\\Boston\\02 PEACE OF MIND.mp3"),  # case differs on disk
            ("Hitch a Ride", "D:\\Music\\Boston\\Boston\\05 Hitch a Ride.mp3"),     # not copied to Linux
            ("Bonus", "D:\\MusicMP3\\Boston\\bonus.mp3"),                            # unmapped root
        ):
            t = Track(release_id=rel.id, title=title, title_key=normalize(title), rating=5)
            s.add(t)
            s.flush()
            s.add(MediaFile(track_id=t.id, path=path))
            tracks[title] = t.id
        s.add(WatchedFolder(path="D:\\Music"))
        jukebox.place_track(s, boston.id, tracks["More Than a Feeling"], "Classic Rock")
        jukebox.place_track(s, boston.id, tracks["Peace of Mind"], "Classic Rock")
    db_session.get_engine().dispose()

    music = tmp_path / "home" / "Music" / "Boston" / "Boston"
    music.mkdir(parents=True)
    (music / "01 More Than a Feeling.mp3").write_bytes(b"x")
    (music / "02 Peace of Mind.mp3").write_bytes(b"x")
    target = tmp_path / "home" / "MusicMgr" / "data"
    return usb, target, tmp_path / "home" / "Music"


def run(usb, target, music, *extra):
    return tool.main(["--source", str(usb), "--target", str(target), "--music", str(music), *extra])


def paths(db_path):
    db = sqlite3.connect(db_path)
    try:
        return {
            "media": dict(db.execute(
                "SELECT t.title, m.path FROM media_files m JOIN tracks t ON t.id = m.track_id").fetchall()),
            "cover": db.execute("SELECT cover_path FROM releases").fetchone()[0],
            "portrait": db.execute("SELECT image_path FROM artists").fetchone()[0],
            "watched": [p for (p,) in db.execute("SELECT path FROM watched_folders")],
            "jukebox": db.execute("SELECT COUNT(*) FROM jukebox_slots").fetchone()[0],
            "ratings": [r for (r,) in db.execute("SELECT rating FROM tracks")],
        }
    finally:
        db.close()


def test_dry_run_changes_nothing(setup, capsys):
    usb, target, music = setup
    before = (usb / "library.db").read_bytes()
    assert run(usb, target, music, "--dry-run") == 0
    assert not (target / "library.db").exists()
    assert (usb / "library.db").read_bytes() == before
    out = capsys.readouterr().out
    assert "D:\\Music" in out and "D:\\MusicMP3" in out and "not mapped" in out


def test_installs_with_linux_paths(setup):
    usb, target, music = setup
    before = (usb / "library.db").read_bytes()
    assert run(usb, target, music) == 0
    p = paths(target / "library.db")
    assert p["media"]["More Than a Feeling"] == str(music / "Boston/Boston/01 More Than a Feeling.mp3")
    expected = str(music / "Boston/Boston/02 Peace of Mind.mp3")
    if (music / "Boston/Boston/02 PEACE OF MIND.mp3").exists():
        # Windows/macOS: the file system ignores case, so the stored spelling
        # already opens the file and the tool has nothing to fix (the tool
        # targets Linux, where the case fix matters)
        assert p["media"]["Peace of Mind"].casefold() == expected.casefold()
    else:
        assert p["media"]["Peace of Mind"] == expected  # case fixed
    assert p["media"]["Hitch a Ride"] == str(music / "Boston/Boston/05 Hitch a Ride.mp3")  # mapped, missing
    assert p["media"]["Bonus"] == "D:\\MusicMP3\\Boston\\bonus.mp3"  # left alone
    assert p["cover"] == str(target / "artwork" / "7.jpg") and (target / "artwork" / "7.jpg").is_file()
    assert p["portrait"] == str(target / "artists" / "boston.jpg")
    assert p["watched"] == [str(music)]
    assert p["jukebox"] == 1 and p["ratings"] == [5, 5, 5, 5]
    assert (usb / "library.db").read_bytes() == before  # USB never written


def test_extra_map(setup, tmp_path):
    usb, target, music = setup
    mp3 = tmp_path / "home" / "MusicMP3" / "Boston"
    mp3.mkdir(parents=True)
    (mp3 / "bonus.mp3").write_bytes(b"x")
    run(usb, target, music, "--map", f"D:\\MusicMP3={mp3.parent}")
    assert paths(target / "library.db")["media"]["Bonus"] == str(mp3 / "bonus.mp3")


def test_existing_target_is_backed_up(setup):
    usb, target, music = setup
    target.mkdir(parents=True)
    old = sqlite3.connect(target / "library.db")
    old.execute("CREATE TABLE marker (x)")
    old.commit()
    old.close()
    run(usb, target, music)
    backups = list(target.glob("library-before-windows-import-*.db"))
    assert len(backups) == 1
    b = sqlite3.connect(backups[0])
    assert b.execute("SELECT name FROM sqlite_master WHERE name='marker'").fetchone()
    b.close()
    assert paths(target / "library.db")["jukebox"] == 1


def test_app_opens_the_result(setup):
    usb, target, music = setup
    run(usb, target, music)
    db_session.init_engine(db_path=target / "library.db")  # runs the app's own migrations
    with db_session.session_scope() as s:
        assert s.query(Track).count() == 4
    db_session.get_engine().dispose()


def test_map_path_helpers():
    maps = [(["d:", "music"], Path("/home/j/Music"))]
    assert tool.map_path("d:\\MUSIC\\a\\b.mp3", maps) == Path("/home/j/Music/a/b.mp3")
    assert tool.map_path("E:\\Music\\a.mp3", maps) is None
    assert tool.root_of("D:\\Music\\a\\b.mp3") == "D:\\Music"
