"""Daily library backups and restore.

Rotating, local-only library backups (daily on close, keep the last 7) with
a Restore option in Settings - in addition to the three pre-update copies
(updater.backup_database_if_version_changed).

- `make_daily_backup` runs when MusicMgr closes: at most one copy a day,
  `backups/library-daily-YYYY-MM-DD.db`, the newest KEEP_DAILY kept. It uses
  SQLite's online backup API (consistent, includes the -wal), writes to a
  `.partial` name first and checks the copy with `PRAGMA quick_check`
  before keeping it. A copy that fails the check is thrown away and nothing
  is pruned, so a damaged library can never rotate the good copies out.
  Settings' "Back up now" calls the same function with force=True.
- Restore can't swap library.db under the running app, so `schedule_restore`
  only records which backup to use (RESTORE_MARKER) and the app restarts.
  `apply_pending_restore` runs at the next start before the database is
  opened: it saves the current library as `library-pre-restore-<stamp>.db`
  (so a restore can itself be undone), copies the backup into place and
  removes the old -wal/-shm, which belong to the replaced file and would
  otherwise be replayed onto it.

Never raises into the caller's shutdown/startup path: failures come back
as a message (BackupResult.error / RestoreOutcome.error) and are logged.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

KEEP_DAILY = 7
DAILY_PREFIX = "library-daily-"
PRE_RESTORE_PREFIX = "library-pre-restore-"
RESTORE_MARKER_NAME = "restore_pending.txt"


@dataclass
class BackupResult:
    path: Optional[Path] = None
    skipped: bool = False      # already backed up today
    error: str = ""


@dataclass
class BackupFile:
    path: Path
    kind: str                  # "Daily", "Before update", "Before restore", "Other"
    when: dt.datetime
    size: int

    @property
    def label(self) -> str:
        mb = self.size / 1_048_576
        return f"{self.when:%a %b %d %Y, %I:%M %p}  ·  {self.kind}  ·  {mb:,.0f} MB"


@dataclass
class RestoreOutcome:
    restored_from: Optional[Path] = None
    saved_current_as: Optional[Path] = None
    error: str = ""


def daily_name(day: dt.date) -> str:
    return f"{DAILY_PREFIX}{day:%Y-%m-%d}.db"


def _copy_database(src_path: Path, dest: Path) -> None:
    """Consistent copy of a (possibly open, WAL-mode) database to `dest`
    via a `.partial` file, so a half-written copy never has a real name."""
    partial = dest.with_name(dest.name + ".partial")
    _remove_quietly(partial)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
            # a backup file should be one self-contained file
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(partial, dest)


def check_database(path: Path) -> str:
    """"" if `path` is a healthy SQLite library, else what's wrong."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                return f"the copy failed its check ({row[0] if row else 'no answer'})"
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks'"
            ).fetchone() is None:
                return "it isn't a MusicMgr library"
        finally:
            con.close()
    except sqlite3.Error as exc:
        return f"it couldn't be read ({exc})"
    return ""


def make_daily_backup(
    db_path: Path, backup_dir: Path, *, today: Optional[dt.date] = None,
    keep: int = KEEP_DAILY, force: bool = False,
) -> BackupResult:
    """Today's backup, unless there already is one (`force` replaces it -
    Settings' "Back up now")."""
    today = today or dt.date.today()
    dest = backup_dir / daily_name(today)
    try:
        if not db_path.is_file():
            return BackupResult(error="There's no library to back up yet")
        if dest.exists() and not force:
            return BackupResult(path=dest, skipped=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        fresh = dest.with_name(dest.name + ".new")
        _copy_database(db_path, fresh)
        problem = check_database(fresh)
        if problem:
            _remove_quietly(fresh)
            log.error("daily backup discarded: %s", problem)
            return BackupResult(error=f"Backup not saved - {problem}. Your earlier backups were kept.")
        os.replace(fresh, dest)
        prune_daily(backup_dir, keep)
        log.info("library backed up to %s", dest)
        return BackupResult(path=dest)
    except Exception as exc:  # never break closing the app over a backup
        log.exception("daily backup failed")
        return BackupResult(error=f"Backup failed ({exc})")


def prune_daily(backup_dir: Path, keep: int = KEEP_DAILY) -> None:
    # the date is in the name, so name order is date order
    daily = sorted(backup_dir.glob(f"{DAILY_PREFIX}*.db"), reverse=True)
    for stale in daily[keep:]:
        _remove_quietly(stale)


def _kind(name: str) -> str:
    if name.startswith(DAILY_PREFIX):
        return "Daily"
    if name.startswith(PRE_RESTORE_PREFIX):
        return "Before restore"
    if name.startswith("library-before-"):
        return "Before update"
    return "Other"


def list_backups(backup_dir: Path) -> list[BackupFile]:
    """Every library copy in `backup_dir`, newest first."""
    found: list[BackupFile] = []
    if not backup_dir.is_dir():
        return found
    for path in backup_dir.glob("library*.db"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file() or stat.st_size == 0:
            continue
        found.append(BackupFile(path, _kind(path.name), dt.datetime.fromtimestamp(stat.st_mtime), stat.st_size))
    found.sort(key=lambda b: b.when, reverse=True)
    return found


# -- restore -------------------------------------------------------------------


def schedule_restore(backup: Path, marker: Path) -> str:
    """Record `backup` to be restored at the next start. "" on success,
    else why not (the backup is checked now, not after the restart)."""
    problem = check_database(backup)
    if problem:
        return f"That backup can't be used - {problem}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(backup), encoding="utf-8")
    return ""


def pending_restore(marker: Path) -> Optional[Path]:
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def cancel_restore(marker: Path) -> None:
    _remove_quietly(marker)


def apply_pending_restore(db_path: Path, backup_dir: Path, marker: Path) -> Optional[RestoreOutcome]:
    """Call before the database is opened. None when no restore was asked
    for. The marker is always removed, so a failing restore can't repeat
    on every start."""
    backup = pending_restore(marker)
    if backup is None:
        return None
    _remove_quietly(marker)
    outcome = RestoreOutcome()
    wal_removed = False
    try:
        problem = check_database(backup) if backup.is_file() else "it's no longer there"
        if problem:
            outcome.error = f"Restore cancelled - {problem}. Your library wasn't changed."
            return outcome
        backup_dir.mkdir(parents=True, exist_ok=True)
        if db_path.is_file():
            saved = backup_dir / f"{PRE_RESTORE_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.db"
            _copy_database(db_path, saved)
            outcome.saved_current_as = saved
        staged = db_path.with_name(db_path.name + ".restoring")
        shutil.copyfile(backup, staged)
        for leftover in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
            if leftover.exists():
                leftover.unlink()
                wal_removed = True
        os.replace(staged, db_path)
        outcome.restored_from = backup
        log.info("restored library from %s (previous library saved as %s)", backup, outcome.saved_current_as)
    except Exception as exc:
        log.exception("restore failed")
        outcome.error = f"Restore failed ({exc}). Your library wasn't changed."
        if outcome.restored_from is None and outcome.saved_current_as is not None and (
            wal_removed or not db_path.is_file()
        ):
            # the old -wal is gone (or the file itself), so library.db alone
            # may be missing recent changes - put back the full copy saved
            # a moment ago, which has them
            try:
                shutil.copyfile(outcome.saved_current_as, db_path)
            except OSError:
                log.exception("couldn't put the saved library back")
                outcome.error = (f"Restore failed ({exc}). Your library as it was is saved "
                                 f"as {outcome.saved_current_as.name} in the backups folder.")
    return outcome


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
