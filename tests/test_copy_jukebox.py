"""tools/copy_jukebox.py - carrying the Windows Jukebox board into a separately
scanned (Linux) library. Both databases are built with the app's own schema
and services, with deliberately different ids and D:\\ vs /home paths."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from musicmgr.db import session as db_session
from musicmgr.db.models import Artist, MediaFile, Release, Setting, Track
from musicmgr.services import jukebox
from musicmgr.services.matching import normalize

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("copy_jukebox", TOOLS / "copy_jukebox.py")
copy_jukebox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy_jukebox)

SONGS = [
    ("Boston", "Boston", "More Than a Feeling"),
    ("Boston", "Boston", "Peace of Mind"),
    ("Heart", "Dreamboat Annie", "Magic Man"),
    ("Alabama", "Mountain Music", "Mountain Music"),
]


def build_db(path: Path, root: str, sep: str, songs, padding: int = 0) -> dict:
    """A library with `songs`; `padding` junk artists first so ids differ."""
    db_session.init_engine(db_path=path)
    ids = {}
    with db_session.session_scope() as s:
        for i in range(padding):
            s.add(Artist(name=f"Filler {i}", name_key=f"filler {i}"))
        s.flush()
        artists, releases = {}, {}
        for artist, album, title in songs:
            if artist not in artists:
                artists[artist] = Artist(name=artist, name_key=normalize(artist))
                s.add(artists[artist])
                s.flush()
            if (artist, album) not in releases:
                releases[(artist, album)] = Release(
                    title=album, title_key=normalize(album), artist_display=artist,
                    album_artist_id=artists[artist].id,
                )
                s.add(releases[(artist, album)])
                s.flush()
            t = Track(release_id=releases[(artist, album)].id, title=title, title_key=normalize(title),
                      artist_display=artist)
            s.add(t)
            s.flush()
            s.add(MediaFile(track_id=t.id, path=sep.join([root, artist, album, f"{title}.mp3"])))
            ids[title] = (artists[artist].id, t.id)
    return ids


@pytest.fixture
def dbs(tmp_path, qapp):
    src, dst = tmp_path / "windows.db", tmp_path / "linux.db"
    src_ids = build_db(src, "D:\\Music", "\\", SONGS)
    with db_session.session_scope() as s:
        for title, genre in (("More Than a Feeling", "Classic Rock"), ("Peace of Mind", "Classic Rock"),
                             ("Magic Man", "Classic Rock"), ("Mountain Music", "Country")):
            artist_id, track_id = src_ids[title]
            jukebox.place_track(s, artist_id, track_id, genre)
        jukebox.add_genre(s, "Road Trip")
    # the Linux library: same music, different ids, POSIX paths, one song missing
    build_db(dst, "/home/jrs58/Music", "/", SONGS[:3], padding=7)
    db_session.get_engine().dispose()
    return src, dst


def board(path: Path):
    db = sqlite3.connect(path)
    rows = db.execute(
        """SELECT j.slot_number, a.name, ta.title, tb.title, j.genre FROM jukebox_slots j
           JOIN artists a ON a.id = j.artist_id
           LEFT JOIN tracks ta ON ta.id = j.side_a_track_id
           LEFT JOIN tracks tb ON tb.id = j.side_b_track_id ORDER BY j.slot_number"""
    ).fetchall()
    genres = db.execute("SELECT value FROM settings WHERE key='jukebox_genres'").fetchone()
    db.close()
    return rows, json.loads(genres[0]) if genres else None


def test_dry_run_changes_nothing(dbs, capsys):
    src, dst = dbs
    assert copy_jukebox.main(["--source", str(src), "--target", str(dst), "--dry-run"]) == 0
    assert board(dst)[0] == []
    out = capsys.readouterr().out
    assert "Would copy 2 card(s)" in out and "Alabama" in out  # Alabama skipped: not in this library


def test_copies_cards_by_file_path(dbs, capsys):
    src, dst = dbs
    assert copy_jukebox.main(["--source", str(src), "--target", str(dst)]) == 0
    rows, genres = board(dst)
    assert rows == [
        (1, "Boston", "More Than a Feeling", "Peace of Mind", "Classic Rock"),
        (2, "Heart", "Magic Man", None, "Classic Rock"),
    ]
    assert "Road Trip" in genres and "Country" in genres
    assert list(dst.parent.glob("library-before-jukebox-copy-*.db"))
    db = sqlite3.connect(dst)
    assert int(db.execute("SELECT value FROM settings WHERE key='jukebox_next_slot_number'").fetchone()[0]) >= 3
    db.close()


def test_second_run_is_harmless(dbs):
    src, dst = dbs
    copy_jukebox.main(["--source", str(src), "--target", str(dst)])
    copy_jukebox.main(["--source", str(src), "--target", str(dst)])
    assert len(board(dst)[0]) == 2


def test_falls_back_to_title_when_paths_differ(tmp_path, qapp):
    src, dst = tmp_path / "w.db", tmp_path / "l.db"
    ids = build_db(src, "D:\\Music", "\\", SONGS[:1])
    with db_session.session_scope() as s:
        jukebox.place_track(s, *ids["More Than a Feeling"], "Rock")
    build_db(dst, "/home/x/Elsewhere/Renamed", "/", SONGS[:1], padding=3)
    db_session.get_engine().dispose()
    copy_jukebox.main(["--source", str(src), "--target", str(dst)])
    assert board(dst)[0][0][1:4] == ("Boston", "More Than a Feeling", None)


def test_slot_number_collision_gets_new_number(dbs, qapp):
    src, dst = dbs
    db_session.init_engine(db_path=dst)
    with db_session.session_scope() as s:
        magic = s.query(Track).filter_by(title="Magic Man").one()
        heart = s.query(Artist).filter_by(name="Heart").one()
        jukebox.place_track(s, heart.id, magic.id, "Rock")  # target already owns slot 1
    db_session.get_engine().dispose()
    copy_jukebox.main(["--source", str(src), "--target", str(dst)])
    rows = board(dst)[0]
    boston = [r for r in rows if r[1] == "Boston"][0]
    assert boston[0] != 1
    assert len([r for r in rows if r[1] == "Heart"]) == 1  # Magic Man already on the board


def test_refuses_empty_target(tmp_path, qapp, dbs):
    src, _ = dbs
    empty = tmp_path / "empty.db"
    db_session.init_engine(db_path=empty)
    db_session.get_engine().dispose()
    assert copy_jukebox.main(["--source", str(src), "--target", str(empty)]) == 1


def test_path_parts_handles_both_styles():
    assert copy_jukebox.path_parts("D:\\Music\\Boston\\x.mp3") == ["music", "boston", "x.mp3"]
    assert copy_jukebox.path_parts("/home/j/Music/Boston/x.mp3")[-3:] == ["music", "boston", "x.mp3"]
