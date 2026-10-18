"""Each side of a 78 shows its own label (2026-10-17).

James: "I need the 78s to show each label A and B". Victor 25523 is one
record (one album, one cover) with "Song of India" on side A and "Marie" on
side B; both showed the A label. A side whose file carries a different
picture now keeps it as "<record cover> - <side>" next to the record's
cover, and everything that plays a song shows that.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr import config
from musicmgr.db.models import Release, Track
from musicmgr.services import artwork_names, scanner
from musicmgr.services.player import QueueItem

from tests.test_scanner import make_tagged_mp3

A_LABEL = b"\xff\xd8 song of india label 25523-A"
B_LABEL = b"\xff\xd8 marie label 25523-B"


def make_side(path: Path, title: str, side: str, picture: bytes | None) -> None:
    from mutagen.id3 import APIC, ID3, TRCK

    make_tagged_mp3(path, title, "Tommy Dorsey And His Orchestra", "Victor - 25523",
                    album_artist="Tommy Dorsey And His Orchestra")
    tags = ID3(str(path))
    tags.add(TRCK(encoding=3, text=[side]))
    if picture:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=picture))
    tags.save()


@pytest.fixture
def art(tmp_path, monkeypatch):
    folder = tmp_path / "artwork"
    folder.mkdir()
    monkeypatch.setattr(config, "ART_DIR", folder)
    return folder


def _record(session, tmp_path, a=A_LABEL, b=B_LABEL):
    folder = tmp_path / "Old78Records"
    make_side(folder / "Song of India.mp3", "Song of India (Cancion India)", "A", a)
    make_side(folder / "Marie.mp3", "Marie", "B", b)
    scanner.scan_folder(session, tmp_path / "Old78Records")
    release = session.scalar(select(Release))
    tracks = {t.position: t for t in session.scalars(select(Track))}
    return release, tracks


class TestScan:
    def test_each_side_shows_its_own_label(self, session, tmp_path, art):
        release, tracks = _record(session, tmp_path)
        record = Path(release.cover_path)
        assert record.read_bytes() in (A_LABEL, B_LABEL)  # the first side scanned

        for side, label in (("A", A_LABEL), ("B", B_LABEL)):
            item = QueueItem.from_track(tracks[side])
            assert Path(item.cover_path).read_bytes() == label, side

        # the side that matches the record's cover needs no file of its own
        names = sorted(p.name for p in art.iterdir())
        assert len(names) == 2
        assert any(n.endswith(" - A.jpg") or n.endswith(" - B.jpg") for n in names)

    def test_same_picture_on_both_sides_makes_no_extra_files(self, session, tmp_path, art):
        release, tracks = _record(session, tmp_path, a=A_LABEL, b=A_LABEL)
        assert [p.name for p in art.iterdir()] == [Path(release.cover_path).name]
        assert QueueItem.from_track(tracks["B"]).cover_path == release.cover_path

    def test_a_side_with_no_picture_shows_the_record(self, session, tmp_path, art):
        release, tracks = _record(session, tmp_path, b=None)
        assert QueueItem.from_track(tracks["B"]).cover_path == release.cover_path

    def test_ordinary_numbered_tracks_are_untouched(self, art):
        cover = art / "Def Leppard - Hysteria.jpg"
        cover.write_bytes(b"x")
        (art / "Def Leppard - Hysteria - 1.jpg").write_bytes(b"y")
        item = QueueItem(track_id=1, title="Women", artist="Def Leppard", album="Hysteria",
                         path="x.mp3", cover_path=str(cover), position="1")
        assert item.cover_path == str(cover)


class TestExistingLibrary:
    def test_save_side_covers_fills_in_already_scanned_records(self, session, tmp_path, art):
        """Files scanned before this existed: only the record's cover is there."""
        release, tracks = _record(session, tmp_path)
        for p in art.iterdir():
            if p != Path(release.cover_path):
                p.unlink()
        assert QueueItem.from_track(tracks["B"]).cover_path == QueueItem.from_track(tracks["A"]).cover_path

        assert scanner.save_side_covers(session) == 1

        assert Path(QueueItem.from_track(tracks["A"]).cover_path).read_bytes() == A_LABEL
        assert Path(QueueItem.from_track(tracks["B"]).cover_path).read_bytes() == B_LABEL
        assert scanner.save_side_covers(session) == 1  # same file rewritten in place, no new ones
        assert len(list(art.iterdir())) == 2

    def test_a_retagged_side_gets_its_new_label_on_rescan(self, session, tmp_path, art):
        release, tracks = _record(session, tmp_path)
        new_label = b"\xff\xd8 a better scan of the B label"
        path = tmp_path / "Old78Records" / "Marie.mp3"
        from mutagen.id3 import APIC, ID3

        tags = ID3(str(path))
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=new_label))
        tags.save()
        scanner.scan_folder(session, tmp_path / "Old78Records", force=True)

        marie = session.scalar(select(Track).where(Track.title == "Marie"))
        assert Path(QueueItem.from_track(marie).cover_path).read_bytes() in (new_label,)


def test_jukebox_cards_show_each_sides_label(session, tmp_path, art):
    from musicmgr.db.models import JukeboxSlot
    from musicmgr.services import jukebox

    release, tracks = _record(session, tmp_path)
    slot = JukeboxSlot(slot_number=1, artist_id=release.album_artist_id,
                       side_a_track_id=tracks["A"].id, side_b_track_id=tracks["B"].id)
    session.add(slot)
    session.flush()
    card = jukebox.slot_to_dict(slot)
    assert Path(card["side_a"]["cover_path"]).read_bytes() == A_LABEL
    assert Path(card["side_b"]["cover_path"]).read_bytes() == B_LABEL


def test_side_cover_helper(art):
    record = art / "Bing Crosby - DECCA - 18429.jpg"
    record.write_bytes(b"r")
    assert artwork_names.side_cover(str(record), "B") == str(record)
    assert artwork_names.save_side_cover(str(record), "B", b"b label")
    assert Path(artwork_names.side_cover(str(record), "B")).name == "Bing Crosby - DECCA - 18429 - B.jpg"
    # the same picture as the record: the side file goes away again
    assert artwork_names.save_side_cover(str(record), "B", b"r") is None
    assert artwork_names.side_cover(str(record), "B") == str(record)
    assert artwork_names.side_cover(None, "B") is None
    assert artwork_names.side_cover(str(record), None) == str(record)
