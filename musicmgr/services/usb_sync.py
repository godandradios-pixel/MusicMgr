"""Two-way sync between this PC's media folders and a MusicMgr USB drive.

2026-09-23 - James: "I would like to eliminate GoodSync from my workflow...
a check for new music against my USB thumb drive", then "The sync of
music, videos, Movies should be two-way" and "I will never run the MusicMgr
directly from the USB". Plan: claude/2026-09-23-usb-sync-plan.md (step 1).

Why this exists: GoodSync copied the live library.db, -wal and -shm files
independently between PCs, which is what corrupted D:\\MusicMgr\\data\\
library.db on 2026-09-23. This module never touches a database file. It
syncs *folders of media* only, and every change is shown for review before
anything is copied or deleted.

Pieces (no Qt in here - the Settings page drives it through two QThreads):

- **The drive.** A USB drive is a MusicMgr drive once
  `MusicMgr\\sync\\drive.json` exists on it (`write_marker`). `find_drives`
  looks for that file on every local drive letter (Windows) or under
  /media and /run/media (Linux), so the drive letter never matters.
- **Pairs.** `SyncPair` rows (db/models.py): local folder <-> folder on the
  drive, relative to the drive root. `ensure_default_pairs` pairs each
  watched folder with the USB folder of the same name (D:\\Music <-> Music).
- **Baseline.** The file list both sides had after the last sync
  (`SyncBaseline`, plus a copy on the drive under
  `MusicMgr\\sync\\baselines\\<pc-id>\\`). It is what lets a two-way sync
  tell "new on the USB" apart from "deleted on this PC".
- **compare()** walks both sides (sizes and times only - no hashing unless
  a size matches but the time doesn't) and returns a `PairPlan`: copies
  each way, conflicts, deletions, case-only renames, plus rows to record.
- **apply()** runs the rows James left checked: renames, then copies, then
  deletions. Copies go to a temp name and are renamed into place, so a
  pulled drive never leaves half a file. Deletions are recoverable: local
  files go to the Recycle Bin / Trash, USB files move to
  `MusicMgr\\sync\\_deleted\\<date>\\`.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db.models import Setting, SyncBaseline, SyncPair, WatchedFolder

log = logging.getLogger(__name__)

#: everything MusicMgr keeps on the drive lives under here
SYNC_DIR_REL = "MusicMgr/sync"
MARKER_NAME = "drive.json"
MARKER_FORMAT = 1
TEMP_SUFFIX = ".mmsync-tmp"
DELETED_DIR = "_deleted"

#: settings-table keys
PREF_PC_ID = "sync_pc_id"
PREF_AUTO_CHECK = "usb_sync_auto_check"

#: FAT/exFAT keep times to 2 seconds, and FAT can be off by exactly an hour
#: across a DST change - both count as "the same time".
MTIME_TOLERANCE_NS = 2_000_000_000
DST_SHIFT_NS = 3600 * 1_000_000_000

COPY_CHUNK = 4 * 1024 * 1024
SIG_BYTES = 1024 * 1024

#: never synced, wherever they turn up
_SKIP_DIRS = {
    "_gsdata_", "$recycle.bin", "system volume information", "found.000", "found.001",
    # artwork\ and artists\ keep set-aside pictures here (artwork_names.py) - local only
    "_unused", "_superseded",
}
_SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]

#: Windows file systems ignore case; compare names the same way there
CASE_INSENSITIVE = os.name == "nt"


def _fold(rel: str) -> str:
    return rel.casefold() if CASE_INSENSITIVE else rel


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------
# the drive
# --------------------------------------------------------------------------


@dataclass
class Drive:
    root: Path
    drive_id: str
    label: str

    @property
    def sync_dir(self) -> Path:
        return self.root / SYNC_DIR_REL

    def describe(self) -> str:
        return f"{self.label} ({self.root})"


def marker_path(root: Path) -> Path:
    return Path(root) / SYNC_DIR_REL / MARKER_NAME


def load_marker(root: Path) -> Optional[Drive]:
    """The Drive at `root`, or None if it isn't a MusicMgr drive (or the
    marker can't be read)."""
    path = marker_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Drive(Path(root), str(data["drive_id"]), str(data.get("label") or "MusicMgr USB"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_marker(root: Path, label: str = "MusicMgr USB") -> Drive:
    """Make `root` a MusicMgr drive. An existing marker keeps its drive_id
    (so its pairs and baselines still match) and just gets the new label."""
    existing = load_marker(root)
    drive_id = existing.drive_id if existing else uuid.uuid4().hex
    path = marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "drive_id": drive_id,
        "label": label,
        "created": _now().isoformat(timespec="seconds"),
        "format": MARKER_FORMAT,
    }
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return Drive(Path(root), drive_id, label)


def candidate_roots() -> list[Path]:
    """Where a MusicMgr drive could be mounted. Windows: every local fixed
    or removable drive letter (network and CD drives are skipped - checking
    a disconnected network drive can stall for seconds). Linux: every
    folder under /media/$USER, /run/media/$USER and /media."""
    if sys.platform.startswith("win"):  # pragma: no cover - Windows only
        import ctypes

        roots = []
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if kernel32.GetDriveTypeW(root) in (2, 3):  # REMOVABLE, FIXED
                roots.append(Path(root))
        return roots
    user = os.environ.get("USER") or ""
    roots: list[Path] = []
    for base in (Path("/media") / user, Path("/run/media") / user, Path("/media")):
        try:
            roots.extend(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
    return roots


def find_drives(roots: Optional[Iterable[Path]] = None) -> list[Drive]:
    """Every connected MusicMgr drive (normally zero or one)."""
    found = []
    seen = set()
    for root in roots if roots is not None else candidate_roots():
        drive = load_marker(root)
        if drive is not None and drive.drive_id not in seen:
            seen.add(drive.drive_id)
            found.append(drive)
    return found


def get_pc_id(db: Session) -> str:
    """This library's own id, created on first use. Baseline copies on the
    drive are filed under it, so each PC keeps its own."""
    row = db.get(Setting, PREF_PC_ID)
    if row is None or not row.value:
        value = uuid.uuid4().hex
        if row is None:
            db.add(Setting(key=PREF_PC_ID, value=value))
        else:
            row.value = value
        db.flush()
        return value
    return row.value


# --------------------------------------------------------------------------
# pairs
# --------------------------------------------------------------------------


def _norm_local(path: str | Path) -> str:
    return str(Path(path))


def _norm_rel(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


REMOVED_PREFIX = "removed:"


def is_removed(pair: SyncPair) -> bool:
    return (pair.kind or "").startswith(REMOVED_PREFIX)


def _clear_baseline(db: Session, pair_id: int) -> None:
    db.execute(delete(SyncBaseline).where(SyncBaseline.pair_id == pair_id))


def ensure_default_pairs(db: Session, drive: Drive) -> list[SyncPair]:
    """Pair every watched folder that has a same-named folder on the drive
    and isn't paired yet (D:\\Music <-> <drive>\\Music), plus the artwork
    folders. Returns all of this drive's pairs, oldest first, including
    removed ones (filter with `is_removed`). Never re-creates a pair James
    removed: `remove_pair` keeps a "removed:" marker row for that."""
    pairs = list(db.scalars(select(SyncPair).where(SyncPair.drive_id == drive.drive_id)))
    existing = {_fold(_norm_local(p.local_path)): p for p in pairs}
    for folder in db.scalars(select(WatchedFolder).order_by(WatchedFolder.id)):
        local = _norm_local(folder.path)
        if _fold(local) in existing:
            continue
        name = Path(local).name
        if name and (drive.root / name).is_dir():
            pair = SyncPair(drive_id=drive.drive_id, local_path=local, usb_rel_path=name, kind="media")
            db.add(pair)
            existing[_fold(local)] = pair
    # step 2 (2026-09-24): the name-based artwork folders travel too - but
    # only once this data folder's pictures have been renamed, or old
    # id-named files would be pushed to the other PCs
    from . import artwork_names

    if artwork_names.naming_done(db):
        for folder, rel in artwork_names.image_folders_for_sync():
            local = _norm_local(folder)
            # an artwork pair always syncs *this* library's own data folder:
            # found by its folder name (artwork / artists), and repointed if
            # the data folder has moved since (2026-09-24, James saw
            # C:\\Projects\\...\\artwork where he expected D:\\MusicMgr\\...)
            match = next(
                (p for p in pairs
                 if p.kind in ("artwork", REMOVED_PREFIX + "artwork")
                 and Path(_norm_local(p.local_path)).name.casefold() == Path(local).name.casefold()),
                None,
            )
            if match is not None:
                if _fold(_norm_local(match.local_path)) != _fold(local):
                    match.local_path = local
                    _clear_baseline(db, match.id)
                continue
            if _fold(local) in existing:
                continue
            pair = SyncPair(drive_id=drive.drive_id, local_path=local, usb_rel_path=rel, kind="artwork")
            db.add(pair)
            pairs.append(pair)
            existing[_fold(local)] = pair
    db.flush()
    return list(
        db.scalars(select(SyncPair).where(SyncPair.drive_id == drive.drive_id).order_by(SyncPair.id))
    )


def add_pair(db: Session, drive: Drive, local_path: str | Path, usb_rel_path: str) -> SyncPair:
    local = _norm_local(local_path)
    rel = _norm_rel(usb_rel_path)
    for pair in db.scalars(select(SyncPair).where(SyncPair.drive_id == drive.drive_id)):
        if _fold(_norm_local(pair.local_path)) == _fold(local):
            if pair.usb_rel_path != rel:
                _clear_baseline(db, pair.id)
            pair.usb_rel_path = rel
            pair.enabled = True
            if is_removed(pair):
                pair.kind = pair.kind[len(REMOVED_PREFIX):] or "media"
            db.flush()
            return pair
    pair = SyncPair(drive_id=drive.drive_id, local_path=local, usb_rel_path=rel, kind="media")
    db.add(pair)
    db.flush()
    return pair


def set_pair_enabled(db: Session, pair_id: int, enabled: bool) -> None:
    pair = db.get(SyncPair, pair_id)
    if pair is not None and not is_removed(pair):
        pair.enabled = enabled
        db.flush()


def update_pair(db: Session, pair_id: int, *, local_path: Optional[str | Path] = None,
                usb_rel_path: Optional[str] = None) -> Optional[SyncPair]:
    """Point a pair at a different folder on either side. Its sync list is
    cleared, so the next check treats it as a first sync (files on one
    side only are copied across; nothing counts as deleted)."""
    pair = db.get(SyncPair, pair_id)
    if pair is None:
        return None
    changed = False
    if local_path is not None and _fold(_norm_local(local_path)) != _fold(_norm_local(pair.local_path)):
        if pair.kind == "artwork":
            raise ValueError("an artwork pair always uses this library's own data folder")
        pair.local_path = _norm_local(local_path)
        changed = True
    if usb_rel_path is not None and _norm_rel(usb_rel_path) != pair.usb_rel_path:
        pair.usb_rel_path = _norm_rel(usb_rel_path)
        changed = True
    if changed:
        _clear_baseline(db, pair.id)
    db.flush()
    return pair


def remove_pair(db: Session, pair_id: int) -> None:
    """Stop syncing a pair and forget its sync list. Nothing on disk or on
    the USB is touched. A pair MusicMgr would create by itself (a watched
    folder, an artwork folder) is kept as a hidden "removed" marker so it
    isn't re-created on the next check; "Add pair…" with the same folder
    brings it back."""
    pair = db.get(SyncPair, pair_id)
    if pair is None:
        return
    _clear_baseline(db, pair.id)
    auto = pair.kind == "artwork" or db.scalar(
        select(WatchedFolder.id).where(WatchedFolder.path == pair.local_path)
    ) is not None
    if auto:
        pair.kind = REMOVED_PREFIX + (pair.kind or "media")
        pair.enabled = False
    else:
        db.delete(pair)
    db.flush()


def pair_label(pair: SyncPair) -> str:
    return f"{pair.local_path}  ↔  USB\\{pair.usb_rel_path.replace('/', chr(92))}"


# --------------------------------------------------------------------------
# walking and comparing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime_ns: int


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name.casefold() in _SKIP_DIRS


def _skip_file(name: str) -> bool:
    low = name.casefold()
    return name.startswith(".") or low in _SKIP_FILES or low.endswith(TEMP_SUFFIX)


def walk(root: Path, should_stop: Optional[StopFn] = None, progress: Optional[ProgressFn] = None,
         exclude: Optional[Path] = None, temp_out: Optional[list] = None) -> dict[str, FileStat]:
    """Every file under `root`, keyed by forward-slash relative path. Only
    stat() calls - fast even for 100k files. `exclude` is a folder to skip
    entirely (the drive's own MusicMgr folder, when a pair is the drive
    root). Leftover `*.mmsync-tmp` files (a crash or a pulled drive) are
    never listed; they're collected into `temp_out` for apply() to remove."""
    root = Path(root)
    out: dict[str, FileStat] = {}
    stack = [(root, "")]
    exclude_s = os.path.normcase(str(exclude)) if exclude else None
    count = 0
    while stack:
        if should_stop and should_stop():
            break
        folder, prefix = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError as exc:
            log.warning("can't read %s: %s", folder, exc)
            continue
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if _skip_dir(name):
                        continue
                    if exclude_s and os.path.normcase(entry.path) == exclude_s:
                        continue
                    stack.append((Path(entry.path), f"{prefix}{name}/"))
                elif entry.is_file(follow_symlinks=False):
                    if name.casefold().endswith(TEMP_SUFFIX):
                        if temp_out is not None:
                            temp_out.append(Path(entry.path))
                        continue
                    if _skip_file(name):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    out[prefix + name] = FileStat(st.st_size, st.st_mtime_ns)
                    count += 1
                    if progress and count % 2000 == 0:
                        progress(f"{root}: {count:,} files")
            except OSError as exc:
                log.warning("can't stat %s: %s", entry.path, exc)
    return out


def same_time(a: int, b: int) -> bool:
    d = abs(a - b)
    return d <= MTIME_TOLERANCE_NS or abs(d - DST_SHIFT_NS) <= MTIME_TOLERANCE_NS


def same_stat(a: FileStat, b: FileStat) -> bool:
    return a.size == b.size and same_time(a.mtime_ns, b.mtime_ns)


def content_sig(path: Path) -> str:
    """SHA-256 of the first and last 1 MB plus the size - enough to tell
    "re-dated but identical" from "really different" without reading a
    whole 40 MB FLAC."""
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(SIG_BYTES))
        if size > SIG_BYTES:
            fh.seek(max(SIG_BYTES, size - SIG_BYTES))
            h.update(fh.read(SIG_BYTES))
    return h.hexdigest()


# item kinds
COPY = "copy"            # one side new or changed -> copy it across
CONFLICT = "conflict"    # both sides changed (or differ with no baseline)
DELETE = "delete"        # gone from one side since the last sync
RENAME = "rename"        # same file, name differs only by case (Windows)

TO_USB = "to_usb"
TO_LOCAL = "to_local"


@dataclass
class SyncItem:
    kind: str
    rel: str                      # name on the side that "wins" (display name)
    local: Optional[FileStat] = None
    usb: Optional[FileStat] = None
    #: COPY / CONFLICT: which way the copy goes.
    #: DELETE: TO_USB/TO_LOCAL means "restore" in that direction when
    #:         `restore` is True; otherwise the file is deleted from the
    #:         side that still has it.
    #: RENAME: which side gets renamed (TO_USB -> rename the USB copy to
    #:         `rel`, TO_LOCAL -> rename the local copy).
    direction: str = TO_USB
    selected: bool = True
    #: DELETE only - False: delete from the side that still has it.
    #: True: copy it back to the side that lost it.
    restore: bool = False
    #: DELETE only: the remaining copy changed after the last sync
    changed_since: bool = False
    local_rel: Optional[str] = None
    usb_rel: Optional[str] = None
    #: RENAME: the old name on the side being renamed.
    #: DELETE: the name as the baseline spelled it
    old_rel: Optional[str] = None

    @property
    def remaining_side(self) -> str:
        """DELETE only: the side the file is still on ("local" or "usb")."""
        return "local" if self.local is not None else "usb"

    @property
    def size(self) -> int:
        stat = self.local if self.direction == TO_USB else self.usb
        stat = stat or self.local or self.usb
        return stat.size if stat else 0


@dataclass
class PairPlan:
    pair_id: int
    label: str
    local_root: Path
    usb_root: Path
    items: list[SyncItem] = field(default_factory=list)
    #: rel -> stat to write into the baseline without copying anything
    record: dict[str, FileStat] = field(default_factory=dict)
    #: baseline rows to drop (gone from both sides)
    forget: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    baseline_source: str = "none"   # "db" | "usb" | "none"
    #: leftover temp files found while walking - removed by apply()
    temp_files: list[Path] = field(default_factory=list)
    local_count: int = 0
    usb_count: int = 0

    def of(self, kind: str, direction: Optional[str] = None) -> list[SyncItem]:
        return [i for i in self.items if i.kind == kind and (direction is None or i.direction == direction)]

    @property
    def has_changes(self) -> bool:
        return bool(self.items)


@dataclass
class ComparePlan:
    drive: Drive
    pairs: list[PairPlan] = field(default_factory=list)
    cancelled: bool = False

    def all_items(self) -> list[SyncItem]:
        return [i for p in self.pairs for i in p.items]

    def counts(self) -> dict[str, int]:
        items = self.all_items()
        return {
            "to_usb": sum(1 for i in items if i.kind == COPY and i.direction == TO_USB),
            "to_local": sum(1 for i in items if i.kind == COPY and i.direction == TO_LOCAL),
            "conflicts": sum(1 for i in items if i.kind == CONFLICT),
            "deletions": sum(1 for i in items if i.kind == DELETE),
            "renames": sum(1 for i in items if i.kind == RENAME),
        }

    def summary(self) -> str:
        c = self.counts()
        parts = []
        if c["to_local"]:
            parts.append(f"{c['to_local']:,} new from USB")
        if c["to_usb"]:
            parts.append(f"{c['to_usb']:,} new to USB")
        if c["conflicts"]:
            parts.append(f"{c['conflicts']:,} conflicts")
        if c["deletions"]:
            parts.append(f"{c['deletions']:,} deletions")
        if c["renames"]:
            parts.append(f"{c['renames']:,} renames")
        return " · ".join(parts) if parts else "Everything is in sync"


def _index(files: dict[str, FileStat]) -> dict[str, tuple[str, FileStat]]:
    return {_fold(rel): (rel, st) for rel, st in files.items()}


def _sig_equal(local_root: Path, local_rel: str, usb_root: Path, usb_rel: str) -> bool:
    try:
        return content_sig(local_root / local_rel) == content_sig(usb_root / usb_rel)
    except OSError:
        return False


def compare(
    local_root: Path,
    usb_root: Path,
    local_files: dict[str, FileStat],
    usb_files: dict[str, FileStat],
    baseline: dict[str, FileStat],
    *,
    pair_id: int = 0,
    label: str = "",
) -> PairPlan:
    """The three-way comparison - see the plan doc's table (§1.3)."""
    plan = PairPlan(pair_id, label, Path(local_root), Path(usb_root))
    plan.local_count = len(local_files)
    plan.usb_count = len(usb_files)
    L, U, B = _index(local_files), _index(usb_files), _index(baseline)

    for key in sorted(set(L) | set(U) | set(B)):
        l, u, b = L.get(key), U.get(key), B.get(key)
        lst = l[1] if l else None
        ust = u[1] if u else None
        bst = b[1] if b else None
        lrel = l[0] if l else None
        urel = u[0] if u else None
        brel = b[0] if b else None

        if l and u:
            equal = same_stat(lst, ust) or (
                lst.size == ust.size and _sig_equal(plan.local_root, lrel, plan.usb_root, urel)
            )
            # case-only rename (Windows): same file, names differ in case
            if lrel != urel and equal:
                if brel == urel:        # the local copy was renamed
                    plan.items.append(SyncItem(RENAME, lrel, lst, ust, TO_USB, True,
                                               local_rel=lrel, usb_rel=urel, old_rel=urel))
                elif brel == lrel:      # the USB copy was renamed
                    plan.items.append(SyncItem(RENAME, urel, lst, ust, TO_LOCAL, True,
                                               local_rel=lrel, usb_rel=urel, old_rel=lrel))
                else:                   # no baseline: follow this PC's name
                    plan.items.append(SyncItem(RENAME, lrel, lst, ust, TO_USB, True,
                                               local_rel=lrel, usb_rel=urel, old_rel=urel))
                continue
            if b is None:
                if equal:
                    plan.record[lrel] = lst
                else:
                    newer = TO_USB if lst.mtime_ns >= ust.mtime_ns else TO_LOCAL
                    plan.items.append(SyncItem(CONFLICT, lrel, lst, ust, newer, True,
                                               local_rel=lrel, usb_rel=urel))
                continue
            l_changed = not same_stat(lst, bst)
            u_changed = not same_stat(ust, bst)
            if not l_changed and not u_changed:
                if brel != lrel:        # baseline spelled differently; refresh it
                    plan.forget.append(brel)
                    plan.record[lrel] = lst
                continue
            if equal:
                if brel != lrel:
                    plan.forget.append(brel)
                plan.record[lrel] = lst
                continue
            if l_changed and not u_changed:
                plan.items.append(SyncItem(COPY, lrel, lst, ust, TO_USB, True, local_rel=lrel, usb_rel=urel))
            elif u_changed and not l_changed:
                plan.items.append(SyncItem(COPY, urel, lst, ust, TO_LOCAL, True, local_rel=lrel, usb_rel=urel))
            else:
                newer = TO_USB if lst.mtime_ns >= ust.mtime_ns else TO_LOCAL
                plan.items.append(SyncItem(CONFLICT, lrel, lst, ust, newer, True, local_rel=lrel, usb_rel=urel))
        elif l:
            if b is None:
                plan.items.append(SyncItem(COPY, lrel, lst, None, TO_USB, True, local_rel=lrel))
            else:
                changed = not same_stat(lst, bst)
                plan.items.append(SyncItem(DELETE, lrel, lst, None, TO_USB, False,
                                           restore=changed, changed_since=changed, local_rel=lrel,
                                           old_rel=brel))
        elif u:
            if b is None:
                plan.items.append(SyncItem(COPY, urel, None, ust, TO_LOCAL, True, usb_rel=urel))
            else:
                changed = not same_stat(ust, bst)
                plan.items.append(SyncItem(DELETE, urel, None, ust, TO_LOCAL, False,
                                           restore=changed, changed_since=changed, usb_rel=urel,
                                           old_rel=brel))
        else:
            plan.forget.append(brel)
    return plan


# --------------------------------------------------------------------------
# baselines
# --------------------------------------------------------------------------


def _usb_baseline_path(drive: Drive, pc_id: str, pair: SyncPair) -> Path:
    safe = hashlib.sha1(_fold(_norm_local(pair.local_path)).encode("utf-8")).hexdigest()[:12]
    name = "".join(c if c.isalnum() else "_" for c in pair.usb_rel_path)[:40]
    return drive.sync_dir / "baselines" / pc_id / f"{name}-{safe}.json.gz"


def load_baseline(db: Session, pair: SyncPair) -> dict[str, FileStat]:
    return {
        row.rel_path: FileStat(row.size, row.mtime_ns)
        for row in db.scalars(select(SyncBaseline).where(SyncBaseline.pair_id == pair.id))
    }


def load_usb_baseline(drive: Drive, pc_id: str, pair: SyncPair) -> dict[str, FileStat]:
    path = _usb_baseline_path(drive, pc_id, pair)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        return {rel: FileStat(int(v[0]), int(v[1])) for rel, v in data.get("files", {}).items()}
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        return {}


def save_usb_baseline(drive: Drive, pc_id: str, pair: SyncPair, files: dict[str, FileStat]) -> None:
    path = _usb_baseline_path(drive, pc_id, pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    data = {
        "format": 1,
        "local_path": pair.local_path,
        "usb_rel_path": pair.usb_rel_path,
        "saved": _now().isoformat(timespec="seconds"),
        "files": {rel: [st.size, st.mtime_ns] for rel, st in files.items()},
    }
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class _BaselineWriter:
    """Batches baseline changes into the session. A crash between a copy
    and the next flush is harmless: both sides then hold the same file, and
    the next compare just records it."""

    def __init__(self, db: Session, pair_id: int, current: dict[str, FileStat]) -> None:
        self.db = db
        self.pair_id = pair_id
        self.current = dict(current)
        self._pending = 0

    def set(self, rel: str, stat: FileStat) -> None:
        self.current[rel] = stat
        self.db.merge(SyncBaseline(pair_id=self.pair_id, rel_path=rel, size=stat.size, mtime_ns=stat.mtime_ns))
        self._tick()

    def drop(self, rel: Optional[str]) -> None:
        if rel is None:
            return
        self.current.pop(rel, None)
        self.db.execute(
            delete(SyncBaseline).where(SyncBaseline.pair_id == self.pair_id, SyncBaseline.rel_path == rel)
        )
        self._tick()

    def bulk_set(self, rows: dict[str, FileStat]) -> None:
        """Fast path for a big first sync (100k 'record only' rows)."""
        if not rows:
            return
        existing = {
            r for r in self.db.scalars(
                select(SyncBaseline.rel_path).where(SyncBaseline.pair_id == self.pair_id)
            )
        }
        new_rows = [
            {"pair_id": self.pair_id, "rel_path": rel, "size": st.size, "mtime_ns": st.mtime_ns}
            for rel, st in rows.items() if rel not in existing
        ]
        if new_rows:
            self.db.execute(SyncBaseline.__table__.insert(), new_rows)
        for rel, st in rows.items():
            if rel in existing:
                self.set(rel, st)
            else:
                self.current[rel] = st
        self.db.flush()

    def _tick(self) -> None:
        self._pending += 1
        if self._pending >= 200:
            self.db.flush()
            self.db.commit()
            self._pending = 0


def build_plan(
    db: Session,
    drive: Drive,
    pairs: Optional[list[SyncPair]] = None,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[StopFn] = None,
) -> ComparePlan:
    """Walk and compare every enabled pair of `drive`. Read-only apart from
    creating this PC's id and the default pairs."""
    pc_id = get_pc_id(db)
    if pairs is None:
        pairs = [p for p in ensure_default_pairs(db, drive) if p.enabled and not is_removed(p)]
    result = ComparePlan(drive)
    for pair in pairs:
        if should_stop and should_stop():
            result.cancelled = True
            break
        local_root = Path(pair.local_path)
        usb_root = drive.root / pair.usb_rel_path
        label = pair_label(pair)
        if not local_root.is_dir():
            plan = PairPlan(pair.id, label, local_root, usb_root)
            plan.errors.append(f"{local_root} isn't available on this PC — skipped")
            result.pairs.append(plan)
            continue
        if progress:
            progress(f"Reading {local_root}…")
        temps: list[Path] = []
        local_files = walk(local_root, should_stop, progress, temp_out=temps)
        if progress:
            progress(f"Reading {usb_root}…")
        usb_files = (
            walk(usb_root, should_stop, progress, exclude=drive.root / "MusicMgr", temp_out=temps)
            if usb_root.is_dir() else {}
        )
        if should_stop and should_stop():
            result.cancelled = True
            break
        baseline = load_baseline(db, pair)
        source = "db" if baseline else "none"
        if not baseline:
            baseline = load_usb_baseline(drive, pc_id, pair)
            source = "usb" if baseline else "none"
        if progress:
            progress(f"Comparing {label}…")
        plan = compare(local_root, usb_root, local_files, usb_files, baseline, pair_id=pair.id, label=label)
        plan.baseline_source = source
        plan.temp_files = temps
        result.pairs.append(plan)
    return result


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


@dataclass
class SyncResult:
    copied_to_usb: int = 0
    copied_to_local: int = 0
    renamed: int = 0
    deleted_local: int = 0
    deleted_usb: int = 0
    restored: int = 0
    recorded: int = 0
    bytes_copied: int = 0
    #: local files that are new or changed on this PC - for a targeted rescan
    new_local_paths: list[Path] = field(default_factory=list)
    #: local files that went away (deleted or renamed from)
    removed_local_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    #: set by update_library: pictures came or went
    artwork_changed: bool = False
    #: set after the library-state step (services/library_state.MergeNotes)
    library_notes: Optional[object] = None

    def summary(self) -> str:
        parts = []
        if self.copied_to_local:
            parts.append(f"{self.copied_to_local:,} copied from USB")
        if self.copied_to_usb:
            parts.append(f"{self.copied_to_usb:,} copied to USB")
        if self.restored:
            parts.append(f"{self.restored:,} restored")
        if self.renamed:
            parts.append(f"{self.renamed:,} renamed")
        if self.deleted_local:
            parts.append(f"{self.deleted_local:,} moved to Recycle Bin")
        if self.deleted_usb:
            parts.append(f"{self.deleted_usb:,} moved to USB _deleted")
        if self.errors:
            parts.append(f"{len(self.errors):,} failed")
        text = " · ".join(parts) if parts else "Nothing to copy"
        return f"Cancelled — {text}" if self.cancelled else text


def bytes_needed(plan: ComparePlan) -> dict[str, int]:
    """Bytes the selected copies would add to each side."""
    need = {TO_USB: 0, TO_LOCAL: 0}
    for item in plan.all_items():
        if not item.selected:
            continue
        if item.kind in (COPY, CONFLICT):
            src = item.local if item.direction == TO_USB else item.usb
            dst = item.usb if item.direction == TO_USB else item.local
            need[item.direction] += max(0, (src.size if src else 0) - (dst.size if dst else 0))
        elif item.kind == DELETE and item.restore:
            src = item.local or item.usb
            need[TO_USB if item.local else TO_LOCAL] += src.size if src else 0
    return need


def free_space(path: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def copy_file(
    src: Path,
    dst: Path,
    should_stop: Optional[StopFn] = None,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> bool:
    """Copy via `<dst>.mmsync-tmp`, then rename into place and give it the
    source's modified time. Returns False (and leaves no temp file) if
    stopped partway."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + TEMP_SUFFIX)
    st = src.stat()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                if should_stop and should_stop():
                    raise _Stopped()
                chunk = fin.read(COPY_CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        _replace_with_retry(tmp, dst)
        # utime again on the final name - some file systems reset it on rename
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        return True
    except _Stopped:
        _remove_quietly(tmp)
        return False
    except BaseException:
        _remove_quietly(tmp)
        raise


class _Stopped(Exception):
    pass


def _replace_with_retry(src: Path, dst: Path, attempts: int = 3) -> None:
    """os.replace, retried briefly - Windows antivirus sometimes holds a
    just-written file for a moment."""
    import time

    for n in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if n == attempts - 1:
                raise
            time.sleep(0.5 * (n + 1))


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def cleanup_temp_files(root: Path) -> int:
    """Remove `*.mmsync-tmp` left behind by a crash or a pulled drive."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        for name in filenames:
            if name.endswith(TEMP_SUFFIX):
                _remove_quietly(Path(dirpath) / name)
                removed += 1
    return removed


def trash_local(path: Path) -> None:
    """Recycle Bin / Trash. Uses send2trash; raises if that isn't possible
    (the file is then left alone, never hard-deleted)."""
    from send2trash import send2trash

    send2trash(str(path))


def usb_deleted_dir(drive: Drive) -> Path:
    return drive.sync_dir / DELETED_DIR


def move_to_usb_deleted(drive: Drive, pair_rel: str, rel: str, src: Path) -> Path:
    stamp = dt.date.today().isoformat()
    dest = usb_deleted_dir(drive) / stamp / pair_rel / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem} ({dt.datetime.now():%H%M%S}){dest.suffix}")
    shutil.move(str(src), str(dest))
    return dest


def folder_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _stat(path: Path) -> FileStat:
    st = path.stat()
    return FileStat(st.st_size, st.st_mtime_ns)


def apply(
    db: Session,
    plan: ComparePlan,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[StopFn] = None,
    *,
    trash: Callable[[Path], None] = trash_local,
) -> SyncResult:
    """Carry out the selected items of `plan`. `progress(done_bytes,
    total_bytes, name)`. Renames first, then copies, then deletions, so a
    cancelled run never leaves a file deleted without its replacement."""
    result = SyncResult()
    drive = plan.drive
    pc_id = get_pc_id(db)
    total = sum(
        (i.size for i in plan.all_items()
         if i.selected and (i.kind in (COPY, CONFLICT) or (i.kind == DELETE and i.restore))),
        0,
    )
    done = [0]

    def tick(n: int, name: str = "") -> None:
        done[0] += n
        if progress:
            progress(done[0], total, name)

    stopped = lambda: bool(should_stop and should_stop())  # noqa: E731

    for pp in plan.pairs:
        pair = db.get(SyncPair, pp.pair_id)
        if pair is None or pp.errors and not pp.items and not pp.record:
            continue
        for tmp in pp.temp_files:
            _remove_quietly(tmp)
        base = _BaselineWriter(db, pp.pair_id, load_baseline(db, pair))
        for rel in pp.forget:
            base.drop(rel)
        base.bulk_set(pp.record)
        result.recorded += len(pp.record)

        items = [i for i in pp.items if i.selected]
        order = {RENAME: 0, COPY: 1, CONFLICT: 1, DELETE: 2}
        items.sort(key=lambda i: order[i.kind])
        for item in items:
            if stopped():
                result.cancelled = True
                break
            try:
                _apply_item(pp, item, drive, base, result, tick, should_stop, trash)
            except _Stopped:
                result.cancelled = True
                break
            except Exception as exc:
                log.exception("usb sync failed on %s", item.rel)
                result.errors.append(f"{item.rel}: {exc}")
        db.flush()
        if not result.cancelled or base.current:
            try:
                save_usb_baseline(drive, pc_id, pair, base.current)
            except OSError as exc:
                result.errors.append(f"couldn't save the USB copy of the sync list: {exc}")
        if not result.cancelled:
            pair.last_synced_at = _now()
        db.commit()
        if result.cancelled:
            break
    return result


def _apply_item(pp: PairPlan, item: SyncItem, drive: Drive, base: _BaselineWriter,
                result: SyncResult, tick, should_stop, trash) -> None:
    L, U = pp.local_root, pp.usb_root

    if item.kind == RENAME:
        if item.direction == TO_USB:
            _case_rename(U / item.usb_rel, U / item.rel)
        else:
            _case_rename(L / item.local_rel, L / item.rel)
            result.removed_local_paths.append(L / item.local_rel)
            result.new_local_paths.append(L / item.rel)
        base.drop(item.old_rel)
        base.drop(item.local_rel if item.direction == TO_LOCAL else item.usb_rel)
        base.set(item.rel, _stat(L / item.rel))
        result.renamed += 1
        return

    if item.kind in (COPY, CONFLICT):
        if item.direction == TO_USB:
            src = L / (item.local_rel or item.rel)
            dst = U / (item.usb_rel or item.local_rel or item.rel)
            if item.kind == CONFLICT and dst.exists():
                move_to_usb_deleted(drive, _norm_rel(str(pp.usb_root.relative_to(drive.root))),
                                    item.usb_rel or item.rel, dst)
        else:
            src = U / (item.usb_rel or item.rel)
            dst = L / (item.local_rel or item.usb_rel or item.rel)
            if item.kind == CONFLICT and dst.exists():
                trash(dst)
        if not copy_file(src, dst, should_stop, lambda n: tick(n, item.rel)):
            raise _Stopped()
        rel = str(dst.relative_to(L if item.direction == TO_LOCAL else U)).replace("\\", "/")
        base.set(rel, _stat(dst))
        result.bytes_copied += src.stat().st_size
        if item.direction == TO_USB:
            result.copied_to_usb += 1
        else:
            result.copied_to_local += 1
            result.new_local_paths.append(dst)
        return

    if item.kind == DELETE:
        if item.restore:
            if item.remaining_side == "local":
                src, dst = L / item.local_rel, U / item.local_rel
            else:
                src, dst = U / item.usb_rel, L / item.usb_rel
                result.new_local_paths.append(dst)
            if not copy_file(src, dst, should_stop, lambda n: tick(n, item.rel)):
                raise _Stopped()
            base.drop(item.old_rel)
            base.set(item.rel, _stat(dst))
            result.restored += 1
            return
        if item.remaining_side == "local":
            path = L / item.local_rel
            trash(path)
            result.deleted_local += 1
            result.removed_local_paths.append(path)
        else:
            path = U / item.usb_rel
            move_to_usb_deleted(drive, _norm_rel(str(pp.usb_root.relative_to(drive.root))), item.usb_rel, path)
            result.deleted_usb += 1
        base.drop(item.old_rel or item.rel)
        return


def _case_rename(src: Path, dst: Path) -> None:
    """Rename that also works when only the case changes (Windows treats
    the two names as the same file, so go through a temp name)."""
    if str(src) == str(dst):
        return
    tmp = src.with_name(src.name + TEMP_SUFFIX)
    os.replace(src, tmp)
    os.replace(tmp, dst)


# --------------------------------------------------------------------------
# after a sync: bring the library up to date
# --------------------------------------------------------------------------


def update_library(
    db: Session,
    result: SyncResult,
    should_stop: Optional[StopFn] = None,
) -> str:
    """Import what arrived and mark what left - only those files, never a
    full rescan. Files outside every watched folder are left alone (a pair
    like MusicMP3 that MusicMgr doesn't manage)."""
    from ..db.models import MediaFile, Video
    from . import scanner, video_scanner

    watched = [
        Path(f.path) for f in db.scalars(select(WatchedFolder).where(WatchedFolder.enabled.is_(True)))
    ]

    def in_watched(p: Path) -> bool:
        ps = os.path.normcase(str(p))
        for w in watched:
            ws = os.path.normcase(str(w)).rstrip("\\/")
            if ps.startswith(ws + os.sep) or ps.startswith(ws + "/"):
                return True
        return False

    new_paths = [p for p in dict.fromkeys(result.new_local_paths) if in_watched(p)]
    audio = scanner.import_paths(db, new_paths, should_stop=should_stop)
    video = video_scanner.import_video_paths(db, new_paths, should_stop=should_stop)

    gone = {str(p) for p in result.removed_local_paths if not p.exists()}
    missing = 0
    if gone:
        for mf in db.scalars(select(MediaFile).where(MediaFile.path.in_(gone))):
            mf.is_missing = True
            missing += 1
        for v in db.scalars(select(Video).where(Video.path.in_(gone))):
            v.is_missing = True
            missing += 1
    db.flush()

    # pictures that arrived (or left) by sync: point releases/artists at
    # their name-based files (services/artwork_names.py)
    from .. import config
    from . import artwork_names

    art_dirs = [os.path.normcase(str(d)) for d in (config.ART_DIR, config.ARTIST_IMG_DIR)]
    touched_art = any(
        os.path.normcase(str(p.parent)) in art_dirs
        for p in list(result.new_local_paths) + list(result.removed_local_paths)
    )
    relinked = artwork_names.relink(db) if touched_art else None
    result.artwork_changed = touched_art

    def n(count: int, word: str) -> str:
        return f"{count:,} {word}{'' if count == 1 else 's'}"

    parts = []
    if audio.added or audio.updated:
        parts.append(f"{n(audio.added, 'track')} added, {audio.updated:,} updated")
    if video.added or video.updated:
        parts.append(f"{n(video.added, 'video')} added, {video.updated:,} updated")
    if missing:
        parts.append(f"{missing:,} marked missing")
    if relinked is not None and (relinked.covers or relinked.artist_images):
        parts.append(relinked.summary())
    errors = len(audio.errors) + len(video.errors)
    if errors:
        parts.append(f"{errors:,} couldn't be read")
    return " · ".join(parts) if parts else "Library already up to date"
