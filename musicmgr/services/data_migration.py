"""Move the data folder (database + artwork cache) to a new location.

Shared by ``tools/migrate_data.py`` (the command-line tool) and
``SettingsView``'s "Move data location…" button (``ui/views/settings.py``)
- one copy-and-rewrite implementation, two front ends. This module lives
inside the ``musicmgr`` package rather than under ``tools/`` specifically
so it ships inside the frozen PyInstaller build (``MusicMgr.spec`` bundles
all of ``musicmgr/``, but not the dev-only ``tools/`` scripts) - the
Settings button has to work from the compiled .exe, not just a source
checkout.

Why this exists rather than "just copy the folder": cover art paths are
stored in the database as absolute paths. Copying ``artwork/`` without
rewriting ``releases.cover_path`` leaves every album showing its
placeholder tile - see architecture.md's "Where things live" and the
2026-09-08 new-PC report this was built for. ``migrate()`` copies,
rewrites those paths, verifies the result, and only then (if asked) offers
to remove the original - never the other order.

Artist portraits (``artists.image_path``, under ``artists/``) are stored
and go stale the exact same way as album covers, and get the exact same
treatment here - copied, repointed by filename, verified. This was missing
from the first version of this module (2026-09-08: "I still don't seem to
be able to get my artist and album artwork showing" turned up covers now
working, portraits still not, since ``migrate()``/``repair()`` only ever
touched ``artwork/``/``cover_path`` and never knew ``artists/``/
``image_path`` existed) - every function/field/message below that
mentions "cover" or "artwork" has an "artist image"/"artists" counterpart
doing the same thing for the other folder.

**The GUI path is riskier than the CLI's documented "close the app
first."** A concurrent write during the ``shutil.copy2`` of ``library.db``
could copy an inconsistent snapshot, and the WAL checkpoint can raise
``sqlite3.OperationalError`` ("database is locked") if something else
holds a transaction open at the same moment. Neither case corrupts
anything real: the checkpoint/copy only ever touches ``dest``, the
original is never modified before verification passes, and ``migrate()``
catches ``OperationalError``/``OSError`` and reports them as an ordinary
failed ``MigrationResult`` rather than raising or touching ``source``.
``verify()``'s ``PRAGMA integrity_check`` on the fresh copy is the backstop
for a torn read making it past the checkpoint. Given that risk,
``SettingsView`` never exposes ``remove_source=True`` - deleting the
folder the running app still has its own database open against is a
different, sharper risk than anything a closed-app CLI run faces, so the
GUI leaves that step to the user, done by hand, after a restart confirms
the new location works.

Your audio files are never touched - ``media_files.path`` still points at
your music where it already lives.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import config

TABLES_EXPECTED = ("artists", "releases", "tracks", "media_files", "chart_entries")

ProgressFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def checkpoint(db_path: Path) -> None:
    """Fold the -wal file back into the main database before copying it."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def snapshot_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        counts = {}
        for table in TABLES_EXPECTED:
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = -1
        return counts
    finally:
        conn.close()


def _basename(path_str: str) -> str:
    """Filename component of ``path_str``, split on either ``/`` or ``\\``.

    ``Path(...).name`` only recognizes the current platform's separator, so
    a Windows-style path left over in the database (backslashes) reads as
    one giant filename on Linux instead of being split at its last
    component - this handles a stored path written by either OS.
    """
    return re.split(r"[\\/]+", path_str)[-1]


