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
from PySide6.QtWidgets import QDialog
from sqlalchemy import func, select

from musicmgr.db.models import Track, Video, WatchedFolder
from musicmgr.services import metadata_health
from musicmgr.services import scanner
from musicmgr.ui.views import settings as settings_module
from musicmgr.ui.views.settings import SettingsView
from musicmgr.ui.widgets.common import MissingMetadataDialog


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


class _FakeSignal:
    """Stands in for a Qt `Signal` on `_FakeMetadataScanThread` below -
    `.connect()` just remembers the slot, nothing here ever actually
    `.emit()`s (these tests never need the thread to "finish")."""

    def connect(self, slot) -> None:
        self.slot = slot


class _FakeMetadataScanThread:
    """Stands in for `MetadataScanThread` - records that it was asked to
    start without spinning up a real `QThread` (this fixture's `:memory:`
    database would be invisible to one anyway - see this module's own
    docstring). `instances` lets a test assert whether a scan was started
    at all, which is the actual thing these tests care about - whether
    `view_missing_metadata` reused `self._last_metadata_scan` or went and
    rescanned."""

    instances: list["_FakeMetadataScanThread"] = []

    def __init__(self, parent=None) -> None:
        self.started = False
        self.progress = _FakeSignal()
        self.finished_with = _FakeSignal()
        self._interruption_requested = False
        _FakeMetadataScanThread.instances.append(self)

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return False

    def requestInterruption(self) -> None:
        self._interruption_requested = True

    def isInterruptionRequested(self) -> bool:
        return self._interruption_requested


class TestMissingMetadataDashboard:
    """2026-09-22 follow-up (James: "is there any way ... I can go back
    and forth to fix them. Right now it's a one time click and then back
    to running the missing metadata query again") - `view_missing_metadata`
    reusing `self._last_metadata_scan` instead of rescanning, a picked row
    marking itself in `self._metadata_visited`, and the dialog's "Rescan"
    button clearing the cache to force a fresh one. See
    services/metadata_health.py and ui/widgets/common.py:
    MissingMetadataDialog for the rest of the story."""

    @pytest.fixture(autouse=True)
    def _reset_fake_thread(self):
        _FakeMetadataScanThread.instances = []
        yield
        _FakeMetadataScanThread.instances = []

    def test_first_open_with_no_cache_starts_a_scan_thread(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "MetadataScanThread", _FakeMetadataScanThread)

        view.view_missing_metadata()

        assert len(_FakeMetadataScanThread.instances) == 1
        assert _FakeMetadataScanThread.instances[0].started

    def test_reopening_reuses_the_cached_scan_without_rescanning(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "MetadataScanThread", _FakeMetadataScanThread)
        view._last_metadata_scan = metadata_health.ScanResult()
        monkeypatch.setattr(MissingMetadataDialog, "exec", lambda self: QDialog.Rejected)

        view.view_missing_metadata()

        assert _FakeMetadataScanThread.instances == []

    def test_picking_a_row_marks_it_visited_and_navigates(self, view, ctx, monkeypatch):
        view._last_metadata_scan = metadata_health.ScanResult(
            missing_covers=[{"id": 7, "title": "Some Album", "artist": "Some Artist"}],
        )
        monkeypatch.setattr(MissingMetadataDialog, "exec", lambda self: QDialog.Accepted)
        monkeypatch.setattr(MissingMetadataDialog, "picked", lambda self: ("release", 7))
        navigated = []
        opened_releases = []
        ctx.navigateRequested.connect(navigated.append)
        ctx.openReleaseFromMissingMetadataRequested.connect(opened_releases.append)

        view.view_missing_metadata()

        assert navigated == ["library"]
        assert opened_releases == [7]
        assert ("release", 7) in view._metadata_visited
        # the cache itself is untouched by a plain pick - only a rescan or
        # a bulk-fix button (_on_bio_done/_on_popularity_done/_on_artwork_done)
        # clears it.
        assert view._last_metadata_scan is not None

    def test_clicking_rescan_clears_the_cache_and_starts_a_new_scan(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "MetadataScanThread", _FakeMetadataScanThread)
        view._last_metadata_scan = metadata_health.ScanResult()
        monkeypatch.setattr(MissingMetadataDialog, "exec", lambda self: MissingMetadataDialog.REFRESH)

        view.view_missing_metadata()

        assert view._last_metadata_scan is None
        assert len(_FakeMetadataScanThread.instances) == 1
        assert _FakeMetadataScanThread.instances[0].started

    def test_a_bulk_fix_action_invalidates_a_stale_cached_scan(self, view, monkeypatch):
        from musicmgr.services import artist_bio_downloader as bio_dl

        monkeypatch.setattr(
            settings_module.QMessageBox, "information", staticmethod(lambda *a, **k: None)
        )
        view._last_metadata_scan = metadata_health.ScanResult()

        view._on_bio_done(bio_dl.BioDownloadResult())

        assert view._last_metadata_scan is None


