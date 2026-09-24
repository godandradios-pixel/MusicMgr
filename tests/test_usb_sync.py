"""services/usb_sync.py - two-way USB sync (2026-09-23, plan §1).

Everything runs on real temp folders: `local` stands in for D:\\Music,
`drive` for the USB drive's root with a `Music` folder on it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db import session as db_session
from musicmgr.db.models import MediaFile, SyncBaseline, SyncPair, WatchedFolder
from musicmgr.services import usb_sync as us
from musicmgr.services.usb_sync import (
    COPY, CONFLICT, DELETE, RENAME, TO_LOCAL, TO_USB, FileStat,
)

S = 1_000_000_000  # one second in ns


def write(path: Path, data: bytes = b"x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def env(tmp_path, db):
    local = tmp_path / "PC" / "Music"
    drive_root = tmp_path / "USB"
    (drive_root / "Music").mkdir(parents=True)
    local.mkdir(parents=True)
    drive = us.write_marker(drive_root, "Test USB")
    with db_session.session_scope() as s:
        s.add(WatchedFolder(path=str(local)))
    return local, drive_root / "Music", drive


def plan_for(drive):
    with db_session.session_scope() as s:
        return us.build_plan(s, drive)


def run(drive, plan, trash=None):
    trashed = []
    with db_session.session_scope() as s:
        result = us.apply(s, plan, trash=trash or (lambda p: (trashed.append(p), p.unlink())))
    return result, trashed


def sync_all(drive):
    plan = plan_for(drive)
    return run(drive, plan)[0]


# --------------------------------------------------------------------------
# drive + pairs
# --------------------------------------------------------------------------


class TestDrive:
    def test_marker_round_trip_and_find(self, tmp_path):
        d = us.write_marker(tmp_path / "E", "My USB")
        assert us.load_marker(tmp_path / "E").drive_id == d.drive_id
        found = us.find_drives([tmp_path / "E", tmp_path / "nothing"])
        assert [f.drive_id for f in found] == [d.drive_id]

    def test_rewriting_marker_keeps_id(self, tmp_path):
        a = us.write_marker(tmp_path, "one")
        b = us.write_marker(tmp_path, "two")
        assert a.drive_id == b.drive_id and us.load_marker(tmp_path).label == "two"

    def test_drive_letter_change_doesnt_matter(self, env, tmp_path):
        local, usb, drive = env
        write(local / "a.mp3")
        sync_all(drive)
        moved = tmp_path / "F"
        (tmp_path / "USB").rename(moved)
        drive2 = us.load_marker(moved)
        plan = plan_for(drive2)
        assert not plan.all_items()   # same pair, same baseline

    def test_default_pairs_match_watched_folder_names(self, env, db):
        local, usb, drive = env
        with db_session.session_scope() as s:
            s.add(WatchedFolder(path=str(local.parent / "Videos")))  # no USB\Videos
            pairs = us.ensure_default_pairs(s, drive)
            assert [(p.local_path, p.usb_rel_path) for p in pairs] == [(str(local), "Music")]
            us.set_pair_enabled(s, pairs[0].id, False)
            again = us.ensure_default_pairs(s, drive)
            assert len(again) == 1 and not again[0].enabled   # not re-created

    def test_second_drive_has_its_own_pairs(self, env, tmp_path):
        local, usb, drive = env
        other_root = tmp_path / "USB2"
        (other_root / "Music").mkdir(parents=True)
        other = us.write_marker(other_root)
        with db_session.session_scope() as s:
            us.ensure_default_pairs(s, drive)
            us.ensure_default_pairs(s, other)
            assert {p.drive_id for p in s.scalars(select(SyncPair))} == {drive.drive_id, other.drive_id}


# --------------------------------------------------------------------------
# the comparison table
# --------------------------------------------------------------------------


def st(size, t):
    return FileStat(size, int(t * S))


class TestCompare:
    def cmp(self, tmp_path, L, U, B):
        return us.compare(tmp_path, tmp_path, L, U, B)

    def test_new_locally_and_new_on_usb(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(1, 10)}, {"b": st(1, 10)}, {})
        assert [(i.kind, i.rel, i.direction) for i in p.items] == [
            (COPY, "a", TO_USB), (COPY, "b", TO_LOCAL)]

    def test_first_sync_identical_is_record_only(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(1, 10)}, {"a": st(1, 10)}, {})
        assert not p.items and p.record == {"a": st(1, 10)}

    def test_first_sync_different_is_conflict_newer_preselected(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(1, 10)}, {"a": st(2, 50)}, {})
        (item,) = p.items
        assert item.kind == CONFLICT and item.direction == TO_LOCAL

    def test_only_one_side_changed(self, tmp_path):
        b = {"a": st(1, 10)}
        p = self.cmp(tmp_path, {"a": st(2, 20)}, {"a": st(1, 10)}, b)
        assert [(i.kind, i.direction) for i in p.items] == [(COPY, TO_USB)]
        p = self.cmp(tmp_path, {"a": st(1, 10)}, {"a": st(3, 30)}, b)
        assert [(i.kind, i.direction) for i in p.items] == [(COPY, TO_LOCAL)]

    def test_both_changed_is_conflict(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(2, 20)}, {"a": st(3, 30)}, {"a": st(1, 10)})
        (item,) = p.items
        assert item.kind == CONFLICT and item.direction == TO_LOCAL

    def test_both_changed_the_same_way_is_recorded(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(2, 20)}, {"a": st(2, 20)}, {"a": st(1, 10)})
        assert not p.items and p.record == {"a": st(2, 20)}

    def test_deletions_default_unselected(self, tmp_path):
        b = {"a": st(1, 10), "b": st(1, 10)}
        p = self.cmp(tmp_path, {"a": st(1, 10)}, {"b": st(1, 10)}, b)
        assert [(i.kind, i.rel, i.remaining_side, i.selected) for i in p.items] == [
            (DELETE, "a", "local", False), (DELETE, "b", "usb", False)]
        assert not any(i.restore for i in p.items)

    def test_changed_after_delete_defaults_to_restore(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(5, 50)}, {}, {"a": st(1, 10)})
        (item,) = p.items
        assert item.kind == DELETE and item.changed_since and item.restore

    def test_gone_from_both_is_forgotten(self, tmp_path):
        p = self.cmp(tmp_path, {}, {}, {"a": st(1, 10)})
        assert not p.items and p.forget == ["a"]

    def test_unchanged_is_nothing(self, tmp_path):
        p = self.cmp(tmp_path, {"a": st(1, 10)}, {"a": st(1, 10)}, {"a": st(1, 10)})
        assert not p.items and not p.record and not p.forget


class TestTimes:
    def test_two_second_and_dst_tolerance(self):
        assert us.same_time(10 * S, 12 * S)
        assert not us.same_time(10 * S, 13 * S)
        assert us.same_time(10 * S, 10 * S + 3600 * S)
        assert us.same_time(10 * S, 10 * S - 3601 * S)
        assert not us.same_time(10 * S, 10 * S + 3700 * S)

    def test_same_size_redated_identical_file_is_not_a_conflict(self, tmp_path):
        write(tmp_path / "L" / "a.mp3", b"same bytes", 1000)
        write(tmp_path / "U" / "a.mp3", b"same bytes", 5000)
        L = us.walk(tmp_path / "L")
        U = us.walk(tmp_path / "U")
        p = us.compare(tmp_path / "L", tmp_path / "U", L, U, {})
        assert not p.items and "a.mp3" in p.record

    def test_same_size_different_bytes_is_a_conflict(self, tmp_path):
        write(tmp_path / "L" / "a.mp3", b"aaaa", 1000)
        write(tmp_path / "U" / "a.mp3", b"bbbb", 5000)
        p = us.compare(tmp_path / "L", tmp_path / "U", us.walk(tmp_path / "L"), us.walk(tmp_path / "U"), {})
        assert [i.kind for i in p.items] == [CONFLICT]


class TestCaseRename:
    def test_case_only_rename_on_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(us, "CASE_INSENSITIVE", True)
        L = {"AC-DC/song.mp3": st(1, 10)}
        U = {"ACDC/song.mp3": st(1, 10)}
        # different names entirely -> not a case rename
        p = us.compare(tmp_path, tmp_path, L, U, {})
        assert {i.kind for i in p.items} == {COPY}
        U = {"ac-dc/song.mp3": st(1, 10)}
        B = {"ac-dc/song.mp3": st(1, 10)}
        p = us.compare(tmp_path, tmp_path, L, U, B)
        (item,) = p.items
        assert item.kind == RENAME and item.direction == TO_USB and item.rel == "AC-DC/song.mp3"


class TestWalk:
    def test_skips_junk_and_collects_temp_files(self, tmp_path):
        write(tmp_path / "a.mp3")
        write(tmp_path / "Thumbs.db")
        write(tmp_path / "_gsdata_" / "x.log")
        write(tmp_path / ".hidden" / "y.mp3")
        write(tmp_path / "b.mp3.mmsync-tmp")
        temps = []
        files = us.walk(tmp_path, temp_out=temps)
        assert set(files) == {"a.mp3"}
        assert [t.name for t in temps] == ["b.mp3.mmsync-tmp"]


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


class TestApply:
    def test_two_way_copy_keeps_times_and_records_baseline(self, env):
        local, usb, drive = env
        write(local / "Artist" / "Album" / "01.mp3", b"local", 1_600_000_000)
        write(usb / "Other" / "02.flac", b"usb!", 1_600_000_500)
        result = sync_all(drive)
        assert result.copied_to_usb == 1 and result.copied_to_local == 1
        assert (usb / "Artist" / "Album" / "01.mp3").read_bytes() == b"local"
        assert (local / "Other" / "02.flac").stat().st_mtime == pytest.approx(1_600_000_500)
        with db_session.session_scope() as s:
            rels = set(s.scalars(select(SyncBaseline.rel_path)))
        assert rels == {"Artist/Album/01.mp3", "Other/02.flac"}
        assert not plan_for(drive).all_items()   # in sync now

    def test_local_delete_is_offered_not_applied(self, env):
        local, usb, drive = env
        write(local / "a.mp3")
        sync_all(drive)
        (local / "a.mp3").unlink()
        plan = plan_for(drive)
        (item,) = plan.all_items()
        assert item.kind == DELETE and not item.selected
        run(drive, plan)             # unchecked -> nothing happens
        assert (usb / "a.mp3").exists()
        assert plan_for(drive).all_items()   # still pending next time

    def test_usb_deletion_goes_to_deleted_folder(self, env):
        local, usb, drive = env
        write(local / "a.mp3", b"keep me")
        sync_all(drive)
        (local / "a.mp3").unlink()
        plan = plan_for(drive)
        plan.all_items()[0].selected = True
        result, _ = run(drive, plan)
        assert result.deleted_usb == 1 and not (usb / "a.mp3").exists()
        kept = list(us.usb_deleted_dir(drive).rglob("a.mp3"))
        assert len(kept) == 1 and kept[0].read_bytes() == b"keep me"
        assert not plan_for(drive).all_items()

    def test_local_deletion_goes_to_trash(self, env):
        local, usb, drive = env
        write(usb / "a.mp3")
        sync_all(drive)
        (usb / "a.mp3").unlink()
        plan = plan_for(drive)
        plan.all_items()[0].selected = True
        result, trashed = run(drive, plan)
        assert result.deleted_local == 1 and trashed == [local / "a.mp3"]

    def test_restore_instead_of_delete(self, env):
        local, usb, drive = env
        write(local / "a.mp3", b"abc")
        sync_all(drive)
        (usb / "a.mp3").unlink()
        plan = plan_for(drive)
        item = plan.all_items()[0]
        item.selected, item.restore = True, True
        result, _ = run(drive, plan)
        assert result.restored == 1 and (usb / "a.mp3").read_bytes() == b"abc"

    def test_conflict_loser_is_kept(self, env):
        local, usb, drive = env
        write(local / "a.mp3", b"old", 1_600_000_000)
        sync_all(drive)
        write(local / "a.mp3", b"local edit", 1_600_000_100)
        write(usb / "a.mp3", b"usb edit!!", 1_600_000_200)
        plan = plan_for(drive)
        (item,) = plan.all_items()
        assert item.kind == CONFLICT and item.direction == TO_LOCAL
        item.direction = TO_USB   # James picks this PC's version
        run(drive, plan)
        assert (usb / "a.mp3").read_bytes() == b"local edit"
        assert [p.read_bytes() for p in us.usb_deleted_dir(drive).rglob("a.mp3")] == [b"usb edit!!"]

    def test_unselected_copy_stays_pending(self, env):
        local, usb, drive = env
        write(local / "a.mp3")
        plan = plan_for(drive)
        plan.all_items()[0].selected = False
        run(drive, plan)
        assert not (usb / "a.mp3").exists()
        assert len(plan_for(drive).all_items()) == 1

    def test_cancel_mid_copy_leaves_no_temp_and_keeps_finished(self, env, monkeypatch):
        local, usb, drive = env
        write(local / "a.mp3", b"a" * 10)
        write(local / "b.mp3", b"b" * 10)
        monkeypatch.setattr(us, "COPY_CHUNK", 4)
        plan = plan_for(drive)
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 6   # part-way through the second file

        with db_session.session_scope() as s:
            result = us.apply(s, plan, should_stop=stop, trash=lambda p: None)
        assert result.cancelled and result.copied_to_usb == 1
        assert not list(usb.rglob("*.mmsync-tmp"))
        with db_session.session_scope() as s:
            assert len(list(s.scalars(select(SyncBaseline)))) == 1
        assert len(plan_for(drive).all_items()) == 1   # the other one, again

    def test_leftover_temp_files_are_removed(self, env):
        local, usb, drive = env
        write(usb / "x.mp3.mmsync-tmp")
        plan = plan_for(drive)
        run(drive, plan)
        assert not (usb / "x.mp3.mmsync-tmp").exists()

    def test_lost_database_falls_back_to_usb_baseline(self, env):
        local, usb, drive = env
        write(local / "a.mp3")
        write(local / "b.mp3")
        sync_all(drive)
        with db_session.session_scope() as s:
            s.execute(SyncBaseline.__table__.delete())
        (usb / "b.mp3").unlink()
        plan = plan_for(drive)
        assert plan.pairs[0].baseline_source == "usb"
        (item,) = plan.all_items()
        assert item.kind == DELETE   # not "new locally" - no resurrection

    def test_case_rename_applied(self, env, monkeypatch):
        monkeypatch.setattr(us, "CASE_INSENSITIVE", True)
        local, usb, drive = env
        write(local / "acdc.mp3", b"x", 1_600_000_000)
        sync_all(drive)
        os.rename(local / "acdc.mp3", local / "ACDC.mp3")
        plan = plan_for(drive)
        (item,) = plan.all_items()
        assert item.kind == RENAME
        run(drive, plan)
        assert [p.name for p in usb.iterdir()] == ["ACDC.mp3"]
        assert not plan_for(drive).all_items()

    def test_bytes_needed(self, env):
        local, usb, drive = env
        write(local / "a.mp3", b"12345")
        write(usb / "b.mp3", b"123")
        plan = plan_for(drive)
        assert us.bytes_needed(plan) == {TO_USB: 5, TO_LOCAL: 3}


# --------------------------------------------------------------------------
# library update after a sync
# --------------------------------------------------------------------------


class TestUpdateLibrary:
    def test_copied_file_is_imported_and_deleted_one_marked_missing(self, env):
        """End to end on real (tiny WAV) files: a track that arrives from
        the USB lands in the library; one deleted here is marked missing."""
        from tests.test_scanner import make_silent_wav
        from musicmgr.services import scanner

        local, usb, drive = env
        make_silent_wav(local / "Artist" / "Album" / "01 Kept.wav")
        with db_session.session_scope() as s:
            scanner.scan_folder(s, local)
        sync_all(drive)

        make_silent_wav(usb / "New Artist" / "New Album" / "01 Arrived.wav")
        plan = plan_for(drive)
        result, _ = run(drive, plan)
        with db_session.session_scope() as s:
            msg = us.update_library(s, result)
            paths = set(s.scalars(select(MediaFile.path)))
        assert "1 track added" in msg
        assert str(local / "New Artist" / "New Album" / "01 Arrived.wav") in paths

        (usb / "Artist" / "Album" / "01 Kept.wav").unlink()
        plan = plan_for(drive)
        plan.all_items()[0].selected = True   # apply the deletion here
        result, trashed = run(drive, plan)
        with db_session.session_scope() as s:
            us.update_library(s, result)
            mf = s.scalar(select(MediaFile).where(MediaFile.path == str(local / "Artist" / "Album" / "01 Kept.wav")))
            assert mf.is_missing

    def test_files_outside_watched_folders_are_not_imported(self, env, tmp_path):
        from tests.test_scanner import make_silent_wav

        local, usb, drive = env
        outside = tmp_path / "NotWatched" / "x.wav"
        make_silent_wav(outside)
        result = us.SyncResult(new_local_paths=[outside])
        with db_session.session_scope() as s:
            assert us.update_library(s, result) == "Library already up to date"
            assert s.scalar(select(MediaFile)) is None


class TestEditPairs:
    """2026-09-24 - James: "how do I edit a folder pair, or delete?"."""

    def test_change_usb_folder_clears_sync_list(self, env):
        local, usb, drive = env
        write(local / "a.mp3")
        sync_all(drive)
        with db_session.session_scope() as s:
            (pair,) = us.ensure_default_pairs(s, drive)
            us.update_pair(s, pair.id, usb_rel_path="Other/Music")
            assert pair.usb_rel_path == "Other/Music"
            assert not list(s.scalars(select(SyncBaseline)))

    def test_remove_watched_pair_is_not_recreated_and_add_revives_it(self, env):
        local, usb, drive = env
        with db_session.session_scope() as s:
            (pair,) = us.ensure_default_pairs(s, drive)
            us.remove_pair(s, pair.id)
            pairs = us.ensure_default_pairs(s, drive)
            assert len(pairs) == 1 and us.is_removed(pairs[0]) and not pairs[0].enabled
            assert us.build_plan(s, drive).pairs == []
            again = us.add_pair(s, drive, local, "Music")
            assert again.id == pair.id and again.enabled and not us.is_removed(again)

    def test_remove_added_pair_deletes_it(self, env, tmp_path):
        local, usb, drive = env
        other = tmp_path / "MP3"
        other.mkdir()
        with db_session.session_scope() as s:
            pair = us.add_pair(s, drive, other, "MusicMP3")
            us.remove_pair(s, pair.id)
            assert s.get(SyncPair, pair.id) is None

    def test_artwork_pair_follows_the_data_folder(self, env, tmp_path, monkeypatch):
        from musicmgr import config
        from musicmgr.services import artwork_names

        local, usb, drive = env
        monkeypatch.setattr(config, "ART_DIR", tmp_path / "C" / "data" / "artwork")
        monkeypatch.setattr(config, "ARTIST_IMG_DIR", tmp_path / "C" / "data" / "artists")
        with db_session.session_scope() as s:
            artwork_names._mark_done(s)
            pairs = us.ensure_default_pairs(s, drive)
            art = next(p for p in pairs if p.usb_rel_path == "MusicMgr/artwork")
            with pytest.raises(ValueError):
                us.update_pair(s, art.id, local_path=tmp_path / "elsewhere")
        monkeypatch.setattr(config, "ART_DIR", tmp_path / "D" / "data" / "artwork")
        with db_session.session_scope() as s:
            pairs = us.ensure_default_pairs(s, drive)
            arts = [p for p in pairs if p.usb_rel_path == "MusicMgr/artwork"]
            assert len(arts) == 1 and arts[0].local_path == str(tmp_path / "D" / "data" / "artwork")
