#!/usr/bin/env python3
"""Use a Windows MusicMgr library.db on Linux (or any other machine).

2026-09-23 - James, on the Linux Mint PC: "I wanted to use the library.db
from the USB MusicMgr\\data directory." That database is the whole Windows
library - tracks, ratings, play counts, playlists, charts, Jukebox cards,
artist bios, radio shows - but every file path in it is a Windows one
("D:\\Music\\Boston\\...\\01 More Than a Feeling.mp3"). Opened as-is on
Linux, every track would show as missing.

This tool installs a copy of it as the Linux library and repoints the paths:

* files and folders under a Windows media folder (songs, videos, watched /
  library / video folders, radio shows and episodes, the Radio page's folder
  and other path settings): a Windows folder prefix -> a Linux folder, e.g.
  D:\\Music -> ~/Music, with \\ turned into /. If the exact result isn't on
  disk, a case-insensitive lookup fixes names whose capitalisation differs
  (Windows didn't care, Linux does).
* pictures in the data folder (album covers, playlist and playlist-folder
  tiles, artist pictures, label images, radio station logos, video
  thumbnails): copied into the new data folder and repointed to the same
  place under it (artwork\\Folders\\x.jpg stays in a Folders subfolder). They
  are taken from the source data folder and, for library copies made by
  "Save a copy of the library on the drive", which leave covers and artist
  pictures out, from the drive's MusicMgr\\artwork and \\artists (--images).
* this PC's USB sync identity is cleared (2026-10-26): the copy carries the
  sync id, folder pairs and sync records of the PC that saved it, and a
  second PC using them would sync as that PC. MusicMgr makes a new id and
  pairs the folders again on the first "Check USB now".
  --keep-sync-identity skips this (only for restoring a PC's own library).

Anything it can't map is left exactly as it was and listed at the end.

Safety: the source (USB) is never written - it's copied to a temp folder
first, which also folds in any leftover -wal file. An existing target
library.db is backed up next to it before being replaced. Stdlib only, so
Mint's own python3 runs it. CLOSE MUSICMGR FIRST.

    python3 import_windows_library.py --dry-run          # report only
    python3 import_windows_library.py                    # do it

Defaults: source /media/$USER/USB/MusicMgr/data, target ~/MusicMgr/data, and
every Windows folder literally named "Music" (e.g. D:\\Music) maps to --music
(~/Music). Add more mappings with --map, e.g.
    --map 'D:\\MusicFLAC=/data/MusicFLAC'
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Optional

#: pictures kept in the data folder: (table, column, data subfolder)
IMAGE_COLUMNS = [
    ("releases", "cover_path", "artwork"),
    ("playlists", "cover_path", "artwork"),
    ("playlist_folders", "cover_path", "artwork"),
    ("artists", "image_path", "artists"),
    ("labels", "image_path", "artists"),
    ("radio_stations", "favicon_path", "stations"),
    ("videos", "thumbnail_path", "video_thumbs"),
]

#: data subfolders copied to the new data folder
IMAGE_FOLDERS = ("artwork", "artists", "stations", "video_thumbs")

#: files and folders under the media folders: (table, column, report label)
PATH_COLUMNS = [
    ("media_files", "path", "Music files"),
    ("videos", "path", "Videos"),
    ("watched_folders", "path", "Watched folders"),
    ("library_folders", "path", "Library folders"),
    ("video_folders", "path", "Video folders"),
    ("radio_shows", "folder", "Radio shows"),
    ("radio_shows", "cover_path", "Radio show pictures"),
    ("radio_episodes", "path", "Radio episodes"),
]

#: settings whose value names this PC's sync identity; the folder pairs and
#: their records (sync_pairs / sync_baseline) are cleared with it
SYNC_ID_KEY = "sync_pc_id"

WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def default_source() -> Path:
    return Path(f"/media/{os.environ.get('USER', '')}/USB/MusicMgr/data")


def default_target() -> Path:
    return Path.home() / "MusicMgr" / "data"


# -- path helpers -------------------------------------------------------------


def split_parts(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", path or "") if p]


def root_of(path: str) -> str:
    """'D:\\Music\\Boston\\x.mp3' -> 'D:\\Music' (drive + first folder)."""
    parts = split_parts(path)
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        return "\\".join(parts[:2]) if len(parts) > 1 else parts[0]
    return "/" + "/".join(parts[:2]) if parts else ""


def parse_map(text: str) -> tuple[list[str], Path]:
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"--map needs OLD=NEW, got {text!r}")
    old, new = text.split("=", 1)
    return [p.lower() for p in split_parts(old)], Path(new).expanduser()


def map_path(path: str, mappings: list[tuple[list[str], Path]]) -> Optional[Path]:
    """The Linux path for `path`, or None if no mapping's prefix matches.
    Prefix comparison is case-insensitive (Windows paths)."""
    parts = split_parts(path)
    lowered = [p.lower() for p in parts]
    for prefix, new_root in mappings:
        if lowered[: len(prefix)] == prefix:
            return new_root.joinpath(*parts[len(prefix):])
    return None


class CaseIndex:
    """Lazy lower-case -> real-path index of one folder tree, for fixing
    names whose capitalisation differs from what Windows stored."""

    def __init__(self) -> None:
        self._roots: dict[Path, dict[str, str]] = {}

    def resolve(self, candidate: Path, root: Path) -> Optional[Path]:
        if candidate.exists():
            return candidate
        if root not in self._roots:
            index: dict[str, str] = {}
            if root.is_dir():
                for dirpath, _dirs, files in os.walk(root):
                    for name in files:
                        full = os.path.join(dirpath, name)
                        index.setdefault(full.lower(), full)
            self._roots[root] = index
        real = self._roots[root].get(str(candidate).lower())
        return Path(real) if real else None


# -- the import -----------------------------------------------------------------


def copy_source_db(source_db: Path, workdir: Path) -> Path:
    """Copy library.db (+ -wal/-shm) to workdir and fold the WAL in there,
    so the source is never written to."""
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(source_db) + suffix)
        if src.exists():
            shutil.copy2(src, workdir / ("library.db" + suffix))
    db = sqlite3.connect(workdir / "library.db")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    return workdir / "library.db"


def image_sources(src_data: Path, images: Optional[Path]) -> list[Path]:
    """Folders holding artwork/ and artists/, searched in order: the source
    data folder, then --images (default: the folder above it, which on the
    USB is MusicMgr\\ with the drive's artwork and artists)."""
    extra = images if images is not None else src_data.parent
    out = [src_data]
    if extra.resolve() != src_data.resolve():
        out.append(extra)
    return out


def find_image(sources: list[Path], sub: str, rel: Path) -> bool:
    return any((src / sub / rel).is_file() for src in sources)


def copy_images(sources: list[Path], dst_data: Path, dry_run: bool) -> dict[str, int]:
    counts = {}
    for sub in IMAGE_FOLDERS:
        n = 0
        for root in sources:
            src = root / sub
            if not src.is_dir():
                continue
            dst = dst_data / sub
            for path in src.rglob("*"):
                if not path.is_file():
                    continue
                target = dst / path.relative_to(src)
                if target.exists() and target.stat().st_size == path.stat().st_size:
                    continue
                n += 1
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
        counts[sub] = n
    return counts


def image_rel(old: str, sub: str) -> Path:
    """'D:\\MusicMgr\\data\\artwork\\Folders\\x.jpg' -> Folders/x.jpg (the part
    after the last `sub` folder); just the file name if `sub` isn't in it."""
    parts = split_parts(old)
    low = [p.lower() for p in parts]
    if sub in low[:-1]:
        i = len(low) - 1 - low[::-1].index(sub)
        if i < len(parts) - 1:
            return Path(*parts[i + 1:])
    return Path(parts[-1]) if parts else Path()


def table_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    cols = [r[1] for r in db.execute(f"PRAGMA table_info('{table}')")]
    return column in cols


def clear_sync_identity(db: sqlite3.Connection, report: dict) -> None:
    """Forget the USB sync identity of the PC that saved this library."""
    stats = {}
    stats["sync id"] = db.execute("DELETE FROM settings WHERE key = ?", (SYNC_ID_KEY,)).rowcount \
        if table_exists(db, "settings", "key") else 0
    for table in ("sync_baseline", "sync_pairs"):
        try:
            stats[table] = db.execute(f"DELETE FROM {table}").rowcount
        except sqlite3.OperationalError:
            stats[table] = 0  # older database without USB sync
    report["USB sync identity cleared"] = stats


def rewrite(db: sqlite3.Connection, sources: list[Path], dst_data: Path, mappings, report: dict) -> None:
    cases = CaseIndex()

    def remap(old: str):
        """(new path string, status) for one Windows path."""
        new = map_path(old, mappings)
        if new is None:
            return None, "no mapping"
        lowered = [x.lower() for x in split_parts(old)]
        root = next(r for p, r in mappings if lowered[: len(p)] == p)
        found = cases.resolve(new, root)
        if found is None and new.is_dir():
            found = new
        return str(found or new), ("found on disk" if found else "mapped, file not on disk")

    def relocate(table: str, column: str, key: str) -> None:
        if not table_exists(db, table, column):
            return
        rows = db.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
        stats = Counter()
        unmapped = Counter()
        for row_id, old in rows:
            if not WINDOWS_PATH.match(old or ""):
                stats["already Linux"] += 1
                continue
            final, status = remap(old)
            if final is None:
                stats[status] += 1
                unmapped[root_of(old)] += 1
                continue
            stats[status] += 1
            try:
                db.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (final, row_id))
            except sqlite3.IntegrityError:
                stats["duplicate after mapping (left as is)"] += 1
        if rows:
            report[key] = dict(stats)
        if unmapped:
            report[key + " unmapped roots"] = dict(unmapped)

    for table, column, key in PATH_COLUMNS:
        relocate(table, column, key)

    # settings holding a Windows path (the Radio page's folder, last browse
    # folders); the sync id is handled by clear_sync_identity
    if table_exists(db, "settings", "value"):
        changed = 0
        for key, old in db.execute("SELECT key, value FROM settings WHERE value IS NOT NULL").fetchall():
            if not WINDOWS_PATH.match(old or ""):
                continue
            final, _status = remap(old)
            if final is not None:
                db.execute("UPDATE settings SET value = ? WHERE key = ?", (final, key))
                changed += 1
        report["Path settings"] = {"repointed": changed}

    for table, column, sub in IMAGE_COLUMNS:
        if not table_exists(db, table, column):
            continue
        folder = dst_data / sub
        changed = missing = 0
        rows = db.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
        for row_id, old in rows:
            if not old:
                continue
            rel = image_rel(old, sub)
            candidate = folder / rel
            # already in the target, or about to be copied there
            if str(rel) and (candidate.is_file() or find_image(sources, sub, rel)):
                if str(candidate) != old:
                    db.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (str(candidate), row_id))
                    changed += 1
            else:
                missing += 1
        if rows:
            report[f"{table}.{column}"] = {"repointed": changed, "image not found": missing}


