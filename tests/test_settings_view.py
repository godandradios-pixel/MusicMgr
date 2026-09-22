"""Tests for SettingsView's watched-folders block: add/remove, the
duplicate-path normalization fix, and "Scan now" vs. the new "Scan
selected" (2026-09-13 - James: "right now it does all on the folders. I
would like an option where you can select a folder to scan").

Threaded scanning itself (`ScanThread` actually walking real audio/video
files) is deliberately not exercised here - see conftest.py's module
docstring for why a real QThread can't see this fixture's :memory: database.
These tests instead monkeypatch `scan_paths` (the one seam both "Scan now"
and "Scan selected" funnel through right before a thread would start) and
assert on *what it was asked to scan* - that's the actual behavior this
feature is about, and it doesn't need a real scan to run to prove it.

2026-09-22 fix - the `scan_paths` stub in `TestScanSelected`/
`TestScanAllUnaffected` (`lambda paths: scanned.append(paths)`) only
accepted one positional argument, a leftover from before the 2026-09-18
"Re-read all tags" follow-up threaded a `force` keyword through
`scan_all`/`scan_selected`'s own `self.scan_paths(paths, force=...)`
calls (see `claude/2026-09-18-comment-tag-fix-and-force-rescan.md`) -
that doc claims these same lambdas were fixed already, but the fix
evidently never made it to disk (the same silent-persistence failure the
doc itself describes catching once before), so three of these five kept
raising `TypeError: <lambda>() got an unexpected keyword argument
'force'` every time `scan_all`/`scan_selected` actually reached its
`scan_paths` call. All five now take `force=False` too, whether or not
the specific test's code path reaches that argument today, so a future
change to the early-return branches above `scan_paths` can't silently
reintroduce the same mismatch. `TestAddFolder`'s own `scan_paths` stubs
two classes down are untouched - `add_folder()` calls
`self.scan_paths([path])` with no `force=` at all, a genuinely different
call site, not the same drift.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import Track, Video, WatchedFolder
from musicmgr.services import scanner
from musicmgr.ui.views import settings as settings_module
from musicmgr.ui.views.settings import SettingsView


@pytest.fixture
def view(ctx):
    return SettingsView(ctx)


def make_silent_wav(path: Path, seconds: float = 0.2) -> None:
    """A tiny, real, tagless WAV file - same helper as test_scanner.py's,
    duplicated locally rather than imported cross-file (this suite's own
    convention - see test_video_scanner.py's make_placeholder_video)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


def add_watched_folder(session, path: str) -> None:
    session.add(WatchedFolder(path=path))


def watched_paths(session) -> list[str]:
    return [f.path for f in session.scalars(select(WatchedFolder).order_by(WatchedFolder.path))]


class TestScanSelected:
    def test_no_row_selected_notifies_and_does_not_scan(self, ctx, view, monkeypatch):
        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths, force=False: scanned.append(paths))
        notifications = []
        ctx.notified.connect(notifications.append)

        view.scan_selected()

        assert scanned == []
        assert notifications and "select a folder" in notifications[0].lower()

    def test_scans_only_the_selected_folder(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            add_watched_folder(session, "/music/one")
            add_watched_folder(session, "/music/two")
        view.refresh()

        # refresh() lists folders ordered by path, so row 0 is "/music/one".
        view.folder_list.setCurrentRow(0)
        assert view.folder_list.current_payload()["path"] == "/music/one"

        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths, force=False: scanned.append(paths))

        view.scan_selected()

        assert scanned == [["/music/one"]]

    def test_selecting_the_other_row_scans_that_one_instead(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            add_watched_folder(session, "/music/one")
            add_watched_folder(session, "/music/two")
        view.refresh()

        view.folder_list.setCurrentRow(1)
        assert view.folder_list.current_payload()["path"] == "/music/two"

        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths, force=False: scanned.append(paths))

        view.scan_selected()

        assert scanned == [["/music/two"]]


class TestScanAllUnaffected:
    """"Scan now" must keep scanning every enabled folder - adding "Scan
    selected" alongside it must not narrow its own behavior."""

    def test_scans_every_enabled_folder_regardless_of_selection(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            add_watched_folder(session, "/music/one")
            add_watched_folder(session, "/music/two")
        view.refresh()
        view.folder_list.setCurrentRow(0)  # a selection exists but scan_all ignores it

        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths, force=False: scanned.append(paths))

        view.scan_all()

        assert len(scanned) == 1
        assert sorted(scanned[0]) == ["/music/one", "/music/two"]

    def test_no_folders_shows_a_message_instead_of_scanning(self, ctx, view, monkeypatch):
        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths, force=False: scanned.append(paths))
        boxes = []
        monkeypatch.setattr(
            settings_module.QMessageBox,
            "information",
            staticmethod(lambda *a, **k: boxes.append(a)),
        )

        view.scan_all()

        assert scanned == []
        assert len(boxes) == 1