def rewrite_cover_paths(db_path: Path, old_art: Path, new_art: Path) -> int:
    """Repoint releases.cover_path at the new artwork folder.

    ``old_art`` isn't consulted - every row is matched purely by filename
    against ``new_art``, which is what lets this same function double as a
    plain "repair the paths already in this database" pass (called with
    ``new_art`` equal to wherever this install's artwork already lives),
    not only a copy-then-rewrite migration.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, cover_path FROM releases WHERE cover_path IS NOT NULL"
        ).fetchall()
        changed = 0
        for release_id, cover in rows:
            name = _basename(cover)
            # match on filename so the rewrite survives separator differences
            # between the machine that wrote the row and this one
            candidate = new_art / name
            if str(candidate) != cover:
                conn.execute(
                    "UPDATE releases SET cover_path = ? WHERE id = ?",
                    (str(candidate), release_id),
                )
                changed += 1
        conn.commit()
        return changed
    finally:
        conn.close()


def rewrite_artist_image_paths(db_path: Path, old_dir: Path, new_dir: Path) -> int:
    """Repoint artists.image_path at the new artist-images folder.

    Same by-filename matching as ``rewrite_cover_paths`` above, for the same
    reason: a portrait imported via ``services.artist_images.import_artist_
    images`` (the Settings "Import artist images…" button, its default
    ``copy=True``) is stored under ``ARTIST_IMG_DIR`` as
    ``{artist.id}_{name}{suffix}`` - a name that's already stable across
    machines, so filename matching against ``new_dir`` is all a rewrite
    needs. ``old_dir`` isn't consulted, for the same reason it isn't in
    ``rewrite_cover_paths``: that's what lets this double as a same-folder
    repair pass, not only a copy-then-rewrite migration.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, image_path FROM artists WHERE image_path IS NOT NULL"
        ).fetchall()
        changed = 0
        for artist_id, image in rows:
            name = _basename(image)
            candidate = new_dir / name
            if str(candidate) != image:
                conn.execute(
                    "UPDATE artists SET image_path = ? WHERE id = ?",
                    (str(candidate), artist_id),
                )
                changed += 1
        conn.commit()
        return changed
    finally:
        conn.close()


def verify(
    db_path: Path,
    art_dir: Path,
    expected: dict[str, int],
    artist_img_dir: Optional[Path] = None,
) -> list[str]:
    """``artist_img_dir`` is optional only so this stays callable the old
    way from anywhere that hasn't been updated - both real callers below
    always pass it."""
    problems: list[str] = []
    if not db_path.exists():
        return [f"database missing at {db_path}"]
    try:
        actual = snapshot_counts(db_path)
        for table, count in expected.items():
            if actual.get(table) != count:
                problems.append(
                    f"{table}: expected {count} rows, found {actual.get(table)}"
                )
        conn = sqlite3.connect(str(db_path))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                problems.append(f"integrity_check said: {integrity}")
            missing_art = 0
            for (cover,) in conn.execute(
                "SELECT cover_path FROM releases WHERE cover_path IS NOT NULL"
            ):
                if not Path(cover).exists():
                    missing_art += 1
            if missing_art:
                problems.append(f"{missing_art} cover image(s) not found at their new path")
            if artist_img_dir is not None:
                missing_portraits = 0
                for (image,) in conn.execute(
                    "SELECT image_path FROM artists WHERE image_path IS NOT NULL"
                ):
                    if not Path(image).exists():
                        missing_portraits += 1
                if missing_portraits:
                    problems.append(
                        f"{missing_portraits} artist portrait(s) not found at their new path"
                    )
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        # 2026-09-13 fix: a database corrupted badly enough (e.g. "database
        # disk image is malformed") can fail before PRAGMA integrity_check
        # ever runs - snapshot_counts()'s own row-count queries above raise
        # first. That used to propagate straight out of verify() as an
        # unhandled sqlite3.DatabaseError, breaking this function's
        # documented "returns a list of problems, never raises" contract
        # (and, one level up, migrate()/repair()'s "reports a clean failed
        # MigrationResult rather than raising" - see the matching notes
        # there). Reporting it as just another problem string means
        # integrity_check's job as "the backstop for a torn read" still
        # gets done even when the corruption is too severe for
        # integrity_check itself to be the thing that notices it.
        problems.append(f"database unreadable: {exc}")
    return problems


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _looks_like_project_root(path: Path) -> bool:
    """True if deleting this folder would take the application with it."""
    return (path / "run.py").exists() or (path / "musicmgr").is_dir()