class _FakeThread:
    """Generic stand-in for any of this page's QThread subclasses - same
    reasoning as _FakeMetadataScanThread above (no real QThread against
    this fixture's :memory: database - see this module's own docstring),
    generalized to accept any constructor args a real one would (a plain
    `*a, **k` sink) so one fake class covers ArtistImagesImportThread/
    VerifyFilesThread/RematchChartsThread instead of needing three
    near-identical ones."""

    instances: list["_FakeThread"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.progress = _FakeSignal()
        self.finished_with = _FakeSignal()
        self._interruption_requested = False
        _FakeThread.instances.append(self)

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        # a _FakeThread instance stands for "a job was started and hasn't
        # been cleaned up" - these tests never simulate one finishing (the
        # "done" handlers are exercised directly, by calling _on_x_done),
        # so unlike a real QThread this never has a reason to say False.
        return True

    def requestInterruption(self) -> None:
        # 2026-09-22 - James: "add a real cancel button that stops any
        # process running within Settings" - the two bits of real QThread
        # API `_on_cancel_clicked`/`_hide_cancel` actually touch.
        self._interruption_requested = True

    def isInterruptionRequested(self) -> bool:
        return self._interruption_requested


class TestBusyWith:
    """2026-09-22 - the shared guard `download_artist_profiles`/
    `update_track_popularity`/`search_missing_artwork`/`view_missing_metadata`/
    `import_artist_images`/`verify_files`/`rematch_all` all now call before
    starting their own background job."""

    def test_none_when_nothing_is_running(self, view):
        assert view._busy_with() is None

    @pytest.mark.parametrize(
        "attr,message",
        [
            ("_thread", "A scan is running — wait for it to finish first"),
            ("_migration_thread", "A data move is running — wait for it to finish first"),
            ("_bio_thread", "Artist profiles are downloading — wait for it to finish first"),
            ("_popularity_thread", "Track popularity is updating — wait for it to finish first"),
            ("_artwork_thread", "Album artwork is downloading — wait for it to finish first"),
            ("_metadata_scan_thread", "Missing metadata is scanning — wait for it to finish first"),
            ("_artist_images_thread", "Artist images are importing — wait for it to finish first"),
            ("_verify_thread", "Files are being verified — wait for it to finish first"),
            ("_rematch_thread", "Charts are re-matching — wait for it to finish first"),
        ],
    )
    def test_reports_whichever_thread_is_running(self, view, attr, message):
        setattr(view, attr, _FakeThread())

        assert view._busy_with() == message


class TestImportArtistImagesThreaded:
    @pytest.fixture(autouse=True)
    def _reset_fake_thread(self):
        _FakeThread.instances = []
        yield
        _FakeThread.instances = []

    def test_no_folder_chosen_starts_nothing(self, view, monkeypatch):
        monkeypatch.setattr(
            settings_module.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: ""),
        )
        monkeypatch.setattr(settings_module, "ArtistImagesImportThread", _FakeThread)

        view.import_artist_images()

        assert _FakeThread.instances == []

    def test_starts_a_thread_with_the_chosen_folder(self, view, monkeypatch, tmp_path):
        monkeypatch.setattr(
            settings_module.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: str(tmp_path)),
        )
        monkeypatch.setattr(settings_module, "ArtistImagesImportThread", _FakeThread)

        view.import_artist_images()

        assert len(_FakeThread.instances) == 1
        thread = _FakeThread.instances[0]
        assert thread.started
        assert thread.args[0] == str(tmp_path)

    def test_refuses_to_start_while_another_bulk_job_is_running(self, view, monkeypatch, tmp_path):
        monkeypatch.setattr(
            settings_module.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: str(tmp_path)),
        )
        monkeypatch.setattr(settings_module, "ArtistImagesImportThread", _FakeThread)
        view._bio_thread = _FakeThread()
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view.import_artist_images()

        assert _FakeThread.instances == [view._bio_thread]  # no new one started
        assert notifications == ["Artist profiles are downloading — wait for it to finish first"]


