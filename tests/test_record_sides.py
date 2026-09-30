"""78 rpm record sides as track numbers, and sorting a playlist by album
then side/track (2026-09-30 - James: "The track number is important in the
playlist because I indicate Side A and Side B. I want the playlist to have
the granularity of Album and then Track #")."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db.models import MediaFile, Playlist, PlaylistFolder, PlaylistItem, Track
from musicmgr.services import playlists as pl_svc
from musicmgr.services import scanner
from musicmgr.services.tracknum import is_side, label, parse_side


def _mp3(path: Path, title: str, album: str, track: str) -> None:
    from mutagen.id3 import TALB, TIT2, TPE1, TRCK
    from mutagen.mp3 import MP3

    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    frame = header + bytes(417 - len(header))
    path.write_bytes(frame * 5)
    audio = MP3(str(path))
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[title]))
    audio.tags.add(TPE1(encoding=3, text=["Ozzie Nelson and his Orchestra"]))
    audio.tags.add(TALB(encoding=3, text=[album]))
    audio.tags.add(TRCK(encoding=3, text=[track]))
    audio.save()


class TestParseSide:
    @pytest.mark.parametrize("text, expected", [
        ("A", (1, "A")), ("b", (2, "B")), ("Side B", (2, "B")),
        ("A1", (101, "A1")), ("B2", (202, "B2")), ("A-3", (103, "A3")),
        ("3", None), ("3/12", None), ("", None), (None, None), ("AB", None),
    ])
    def test_parse(self, text, expected):
        assert parse_side(text) == expected

    def test_label(self):
        assert label(1, "A") == "A" and label(202, "B2") == "B2"
        assert label(3, "3") == "3" and label(None, None) == ""
        assert is_side("B") and not is_side("1-03")


def _scan(db, root: Path) -> None:
    scanner.scan_folder(db, root)
    db.flush()


class TestScannerKeepsSides:
    def test_side_tags_become_position_and_sort_number(self, session, tmp_path):
        db = session
        _mp3(tmp_path / "b.mp3", "They All Laughed", "Bluebird - B-6873", "B")
        _mp3(tmp_path / "a.mp3", "They Can't Take That Away From Me", "Bluebird - B-6873", "A")
        _scan(db, tmp_path)
        rows = {t.title: (t.track_no, t.position) for t in db.scalars(select(Track))}
        assert rows == {"They All Laughed": (2, "B"),
                        "They Can't Take That Away From Me": (1, "A")}


class TestSortByAlbumTrack:
    def _playlist(self, db, tmp_path, folder_id=None) -> Playlist:
        records = [
            ("Bluebird - B-10096", "B", "29th And Dearborn"),
            ("Bluebird - B-6873", "B", "They All Laughed"),
            ("Bluebird - B-10096", "A", "Sugar"),
            ("Bluebird - B-6873", "A", "They Can't Take That Away From Me"),
        ]
        for n, (album, side, title) in enumerate(records):
            _mp3(tmp_path / f"{n}.mp3", title, album, side)
        _scan(db, tmp_path)
        pl = Playlist(name="Bluebird", kind=Playlist.KIND_MANUAL, folder_id=folder_id)
        db.add(pl)
        db.flush()
        for pos, (_a, _s, title) in enumerate(records):
            track = db.scalar(select(Track).where(Track.title == title))
            db.add(PlaylistItem(playlist_id=pl.id, track_id=track.id, position=pos))
        db.flush()
        return pl

    def _titles(self, db, pl) -> list[str]:
        return [t.title for t in pl_svc.playlist_tracks(db, pl.id)]

    def test_records_in_catalogue_order_a_side_first(self, session, tmp_path):
        db = session
        pl = self._playlist(db, tmp_path)
        assert pl_svc.sort_playlist_by_album_track(db, pl.id)
        assert self._titles(db, pl) == [
            "They Can't Take That Away From Me", "They All Laughed",   # B-6873
            "Sugar", "29th And Dearborn",                               # B-10096
        ]
        assert not pl_svc.sort_playlist_by_album_track(db, pl.id)      # already sorted

    def test_whole_folder(self, session, tmp_path):
        db = session
        folder = PlaylistFolder(name="78 Records")
        db.add(folder)
        db.flush()
        pl = self._playlist(db, tmp_path, folder_id=folder.id)
        db.add(Playlist(name="Smart", kind=Playlist.KIND_SMART, folder_id=folder.id, rules="{}"))
        db.flush()
        assert pl_svc.sort_folder_by_album_track(db, folder.id) == (1, 1)
        assert self._titles(db, pl)[0] == "They Can't Take That Away From Me"

    def test_natural_album_order(self):
        names = ["Bluebird - B-10096", "Bluebird - B-6873", "bluebird - b-7000"]
        assert sorted(names, key=pl_svc.natural_key) == [
            "Bluebird - B-6873", "bluebird - b-7000", "Bluebird - B-10096"]
