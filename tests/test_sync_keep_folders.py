"""2026-09-30 - James: "Please fix the sync so it asks before removing a
whole folder of playlists." A sync had removed 78 Records (with DECCA) and
Personal Favorites from his D:\\MusicMgr PC because the drive no longer
listed them. Now emptying a whole folder needs a yes; Keep sends the folder
back to the drive."""

from __future__ import annotations

from sqlalchemy import select

from musicmgr.db import session as db_session
from musicmgr.db.models import Playlist, PlaylistFolder, PlaylistItem
from musicmgr.services import library_state as ls
from musicmgr.services import usb_sync as us
from musicmgr.ui.widgets.usb_sync import UsbSyncThread
from tests.test_library_state import SONGS, two_pcs  # noqa: F401 (fixture)


def sync(pc, confirm):
    pc.use()
    with db_session.session_scope() as s:
        plan = us.build_plan(s, pc.drive)
        result = us.apply(s, plan, trash=lambda p: None)
        us.update_library(s, result)
        return ls.sync_library_state(s, pc.drive, us.get_pc_id(s), confirm_removals=confirm)


def make_78s(pc):
    """78 Records › Bluebird, 78 Records › DECCA › Bing, plus a top-level one."""
    pc.use()
    with db_session.session_scope() as s:
        top = PlaylistFolder(name="78 Records")
        s.add(top)
        s.flush()
        decca = PlaylistFolder(name="DECCA", parent_id=top.id)
        s.add(decca)
        s.flush()
        t = pc.track(s, SONGS[0])
        for name, folder in (("Bluebird", top.id), ("Bing", decca.id), ("Loose", None)):
            pl = Playlist(name=name, kind=Playlist.KIND_MANUAL, folder_id=folder)
            s.add(pl)
            s.flush()
            s.add(PlaylistItem(playlist_id=pl.id, track_id=t.id, position=0))


def delete_78s(pc):
    pc.use()
    with db_session.session_scope() as s:
        for pl in s.scalars(select(Playlist).where(Playlist.name.in_(["Bluebird", "Bing"]))):
            s.delete(pl)
        s.flush()
        for f in s.scalars(select(PlaylistFolder).order_by(PlaylistFolder.id.desc())):
            s.delete(f)


def names(pc):
    pc.use()
    with db_session.session_scope() as s:
        return (sorted(p.name for p in s.scalars(select(Playlist)) if p.kind == Playlist.KIND_MANUAL),
                sorted(f.name for f in s.scalars(select(PlaylistFolder))))


def test_asks_once_per_outer_folder_and_keep_sends_it_back(two_pcs):
    pc1, pc2, drive = two_pcs
    make_78s(pc1)
    sync(pc1, None)
    sync(pc2, None)
    delete_78s(pc2)
    sync(pc2, None)                       # the drive no longer has 78 Records

    asked = []
    notes = sync(pc1, lambda folders: asked.append(folders) or False)
    assert [[(f.path, f.playlists) for f in a] for a in asked] == [[("78 Records", 2)]]
    assert asked[0][0].label() == "78 Records (2 playlists)"
    assert notes.playlists_removed == [] and notes.folders_removed == []
    assert notes.folders_kept == ["78 Records"]
    assert "kept 1 folder the drive didn't have" in notes.summary()
    assert names(pc1) == (["Bing", "Bluebird", "Loose"], ["78 Records", "DECCA"])

    # kept = back on the drive, so PC2 gets it again, and nobody is asked
    asked.clear()
    notes = sync(pc2, lambda folders: asked.append(folders) or False)
    assert asked == []
    assert names(pc2) == (["Bing", "Bluebird", "Loose"], ["78 Records", "DECCA"])
    assert sync(pc1, lambda f: asked.append(f) or False).summary() == "Library data already in sync"
    assert asked == []


def test_remove_answer_removes_as_before(two_pcs):
    pc1, pc2, drive = two_pcs
    make_78s(pc1)
    sync(pc1, None)
    sync(pc2, None)
    delete_78s(pc2)
    sync(pc2, None)
    notes = sync(pc1, lambda folders: True)
    assert sorted(notes.playlists_removed) == ["78 Records/Bluebird", "78 Records/DECCA/Bing"]
    assert names(pc1) == (["Loose"], [])


