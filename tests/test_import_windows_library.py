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


# -- 2026-10-26: drive snapshots, newer tables, sync identity ---------------------


@pytest.fixture
def snapshot(tmp_path, qapp):
    """A library copy as "Save a copy of the library on the drive" writes it:
    USB/MusicMgr/data-20261026 holds library.db, stations/ and video_thumbs/,
    while covers and artist pictures live in USB/MusicMgr/artwork and
    /artists. The database belongs to a PC that has synced (sync id, pairs,
    sync records) and has radio shows, station logos, video thumbnails and
    a playlist-folder tile in a subfolder."""
    usb_mm = tmp_path / "USB" / "MusicMgr"
    data = usb_mm / "data-20261026"
    (usb_mm / "artwork" / "Folders").mkdir(parents=True)
    (usb_mm / "artists").mkdir()
    (usb_mm / "artwork" / "Boston - Boston.jpg").write_bytes(b"cover")
    (usb_mm / "artwork" / "Folders" / "rock.jpg").write_bytes(b"tile")
    (usb_mm / "artists" / "Boston.jpg").write_bytes(b"portrait")
    (data / "stations").mkdir(parents=True)
    (data / "video_thumbs").mkdir()
    (data / "stations" / "2.png").write_bytes(b"logo")
    (data / "video_thumbs" / "abc.jpg").write_bytes(b"thumb")

    db_session.init_engine(db_path=data / "library.db")
    with db_session.session_scope() as s:
        boston = Artist(name="Boston", name_key="boston", image_path="D:\\MusicMgr\\data\\artists\\Boston.jpg")
        s.add(boston)
        s.flush()
        rel = Release(title="Boston", title_key="boston", album_artist_id=boston.id,
                      cover_path="D:\\MusicMgr\\data\\artwork\\Boston - Boston.jpg")
        s.add(rel)
        s.flush()
        t = Track(release_id=rel.id, title="Smokin", title_key="smokin")
        s.add(t)
        s.flush()
        s.add(MediaFile(track_id=t.id, path="D:\\MusicFLAC\\Boston\\Boston\\06 Smokin.flac"))
        s.add(WatchedFolder(path="D:\\MusicFLAC"))
    db_session.get_engine().dispose()

    db = sqlite3.connect(data / "library.db")
    now = "2026-10-26 12:00:00"
    db.executescript(f"""
        INSERT INTO settings (key, value) VALUES ('sync_pc_id', '6bd7433e53dd462ea993a824bbf64a10');
        INSERT INTO settings (key, value) VALUES ('otr_root', 'D:/Radio');
        INSERT INTO settings (key, value) VALUES ('usb_sync_auto_check', '0');
        INSERT INTO sync_pairs (id, drive_id, local_path, usb_rel_path, kind, enabled)
            VALUES (1, 'drive1', 'D:\\MusicFLAC', 'MusicFLAC', 'media', 1);
        INSERT INTO sync_baseline (pair_id, rel_path, size, mtime_ns)
            VALUES (1, 'Boston/Boston/06 Smokin.flac', 1, 1);
        INSERT INTO radio_shows (id, name, folder, kind, cover_path, band, is_missing)
            VALUES (1, 'Dragnet', 'D:\\Radio\\Dragnet', 'series', 'D:\\Radio\\Dragnet\\cover.jpg', 'am', 0);
        INSERT INTO radio_episodes (id, show_id, path, title, sort_key, played, position_ms, rg_failed, is_missing)
            VALUES (1, 1, 'D:\\Radio\\Dragnet\\Dragnet 1949-06-03.mp3', 'Dragnet', 'a', 0, 0, 0, 0);
        INSERT INTO radio_stations (id, name, stream_url, favicon_path, dial_order, band, created_at)
            VALUES (1, 'WGYY', 'https://x', 'D:\\MusicMgr\\data\\stations\\2.png', 1, 'fm', '{now}');
        INSERT INTO playlist_folders (id, name, created_at, cover_path)
            VALUES (1, 'Rock', '{now}', 'D:\\MusicMgr\\data\\artwork\\Folders\\rock.jpg');
        INSERT INTO videos (id, title, title_key, path, thumbnail_path, is_missing, play_count,
                            added_at, last_seen_at)
            VALUES (1, 'Amanda', 'amanda', 'D:\\Videos\\Boston - Amanda.mp4',
                    'D:\\MusicMgr\\data\\video_thumbs\\abc.jpg', 0, 0, '{now}', '{now}');
    """)
    db.commit()
    db.close()

    media = tmp_path / "data"
    (media / "MusicFLAC" / "Boston" / "Boston").mkdir(parents=True)
    (media / "MusicFLAC" / "Boston" / "Boston" / "06 Smokin.flac").write_bytes(b"x")
    (media / "Radio" / "Dragnet").mkdir(parents=True)
    (media / "Radio" / "Dragnet" / "Dragnet 1949-06-03.mp3").write_bytes(b"x")
    (media / "Radio" / "Dragnet" / "cover.jpg").write_bytes(b"x")
    (media / "Videos").mkdir()
    (media / "Videos" / "Boston - Amanda.mp4").write_bytes(b"x")
    target = tmp_path / "home" / "MusicMgr" / "data"
    return data, target, media


