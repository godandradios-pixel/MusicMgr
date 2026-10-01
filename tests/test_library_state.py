"""services/library_state.py - plays, ratings, playlists and the Jukebox
board shared between PCs through the USB drive (2026-09-24, step 3).

Two real libraries (two sqlite files, two data folders, two music
folders) share one temp "USB drive"; the engine is switched between them
the way two PCs would each have their own."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr import config
from musicmgr.db import session as db_session
from musicmgr.db.models import (
    Artist, JukeboxSlot, MediaFile, PlayEvent, Playlist, PlaylistItem, Track, WatchedFolder,
)
from musicmgr.services import library_state as ls
from musicmgr.services import scanner
from musicmgr.services import usb_sync as us
from musicmgr.services.library_state import _MISSING, _pick
from tests.test_scanner import make_silent_wav

SONGS = ["Rush/Signals/01 Subdivisions.wav", "Rush/Signals/02 The Analog Kid.wav",
         "Kinks/Hits/01 You Really Got Me.wav"]


class PC:
    def __init__(self, name: str, root: Path, drive: us.Drive, monkeypatch) -> None:
        self.name, self.root, self.drive, self.mp = name, root, drive, monkeypatch
        self.music = root / "Music"
        self.data = root / "data"
        for i, rel in enumerate(SONGS):
            make_silent_wav(self.music / rel)
        self.use()
        with db_session.session_scope() as s:
            s.add(WatchedFolder(path=str(self.music)))
            s.flush()
            scanner.scan_folder(s, self.music)

    def use(self) -> None:
        self.mp.setattr(config, "DATA_DIR", self.data)
        self.mp.setattr(config, "ART_DIR", self.data / "artwork")
        self.mp.setattr(config, "ARTIST_IMG_DIR", self.data / "artists")
        db_session.init_engine(db_path=self.root / "library.db")

    def sync(self) -> ls.MergeNotes:
        self.use()
        with db_session.session_scope() as s:
            plan = us.build_plan(s, self.drive)
            result = us.apply(s, plan, trash=lambda p: None)
            us.update_library(s, result)
            return ls.sync_library_state(s, self.drive, us.get_pc_id(s))

    def track(self, s, rel: str) -> Track:
        mf = s.scalar(select(MediaFile).where(MediaFile.path == str(self.music / rel)))
        return s.get(Track, mf.track_id)


@pytest.fixture
def two_pcs(tmp_path, monkeypatch, qapp):
    usb_root = tmp_path / "USB"
    (usb_root / "Music").mkdir(parents=True)
    drive = us.write_marker(usb_root)
    # PC2 gets its songs in a different order, so its ids differ from PC1's
    pc1 = PC("pc1", tmp_path / "pc1", drive, monkeypatch)
    global SONGS
    saved = SONGS
    SONGS = list(reversed(saved))
    try:
        pc2 = PC("pc2", tmp_path / "pc2", drive, monkeypatch)
    finally:
        SONGS = saved
    return pc1, pc2, drive


class TestPick:
    def test_rules(self):
        assert _pick(1, 1, 1) == (1, False)
        assert _pick(2, 1, 1) == (2, False)          # changed here
        assert _pick(1, 2, 1) == (2, True)           # changed there
        assert _pick(2, 3, 1) == (2, False)          # both: ours unless theirs newer
        assert _pick(2, 3, 1, usb_newer=True) == (3, True)
        assert _pick(_MISSING, 1, 1) == (_MISSING, False)   # deleted here
        assert _pick(1, _MISSING, 1) == (_MISSING, True)    # deleted there
        assert _pick(_MISSING, 2, 1) == (2, True)    # edit beats delete


class TestTwoPCs:
    def test_ratings_playlists_plays_and_board_travel(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            sub = pc1.track(s, SONGS[0])
            kid = pc1.track(s, SONGS[1])
            sub.rating = 5
            pl = Playlist(name="Road Trip", kind=Playlist.KIND_MANUAL)
            s.add(pl)
            s.flush()
            s.add_all([PlaylistItem(playlist_id=pl.id, track_id=kid.id, position=0),
                       PlaylistItem(playlist_id=pl.id, track_id=sub.id, position=1)])
            s.add(PlayEvent(track_id=sub.id, ms_played=200_000, completed=True, source="jukebox"))
            artist = s.scalar(select(Artist).where(Artist.name == "Rush")) or s.scalar(select(Artist))
            s.add(JukeboxSlot(slot_number=7, artist_id=artist.id, side_a_track_id=sub.id,
                              side_b_track_id=kid.id, genre="Classic Rock"))
        pc1.sync()
        notes = pc2.sync()
        assert notes.plays_in == 1 and notes.ratings_in == 1
        assert notes.playlists_in == ["Road Trip"] and notes.board_in
        with db_session.session_scope() as s:
            sub = pc2.track(s, SONGS[0])
            kid = pc2.track(s, SONGS[1])
            assert sub.rating == 5 and sub.play_count == 1
            pl = s.scalar(select(Playlist).where(Playlist.name == "Road Trip"))
            assert [i.track_id for i in pl.items] == [kid.id, sub.id]
            (slot,) = s.scalars(select(JukeboxSlot)).all()
            assert (slot.slot_number, slot.side_a_track_id, slot.side_b_track_id, slot.genre) == (
                7, sub.id, kid.id, "Classic Rock")
        # nothing new either way on the next round
        assert pc1.sync().summary() == "Library data already in sync"
        assert pc2.sync().summary() == "Library data already in sync"
        with db_session.session_scope() as s:
            assert len(s.scalars(select(PlayEvent)).all()) == 1
            assert pc2.track(s, SONGS[0]).play_count == 1

    def test_edit_and_delete_come_back(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            s.add_all([Playlist(name="Keep", kind=Playlist.KIND_MANUAL),
                       Playlist(name="Drop", kind=Playlist.KIND_MANUAL)])
        pc1.sync()
        pc2.sync()
        with db_session.session_scope() as s:            # on PC2
            s.delete(s.scalar(select(Playlist).where(Playlist.name == "Drop")))
            keep = s.scalar(select(Playlist).where(Playlist.name == "Keep"))
            keep.description = "edited on PC2"
            pc2.track(s, SONGS[2]).rating = 3
        pc2.sync()
        notes = pc1.sync()
        assert notes.playlists_removed == ["Drop"] and notes.playlists_in == ["Keep"]
        with db_session.session_scope() as s:
            names = set(s.scalars(select(Playlist.name).where(Playlist.kind == Playlist.KIND_MANUAL)))
            assert names == {"Keep"}
            assert s.scalar(select(Playlist).where(Playlist.name == "Keep")).description == "edited on PC2"
            assert pc1.track(s, SONGS[2]).rating == 3

    def test_board_changed_on_both_newer_wins_and_loser_is_kept(self, two_pcs, monkeypatch):
        pc1, pc2, drive = two_pcs
        pc1.sync()
        pc2.sync()

        def add_card(pc, genre):
            pc.use()
            with db_session.session_scope() as s:
                t = pc.track(s, SONGS[2])
                a = s.scalar(select(Artist))
                s.add(JukeboxSlot(slot_number=1, artist_id=a.id, side_a_track_id=t.id, genre=genre))

        add_card(pc1, "Pop")
        pc1.sync()                       # PC1's change reaches the USB first
        add_card(pc2, "Metal")
        # PC2 notices its change later -> newer
        notes = pc2.sync()
        assert notes.board_conflict == "this PC's (newer)"
        kept = list(us.usb_deleted_dir(drive).rglob("jukebox-board-*.json"))
        assert len(kept) == 1 and json.loads(kept[0].read_text())["cards"][0]["genre"] == "Pop"
        notes = pc1.sync()
        assert notes.board_in
        with db_session.session_scope() as s:
            assert s.scalar(select(JukeboxSlot)).genre == "Metal"

    def test_plays_for_tracks_this_pc_lacks_are_kept_for_the_others(self, two_pcs):
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            s.add(PlayEvent(track_id=pc1.track(s, SONGS[0]).id, ms_played=1000, source="x"))
        pc1.sync()
        state = ls.load_state(ls.usb_state_path(drive))
        state["plays"].append(["Music/Somebody/Else.wav", "2026-01-01T00:00:00+00:00", 5, True, False, "x"])
        ls.save_state(ls.usb_state_path(drive), state, "other")
        pc2.sync()
        after = ls.load_state(ls.usb_state_path(drive))
        assert any(p[0] == "Music/Somebody/Else.wav" for p in after["plays"])


class TestUnplaceable:
    def test_cards_this_pc_cant_place_are_kept_and_placed_later(self, two_pcs):
        """PC2 can't see PC1's music (its pair is removed): the card must
        survive PC2's syncs untouched, and appear once the music is back."""
        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            t = pc1.track(s, SONGS[2])
            s.add(JukeboxSlot(slot_number=3, artist_id=s.scalar(select(Artist)).id,
                              side_a_track_id=t.id, genre="Pop"))
            s.add(PlayEvent(track_id=t.id, ms_played=100_000, completed=True))
            t.rating = 4
        pc1.sync()
        pc2.use()
        with db_session.session_scope() as s:
            (pair,) = us.ensure_default_pairs(s, drive)
            us.remove_pair(s, pair.id)
        for _ in range(2):
            notes = pc2.sync()
            assert notes.plays_in == 0
            with db_session.session_scope() as s:
                assert s.scalar(select(JukeboxSlot)) is None
        state = ls.load_state(ls.usb_state_path(drive))
        assert [c["slot"] for c in state["jukebox"]["cards"]] == [3]      # still there for PC1
        assert list(state["ratings"].values()) == [4]
        # the music comes back to PC2
        with db_session.session_scope() as s:
            us.add_pair(s, drive, pc2.music, "Music")
        notes = pc2.sync()
        assert notes.board_in
        with db_session.session_scope() as s:
            slot = s.scalar(select(JukeboxSlot))
            assert slot is not None and slot.side_a_track_id == pc2.track(s, SONGS[2]).id
            assert pc2.track(s, SONGS[2]).rating == 4
        pc1.use()
        with db_session.session_scope() as s:
            assert s.scalar(select(JukeboxSlot)).slot_number == 3


