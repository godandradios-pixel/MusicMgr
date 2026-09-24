"""services/library_state.py - plays, ratings, playlists and the Jukebox
board shared between PCs through the USB drive (2026-09-24, step 3).

Two real libraries (two sqlite files, two data folders, two music
folders) share one temp "USB drive"; the engine is switched between them
the way two PCs would each have their own."""

from __future__ import annotations

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
