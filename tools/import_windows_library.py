#!/usr/bin/env python3
"""Use a Windows MusicMgr library.db on Linux (or any other machine).

2026-09-23 - James, on the Linux Mint PC: "I wanted to use the library.db
from the USB MusicMgr\\data directory." That database is the whole Windows
library - tracks, ratings, play counts, playlists, charts, Jukebox cards,
artist bios - but every file path in it is a Windows one ("D:\\Music\\Boston\\
...\\01 More Than a Feeling.mp3"), and the music now lives at ~/Music (copied
from USB/Music with rsync). Opened as-is, every track would show as missing.

This tool installs a copy of it as the Linux library and repoints the paths:

* media files and videos: a Windows folder prefix -> a Linux folder, e.g.
  D:\\Music -> ~/Music, with \\ turned into /. If the exact result isn't on
  disk, a case-insensitive lookup fixes names whose capitalisation differs
  (Windows didn't care, Linux does).
* album covers / artist pictures / label / playlist images: copied from the
  source data folder's artwork/ and artists/ into the new data folder and
  repointed by file name (the same by-name rule MusicMgr's own
  "Move data" repair uses).
* watched folders: repointed with the same prefix mapping, so "Scan now"
  rescans ~/Music and recognises every file as already known (no duplicates).

Anything it can't map is left exactly as it was and listed at the end.

Safety: the source (USB) is never written - it's copied to a temp folder
first, which also folds in any leftover -wal file. An existing target
library.db is backed up next to it before being replaced. Stdlib only, so
Mint's own python3 runs it. CLOSE MUSICMGR FIRST.

    python3 import_windows_library.py --dry-run          # report only
    python3 import_windows_library.py                    # do it

Defaults: source /media/$USER/USB/MusicMgr/data, target ~/MusicMgr/data, and
every Windows folder literally named "Music" (e.g. D:\\Music) maps to ~/Music.
Add more mappings with --map, e.g.
    --map 'D:\\MusicMP3=/home/jrs58/MusicMP3'
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

IMAGE_COLUMNS = [
    # (table, column, subfolder of the data dir the files live in)
    ("releases", "cover_path", "artwork"),
    ("playlists", "cover_path", "artwork"),
    ("artists", "image_path", "artists"),
    ("labels", "image_path", "artists"),
]


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


def copy_images(src_data: Path, dst_data: Path, dry_run: bool) -> dict[str, int]:
    counts = {}
    for sub in ("artwork", "artists"):
        src, dst = src_data / sub, dst_data / sub
        n = 0
        if src.is_dir():
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


def rewrite(db: sqlite3.Connection, src_data: Path, dst_data: Path, mappings, report: dict) -> None:
    cases = CaseIndex()

    def relocate(table: str, column: str, key: str) -> None:
        rows = db.execute(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
        stats = Counter()
        unmapped = Counter()
        for row_id, old in rows:
            if not re.match(r"^[A-Za-z]:[\\/]", old or ""):
                stats["already Linux"] += 1
                continue
            new = map_path(old, mappings)
            if new is None:
                stats["no mapping"] += 1
                unmapped[root_of(old)] += 1
                continue
            root = next(r for p, r in mappings if [x.lower() for x in split_parts(old)][: len(p)] == p)
            found = cases.resolve(new, root)
            final = found or new
            stats["found on disk" if found else "mapped, file not on disk"] += 1
            try:
                db.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (str(final), row_id))
            except sqlite3.IntegrityError:
                stats["duplicate after mapping (left as is)"] += 1
        report[key] = dict(stats)
        if unmapped:
            report[key + " unmapped roots"] = dict(unmapped)

    relocate("media_files", "path", "Music files")
    relocate("videos", "path", "Videos")
    relocate("watched_folders", "path", "Watched folders")

    for table, column, sub in IMAGE_COLUMNS:
        folder = dst_data / sub
        changed = missing = 0
        try:
            rows = db.execute(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            continue  # older database without this column
        for row_id, old in rows:
            name = split_parts(old)[-1] if old else ""
            candidate = folder / name
            # the image is either already in the target, or about to be copied there
            if name and (candidate.is_file() or (src_data / sub / name).is_file()):
                if str(candidate) != old:
                    db.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (str(candidate), row_id))
                    changed += 1
            else:
                missing += 1
        report[f"{table}.{column}"] = {"repointed": changed, "image not found": missing}

    # video thumbnails live wherever the app generated them - repoint by name too
    try:
        for row_id, old in db.execute("SELECT id, thumbnail_path FROM videos WHERE thumbnail_path IS NOT NULL").fetchall():
            name = split_parts(old)[-1]
            for sub in ("artwork", "thumbnails", "video_thumbs"):
                cand = dst_data / sub / name
                if cand.is_file() or (src_data / sub / name).is_file():
                    db.execute("UPDATE videos SET thumbnail_path = ? WHERE id = ?", (str(cand), row_id))
                    break
    except sqlite3.OperationalError:
        pass


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

        report: dict = {}
        db = sqlite3.connect(work_db)
        try:
            rewrite(db, src_data, dst_data, mappings, report)
            db.commit()
        finally:
            db.close()
        images = copy_images(src_data, dst_data, dry_run=True)

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
        copied = copy_images(src_data, dst_data, dry_run=False)
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
        print("\nNow open MusicMgr. Settings > Scan now picks up anything new in ~/Music.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