class TestChoices:
    def test_defaults_all_on_and_saved_per_pc(self, db):
        with db_session.session_scope() as s:
            assert ls.enabled_categories(s) == frozenset(ls.CATEGORIES)
            ls.set_category_enabled(s, ls.JUKEBOX, False)
            assert ls.enabled_categories(s) == frozenset(ls.CATEGORIES) - {ls.JUKEBOX}

    def test_jukebox_off_neither_sends_nor_takes_then_on_merges(self, two_pcs):
        pc1, pc2, drive = two_pcs

        def add_card(pc, slot, genre):
            pc.use()
            with db_session.session_scope() as s:
                t = pc.track(s, SONGS[slot])
                s.add(JukeboxSlot(slot_number=slot + 1, artist_id=s.scalar(select(Artist)).id,
                                  side_a_track_id=t.id, genre=genre))

        add_card(pc1, 0, "Pop")
        pc1.sync()
        pc2.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.JUKEBOX, False)
            pc2.track(s, SONGS[1]).rating = 2
        add_card(pc2, 2, "Metal")
        notes = pc2.sync()
        assert not notes.board_in and notes.board_conflict is None
        with db_session.session_scope() as s:           # PC2 kept its own board
            assert [c.genre for c in s.scalars(select(JukeboxSlot))] == ["Metal"]
        state = ls.load_state(ls.usb_state_path(drive))
        assert [c["genre"] for c in state["jukebox"]["cards"]] == ["Pop"]   # USB untouched
        assert list(state["ratings"].values()) == [2]                      # ratings still sync

        pc1.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.PLAYS, False)
            s.add(PlayEvent(track_id=pc1.track(s, SONGS[0]).id, ms_played=99_000, completed=True))
        pc1.sync()
        assert ls.load_state(ls.usb_state_path(drive))["plays"] == []       # plays off on PC1

        pc2.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.JUKEBOX, True)
        notes = pc2.sync()       # both boards changed since PC2's base -> newer (PC2) wins
        assert notes.board_conflict == "this PC's (newer)"


