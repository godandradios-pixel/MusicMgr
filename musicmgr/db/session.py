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
        # a unique constraint. A fresh install gets the unique index from
        # create_all; a migrated database doesn't, which is harmless since
        # nothing inserts more than one playback playlist per slug.
        ("slug", "VARCHAR(120)"),
        ("cover_path", "TEXT"),
    ],
    # a chosen folder image for the Playlists page's tile grid.
    # (playlists.cover_path above is a cheap defensive guard, like
    # "tracks.rating" below.)
    "playlist_folders": [("cover_path", "TEXT")],
    "charts": [("folder_id", "INTEGER REFERENCES chart_folders(id)")],
    # rating: defensive - very old databases have no documented history for
    # the column, and the guard costs nothing when it's already there.
    # lastfm_popularity: see services/lastfm_popularity.py and
    # db.models.Track.lastfm_popularity. (An unused spotify_popularity column
    # may exist on some databases; columns are never dropped.)
    "tracks": [("rating", "INTEGER"), ("lastfm_popularity", "INTEGER")],
    # see db.models.Artist.similar_fetched_at / profile_checked_at
    "artists": [("similar_fetched_at", "DATETIME"), ("profile_checked_at", "DATETIME")],
    # see db.models.RadioStation.off_air_since
    "radio_stations": [("off_air_since", "DATETIME")],
    # the Jukebox page's genre chips - see db.models.JukeboxSlot.genre and
    # services/jukebox.py:DEFAULT_JUKEBOX_GENRE.
    "jukebox_slots": [("genre", "VARCHAR(40) DEFAULT 'Rock'")],
    # volume levelling - see db.models.MediaFile.rg_track_gain
    # and services/replaygain.py
    "media_files": [
        ("rg_track_gain", "FLOAT"),
        ("rg_track_peak", "FLOAT"),
        ("rg_album_gain", "FLOAT"),
        ("rg_album_peak", "FLOAT"),
        ("rg_source", "VARCHAR(10)"),
    ],
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


#: indexes added after a table first shipped - create_all() only makes the
#: indexes of tables it creates, so an existing library gets these here
_INDEX_ADDITIONS = [
    # "this song's files" (duplicates.album_copy_for during chart
    # matching, purge_orphaned_tracks, ...) would otherwise scan every row
    ("ix_media_files_track_id", "media_files", "track_id"),
    # case-insensitive path lookups on Windows (scanner.path_matches)
    ("ix_media_files_path_nocase", "media_files", "path COLLATE NOCASE"),
    ("ix_videos_path_nocase", "videos", "path COLLATE NOCASE"),
]


def _ensure_indexes(engine: Engine) -> None:
    with engine.connect() as conn:
        for name, table, column in _INDEX_ADDITIONS:
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")
        conn.commit()


def _table_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None


def _migrate_watched_folders(engine: Engine) -> None:
    """One-time copy of the old `library_folders`/`video_folders` rows into
    the unified `watched_folders` table (see `db.models.WatchedFolder`).
    `init_engine` calls it only when it has just created `watched_folders`,
    so it can never re-run and resurrect a folder since removed (an empty
    table is otherwise indistinguishable from "not migrated yet"). A fresh
    install has no old tables, so it's a no-op there. A folder found in
    both old tables (path-normalized) merges into one: `enabled` if either
    was, and the more recent `last_scan_at`. The old tables are left in
    place, unused - migrations here are additive-only (see
    `_ensure_columns`)."""
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
    _ensure_indexes(_engine)
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


def reset_database() -> None:
    """Permanently erase the current library database and recreate a
    completely empty one in its place.

    Keeps no backup copy, by the owner's choice. `NukeConfirmDialog`/
    `SettingsView.nuke_library` (ui/views/settings/) are what stand between
    an accidental tap and this; the function itself confirms nothing.

    Disposes the current engine first so its pooled connection releases
    the file - SQLite's WAL mode (see the `_sqlite_pragmas` connect hook
    above) otherwise leaves the `-wal`/`-shm` sidecar files (and
    sometimes the main file itself) locked, which would make the delete
    fail on Windows.
    """
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None

    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(str(config.DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    init_engine()


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