def refuse_unsafe_removal(source: Path, dest: Path) -> Optional[str]:
    """Explain why removing the source folder must not run, or None if it
    is safe.

    The dangerous case is migrating a root-level data folder *into* its own
    subdirectory (``C:\\Projects\\MusicMgr`` -> ``C:\\Projects\\MusicMgr\\data``).
    Removing the source there would rmtree the project, the code, and the copy
    that was just made.
    """
    if _is_within(dest, source):
        return (
            f"the target sits inside the source ({dest} is under {source}), so "
            "removing the source would delete the copy that was just made"
        )
    if _is_within(source, dest):
        return f"the source sits inside the target ({source} is under {dest})"
    if _looks_like_project_root(source):
        return (
            f"{source} looks like the application folder itself (it contains "
            "run.py or the musicmgr package), not a plain data directory"
        )
    return None


@dataclass
class MigrationResult:
    """Structured outcome of ``migrate()``. ``tools/migrate_data.py``
    reconstructs its own printed report from the fields directly (so its
    output text stays exactly what it always was); ``SettingsView`` just
    shows ``summary()`` in a message box, the same convention
    ``services.artist_images.ImportResult`` already uses."""

    source: Path
    dest: Path
    ok: bool = False
    #: machine-readable outcome: "noop", "no_source_db", "dest_exists",
    #: "unsafe_removal", "db_busy", "io_error", "verify_failed", "success"
    reason: str = ""
    error: Optional[str] = None
    expected_counts: dict[str, int] = field(default_factory=dict)
    changed_covers: int = 0
    #: 2026-09-08 follow-up - artist portraits get the exact same
    #: copy/repoint/verify treatment as album covers now; see the module
    #: docstring for why this was missing from the first version.
    changed_artist_images: int = 0
    problems: list[str] = field(default_factory=list)
    removed_source: bool = False
    leftovers: list[Path] = field(default_factory=list)
    is_portable_dest: bool = False
    stale_musicmgr_home: Optional[str] = None

    def summary(self) -> str:
        if self.reason == "noop":
            return "Source and destination are the same folder — nothing to do."
        if self.reason == "repaired":
            if self.changed_covers == 0:
                bits = [
                    "No cover art paths needed fixing — they already match the "
                    "files in this folder."
                ]
            else:
                bits = [
                    f"{self.changed_covers} cover art path(s) repointed to match "
                    f"files already in {self.dest / 'artwork'}."
                ]
            if self.changed_artist_images == 0:
                bits.append(
                    "No artist portrait paths needed fixing — they already match "
                    "the files in this folder."
                )
            else:
                bits.append(
                    f"{self.changed_artist_images} artist portrait path(s) "
                    f"repointed to match files already in {self.dest / 'artists'}."
                )
            if self.problems:
                bits.append("Some still don't resolve:")
                bits += [f"  ! {p}" for p in self.problems]
            return "\n".join(bits)
        if self.reason == "no_source_db":
            return f"No library.db found at {self.source / 'library.db'} — nothing to move."
        if self.reason == "dest_exists":
            return (
                f"{self.dest / 'library.db'} already exists. Choose a different "
                "folder, or confirm overwrite."
            )
        if self.reason == "unsafe_removal":
            return f"Can't delete the original: {self.error}."
        if self.reason == "db_busy":
            return (
                f"The database was busy and the move was stopped safely (nothing "
                f"was changed): {self.error}\nClose MusicMgr fully and try again."
            )
        if self.reason == "io_error":
            return f"The move was stopped safely after a file error: {self.error}"
        if self.reason == "verify_failed":
            lines = [
                "The move did not verify cleanly, and your original data was "
                "left untouched:"
            ]
            lines += [f"  ! {p}" for p in self.problems]
            return "\n".join(lines)

        # success
        bits = [
            f"Moved your data to {self.dest}.",
            f"{self.changed_covers} cover art path(s) repointed.",
            f"{self.changed_artist_images} artist portrait path(s) repointed.",
        ]
        if self.removed_source:
            bits.append(f"The original files at {self.source} were deleted.")
        elif self.leftovers:
            bits.append(
                "The originals are still in place — once you've confirmed "
                "everything looks right in MusicMgr, delete these by hand:\n"
                + "\n".join(f"  {p}" for p in self.leftovers)
            )
        if self.is_portable_dest:
            bits.append("Portable mode is now active for this folder.")
            if self.stale_musicmgr_home:
                bits.append(
                    f"Note: MUSICMGR_HOME is still set to {self.stale_musicmgr_home} "
                    "and takes priority over portable mode — clear it, or MusicMgr "
                    "will keep reading from there instead."
                )
        else:
            bits.append(f"Set MUSICMGR_HOME={self.dest} so MusicMgr finds this folder.")
        bits.append("Restart MusicMgr for this to take effect.")
        return "\n".join(bits)