# --------------------------------------------------------------------------
# playlist / folder images (2026-09-28)
# --------------------------------------------------------------------------


def _picture(tmp: Path, name: str, color: str) -> Path:
    from PySide6.QtGui import QColor, QImage

    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(QColor(color))
    path = tmp / name
    img.save(str(path))
    return path


def _artwork_travels(*pcs) -> None:
    """The artwork folder only syncs once a PC's pictures are renamed."""
    from musicmgr.services import artwork_names

    for pc in pcs:
        pc.use()
        (pc.data / "artwork").mkdir(parents=True, exist_ok=True)
        (pc.data / "artists").mkdir(parents=True, exist_ok=True)
        with db_session.session_scope() as s:
            artwork_names._mark_done(s)


class TestPlaylistImages:
    def test_playlist_and_folder_images_travel(self, two_pcs, tmp_path):
        from musicmgr.db.models import PlaylistFolder
        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        _artwork_travels(pc1, pc2)
        red = _picture(tmp_path, "red.png", "red")
        blue = _picture(tmp_path, "blue.png", "blue")
        pc1.use()
        with db_session.session_scope() as s:
            folder = PlaylistFolder(name="Road")
            s.add(folder)
            s.flush()
            pl = Playlist(name="Road Trip", kind=Playlist.KIND_MANUAL, folder_id=folder.id)
            s.add(pl)
            s.flush()
            pl_svc.set_playlist_image(s, pl.id, str(red))
            pl_svc.set_folder_image(s, folder.id, str(blue))
            red_name = Path(pl.cover_path).name
            blue_name = Path(folder.cover_path).name
        pc1.sync()
        notes = pc2.sync()
        assert notes.images_in == 2
        assert "2 playlist images updated" in notes.summary()
        with db_session.session_scope() as s:
            pl = s.scalar(select(Playlist).where(Playlist.name == "Road Trip"))
            folder = s.scalar(select(PlaylistFolder).where(PlaylistFolder.name == "Road"))
            assert pl.cover_path == str(pc2.data / "artwork" / red_name)
            assert folder.cover_path == str(pc2.data / "artwork" / blue_name)
            assert Path(pl.cover_path).is_file() and Path(folder.cover_path).is_file()
        # quiet afterwards
        assert pc1.sync().images_in == 0
        assert pc2.sync().images_in == 0

        # changed on PC2, cleared on PC2 -> back on PC1
        green = _picture(tmp_path, "green.png", "green")
        pc2.use()
        with db_session.session_scope() as s:
            pl = s.scalar(select(Playlist).where(Playlist.name == "Road Trip"))
            folder = s.scalar(select(PlaylistFolder).where(PlaylistFolder.name == "Road"))
            pl_svc.set_playlist_image(s, pl.id, str(green))
            pl_svc.set_folder_image(s, folder.id, None)
            green_name = Path(pl.cover_path).name
        pc2.sync()
        assert pc1.sync().images_in == 2
        with db_session.session_scope() as s:
            pl = s.scalar(select(Playlist).where(Playlist.name == "Road Trip"))
            folder = s.scalar(select(PlaylistFolder).where(PlaylistFolder.name == "Road"))
            assert pl.cover_path == str(pc1.data / "artwork" / green_name)
            assert folder.cover_path is None

    def test_chart_playlist_arrives_with_its_image(self, two_pcs, tmp_path):
        """Chart playlists sync themselves since 2026-09-28, so the picture
        comes over in the same sync as the playlist."""
        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        _artwork_travels(pc1, pc2)
        red = _picture(tmp_path, "red.png", "red")
        pc1.use()
        with db_session.session_scope() as s:
            pl = Playlist(name="Hot 100 1984", kind=Playlist.KIND_CHART)
            s.add(pl)
            s.flush()
            pl_svc.set_playlist_image(s, pl.id, str(red))
            red_name = Path(pl.cover_path).name
        pc1.sync()
        notes = pc2.sync()
        assert notes.playlists_in == ["Hot 100 1984"] and notes.images_in == 1
        with db_session.session_scope() as s:
            pl = s.scalar(select(Playlist).where(Playlist.name == "Hot 100 1984"))
            assert pl.kind == Playlist.KIND_CHART
            assert pl.cover_path == str(pc2.data / "artwork" / red_name)
            assert Path(pl.cover_path).is_file()

    def test_playlists_off_leaves_images_alone(self, two_pcs, tmp_path):
        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        _artwork_travels(pc1, pc2)
        for pc in (pc1, pc2):
            pc.use()
            with db_session.session_scope() as s:
                s.add(Playlist(name="Mix", kind=Playlist.KIND_MANUAL))
            pc.sync()
        red = _picture(tmp_path, "red.png", "red")
        pc1.use()
        with db_session.session_scope() as s:
            pl = s.scalar(select(Playlist).where(Playlist.name == "Mix"))
            pl_svc.set_playlist_image(s, pl.id, str(red))
        pc1.sync()
        pc2.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.PLAYLISTS, False)
        assert pc2.sync().images_in == 0
        with db_session.session_scope() as s:
            assert s.scalar(select(Playlist).where(Playlist.name == "Mix")).cover_path is None
            ls.set_category_enabled(s, ls.PLAYLISTS, True)
        assert pc2.sync().images_in == 1

    def test_drive_from_an_older_musicmgr_removes_nothing(self, two_pcs, tmp_path):
        import gzip

        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        _artwork_travels(pc1, pc2)
        red = _picture(tmp_path, "red.png", "red")
        pc1.use()
        with db_session.session_scope() as s:
            pl = Playlist(name="Mix", kind=Playlist.KIND_MANUAL)
            s.add(pl)
            s.flush()
            pl_svc.set_playlist_image(s, pl.id, str(red))
        pc1.sync()
        # another PC on an older version rewrites the drive without images
        path = ls.usb_state_path(drive)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("images")
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        pc1.sync()
        with db_session.session_scope() as s:
            assert s.scalar(select(Playlist).where(Playlist.name == "Mix")).cover_path is not None
        assert ls.load_state(path)["images"]


