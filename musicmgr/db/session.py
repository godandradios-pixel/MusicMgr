"""Engine / session management. SQLite with foreign keys and WAL turned on."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .. import config
from .models import Base

_engine: Optional[Engine] = None
_Session: Optional[sessionmaker] = None


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
    except Exception:
        pass


#: columns added to an already-shipped table after its first release.
#: `create_all` only creates tables that don't exist yet - it never alters one
#: that's already there, so a database that predates one of these needs a
#: manual ALTER. Each entry is a no-op once the column exists; there is no
#: Alembic here, so this is the whole migration story for a simple additive
#: column. A column that needs backfilling (not just a default) still needs
#: its own one-time fix-up - see services/library.py:backfill_album_artists.
_COLUMN_ADDITIONS = {
    "releases": [("album_artist_id", "INTEGER REFERENCES artists(id)")],
    "playlists": [
        ("folder_id", "INTEGER REFERENCES playlist_folders(id)"),
        # plain column, no UNIQUE - SQLite's ALTER TABLE ADD COLUMN can't add
        # a unique constraint to an existing table. Same trade-off the model
        # already accepts for folder_id/album_artist_id above: a fresh
        # install gets the unique index from create_all, a migrated database
        # gets the column without it. Nothing in this app inserts more than
        # one playback playlist per slug, so the missing DB-level constraint
        # on an old database is not load-bearing.
        ("slug", "VARCHAR(120)"),
    ],
    "charts": [("folder_id", "INTEGER REFERENCES chart_folders(id)")],
    # defensive, not a dated addition - Track.rating has been in the model
    # since before Title Details existed to read it (2026-09-05), but a
    # database this old has no documented history for the column, and the
    # guard costs nothing when it's already there.
    #
    # lastfm_popularity: 2026-09-16 follow-up - James wanted the artist
    # page's "Top Tracks" ranked by an authoritative outside source rather
    # than just local play counts; see services/lastfm_popularity.py and
    # db.models.Track.lastfm_popularity's own docstring (which also covers
    # the same-day pivot away from a short-lived spotify_popularity column,
    # once Spotify turned out to be a dead end for this - see that
    # docstring for why). A database that picked up the old column during
    # the few hours it existed just keeps it sitting there unused, same as
    # every other already-shipped column this project has never bothered
    # dropping.
    "tracks": [("rating", "INTEGER"), ("lastfm_popularity", "INTEGER")],
    # 2026-09-07 follow-up: genre chips on the Jukebox page (James: "I would
    # like the jukebox page to have a chip of 5 genres... select the
    # location and what genre page a track will be organized by") - see
    # db.models.JukeboxSlot.genre and services/jukebox.py:DEFAULT_JUKEBOX_GENRE.
    "jukebox_slots": [("genre", "VARCHAR(40) DEFAULT 'Rock'")],
}


def _ensure_columns(engine: Engine) -> None:
    with engine.connect() as conn:
        for table, columns in _COLUMN_ADDITIONS.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()


def _table_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None


def _migrate_watched_folders(engine: Engine) -> None:
    """One-time copy of the old `library_folders`/`video_folders` rows into
    the new unified `watched_folders` table (2026-09-07 follow-up, James: "I
    really don't need a different block for audio and video. Just one block
    to add folders and then one option to scan all those folders" - see
    `db.models.WatchedFolder`'s docstring). Only ever runs the turn
    `watched_folders` itself gets created - `init_engine` passes
    `existed_before=False` exactly once, the very first time an upgraded
    database (one old enough to still have the pre-merge tables) is opened
    under this app version - so it can never re-run and resurrect a folder
    the person has since removed, even though a `WatchedFolder` table with
    zero rows in it is otherwise indistinguishable from "not migrated yet".
    A brand-new install never has `library_folders`/`video_folders` at all,
    so this is a no-op for it. Old rows found in *both* tables for the same
    (path-normalized) folder merge into one: `enabled` if either was, and
    whichever `last_scan_at` is more recent - nothing scanned only gets
    'never scanned' if it truly never scanned in either table. The two old
    tables are left in place afterward, unused, rather than dropped - this
    project's migrations are additive-only (see `_ensure_columns` above),
    and dropping a table this app no longer reads buys nothing but risk."""
    from pathlib import Path as _Path

    old_tables = [t for t in ("library_folders", "video_folders") if _table_exists(engine, t)]
    if not old_tables:
        return
    merged: dict[str, dict] = {}
    with engine.connect() as conn:
        for table in old_tables:
            for path, enabled, last_scan_at in conn.exec_driver_sql(
                f"SELECT path, enabled, last_scan_at FROM {table}"
            ):
                key = str(_Path(path))
                row = merged.setdefault(key, {"enabled": False, "last_scan_at": None})
                row["enabled"] = row["enabled"] or bool(enabled)
                if last_scan_at and (row["last_scan_at"] is None or last_scan_at > row["last_scan_at"]):
                    row["last_scan_at"] = last_scan_at
    if not merged:
        return
    with engine.begin() as conn:
        for path, row in merged.items():
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO watched_folders (path, enabled, last_scan_at) "
                "VALUES (?, ?, ?)",
                (path, 1 if row["enabled"] else 0, row["last_scan_at"]),
            )


def init_engine(db_path: Optional[Path] = None, echo: bool = False) -> Engine:
    """Create (or recreate) the global engine and make sure tables exist."""
    global _engine, _Session
    if db_path is None:
        config.ensure_dirs()
        db_path = config.DB_PATH
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    _engine = create_engine(url, echo=echo, future=True)
    watched_folders_existed = _table_exists(_engine, "watched_folders")
    Base.metadata.create_all(_engine)
    _ensure_columns(_engine)
    if not watched_folders_existed:
        _migrate_watched_folders(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> sessionmaker:
    if _Session is None:
        init_engine()
    assert _Session is not None
    return _Session


def new_session() -> Session:
    return get_sessionmaker()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
