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