def repair(data_dir: Path) -> MigrationResult:
    """Repoint every release's cover_path - and every artist's image_path,
    since 2026-09-08 - at the artwork/portrait files already sitting in
    this exact data folder, by filename - without moving or copying
    anything.

    This is the fix for the specific bug that motivated this module: a
    data/ folder copied by hand (or otherwise already correctly in place)
    where the database still remembers the *old* machine's absolute
    cover_path/image_path values, even though the image files themselves
    made the trip fine under the same filenames. migrate() can't help here -
    its very first check treats a matching source and destination as a
    no-op, which is the right behavior for "move my data" but wrong for
    "the data's already here, the paths just don't know it" - hence this
    separate, dedicated pass. Nothing is copied and no other file is
    touched; this is just rewrite_cover_paths()/rewrite_artist_image_paths()
    against a database's own folder, guarded and reported the same way
    migrate() is.
    """
    db_path = data_dir / "library.db"
    art_dir = data_dir / "artwork"
    artist_img_dir = data_dir / "artists"
    result = MigrationResult(source=data_dir, dest=data_dir)

    if not db_path.exists():
        result.reason = "no_source_db"
        return result

    try:
        result.changed_covers = rewrite_cover_paths(db_path, art_dir, art_dir)
        result.changed_artist_images = rewrite_artist_image_paths(
            db_path, artist_img_dir, artist_img_dir
        )
        result.problems = verify(
            db_path, art_dir, snapshot_counts(db_path), artist_img_dir
        )
    except sqlite3.DatabaseError as exc:
        # 2026-09-13 fix: this used to catch only sqlite3.OperationalError
        # ("database is locked"/"busy") - but OperationalError is a
        # *subclass* of DatabaseError, not the whole family. A genuinely
        # corrupted library.db (e.g. "database disk image is malformed")
        # raises plain DatabaseError, which slipped straight past this
        # narrower catch and crashed repair()/migrate() outright - exactly
        # what the module docstring promises never happens ("catches
        # OperationalError/OSError and reports them as an ordinary failed
        # MigrationResult rather than raising"). Catching the parent class
        # closes that gap while still covering every OperationalError case
        # this already handled.
        result.reason = "db_busy"
        result.error = str(exc)
        return result
    except OSError as exc:
        result.reason = "io_error"
        result.error = str(exc)
        return result

    result.ok = True
    result.reason = "repaired"
    return result


