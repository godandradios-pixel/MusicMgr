"""USB sync carries the old-time radio folder (2026-10-13 - James: "I would
like to add D:\\Radio as a Sync settings")."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from sqlalchemy import func, select

from musicmgr.db import session as db_session
from musicmgr.db.models import MediaFile, RadioEpisode, RadioShow, SyncPair, WatchedFolder
from musicmgr.services import otr
from musicmgr.services import usb_sync as us


def wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


@pytest.fixture
def drive(tmp_path, db, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    music = tmp_path / "PC" / "Music"
    music.mkdir(parents=True)
    root = tmp_path / "USB"
    (root / "Music").mkdir(parents=True)
    d = us.write_marker(root, "Test USB")
    with db_session.session_scope() as s:
        s.add(WatchedFolder(path=str(music)))
    return d


def sync(drive):
    with db_session.session_scope() as s:
        plan = us.build_plan(s, drive)
    with db_session.session_scope() as s:
        result = us.apply(s, plan, trash=lambda p: p.unlink())
    with db_session.session_scope() as s:
        message = us.update_library(s, result)
    return result, message


def radio_pairs(s, drive):
    return [p for p in us.ensure_default_pairs(s, drive) if p.kind == us.RADIO_KIND]


def test_this_pcs_radio_folder_goes_to_the_drive(tmp_path, drive):
    radio = tmp_path / "PC" / "Radio"
    wav(radio / "Billy Graham" / "Revival.wav")
    with db_session.session_scope() as s:
        otr.set_root(s, str(radio))
        s.commit()
        (pair,) = radio_pairs(s, drive)
        assert Path(pair.local_path) == radio and pair.usb_rel_path == "Radio"
        s.commit()
    assert (drive.root / "Radio").is_dir()
    sync(drive)
    assert (drive.root / "Radio" / "Billy Graham" / "Revival.wav").is_file()


def test_a_new_pc_gets_the_shows_from_the_drive(tmp_path, drive):
    wav(drive.root / "Radio" / "Billy Graham" / "Revival.wav")
    wav(drive.root / "Radio" / "The Shadow" / "1937-09-26.wav")
    with db_session.session_scope() as s:
        assert otr.get_root(s) is None
        (pair,) = radio_pairs(s, drive)
        s.commit()
    home_radio = tmp_path / "home" / "Radio"
    assert Path(pair.local_path) == home_radio
    _result, message = sync(drive)
    assert (home_radio / "Billy Graham" / "Revival.wav").is_file()
    assert "2 radio episodes added" in message
    with db_session.session_scope() as s:
        assert otr.get_root(s) == str(home_radio)
        assert sorted(s.scalars(select(RadioShow.name))) == ["Billy Graham", "The Shadow"]
        assert s.scalar(select(func.count(RadioEpisode.id))) == 2
        # radio episodes are not music
        assert s.scalar(select(func.count(MediaFile.id))) == 0


def test_no_radio_anywhere_means_no_pair(drive):
    with db_session.session_scope() as s:
        assert radio_pairs(s, drive) == []
    assert not (drive.root / "Radio").exists()


def test_a_removed_radio_pair_stays_removed(tmp_path, drive):
    radio = tmp_path / "PC" / "Radio"
    radio.mkdir(parents=True)
    with db_session.session_scope() as s:
        otr.set_root(s, str(radio))
        (pair,) = radio_pairs(s, drive)
        s.commit()
        us.remove_pair(s, pair.id)
        s.commit()
        assert radio_pairs(s, drive) == []
        kinds = [p.kind for p in s.scalars(select(SyncPair))]
        assert us.REMOVED_PREFIX + us.RADIO_KIND in kinds


def test_the_pair_follows_the_radio_pages_folder(tmp_path, drive):
    old, new = tmp_path / "PC" / "Radio", tmp_path / "PC" / "OTR"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    with db_session.session_scope() as s:
        otr.set_root(s, str(old))
        radio_pairs(s, drive)
        s.commit()
        otr.set_root(s, str(new))
        (pair,) = radio_pairs(s, drive)
        assert Path(pair.local_path) == new