class TestAddFolder:
    """Covers the 2026-09-07 duplicate-folder fix (architecture.md): a
    forward-slash path from QFileDialog and a backslash path from
    str(Path(...)) used to be treated as two different folders."""

    def test_normalizes_the_dialog_path_before_storing_it(self, ctx, view, monkeypatch, tmp_path):
        chosen = tmp_path.as_posix()  # forward slashes, like QFileDialog returns on Windows
        monkeypatch.setattr(
            settings_module.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *a, **k: chosen),
        )
        monkeypatch.setattr(view, "scan_paths", lambda paths: None)

        view.add_folder()

        with ctx.session() as session:
            paths = watched_paths(session)
        assert paths == [str(Path(chosen).expanduser())]

    def test_adding_the_same_folder_twice_does_not_duplicate_it(self, ctx, view, monkeypatch, tmp_path):
        chosen = tmp_path.as_posix()
        monkeypatch.setattr(
            settings_module.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *a, **k: chosen),
        )
        monkeypatch.setattr(view, "scan_paths", lambda paths: None)

        view.add_folder()
        view.add_folder()

        with ctx.session() as session:
            paths = watched_paths(session)
        assert len(paths) == 1

    def test_cancelling_the_dialog_adds_nothing(self, ctx, view, monkeypatch):
        monkeypatch.setattr(
            settings_module.QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *a, **k: ""),  # Qt returns "" on Cancel
        )

        view.add_folder()

        with ctx.session() as session:
            assert watched_paths(session) == []


class TestRemoveFolder:
    def test_removes_the_selected_folder_but_keeps_the_rest(self, ctx, view):
        with ctx.session() as session:
            add_watched_folder(session, "/music/one")
            add_watched_folder(session, "/music/two")
        view.refresh()
        view.folder_list.setCurrentRow(0)

        view.remove_folder()

        with ctx.session() as session:
            assert watched_paths(session) == ["/music/two"]

    def test_nothing_selected_is_a_no_op(self, ctx, view):
        with ctx.session() as session:
            add_watched_folder(session, "/music/one")
        view.refresh()
        # no setCurrentRow() call - nothing selected

        view.remove_folder()

        with ctx.session() as session:
            assert watched_paths(session) == ["/music/one"]


class TestPurgeMissingFiles:
    """purge_missing_files covers both a missing Track (subject to
    purge_orphaned_tracks's own history-skip rules - already fully covered
    by tests/test_scanner.py:TestPurgeOrphanedTracks, not re-tested here)
    and a missing Video (unconditional - see purge_missing_videos's
    docstring). Each case below carries one real scanned-then-deleted
    audio file alongside a plain Video row, so both sides are exercised
    together the way the actual button call does it."""

    def test_no_missing_files_notifies_and_skips_the_confirmation(self, ctx, view, monkeypatch):
        boxes = []
        monkeypatch.setattr(
            settings_module.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: boxes.append(a) or settings_module.QMessageBox.Yes),
        )
        notifications = []
        ctx.notified.connect(notifications.append)

        view.purge_missing_files()

        assert boxes == []
        assert notifications and "no missing files" in notifications[0].lower()

    def test_declining_the_confirmation_purges_nothing(self, ctx, view, monkeypatch, tmp_path):
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        with ctx.session() as session:
            scanner.scan_folder(session, tmp_path)
            session.add(Video(title="Gone", title_key="gone", path="/old/gone.mp4", is_missing=True))
        wav.unlink()
        monkeypatch.setattr(
            settings_module.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: settings_module.QMessageBox.No),
        )

        view.purge_missing_files()

        with ctx.session() as session:
            assert session.scalar(select(func.count(Track.id))) == 1
            assert session.scalar(select(func.count(Video.id))) == 1

    def test_confirming_purges_both_missing_tracks_and_missing_videos(
        self, ctx, view, monkeypatch, tmp_path
    ):
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        # a real file, not just is_missing=False on the row - purge_missing_files
        # re-checks disk state (mark_missing_videos) before purging, so a
        # merely-flagged-False row whose path doesn't actually exist would
        # get correctly re-flagged missing and purged too, defeating the
        # point of this "keeps present ones" assertion below. .mkv rather
        # than .mp4 deliberately - .mp4 is in config.AUDIO_EXTENSIONS too
        # (an MP4 container can hold audio-only content), so scanner.scan_folder
        # below would also pick this up as a *second*, unrelated Track.
        here = tmp_path / "real" / "here.mkv"
        here.parent.mkdir(parents=True, exist_ok=True)
        here.write_bytes(b"not a real video")
        with ctx.session() as session:
            scanner.scan_folder(session, tmp_path)
            session.add(Video(title="Gone", title_key="gone", path="/old/gone.mp4", is_missing=True))
            session.add(Video(title="Here", title_key="here", path=str(here), is_missing=False))
        wav.unlink()
        monkeypatch.setattr(
            settings_module.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: settings_module.QMessageBox.Yes),
        )
        notifications = []
        ctx.notified.connect(notifications.append)

        view.purge_missing_files()

        with ctx.session() as session:
            assert session.scalar(select(func.count(Track.id))) == 0
            remaining_videos = [v.title for v in session.scalars(select(Video))]
        assert remaining_videos == ["Here"]
        assert notifications[-1] == "Removed 1 missing track and 1 missing video"
