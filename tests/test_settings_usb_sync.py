"""Settings → USB sync card and UsbSyncDialog (2026-09-23).

services/usb_sync.py's own tests cover walking/comparing/copying. These
cover the UI decisions: what the card shows with and without a drive, the
plug-in check leaving a "Review…" button, a manual check opening the
review, a first sync of identical folders recording quietly, and the
dialog's defaults (deletions unchecked, not-enough-room blocks Sync).

Threads are run synchronously via .run() on the test thread - the
in-memory test database is per-thread (see conftest.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox
from sqlalchemy import select

from musicmgr.db.models import MediaFile, SyncBaseline, WatchedFolder
from musicmgr.db.session import session_scope
from musicmgr.services import usb_sync as us
from musicmgr.ui.views.settings import SettingsView
from musicmgr.ui.widgets.usb_sync import UsbCompareThread, UsbSyncDialog, UsbSyncThread


def write(path: Path, data: bytes = b"x", mtime: float = 1_600_000_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def setup(ctx, tmp_path, monkeypatch):
    local = tmp_path / "PC" / "Music"
    local.mkdir(parents=True)
    (tmp_path / "USB" / "Music").mkdir(parents=True)
    drive = us.write_marker(tmp_path / "USB", "Test USB")
    with ctx.session() as db:
        db.add(WatchedFolder(path=str(local)))
    drives = []
    monkeypatch.setattr(SettingsView, "_find_usb_drives", lambda self: list(drives))
    view = SettingsView(ctx)
    return view, local, tmp_path / "USB" / "Music", drive, drives


def run_compare(drive):
    out = {}
    t = UsbCompareThread(drive)
    t.finished_with.connect(lambda plan, err: out.update(plan=plan, err=err))
    t.run()
    assert out["err"] is None
    return out["plan"]


class TestCard:
    def test_no_drive(self, setup):
        view, *_ = setup
        view.poll_usb()
        assert "No MusicMgr USB drive" in view.usb_drive_label.text()
        assert not view.usb_check_btn.isEnabled()
        assert view.usb_setup_btn.isEnabled()
        assert view.usb_review_btn.isHidden()

    def test_drive_appears_shows_pairs_and_auto_checks(self, setup, monkeypatch):
        view, local, usb, drive, drives = setup
        calls = []
        monkeypatch.setattr(view, "check_usb", lambda auto=False: calls.append(auto))
        view.poll_usb()                      # launch: no drive yet
        drives.append(drive)
        view.poll_usb()                      # plugged in while running
        assert "Test USB" in view.usb_drive_label.text()
        assert str(local) in view.usb_pairs_label.text()
        assert view.usb_check_btn.isEnabled()
        assert calls == [True]
        view.poll_usb()                      # same drive: no second check
        assert calls == [True]

    def test_auto_check_off(self, setup, monkeypatch):
        view, local, usb, drive, drives = setup
        view.usb_auto_cb.setChecked(False)
        calls = []
        monkeypatch.setattr(view, "check_usb", lambda auto=False: calls.append(auto))
        view.poll_usb()
        drives.append(drive)
        view.poll_usb()
        assert calls == []

    def test_drive_already_in_at_launch_is_not_checked(self, setup, monkeypatch):
        """2026-09-24: nothing slow at startup - a drive that's already
        plugged in when MusicMgr starts is only shown, not walked."""
        view, local, usb, drive, drives = setup
        calls = []
        monkeypatch.setattr(view, "check_usb", lambda auto=False: calls.append(auto))
        drives.append(drive)
        view.poll_usb()
        assert "Test USB" in view.usb_drive_label.text()
        assert calls == []

    def test_auto_check_with_changes_leaves_review_button(self, setup, monkeypatch):
        view, local, usb, drive, drives = setup
        write(usb / "New" / "song.mp3")
        drives.append(drive)
        view._usb_drive = drive
        plan = run_compare(drive)
        notes = []
        view.ctx.notified.connect(notes.append)
        view._on_usb_compare_done(plan, None, auto=True)
        assert view._usb_pending_plan is plan
        assert not view.usb_review_btn.isHidden()
        assert "1 new from USB" in notes[-1]

    def test_manual_check_opens_review_and_syncs(self, setup, monkeypatch):
        view, local, usb, drive, drives = setup
        write(usb / "New" / "song.mp3")
        view._usb_drive = drive
        plan = run_compare(drive)

        class FakeDialog:
            def exec(self):
                return QDialog.Accepted

        started = []
        monkeypatch.setattr(view, "_make_usb_dialog", lambda p: FakeDialog())
        monkeypatch.setattr(view, "_start_usb_sync", lambda p, quiet=False: started.append((p, quiet)))
        view._on_usb_compare_done(plan, None, auto=False)
        assert started == [(plan, False)]

    def test_identical_first_sync_records_quietly(self, setup, monkeypatch):
        view, local, usb, drive, drives = setup
        write(local / "a.mp3")
        write(usb / "a.mp3")
        view._usb_drive = drive
        plan = run_compare(drive)
        assert not plan.all_items() and plan.pairs[0].record
        started = []
        monkeypatch.setattr(view, "_start_usb_sync", lambda p, quiet=False: started.append(quiet))
        view._on_usb_compare_done(plan, None, auto=False)
        assert started == [True]
        assert "in sync" in view.usb_status.text()

    def test_sync_done_refreshes_library(self, setup, qapp):
        from tests.test_scanner import make_silent_wav

        view, local, usb, drive, drives = setup
        make_silent_wav(usb / "New" / "song.wav")
        view._usb_drive = drive
        plan = run_compare(drive)
        out = {}
        t = UsbSyncThread(plan)
        t.finished_with.connect(lambda r, m, e: out.update(r=r, m=m, e=e))
        t.run()
        assert out["e"] is None and out["r"].copied_to_local == 1
        assert (local / "New" / "song.wav").exists()
        assert "1 track added" in out["m"]
        fired = []
        view.ctx.libraryChanged.connect(lambda: fired.append(1))
        view._on_usb_sync_done(out["r"], out["m"], None)
        assert fired and "1 copied from USB" in view.usb_status.text()
        with view.ctx.session() as db:
            assert db.scalar(select(SyncBaseline.rel_path)) == "New/song.wav"
            assert db.scalar(select(MediaFile.path)) == str(local / "New" / "song.wav")

    def test_busy_blocks_scan(self, setup):
        view, *_ = setup

        class Running:
            def isRunning(self):
                return True

        view._usb_sync_thread = Running()
        assert "USB" in view._busy_with()
        view._usb_sync_thread = None

    def test_setup_drive(self, setup, tmp_path, monkeypatch):
        view, *_ = setup
        root = tmp_path / "NewUSB"
        root.mkdir()
        monkeypatch.setattr(view, "_pick_usb_root", lambda: str(root))
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        view.setup_usb_drive()
        assert us.load_marker(root) is not None
        assert view._usb_drive.root == root


class TestDialog:
    def make(self, setup, **kw):
        view, local, usb, drive, drives = setup
        write(local / "a.mp3", b"aaaa")
        write(local / "gone.mp3")
        write(usb / "gone.mp3")
        write(usb / "b.mp3", b"bb")
        plan = run_compare(drive)
        # record gone.mp3, then delete it from the USB to make a deletion
        with session_scope() as db:
            p2 = us.build_plan(db, drive)
            for i in p2.all_items():
                i.selected = False
            us.apply(db, p2, trash=lambda p: None)
        (usb / "gone.mp3").unlink()
        plan = run_compare(drive)
        return UsbSyncDialog(plan, **kw), plan

    def test_tabs_and_defaults(self, setup):
        dlg, plan = self.make(setup)
        assert "From USB (1)" in dlg.tabs.tabText(0)
        assert "To USB (1)" in dlg.tabs.tabText(1)
        assert "Deletions (1)" in dlg.tabs.tabText(3)
        assert not dlg.tabs.isTabEnabled(2)          # no conflicts
        (node, item), = list(dlg.deletions.leaves())
        assert node.checkState(0) == Qt.Unchecked and item.kind == us.DELETE
        assert dlg.sync_btn.isEnabled()
        assert "↓ 1 file ·" in dlg.totals.text() and "↑ 1 file ·" in dlg.totals.text()

    def test_unchecking_everything_disables_sync(self, setup):
        dlg, plan = self.make(setup)
        for tree in dlg._trees():
            for node, _ in tree.leaves():
                node.setCheckState(0, Qt.Unchecked)
        dlg._update_totals()
        assert not dlg.sync_btn.isEnabled()

    def test_restore_button(self, setup):
        dlg, plan = self.make(setup)
        (node, item), = list(dlg.deletions.leaves())
        node.setCheckState(0, Qt.Checked)
        dlg._set_restore(True)
        assert item.restore and "restore to the USB" in node.text(2)

    def test_not_enough_room(self, setup):
        dlg, plan = self.make(setup, usb_free=1)
        assert "Not enough room" in dlg.totals.text()
        assert not dlg.sync_btn.isEnabled()

    def test_deletions_ask_before_sync(self, setup, monkeypatch):
        dlg, plan = self.make(setup)
        (node, item), = list(dlg.deletions.leaves())
        node.setCheckState(0, Qt.Checked)
        asked = []
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(1) or QMessageBox.No)
        dlg._on_sync()
        assert asked and dlg.result() != QDialog.Accepted


class TestArtworkUI:
    def test_relink_row(self, setup, tmp_path, monkeypatch):
        from musicmgr import config
        from musicmgr.db.models import Release

        view, *_ = setup
        monkeypatch.setattr(config, "ART_DIR", tmp_path / "artwork")
        monkeypatch.setattr(config, "ARTIST_IMG_DIR", tmp_path / "artists")
        (tmp_path / "artwork").mkdir()
        (tmp_path / "artists").mkdir()
        write(tmp_path / "artwork" / "Rush - Signals.jpg")
        with view.ctx.session() as db:
            db.add(Release(title="Signals", title_key="signals", artist_display="Rush"))
        notes = []
        view.ctx.notified.connect(notes.append)
        view.relink_artwork()
        assert notes[-1].startswith("Linked 1 covers")

    def test_conflict_preview_shows_both_pictures(self, qapp, tmp_path):
        from PySide6.QtGui import QColor, QImage

        local, usb = tmp_path / "L", tmp_path / "U"
        for root, color in ((local, "red"), (usb, "blue")):
            root.mkdir()
            im = QImage(20, 20, QImage.Format_RGB32)
            im.fill(QColor(color))
            im.save(str(root / "Rush.jpg"))
        plan = us.ComparePlan(us.Drive(tmp_path, "d", "USB"))
        pp = us.PairPlan(1, "artists", local, usb)
        pp.items.append(us.SyncItem(us.CONFLICT, "Rush.jpg", us.FileStat(1, 1), us.FileStat(2, 2),
                                    us.TO_LOCAL, True, local_rel="Rush.jpg", usb_rel="Rush.jpg"))
        plan.pairs.append(pp)
        dlg = UsbSyncDialog(plan, local_free=10**9, usb_free=10**9)
        (node, _), = list(dlg.conflicts.leaves())
        dlg.conflicts.setCurrentItem(node)
        assert not dlg.conflict_preview.isHidden()
        assert dlg.conflict_preview.local.pixmap() is not None


class TestPairsDialog:
    def make(self, setup, monkeypatch):
        from musicmgr.ui.widgets.usb_sync import UsbPairsDialog

        view, local, usb, drive, drives = setup
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        return UsbPairsDialog(drive), local, drive

    def test_change_usb_folder_and_remove(self, setup, monkeypatch, tmp_path):
        from musicmgr.db.models import SyncPair

        dlg, local, drive = self.make(setup, monkeypatch)
        (drive.root / "Elsewhere").mkdir()
        dlg.list.setCurrentRow(0)
        assert dlg.remove_btn.isEnabled() and dlg.local_btn.isEnabled()
        monkeypatch.setattr(dlg, "_pick_usb", lambda start="": str(drive.root / "Elsewhere"))
        dlg._change_usb()
        assert "(changed)" in dlg.list.item(0).text()
        dlg._save()
        with session_scope() as db:
            (pair,) = db.scalars(select(SyncPair)).all()
            assert pair.usb_rel_path == "Elsewhere"

    def test_remove_then_ok(self, setup, monkeypatch):
        dlg, local, drive = self.make(setup, monkeypatch)
        dlg.list.setCurrentRow(0)
        dlg._remove()
        assert dlg.list.count() == 0
        dlg._save()
        with session_scope() as db:
            assert us.build_plan(db, drive).pairs == []

    def test_artwork_pair_local_side_is_locked(self, setup, monkeypatch, tmp_path):
        from musicmgr import config
        from musicmgr.services import artwork_names

        monkeypatch.setattr(config, "ART_DIR", tmp_path / "data" / "artwork")
        monkeypatch.setattr(config, "ARTIST_IMG_DIR", tmp_path / "data" / "artists")
        with session_scope() as db:
            artwork_names._mark_done(db)
        dlg, local, drive = self.make(setup, monkeypatch)
        rows = [dlg.list.item(i).text() for i in range(dlg.list.count())]
        idx = next(i for i, t in enumerate(rows) if t.endswith("USB\\MusicMgr\\artwork"))
        dlg.list.setCurrentRow(idx)
        assert not dlg.local_btn.isEnabled() and dlg.usb_btn.isEnabled()


class TestLibraryDataChoices:
    def test_four_checkboxes_default_on_and_persist(self, setup, ctx):
        from musicmgr.services import library_state as ls

        view, *_ = setup
        assert set(view.usb_data_cbs) == set(ls.CATEGORIES)
        assert all(cb.isChecked() for cb in view.usb_data_cbs.values())
        view.usb_data_cbs[ls.JUKEBOX].setChecked(False)
        with ctx.session() as db:
            assert ls.JUKEBOX not in ls.enabled_categories(db)
        again = SettingsView(ctx)
        assert not again.usb_data_cbs[ls.JUKEBOX].isChecked()
        assert again.usb_data_cbs[ls.PLAYS].isChecked()
