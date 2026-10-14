"""The old-time radio folder stays out of the music library (2026-10-13 -
James: "Keep the Radio folder out of the music scan", the scanner side of
the artist-profile change the same evening)."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import MediaFile, Track
from musicmgr.services import otr, radio_folder, scanner
from musicmgr.services import playlists as pl_svc


def wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


@pytest.mark.parametrize("path, radio", [
    (r"D:\Radio\The Shadow\1937-09-26.mp3", True),
    ("d:/radio/Lone Ranger/ep.mp3", True),
    (str(Path.home() / "Radio" / "Paul Harvey" / "a.mp3"), True),
    (r"D:\Music\Queen\Radio\07 Radio Ga Ga.flac", False),
    (r"D:\MusicFLAC\Rush\Permanent Waves\01 The Spirit of Radio.flac", False),
    (r"D:\Radiohead\OK Computer\01.flac", False),
])
def test_what_counts_as_the_radio_folder(path, radio):
    home_radio = [radio_folder._norm(str(Path.home() / "Radio"))]
    assert radio_folder.is_radio_path(path, home_radio) is radio


@pytest.fixture
def library(tmp_path, session):
    """A watched folder with music, a Queen album called Radio, and the
    Radio page's folder inside it."""
    root = tmp_path / "Media"
    wav(root / "Music" / "Rush" / "01 Tom Sawyer.wav")
    wav(root / "Music" / "Queen" / "Radio" / "07 Radio Ga Ga.wav")
    wav(root / "Radio" / "The Shadow" / "1937-09-26.wav")
    wav(root / "Radio" / "Lone Ranger" / "1938-01-01.wav")
    otr.set_root(session, str(root / "Radio"))
    session.flush()
    return root


def paths(session):
    return sorted(Path(p).name for p in session.scalars(select(MediaFile.path)))


def test_a_scan_leaves_the_radio_folder_out(session, library):
    result = scanner.scan_folder(session, library)
    assert result.scanned == 2
    assert paths(session) == ["01 Tom Sawyer.wav", "07 Radio Ga Ga.wav"]


def test_the_radio_folder_itself_isnt_scanned_as_music(session, library):
    result = scanner.scan_folder(session, library / "Radio")
    assert paths(session) == []
    assert any("old-time radio" in e for e in result.errors)


def test_usb_sync_imports_skip_it_too(session, library):
    scanner.import_paths(session, [library / "Radio" / "The Shadow" / "1937-09-26.wav",
                                   library / "Music" / "Rush" / "01 Tom Sawyer.wav"])
    assert paths(session) == ["01 Tom Sawyer.wav"]


def test_radio_files_an_older_scan_added_are_cleared(session, library):
    # what a library scanned before today looks like
    for p in (library / "Radio").rglob("*.wav"):
        scanner.import_single(session, p)
    scanner.import_single(session, library / "Music" / "Rush" / "01 Tom Sawyer.wav")
    session.flush()
    shadow = session.scalar(select(MediaFile).where(MediaFile.path.like("%Shadow%"))).track
    pl = pl_svc.create_playlist(session, "Saturday night")
    pl_svc.add_tracks(session, pl.id, [shadow.id])
    session.flush()
    assert session.scalar(select(func.count(Track.id))) == 3

    removed = scanner.remove_radio_files(session)
    purged = scanner.purge_orphaned_tracks(session)

    assert removed == 2
    assert paths(session) == ["01 Tom Sawyer.wav"]
    # the Lone Ranger track had no history and is gone; The Shadow is in a
    # playlist, so its track stays (as any track whose file went away)
    assert purged == 1
    assert session.get(Track, shadow.id) is not None


def test_rescan_all_reports_it(session, library):
    for p in (library / "Radio").rglob("*.wav"):
        scanner.import_single(session, p)
    scanner.scan_folder(session, library)
    result = scanner.rescan_all(session)
    assert result.radio_removed == 2
    assert "old-time radio files" in result.summary()
    assert paths(session) == ["01 Tom Sawyer.wav", "07 Radio Ga Ga.wav"]
