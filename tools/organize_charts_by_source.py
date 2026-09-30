#!/usr/bin/env python3
"""Re-file the Charts tree as  Source > Genre > Type > date.

2026-09-30 - James: "On the Charts. I want my first level to be the chart
name, Billboard, Playback, then Genre, then type of chart (Year End or
Weekly) and then the date".

Before (genre folders at the top, source + type squashed into the name):

    Pop
      Billboard YE        -> editions
      Billboard Weekly    -> editions
      Playback YE         -> editions

After (the chart itself is named by its type, so its editions - the dates -
are the level under it):

    Billboard
      Pop
        Year End          -> 1946-12-31 ... 2025-12-31
        Weekly            -> 1958-08-04 ... 2023-08-15
    Playback
      Pop
        Year End          -> 1900-12-31 ... 2024-12-31

How each chart is placed:
  * source = the first word of its name when that word is a known source
    (Billboard, Playback, ...); otherwise the chart is left alone and listed.
  * genre  = the name of the folder it sits in now.
  * type   = "Year End" when it has at most one edition per calendar year,
             else "Weekly" (the same rule as the Type column,
             services/charts.py:chart_shape). A chart with no editions yet
             falls back to its name ending in YE / Weekly.
  * the source and genre folders are reused when they already exist
    (case-insensitive), so a re-run, or one after new imports, is safe.
  * a genre folder that ends up empty is removed; anything still in one is
    kept and reported.
  * a chart already under <Source>/<Genre> only gets its name fixed.

Since 2026-09-30 charts travel with Settings > USB sync > Charts, so running
this on one PC is enough: the next sync moves the other PC's copies to
match (see services/chart_sync.py). Stdlib only. CLOSE MUSICMGR FIRST. A backup is written next to the database
before anything changes.

    python tools/organize_charts_by_source.py --db D:\\MusicMgr\\data\\library.db            # dry run
    python tools/organize_charts_by_source.py --db D:\\MusicMgr\\data\\library.db --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

#: first word of a chart name -> the top-level folder it goes in
SOURCES = {
    "billboard": "Billboard",
    "playback": "Playback",
    "playback.fm": "Playback",
}

YEAR_END = "Year End"
WEEKLY = "Weekly"


def default_db() -> Path:
    here = Path(__file__).resolve().parent.parent / "data" / "library.db"
    return here


def chart_type(db: sqlite3.Connection, chart_id: int, name: str) -> str:
    years = [
        r[0]
        for r in db.execute(
            "SELECT substr(chart_date, 1, 4) FROM chart_issues WHERE chart_id = ?",
            (chart_id,),
        )
    ]
    if years:
        return YEAR_END if len(years) == len(set(years)) else WEEKLY
    lowered = name.lower()
    if lowered.endswith(" ye") or "year end" in lowered or "year-end" in lowered:
        return YEAR_END
    return WEEKLY


class Folders:
    def __init__(self, db: sqlite3.Connection, apply: bool) -> None:
        self.db = db
        self.apply = apply
        self.rows: dict[int, tuple[str, Optional[int]]] = {
            fid: (name, parent)
            for fid, name, parent in db.execute(
                "SELECT id, name, parent_id FROM chart_folders"
            )
        }
        self._fake_id = -1
        self.created: list[str] = []

    def name(self, fid: Optional[int]) -> Optional[str]:
        return self.rows[fid][0] if fid in self.rows else None

    def parent(self, fid: Optional[int]) -> Optional[int]:
        return self.rows[fid][1] if fid in self.rows else None

    def path(self, fid: Optional[int]) -> str:
        parts = []
        while fid is not None and fid in self.rows:
            parts.append(self.rows[fid][0])
            fid = self.rows[fid][1]
        return "/".join(reversed(parts)) or "(top level)"

    def find_or_create(self, name: str, parent_id: Optional[int]) -> int:
        for fid, (fname, fparent) in self.rows.items():
            if fparent == parent_id and fname.strip().lower() == name.lower():
                return fid
        if self.apply:
            cur = self.db.execute(
                "INSERT INTO chart_folders (name, parent_id, created_at) "
                "VALUES (?, ?, datetime('now'))",
                (name, parent_id),
            )
            fid = cur.lastrowid
        else:
            fid = self._fake_id
            self._fake_id -= 1
        self.rows[fid] = (name, parent_id)
        self.created.append(self.path(fid))
        return fid

    def is_empty(self, fid: int, chart_folder: dict[int, Optional[int]]) -> bool:
        if any(p == fid for (_n, p) in self.rows.values()):
            return False
        return not any(f == fid for f in chart_folder.values())

    def delete(self, fid: int) -> None:
        if self.apply:
            self.db.execute("DELETE FROM chart_folders WHERE id = ?", (fid,))
        del self.rows[fid]


def run(db_path: Path, apply: bool) -> int:
    if not db_path.exists():
        print(f"No database at {db_path}")
        return 1
    if apply:
        backup = db_path.with_name(
            f"{db_path.stem}.before-chart-reorg-{time.strftime('%Y%m%d-%H%M%S')}{db_path.suffix}"
        )
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup)
        src.backup(dst)  # includes anything still in the -wal file
        dst.close()
        src.close()
        print(f"Backup: {backup}")

    db = sqlite3.connect(db_path)
    folders = Folders(db, apply)
    source_names = set(SOURCES.values())
    charts = list(db.execute("SELECT id, name, folder_id FROM charts ORDER BY name"))
    chart_folder = {cid: fid for cid, _n, fid in charts}
    old_genre_folders: set[int] = set()
    taken: dict[tuple[int, str], int] = {}  # (genre folder, new name) -> chart id
    moved = renamed = 0
    skipped: list[str] = []

    for cid, name, fid in charts:
        first = name.split()[0].lower() if name.split() else ""
        parent = folders.parent(fid)
        already = fid is not None and parent is not None and folders.parent(parent) is None \
            and folders.name(parent) in source_names

        if already:
            source, genre_fid = folders.name(parent), fid
        elif first in SOURCES and fid is not None:
            source = SOURCES[first]
            src_fid = folders.find_or_create(source, None)
            genre_fid = folders.find_or_create(folders.name(fid), src_fid)
            old_genre_folders.add(fid)
        else:
            why = "no known source in its name" if first not in SOURCES else "not in a genre folder"
            skipped.append(f"  {name!r} in {folders.path(fid)} - {why}")
            continue

        new_name = chart_type(db, cid, name)
        key = (genre_fid, new_name.lower())
        if key in taken and taken[key] != cid:
            n = 2
            while (genre_fid, f"{new_name} ({n})".lower()) in taken:
                n += 1
            new_name = f"{new_name} ({n})"
            key = (genre_fid, new_name.lower())
        taken[key] = cid

        changes = []
        if genre_fid != fid:
            changes.append(f"{folders.path(fid)} -> {folders.path(genre_fid)}")
            chart_folder[cid] = genre_fid
            moved += 1
            if apply:
                db.execute("UPDATE charts SET folder_id = ? WHERE id = ?", (genre_fid, cid))
        if new_name != name:
            changes.append(f"renamed {new_name!r}")
            renamed += 1
            if apply:
                db.execute("UPDATE charts SET name = ? WHERE id = ?", (new_name, cid))
        if changes:
            print(f"{name!r}: " + ", ".join(changes))

    removed = []
    for fid in sorted(old_genre_folders):
        if fid in folders.rows and folders.is_empty(fid, chart_folder):
            removed.append(folders.path(fid))
            folders.delete(fid)
    kept = [folders.path(f) for f in old_genre_folders if f in folders.rows]

    if folders.created:
        print("\nFolders created:\n  " + "\n  ".join(folders.created))
    if removed:
        print("\nEmpty old folders removed:\n  " + "\n  ".join(removed))
    if kept:
        print("\nOld folders kept (something is still in them):\n  " + "\n  ".join(kept))
    if skipped:
        print("\nLeft alone:\n" + "\n".join(skipped))

    print(f"\n{moved} chart(s) moved, {renamed} renamed.")
    if apply:
        db.commit()
        print("Done.")
    else:
        print("Dry run - nothing changed. Add --apply to do it.")
    db.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=default_db(),
                    help="library.db to reorganize (default: this repo's data/library.db)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = ap.parse_args(argv)
    return run(args.db, args.apply)


if __name__ == "__main__":
    sys.exit(main())