def test_single_playlist_removal_is_not_asked(two_pcs):
    """Only a folder losing everything is asked about; one playlist out of
    a folder still goes quietly."""
    pc1, pc2, drive = two_pcs
    make_78s(pc1)
    sync(pc1, None)
    sync(pc2, None)
    pc2.use()
    with db_session.session_scope() as s:
        s.delete(s.scalar(select(Playlist).where(Playlist.name == "Bluebird")))
        s.delete(s.scalar(select(Playlist).where(Playlist.name == "Loose")))
    sync(pc2, None)
    asked = []
    notes = sync(pc1, lambda f: asked.append(f) or False)
    assert asked == []            # 78 Records still has DECCA › Bing
    assert sorted(notes.playlists_removed) == ["78 Records/Bluebird", "Loose"]


def test_a_subfolder_emptied_on_its_own_is_asked_about(two_pcs):
    pc1, pc2, drive = two_pcs
    make_78s(pc1)
    sync(pc1, None)
    sync(pc2, None)
    pc2.use()
    with db_session.session_scope() as s:
        s.delete(s.scalar(select(Playlist).where(Playlist.name == "Bing")))
    sync(pc2, None)
    asked = []
    sync(pc1, lambda f: asked.append([x.path for x in f]) or False)
    assert asked == [["78 Records/DECCA"]]
    assert names(pc1)[0] == ["Bing", "Bluebird", "Loose"]


def test_thread_asks_through_its_callback(two_pcs, qapp):
    pc1, pc2, drive = two_pcs
    make_78s(pc1)
    sync(pc1, None)
    sync(pc2, None)
    delete_78s(pc2)
    sync(pc2, None)
    pc1.use()
    with db_session.session_scope() as s:
        plan = us.build_plan(s, drive)
    t = UsbSyncThread(plan)
    asked = []
    t.confirm_removals = lambda folders: asked.append([f.path for f in folders]) or False
    out = {}
    t.finished_with.connect(lambda r, m, e: out.update(r=r, m=m, e=e))
    t.run()                               # same thread: called directly
    assert out["e"] is None and asked == [["78 Records"]]
    assert out["r"].library_notes.folders_kept == ["78 Records"]
    assert names(pc1)[1] == ["78 Records", "DECCA"]


def test_thread_asks_on_the_gui_thread_when_running(two_pcs, qapp):
    """The real case: run() on the worker, the question on the GUI thread."""
    import threading

    from PySide6.QtCore import QEventLoop, QTimer

    pc1, pc2, drive = two_pcs
    make_78s(pc1)
    sync(pc1, None)
    sync(pc2, None)
    delete_78s(pc2)
    sync(pc2, None)
    pc1.use()
    with db_session.session_scope() as s:
        plan = us.build_plan(s, drive)
    t = UsbSyncThread(plan)
    where = []
    t.confirm_removals = lambda folders: where.append(threading.current_thread()) or False
    loop = QEventLoop()
    out = {}
    t.finished_with.connect(lambda r, m, e: (out.update(r=r, e=e), loop.quit()))
    QTimer.singleShot(20000, loop.quit)
    t.start()
    loop.exec()
    t.wait()
    assert out.get("e") is None and where == [threading.main_thread()]
    assert out["r"].library_notes.folders_kept == ["78 Records"]


def test_settings_question_defaults_to_keep(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QWidget

    from musicmgr.ui.views.settings import SettingsView

    shown = {}

    def fake_exec(box):
        shown["text"] = box.text() + "\n" + box.informativeText()
        shown["default"] = box.defaultButton().text()
        target = next(b for b in box.buttons() if b.text() == shown.get("press", "Keep"))
        target.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    folders = [ls.FolderRemoval("78 Records", 24), ls.FolderRemoval("Personal Favorites", 5)]
    parent = QWidget()
    assert SettingsView._confirm_usb_folder_removals(parent, folders) is False
    assert shown["default"] == "Keep"
    assert "78 Records (24 playlists)" in shown["text"]
    assert "Personal Favorites (5 playlists)" in shown["text"]
    shown["press"] = "Remove"
    assert SettingsView._confirm_usb_folder_removals(parent, folders) is True