# --------------------------------------------------------------------------
# chart playlists and folders (2026-09-28: "it didn't update Billboard Hot
# Country folder and playlists" on PC2)
# --------------------------------------------------------------------------


def _chart(s, pc, folder_id, name, rels):
    pl = Playlist(name=name, kind=Playlist.KIND_CHART, folder_id=folder_id)
    s.add(pl)
    s.flush()
    for pos, rel in enumerate(rels):
        s.add(PlaylistItem(playlist_id=pl.id, track_id=pc.track(s, rel).id, position=pos))
    return pl


def _tree(s) -> dict[str, list]:
    """{playlist key: [kind, track titles]} and folder paths, as this PC has them."""
    out = {}
    for key, pl in ls._keyed_playlists(s).items():
        out[key] = [pl.kind, [i.track.title for i in pl.items]]
    return out


class TestChartsAndFolders:
    def test_chart_folder_tree_travels(self, two_pcs):
        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            country = pl_svc.create_folder(s, "Billboard Hot Country")
            c80 = pl_svc.create_folder(s, "1980-89", country.id)
            rock = pl_svc.create_folder(s, "Playback Rock")
            r80 = pl_svc.create_folder(s, "1980-89", rock.id)
            pl_svc.create_folder(s, "Empty for now")
            _chart(s, pc1, c80.id, "1984", [SONGS[2], SONGS[0]])
            _chart(s, pc1, c80.id, "1985", [SONGS[1]])
            _chart(s, pc1, r80.id, "1984", [SONGS[0]])     # same name, other folder
            pl_svc.ensure_builtin_playback_playlists(s)
            want = _tree(s)
        assert "Billboard Hot Country/1980-89/1984" in want
        assert "Playback Rock/1980-89/1984" in want
        sent = pc1.sync()
        assert sent.playlists_out == 3 and sent.folders_out == 5
        assert "sent 3 playlists, 5 folders to the drive" in sent.summary()
        assert sent.status().endswith("(3 playlists, 5 folders)")
        notes = pc2.sync()
        assert "Empty for now" in notes.folders_in
        assert len(notes.playlists_in) == 3 and notes.playlists_out == 0
        with db_session.session_scope() as s:
            assert _tree(s) == want
            assert set(ls._folders_by_path(s)) == {
                "Billboard Hot Country", "Billboard Hot Country/1980-89",
                "Playback Rock", "Playback Rock/1980-89", "Empty for now"}
            # the Most Played playlists are each PC's own, built from its plays
            assert s.scalar(select(Playlist).where(Playlist.kind == Playlist.KIND_PLAYBACK)) is None
        assert pc1.sync().summary() == "Library data already in sync"
        assert pc2.sync().summary() == "Library data already in sync"

    def test_folder_deleted_on_one_pc_goes_on_the_other(self, two_pcs):
        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            old = pl_svc.create_folder(s, "Old charts")
            pl_svc.create_folder(s, "Gone too")
            _chart(s, pc1, old.id, "1990", [SONGS[0]])
        pc1.sync()
        pc2.sync()
        with db_session.session_scope() as s:              # on PC2
            for folder in list(s.scalars(select(ls.PlaylistFolder))):
                pl_svc.delete_folder(s, folder.id)          # contents move up
        pc2.sync()
        notes = pc1.sync()
        assert notes.folders_removed == ["Gone too", "Old charts"]
        with db_session.session_scope() as s:
            assert ls._folders_by_path(s) == {}
            assert list(ls._keyed_playlists(s)) == ["1990"]

    def test_state_from_before_folder_keys_moves_nothing(self, two_pcs):
        """A drive and base written by the previous MusicMgr (playlists keyed
        by name, no folders) must not look like deletions."""
        import gzip

        from musicmgr.services import playlists as pl_svc

        pc1, pc2, drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            fav = pl_svc.create_folder(s, "Favourites")
            s.add(Playlist(name="Road Trip", kind=Playlist.KIND_MANUAL, folder_id=fav.id))
            s.add(Playlist(name="Top level", kind=Playlist.KIND_MANUAL))
        pc1.sync()
        # rewrite both copies the way the old version wrote them
        for path in (ls.usb_state_path(drive), ls.base_state_path(drive)):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            data.pop("playlist_keys")
            data.pop(ls.FOLDERS)
            data["playlists"] = {v.pop("name"): v for v in data["playlists"].values()}
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                json.dump(data, fh)
        notes = pc1.sync()
        assert notes.playlists_in == [] and notes.playlists_removed == []
        with db_session.session_scope() as s:
            assert set(ls._keyed_playlists(s)) == {"Favourites/Road Trip", "Top level"}
        assert pc2.sync().playlists_in == ["Favourites/Road Trip", "Top level"]
        with db_session.session_scope() as s:
            assert set(ls._keyed_playlists(s)) == {"Favourites/Road Trip", "Top level"}