def migrate(
    source: Path,
    dest: Path,
    *,
    remove_source: bool = False,
    force: bool = False,
    progress: Optional[ProgressFn] = None,
) -> MigrationResult:
    """Copy ``library.db`` + ``artwork/`` + ``artists/`` from ``source`` to
    ``dest``, rewriting every release's absolute ``cover_path`` and every
    artist's absolute ``image_path`` to match, verifying the copy, and only
    then (if asked) removing the original. Never modifies ``source`` before
    ``verify()`` passes. See the module docstring for the concurrent-access
    caveat when this runs from inside a live app instead of the CLI tool's
    documented "close the app first".

    ``progress``, if given, is called with a line of text at each step of
    the actual copy/rewrite/verify work (not for the early "can't proceed"
    checks below, which the caller reports itself from ``result.reason``
    since the right wording differs between the CLI and the GUI).
    """
    report = progress or _noop
    result = MigrationResult(source=source, dest=dest)

    if source == dest:
        result.ok = True
        result.reason = "noop"
        return result

    src_db = source / "library.db"
    dst_db = dest / "library.db"

    if not src_db.exists():
        result.reason = "no_source_db"
        return result

    if dst_db.exists() and not force:
        result.reason = "dest_exists"
        return result

    unsafe = refuse_unsafe_removal(source, dest) if remove_source else None
    if unsafe:
        result.reason = "unsafe_removal"
        result.error = unsafe
        return result

    try:
        report("Checkpointing the write-ahead log…")
        checkpoint(src_db)
        expected = snapshot_counts(src_db)
        result.expected_counts = expected
        report("  " + ", ".join(f"{t}={n}" for t, n in expected.items()))

        dest.mkdir(parents=True, exist_ok=True)
        report("Copying database…")
        shutil.copy2(src_db, dst_db)
        # stale -wal/-shm at the destination would shadow the copy
        for suffix in ("-wal", "-shm"):
            stale = Path(str(dst_db) + suffix)
            if stale.exists():
                stale.unlink()

        src_art = source / "artwork"
        dst_art = dest / "artwork"
        if src_art.is_dir():
            report("Copying artwork cache…")
            dst_art.mkdir(parents=True, exist_ok=True)
            for item in src_art.iterdir():
                if item.is_file():
                    shutil.copy2(item, dst_art / item.name)
        else:
            dst_art.mkdir(parents=True, exist_ok=True)

        # 2026-09-08 follow-up - same treatment as artwork/ above, for the
        # artist portraits folder this originally missed entirely (see the
        # module docstring).
        src_artist_img = source / "artists"
        dst_artist_img = dest / "artists"
        if src_artist_img.is_dir():
            report("Copying artist portraits…")
            dst_artist_img.mkdir(parents=True, exist_ok=True)
            for item in src_artist_img.iterdir():
                if item.is_file():
                    shutil.copy2(item, dst_artist_img / item.name)
        else:
            dst_artist_img.mkdir(parents=True, exist_ok=True)

        report("Rewriting cover art paths…")
        result.changed_covers = rewrite_cover_paths(dst_db, src_art, dst_art)
        report(f"  {result.changed_covers} release row(s) updated")

        report("Rewriting artist portrait paths…")
        result.changed_artist_images = rewrite_artist_image_paths(
            dst_db, src_artist_img, dst_artist_img
        )
        report(f"  {result.changed_artist_images} artist row(s) updated")

        report("Verifying…")
        problems = verify(dst_db, dst_art, expected, dst_artist_img)
    except sqlite3.DatabaseError as exc:
        # 2026-09-13 fix - see the matching note in repair(): OperationalError
        # is a subclass of DatabaseError, not the whole family, so a
        # corrupted source database used to crash migrate() outright
        # instead of coming back as a clean, reportable MigrationResult.
        result.reason = "db_busy"
        result.error = str(exc)
        return result
    except OSError as exc:
        result.reason = "io_error"
        result.error = str(exc)
        return result

    if problems:
        result.reason = "verify_failed"
        result.problems = problems
        return result

    report("  row counts match, integrity_check ok, all cover art and portraits resolve")

    if remove_source:
        report(f"Removing {source}…")
        shutil.rmtree(source, ignore_errors=True)
        result.removed_source = True
        report("  done")
    else:
        leftovers = [src_db]
        leftovers += [
            Path(str(src_db) + suffix)
            for suffix in ("-wal", "-shm")
            if Path(str(src_db) + suffix).exists()
        ]
        if src_art.is_dir():
            leftovers.append(src_art)
        if src_artist_img.is_dir():
            leftovers.append(src_artist_img)
        result.leftovers = leftovers

    result.is_portable_dest = dest == config.PROJECT_ROOT / "data"
    if result.is_portable_dest:
        stale = os.environ.get("MUSICMGR_HOME")
        if stale and Path(stale).expanduser().resolve() != dest:
            result.stale_musicmgr_home = stale

    result.ok = True
    result.reason = "success"
    return result
