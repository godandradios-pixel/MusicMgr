"""Shared fixtures for the MusicMgr test suite.

**Headless Qt.** Every GUI test needs a live `QApplication` - Qt refuses to
construct any widget without one - and, on a machine with no display (CI, a
scheduled task, this project's own cloud sandbox), a real display connection
too. `QT_QPA_PLATFORM=offscreen` is set below, before Qt is ever imported,
so the whole suite runs without ever popping an actual window open. Override
it yourself (e.g. `QT_QPA_PLATFORM=windows pytest`, on Windows) if you want
to *watch* a test's window while debugging it.

**Fresh database per test.** `musicmgr.db.session.init_engine` already
special-cases `db_path=":memory:"` into a real in-memory SQLite database
(see its own module for why) - this is the app's own built-in support for
exactly this, not a test-only trick. The `db`/`session`/`ctx` fixtures below
give every test its own empty database, so tests never depend on each other
or on whatever's in your real `data\\library.db`.

**One caveat worth knowing before you rely on it:** SQLite's `:memory:`
database lives on a single connection, and this app's `SingletonThreadPool`
(SQLAlchemy's default pool for `:memory:`) hands out one connection *per
thread*. A test that talks to the `db`/`session` fixture from the main
thread and then spins up a real `QThread` (e.g. actually running
`SettingsView.ScanThread`) will find that thread's own database connection
empty - it's a different in-memory database, not the one the test set up.
Test threaded scanning by monkeypatching the thing that *starts* the thread
(assert it was asked to scan the right paths) rather than letting a real
`ScanThread` run against `:memory:`; save real end-to-end scans for a test
that points at an actual temp folder on disk with a real (non-`:memory:`)
sqlite file, if one is ever needed.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# the default skin and finishes whatever this PC has chosen in Settings ›
# Appearance (2026-10-13, ui/skins.py) - tests check colours by name
os.environ.setdefault("MUSICMGR_DEFAULT_LOOK", "1")

import pytest
from PySide6.QtWidgets import QApplication

from musicmgr.db import session as db_session
from musicmgr.services.player import PlayerController
from musicmgr.ui.context import AppContext


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole run - Qt allows only one per process."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _usb_sync_default_two_way(monkeypatch):
    """USB sync ships defaulting to one way, USB → this PC (2026-10-27,
    services/usb_sync.py DEFAULT_MODE). Most sync tests were written for
    two-way and rely on build_plan's default; the one-way tests ask for
    MODE_FROM_USB by name, and test_settings_usb_sync checks the shipped
    default itself."""
    from musicmgr.services import usb_sync

    monkeypatch.setattr(usb_sync, "DEFAULT_MODE", usb_sync.MODE_TWO_WAY)


@pytest.fixture
def db(qapp):
    """A fresh, empty in-memory database, wired up as the app's global
    engine/sessionmaker for the duration of one test (same call the real app
    makes at startup, just pointed at ":memory:" instead of library.db)."""
    db_session.init_engine(db_path=":memory:")
    yield db_session


@pytest.fixture
def session(db):
    """A single ORM session against the fresh in-memory database - for
    arranging rows directly (e.g. `session.add(WatchedFolder(...))`) without
    going through the UI."""
    with db_session.session_scope() as s:
        yield s


@pytest.fixture
def ctx(qapp, db):
    """A real AppContext, backed by a real (headless) PlayerController and
    the fresh in-memory database - what every view actually gets handed at
    runtime, just with no real audio device or window underneath it."""
    return AppContext(PlayerController())


# -- exit straight after the run (2026-10-13) ---------------------------------
#
# The v2.0.0 release run: "1323 passed", then "QObject: shared QObject was
# deleted directly" and a segmentation fault (exit 139) while Python tore
# down Qt's multimedia objects - so the release failed although every test
# passed (the same family as the 1.9.13 exit crash, which a re-run gets
# past). Once pytest has printed its summary there's nothing left to check,
# so the run ends there with pytest's own exit status, skipping that
# teardown. A failing test still fails the run.

def pytest_unconfigure(config):
    status = getattr(config, "_musicmgr_exitstatus", None)
    if status is None:
        return
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(status))


def pytest_sessionfinish(session, exitstatus):
    session.config._musicmgr_exitstatus = exitstatus


# -- watchdog for a frozen test (2026-10-21) ------------------------------------
#
# The audio player now and then freezes inside Qt's FFmpeg backend at a
# track hand-off (py-spy: QMediaPlayer::mediaStatusChanged -> our handler ->
# play() -> QBasicMutex::lockInternal - a deadlock in Qt's own code; about
# 2 in 25 runs of the player tests here). The 2.3.7 release build sat on it
# for 10+ minutes: pytest-timeout's thread can't fire, because the frozen
# main thread is stuck in Qt and never lets Python run. faulthandler's timer
# is a native thread that needs no Python, so it still prints every
# thread's stack and exits - the run fails in minutes instead of hanging,
# and the release workflow runs the tests once more. (The deadlock itself
# is a separate fix; see the project doc.)

WATCHDOG_S = 200  # a little past pytest-timeout's 180, which catches ordinary Python hangs


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    import faulthandler
    import sys

    faulthandler.dump_traceback_later(WATCHDOG_S, exit=True, file=sys.stderr)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