class TestImagesWithNewFolders:
    """2026-09-28: PC2's first 1.7.1 sync created Billboard Hot Country and
    its chart playlists but left them without their images."""

    def _pc1_country(self, pc1, tmp_path):
        from musicmgr.services import playlists as pl_svc

        _artwork_travels(pc1)
        blue = _picture(tmp_path, "blue.png", "blue")
        red = _picture(tmp_path, "red.png", "red")
        pc1.use()
        with db_session.session_scope() as s:
            country = pl_svc.create_folder(s, "Billboard Hot Country")
            c20 = pl_svc.create_folder(s, "2020-29", country.id)
            chart = _chart(s, pc1, c20.id, "2022", [SONGS[0]])
            pl_svc.set_folder_image(s, country.id, str(blue))
            pl_svc.set_playlist_image(s, chart.id, str(red))
            return Path(country.cover_path).name, Path(chart.cover_path).name

    @staticmethod
    def _pc2_images(pc2):
        from musicmgr.db.models import PlaylistFolder

        pc2.use()
        with db_session.session_scope() as s:
            folder = s.scalar(select(PlaylistFolder).where(
                PlaylistFolder.name == "Billboard Hot Country"))
            chart = s.scalar(select(Playlist).where(Playlist.name == "2022"))
            return (Path(folder.cover_path).name if folder.cover_path else None,
                    Path(chart.cover_path).name if chart.cover_path else None)

    def _old_base(self, pc2, drive, *, unplaced: bool):
        """PC2's base as an earlier build left it: the images listed, no
        `images_placed` marker."""
        pc2.use()
        path = ls.base_state_path(drive)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        usb = ls.load_state(ls.usb_state_path(drive))
        data[ls.IMAGES] = usb[ls.IMAGES]
        if unplaced:
            data["images_unplaced"] = sorted(usb[ls.IMAGES])
        data.pop("images_placed", None)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_images_set_on_folders_and_playlists_created_in_the_same_sync(
            self, two_pcs, tmp_path):
        pc1, pc2, drive = two_pcs
        _artwork_travels(pc2)
        pc2.sync()                       # PC2 has a base, without the charts
        folder_img, chart_img = self._pc1_country(pc1, tmp_path)
        pc1.sync()
        # a 1.7.0 PC2 saw the images but couldn't place them (no chart sync)
        self._old_base(pc2, drive, unplaced=True)
        pc2.sync()
        assert self._pc2_images(pc2) == (folder_img, chart_img)
        # nothing is sent back as removed, and it's quiet afterwards
        usb = ls.load_state(ls.usb_state_path(drive))
        assert usb[ls.IMAGES]["folder:Billboard Hot Country"] == folder_img
        pc1.sync()
        assert pc2.sync().images_in == 0
        assert self._pc2_images(pc2) == (folder_img, chart_img)

    def test_pc_left_without_images_is_repaired_not_read_as_removal(self, two_pcs, tmp_path):
        from musicmgr.db.models import PlaylistFolder

        pc1, pc2, drive = two_pcs
        _artwork_travels(pc2)
        folder_img, chart_img = self._pc1_country(pc1, tmp_path)
        pc1.sync()
        pc2.sync()
        # what the bug left behind: PC2 has the folder and chart with no
        # image, while its base (no marker) says the images are agreed
        pc2.use()
        with db_session.session_scope() as s:
            for row in s.scalars(select(Playlist)):
                row.cover_path = None
            for row in s.scalars(select(PlaylistFolder)):
                row.cover_path = None
        self._old_base(pc2, drive, unplaced=False)
        assert self._pc2_images(pc2) == (None, None)

        notes = pc2.sync()
        assert notes.images_in == 2
        assert self._pc2_images(pc2) == (folder_img, chart_img)
        usb = ls.load_state(ls.usb_state_path(drive))
        assert usb[ls.IMAGES]["folder:Billboard Hot Country"] == folder_img
        # PC1 keeps its images
        pc1.sync()
        pc1.use()
        with db_session.session_scope() as s:
            f = s.scalar(select(PlaylistFolder).where(PlaylistFolder.name == "Billboard Hot Country"))
            assert Path(f.cover_path).name == folder_img

    def test_clear_after_the_repair_still_travels(self, two_pcs, tmp_path):
        from musicmgr.db.models import PlaylistFolder

        pc1, pc2, drive = two_pcs
        _artwork_travels(pc2)
        folder_img, _ = self._pc1_country(pc1, tmp_path)
        pc1.sync()
        pc2.sync()
        pc2.use()
        with db_session.session_scope() as s:
            s.scalar(select(PlaylistFolder).where(
                PlaylistFolder.name == "Billboard Hot Country")).cover_path = None
        pc2.sync()
        pc1.sync()
        pc1.use()
        with db_session.session_scope() as s:
            f = s.scalar(select(PlaylistFolder).where(PlaylistFolder.name == "Billboard Hot Country"))
            assert f.cover_path is None