def run_snapshot(data, target, media, *extra):
    maps = []
    for name in ("MusicFLAC", "Videos", "Movies", "Radio"):
        maps += ["--map", f"D:\\{name}={media / name}"]
    return tool.main(["--source", str(data), "--target", str(target),
                      "--music", str(media / "Music"), *maps, *extra])


def one(db_path, sql):
    db = sqlite3.connect(db_path)
    try:
        row = db.execute(sql).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def test_snapshot_pictures_come_from_the_drive_folders(snapshot):
    data, target, media = snapshot
    assert run_snapshot(data, target, media) == 0
    db = target / "library.db"
    assert one(db, "SELECT cover_path FROM releases") == str(target / "artwork" / "Boston - Boston.jpg")
    assert one(db, "SELECT image_path FROM artists") == str(target / "artists" / "Boston.jpg")
    assert one(db, "SELECT cover_path FROM playlist_folders") == str(target / "artwork" / "Folders" / "rock.jpg")
    assert one(db, "SELECT favicon_path FROM radio_stations") == str(target / "stations" / "2.png")
    assert one(db, "SELECT thumbnail_path FROM videos") == str(target / "video_thumbs" / "abc.jpg")
    for rel in ("artwork/Boston - Boston.jpg", "artwork/Folders/rock.jpg", "artists/Boston.jpg",
                "stations/2.png", "video_thumbs/abc.jpg"):
        assert (target / rel).is_file(), rel


def test_media_radio_and_path_settings_are_repointed(snapshot):
    data, target, media = snapshot
    run_snapshot(data, target, media)
    db = target / "library.db"
    assert one(db, "SELECT path FROM media_files") == str(media / "MusicFLAC/Boston/Boston/06 Smokin.flac")
    assert one(db, "SELECT path FROM watched_folders") == str(media / "MusicFLAC")
    assert one(db, "SELECT path FROM videos") == str(media / "Videos/Boston - Amanda.mp4")
    assert one(db, "SELECT folder FROM radio_shows") == str(media / "Radio/Dragnet")
    assert one(db, "SELECT cover_path FROM radio_shows") == str(media / "Radio/Dragnet/cover.jpg")
    assert one(db, "SELECT path FROM radio_episodes") == str(media / "Radio/Dragnet/Dragnet 1949-06-03.mp3")
    assert one(db, "SELECT value FROM settings WHERE key = 'otr_root'") == str(media / "Radio")
    assert one(db, "SELECT value FROM settings WHERE key = 'usb_sync_auto_check'") == "0"


def test_sync_identity_is_cleared_for_a_new_pc(snapshot):
    data, target, media = snapshot
    before = (data / "library.db").read_bytes()
    run_snapshot(data, target, media)
    db = target / "library.db"
    assert one(db, "SELECT value FROM settings WHERE key = 'sync_pc_id'") is None
    assert one(db, "SELECT COUNT(*) FROM sync_pairs") == 0
    assert one(db, "SELECT COUNT(*) FROM sync_baseline") == 0
    assert (data / "library.db").read_bytes() == before  # the drive's copy keeps it


def test_keep_sync_identity(snapshot):
    data, target, media = snapshot
    run_snapshot(data, target, media, "--keep-sync-identity")
    db = target / "library.db"
    assert one(db, "SELECT value FROM settings WHERE key = 'sync_pc_id'") == "6bd7433e53dd462ea993a824bbf64a10"
    assert one(db, "SELECT COUNT(*) FROM sync_pairs") == 1


def test_app_gives_the_imported_library_a_new_sync_id(snapshot):
    data, target, media = snapshot
    run_snapshot(data, target, media)
    from musicmgr.services import usb_sync

    db_session.init_engine(db_path=target / "library.db")
    with db_session.session_scope() as s:
        new_id = usb_sync.get_pc_id(s)
    db_session.get_engine().dispose()
    assert new_id and new_id != "6bd7433e53dd462ea993a824bbf64a10"


def test_image_rel():
    assert tool.image_rel("D:\\MusicMgr\\data\\artwork\\Folders\\x.jpg", "artwork") == Path("Folders/x.jpg")
    assert tool.image_rel("D:\\MusicMgr\\data\\artwork\\x.jpg", "artwork") == Path("x.jpg")
    assert tool.image_rel("C:\\elsewhere\\x.jpg", "artwork") == Path("x.jpg")