class TestVerifyFilesThreaded:
    @pytest.fixture(autouse=True)
    def _reset_fake_thread(self):
        _FakeThread.instances = []
        yield
        _FakeThread.instances = []

    def test_starts_a_thread(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "VerifyFilesThread", _FakeThread)

        view.verify_files()

        assert len(_FakeThread.instances) == 1
        assert _FakeThread.instances[0].started

    def test_refuses_to_start_while_another_bulk_job_is_running(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "VerifyFilesThread", _FakeThread)
        view._artwork_thread = _FakeThread()
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view.verify_files()

        assert _FakeThread.instances == [view._artwork_thread]
        assert notifications == ["Album artwork is downloading — wait for it to finish first"]

    def test_on_verify_done_reports_the_count_and_refreshes(self, view, monkeypatch):
        notifications = []
        view.ctx.notified.connect(notifications.append)
        refreshed = []
        monkeypatch.setattr(view, "refresh", lambda: refreshed.append(True))

        view._on_verify_done(3)

        assert notifications == ["3 file(s) newly marked missing"]
        assert refreshed == [True]
        assert view.progress.isVisible() is False


class TestRematchAllThreaded:
    @pytest.fixture(autouse=True)
    def _reset_fake_thread(self):
        _FakeThread.instances = []
        yield
        _FakeThread.instances = []

    def test_starts_a_thread(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "RematchChartsThread", _FakeThread)

        view.rematch_all()

        assert len(_FakeThread.instances) == 1
        assert _FakeThread.instances[0].started

    def test_refuses_to_start_while_another_bulk_job_is_running(self, view, monkeypatch):
        monkeypatch.setattr(settings_module, "RematchChartsThread", _FakeThread)
        view._verify_thread = _FakeThread()
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view.rematch_all()

        assert _FakeThread.instances == [view._verify_thread]
        assert notifications == ["Files are being verified — wait for it to finish first"]

    def test_on_rematch_done_reports_the_count(self, view, ctx):
        notifications = []
        view.ctx.notified.connect(notifications.append)
        library_changed = []
        ctx.libraryChanged.connect(lambda: library_changed.append(True))

        view._on_rematch_done(5)

        assert notifications == ["5 chart entries now point at tracks you own"]
        assert library_changed == [True]