class TestRadioStations:
    """James: "With the new Radio feature, I would like to add the preset
    radio stations buttons to the USB Sync option"."""

    def _dial(self, pc):
        from musicmgr.db.models import RadioStation
        from musicmgr.services import stations as st_svc

        pc.use()
        with db_session.session_scope() as s:
            return [(st.name, st.stream_url, st.band) for st in st_svc.dial(s)]

    def test_stations_travel_and_edits_and_removals_follow(self, two_pcs):
        from musicmgr.db.models import RadioStation
        from musicmgr.services import stations as st_svc

        pc1, pc2, _drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            st_svc.add_manual(s, "KDKA", "https://live.example/kdka", band="am")
            wdve = st_svc.add_manual(s, "WDVE", "https://live.example/wdve")
            wdve.favicon_url = "https://example/wdve.png"
            wdve.favicon_path = "/pc1/stations/2.png"
        notes = pc1.sync()
        assert notes.stations_out == 2 and "2 radio stations" in notes.summary()

        notes = pc2.sync()
        assert sorted(notes.stations_in) == ["https://live.example/kdka", "https://live.example/wdve"]
        assert self._dial(pc2) == [("KDKA", "https://live.example/kdka", "am"),
                                   ("WDVE", "https://live.example/wdve", "fm")]
        pc2.use()
        with db_session.session_scope() as s:
            w = s.scalar(select(RadioStation).where(RadioStation.name == "WDVE"))
            assert w.favicon_url == "https://example/wdve.png" and w.favicon_path is None
            # PC2 renames one and moves it to AM; removes the other
            w.name, w.band = "WDVE 102.5", "am"
            k = s.scalar(select(RadioStation).where(RadioStation.name == "KDKA"))
            st_svc.remove(s, k.id)
        pc2.sync()

        notes = pc1.sync()
        assert notes.stations_removed == ["https://live.example/kdka"]
        assert self._dial(pc1) == [("WDVE 102.5", "https://live.example/wdve", "am")]
        pc1.use()
        with db_session.session_scope() as s:  # PC1 keeps its own logo file
            assert s.scalar(select(RadioStation)).favicon_path == "/pc1/stations/2.png"
        assert pc1.sync().summary() == "Library data already in sync"

    def test_switched_off_stations_stay_put(self, two_pcs):
        from musicmgr.services import stations as st_svc

        pc1, pc2, _drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            st_svc.add_manual(s, "KDKA", "https://live.example/kdka", band="am")
        pc1.sync()
        pc2.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.RADIO, False)
        notes = pc2.sync()
        assert notes.stations_in == [] and self._dial(pc2) == []
        pc2.use()
        with db_session.session_scope() as s:
            ls.set_category_enabled(s, ls.RADIO, True)
        assert pc2.sync().stations_in == ["https://live.example/kdka"]

    def test_a_drive_written_before_radio_sync_removes_nothing(self, two_pcs, tmp_path):
        from musicmgr.services import stations as st_svc

        pc1, _pc2, _drive = two_pcs
        pc1.use()
        with db_session.session_scope() as s:
            st_svc.add_manual(s, "KDKA", "https://live.example/kdka", band="am")
        pc1.sync()
        # an older MusicMgr rewrites the drive's state without stations
        import gzip
        import json

        path = ls.usb_state_path(pc1.drive)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop(ls.RADIO, None)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        notes = pc1.sync()
        assert notes.stations_removed == [] and len(self._dial(pc1)) == 1