def roots_summary(db_path: Path) -> Counter:
    db = sqlite3.connect(db_path)
    try:
        return Counter(root_of(p) for (p,) in db.execute("SELECT path FROM media_files"))
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=default_source(),
                        help="the Windows data folder (holding library.db, artwork/, artists/)")
    parser.add_argument("--target", type=Path, default=default_target(),
                        help="this computer's MusicMgr data folder")
    parser.add_argument("--music", type=Path, default=Path.home() / "Music",
                        help="where the music lives here (default ~/Music)")
    parser.add_argument("--map", type=parse_map, action="append", default=[],
                        help=r"extra 'OLD=NEW' folder mapping, e.g. 'D:\MusicMP3=/home/you/MusicMP3'")
    parser.add_argument("--images", type=Path, default=None,
                        help="another folder holding artwork/ and artists/ (default: the folder above "
                             "--source, i.e. the USB's MusicMgr folder)")
    parser.add_argument("--keep-sync-identity", action="store_true",
                        help="keep the source PC's USB sync id, folder pairs and sync records "
                             "(only when restoring a PC's own library onto the same PC)")
    parser.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    args = parser.parse_args(argv)

    src_data, dst_data = args.source.expanduser(), args.target.expanduser()
    source_db = src_data / "library.db"
    if not source_db.is_file():
        print(f"No library.db in {src_data}", file=sys.stderr)
        return 1
    if src_data.resolve() == dst_data.resolve():
        print("Source and target are the same folder.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        work_db = copy_source_db(source_db, Path(tmp))
        roots = roots_summary(work_db)

        # default mapping: every Windows "<drive>:\Music" -> --music
        mappings = list(args.map)
        for root in roots:
            parts = split_parts(root)
            if len(parts) == 2 and re.fullmatch(r"[A-Za-z]:", parts[0]) and parts[1].lower() == "music":
                mappings.append(([p.lower() for p in parts], args.music.expanduser()))

        print(f"Source: {source_db}")
        print("Music file locations in it:")
        for root, n in roots.most_common():
            target = map_path(root + "\\x", mappings)
            where = f"-> {target.parent}" if target else "-> (not mapped; add --map to include)"
            print(f"  {n:>7}  {root}  {where}")

        sources = image_sources(src_data, args.images.expanduser() if args.images else None)
        report: dict = {}
        db = sqlite3.connect(work_db)
        try:
            rewrite(db, sources, dst_data, mappings, report)
            if not args.keep_sync_identity:
                clear_sync_identity(db, report)
            db.commit()
        finally:
            db.close()
        images = copy_images(sources, dst_data, dry_run=True)

        print("\nResult" + (" (dry run - nothing changed)" if args.dry_run else "") + ":")
        for key, value in report.items():
            print(f"  {key}: {value}")
        print(f"  images to copy: {images}")
        if report.get("Music files", {}).get("mapped, file not on disk"):
            print("\n  'file not on disk' = mapped fine, but that song isn't in "
                  f"{args.music} - copy it there and it will be found.")

        if args.dry_run:
            print("\nRun again without --dry-run to install it.")
            return 0

        dst_data.mkdir(parents=True, exist_ok=True)
        target_db = dst_data / "library.db"
        if target_db.exists():
            backup = dst_data / f"library-before-windows-import-{time.strftime('%Y%m%d-%H%M%S')}.db"
            src = sqlite3.connect(target_db)
            try:
                out = sqlite3.connect(backup)
                src.backup(out)
                out.close()
            finally:
                src.close()
            print(f"\nBacked up the existing library to {backup}")
            for suffix in ("-wal", "-shm"):
                side = Path(str(target_db) + suffix)
                if side.exists():
                    side.unlink()
        copied = copy_images(sources, dst_data, dry_run=False)
        db = sqlite3.connect(work_db)
        try:
            out = sqlite3.connect(target_db)
            try:
                db.backup(out)
            finally:
                out.close()
        finally:
            db.close()
        print(f"Copied images: {copied}")
        print(f"Installed {target_db}")
        print(f"\nNow open MusicMgr. Settings > Scan now picks up anything new in {args.music}.")
        if not args.keep_sync_identity:
            print("Settings > USB sync > Check USB now pairs the folders with the drive as a new PC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
