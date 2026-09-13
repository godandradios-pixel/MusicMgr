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
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db.models import WatchedFolder
from musicmgr.ui.views import settings as settings_module
from musicmgr.ui.views.settings import SettingsView


@pytest.fixture
def view(ctx):
    return SettingsView(ctx)


def add_watched_folder(session, path: str) -> None:
    session.add(WatchedFolder(path=path))


def watched_paths(session) -> list[str]:
    return [f.path for f in session.scalars(select(WatchedFolder).order_by(WatchedFolder.path))]


class TestScanSelected:
    def test_no_row_selected_notifies_and_does_not_scan(self, ctx, view, monkeypatch):
        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths: scanned.append(paths))
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
        monkeypatch.setattr(view, "scan_paths", lambda paths: scanned.append(paths))

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
        monkeypatch.setattr(view, "scan_paths", lambda paths: scanned.append(paths))

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
        monkeypatch.setattr(view, "scan_paths", lambda paths: scanned.append(paths))

        view.scan_all()

        assert len(scanned) == 1
        assert sorted(scanned[0]) == ["/music/one", "/music/two"]

    def test_no_folders_shows_a_message_instead_of_scanning(self, ctx, view, monkeypatch):
        scanned = []
        monkeypatch.setattr(view, "scan_paths", lambda paths: scanned.append(paths))
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