class TestRadioShowProgress:
    """James: "Please add moves for shows ... the Mint PC to pick up where
    you left off"."""

    EPS = ("1937-09-26 - Death House Rescue", "1937-10-17 - Murder By The Dead",
           "1937-10-24 - The Temple Bells")

    def _radio(self, pc, with_whistler=True):
        import wave

        from musicmgr.services import otr

        root = pc.root / "Radio"
        files = [root / "The Shadow" / f"{e}.wav" for e in self.EPS]
        if with_whistler:
            files.append(root / "The Whistler" / "Whistler 44-11-20 (130) Death Sees Double.wav")
        for f in files:
            f.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(f), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 3200)
        pc.use()
        otr.scan(db_session.new_session, str(root), read_tags=lambda p: {"length": 0.4})

    def _show(self, s, name):
        from musicmgr.db.models import RadioShow

        return s.scalar(select(RadioShow).where(RadioShow.name == name))

    def test_band_moves_heard_episodes_and_resume_point_travel(self, two_pcs):
        from musicmgr.services import otr

        pc1, pc2, _drive = two_pcs
        self._radio(pc1)
        self._radio(pc2)
        pc1.use()
        with db_session.session_scope() as s:
            shadow = self._show(s, "The Shadow")
            otr.set_band(s, shadow.id, "lw")
            eps = otr.episodes(s, shadow.id)
            otr.mark_played(s, eps[0])                    # heard the first
            eps[1].position_ms = 754_000                  # stopped 12:34 into the second
            shadow.current_episode_id = eps[1].id
        pc1.sync()

        notes = pc2.sync()
        assert notes.otr_in and "radio show progress updated" in notes.summary()
        pc2.use()
        with db_session.session_scope() as s:
            shadow = self._show(s, "The Shadow")
            eps = otr.episodes(s, shadow.id)
            assert shadow.band == "lw"
            assert [e.played for e in eps] == [True, False, False]
            assert eps[1].position_ms == 754_000
            cur = otr.current_episode(s, shadow)
            assert cur.id == eps[1].id                    # Mint picks up where PC1 left off
            # Mint finishes it and moves The Whistler
            otr.mark_played(s, eps[1])
            otr.set_band(s, self._show(s, "The Whistler").id, "sw1")
        pc2.sync()

        pc1.sync()
        pc1.use()
        with db_session.session_scope() as s:
            shadow = self._show(s, "The Shadow")
            eps = otr.episodes(s, shadow.id)
            assert [e.played for e in eps] == [True, True, False]
            assert eps[1].position_ms == 0
            assert otr.current_episode(s, shadow).id == eps[2].id
            assert self._show(s, "The Whistler").band == "sw1"
        assert pc1.sync().otr_in == 0

    def test_a_show_one_pc_doesnt_have_keeps_its_progress(self, two_pcs):
        from musicmgr.services import otr

        pc1, pc2, _drive = two_pcs
        self._radio(pc1)
        self._radio(pc2, with_whistler=False)
        pc1.use()
        with db_session.session_scope() as s:
            w = self._show(s, "The Whistler")
            otr.mark_played(s, otr.episodes(s, w.id)[0])
        pc1.sync()
        pc2.sync()
        pc2.sync()                                        # twice: nothing read as removed
        pc1.sync()
        pc1.use()
        with db_session.session_scope() as s:
            w = self._show(s, "The Whistler")
            assert otr.episodes(s, w.id)[0].played