class TestCancelButton:
    """2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings", asked right after being told "Download
    Artist Profiles" had no way to stop it short of closing the whole app.
    `_active_cancellable_thread`/`_show_cancel`/`_hide_cancel`/
    `_on_cancel_clicked` are the plumbing behind the one `self.cancel_btn`
    - see `_CANCELLABLE_THREAD_ATTRS`'s own docstring for why
    `_migration_thread` is deliberately left out of all of this."""

    def test_hidden_by_default(self, view):
        assert view.cancel_btn.isVisible() is False

    def test_active_cancellable_thread_is_none_when_nothing_is_running(self, view):
        assert view._active_cancellable_thread() is None

    @pytest.mark.parametrize(
        "attr",
        [
            "_thread",
            "_bio_thread",
            "_popularity_thread",
            "_artwork_thread",
            "_metadata_scan_thread",
            "_artist_images_thread",
            "_verify_thread",
            "_rematch_thread",
        ],
    )
    def test_active_cancellable_thread_finds_whichever_one_is_running(self, view, attr):
        thread = _FakeThread()
        setattr(view, attr, thread)

        assert view._active_cancellable_thread() is thread

    def test_migration_thread_is_never_returned_even_while_running(self, view):
        # the one background job Cancel can't touch - data_migration.migrate
        # copies files in several discrete steps with no safe mid-flight
        # stop, so it's simply not in _CANCELLABLE_THREAD_ATTRS at all.
        view._migration_thread = _FakeThread()

        assert view._active_cancellable_thread() is None

    def test_on_cancel_clicked_requests_interruption_on_the_active_thread(self, view):
        thread = _FakeThread()
        view._bio_thread = thread
        view._show_cancel()

        view._on_cancel_clicked()

        assert thread.isInterruptionRequested() is True
        assert view.cancel_btn.isEnabled() is False

    def test_on_cancel_clicked_is_a_no_op_when_nothing_is_running(self, view):
        # nothing running at all - most notably, must not raise just
        # because there's no thread to call requestInterruption() on.
        view._on_cancel_clicked()

    def test_on_cancel_clicked_does_nothing_while_only_migration_is_running(self, view):
        migration_thread = _FakeThread()
        view._migration_thread = migration_thread

        view._on_cancel_clicked()

        assert migration_thread.isInterruptionRequested() is False

    def test_show_cancel_makes_the_button_visible_and_enabled(self, view):
        # isVisible() reflects the whole ancestor chain, not just this
        # widget's own flag - show() the (offscreen, headless) view itself
        # first so a True case is actually observable here, matching every
        # other isVisible()-is-True assertion this suite would need.
        view.show()
        view.cancel_btn.setEnabled(False)

        view._show_cancel()

        assert view.cancel_btn.isVisible() is True
        assert view.cancel_btn.isEnabled() is True

    def test_hide_cancel_hides_the_button(self, view):
        view._show_cancel()

        view._hide_cancel()

        assert view.cancel_btn.isVisible() is False

    def test_hide_cancel_notifies_when_the_given_thread_was_interrupted(self, view):
        thread = _FakeThread()
        thread.requestInterruption()
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view._hide_cancel(thread)

        assert notifications == ["Cancelled — showing partial results"]

    def test_hide_cancel_says_nothing_when_the_thread_finished_on_its_own(self, view):
        thread = _FakeThread()  # never interrupted
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view._hide_cancel(thread)

        assert notifications == []

    def test_hide_cancel_with_no_thread_says_nothing(self, view):
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view._hide_cancel()

        assert notifications == []

    def test_starting_a_bulk_action_shows_the_cancel_button(self, view, monkeypatch):
        view.show()
        _FakeThread.instances = []
        monkeypatch.setattr(settings_module, "VerifyFilesThread", _FakeThread)

        view.verify_files()

        assert view.cancel_btn.isVisible() is True

    def test_finishing_a_bulk_action_hides_the_cancel_button(self, view):
        view._verify_thread = _FakeThread()
        view._show_cancel()

        view._on_verify_done(0)

        assert view.cancel_btn.isVisible() is False

    def test_a_cancelled_scan_hides_the_button_without_a_second_notification(self, view):
        # ScanThread folds "Cancelled — " into its own summary string (see
        # ScanThread.run()) rather than going through _hide_cancel's own
        # notify - _on_scan_done deliberately just hides the button here,
        # so a cancelled scan doesn't say "Cancelled" twice.
        view._thread = _FakeThread()
        view._thread.requestInterruption()
        view._show_cancel()
        notifications = []
        view.ctx.notified.connect(notifications.append)

        view._on_scan_done("Cancelled — Audio: nothing to do · Video: nothing to do")

        assert view.cancel_btn.isVisible() is False
        assert notifications == ["Cancelled — Audio: nothing to do · Video: nothing to do"]

    def test_a_cancelled_metadata_scan_is_not_cached(self, view, monkeypatch):
        monkeypatch.setattr(
            settings_module.QMessageBox, "information", staticmethod(lambda *a, **k: None)
        )
        thread = _FakeMetadataScanThread()
        thread.requestInterruption()
        view._metadata_scan_thread = thread
        monkeypatch.setattr(
            view, "_open_missing_metadata_dialog", lambda result: None
        )

        view._on_metadata_scan_done(metadata_health.ScanResult())

        assert view._last_metadata_scan is None

    def test_an_uncancelled_metadata_scan_is_cached_as_before(self, view, monkeypatch):
        thread = _FakeMetadataScanThread()
        view._metadata_scan_thread = thread
        monkeypatch.setattr(
            view, "_open_missing_metadata_dialog", lambda result: None
        )
        result = metadata_health.ScanResult()

        view._on_metadata_scan_done(result)

        assert view._last_metadata_scan is result
