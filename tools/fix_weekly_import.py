#!/usr/bin/env python3
"""Undo a Hot 100 *weekly* CSV imported into the Pop *Year End* chart, then
import it into the Weekly chart where it belongs (2026-09-30).

James imported billboard-hot-100-weekly-2023-08-22-to-2026-09-29.csv with
the Chart name box left at its default, "Billboard Hot 100". That is the
Year End chart's internal name (its slug), so 163 weekly editions landed
under Billboard > Pop > Year End. One of those weeks, 31 Dec 2024, fell on
the same date as the real 2024 year-end edition and overwrote its rows.

This tool:
  1. finds the Year End chart that got the weeks and the Weekly chart that
     should have had them (it prints both - check them on the dry run);
  2. removes every edition dated like a week in the CSV from the Year End
     chart;
  3. puts the 2024 year-end edition back exactly as it was, from the newest
     pre-update backup in data\\backups that still has it untouched
     (MusicMgr makes one each time it updates);
  4. imports the CSV into the Weekly chart.

CLOSE MUSICMGR FIRST. A copy of the database is saved to data\\backups
before anything changes. Run from C:\\Projects\\MusicMgr:

    python tools\\fix_weekly_import.py                 # dry run - shows what it would do
    python tools\\fix_weekly_import.py --apply
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(r"D:\MusicMgr\data\library.db")
DEFAULT_CSV = REPO / "Claude outputs" / "billboard-hot-100-weekly-2023-08-22-to-2026-09-29.csv"


def csv_dates(path: Path) -> set[str]:
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["date"] for row in csv.DictReader(fh)}


def folder_path(db: sqlite3.Connection, fid: Optional[int]) -> str:
    parts = []
    while fid is not None:
        row = db.execute("SELECT name, parent_id FROM chart_folders WHERE id = ?", (fid,)).fetchone()
        if row is None:
            break
        parts.append(row[0])
        fid = row[1]
    return "/".join(reversed(parts))


def describe(db: sqlite3.Connection, cid: int) -> str:
    name, slug, fid = db.execute("SELECT name, slug, folder_id FROM charts WHERE id = ?", (cid,)).fetchone()
    n, lo, hi = db.execute(
        "SELECT count(*), min(chart_date), max(chart_date) FROM chart_issues WHERE chart_id = ?", (cid,)
    ).fetchone()
    return f"{folder_path(db, fid)}/{name} (slug {slug!r}, {n} editions, {lo} .. {hi})"


def find_charts(db: sqlite3.Connection, dates: set[str]) -> tuple[Optional[int], Optional[int]]:
    """(chart that wrongly got the weeks, Weekly chart they belong in).
    Weekly = the chart with the most editions before the file's first week
    that aren't 31 December; decided first, so a re-run after --apply never
    mistakes the (now correct) Weekly chart for the wrong one."""
    first = min(dates)
    row = db.execute(
        "SELECT chart_id FROM chart_issues WHERE chart_date < ? AND chart_date NOT LIKE '%-12-31' "
        "GROUP BY chart_id ORDER BY count(*) DESC LIMIT 1", (first,)).fetchone()
    weekly = row[0] if row else None
    hits: dict[int, int] = {}
    for cid, d in db.execute("SELECT chart_id, chart_date FROM chart_issues"):
        if d in dates and cid != weekly:
            hits[cid] = hits.get(cid, 0) + 1
    wrong = max(hits, key=hits.get) if hits else None
    if wrong is not None and hits[wrong] < len(dates) // 2:
        wrong = None
    return wrong, weekly


def year_end_collisions(dates: set[str]) -> list[str]:
    return sorted(d for d in dates if d.endswith("-12-31"))


def find_backup(db_path: Path, slug: str, dates: set[str], collisions: list[str]) -> Optional[Path]:
    """Newest backup whose copy of the chart has none of the imported weeks
    but does have the year-end editions that got overwritten."""
    folder = db_path.parent / "backups"
    candidates = sorted(folder.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    weekly_only = dates - set(collisions)
    for path in candidates:
        try:
            con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            row = con.execute("SELECT id FROM charts WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                continue
            have = {d for (d,) in con.execute(
                "SELECT chart_date FROM chart_issues WHERE chart_id = ?", (row[0],))}
            con.close()
        except sqlite3.Error:
            continue
        if not (have & weekly_only) and set(collisions) <= have:
            return path
    return None


def restore_issue(db: sqlite3.Connection, backup: Path, slug: str, chart_id: int, date: str) -> int:
    src = sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    b_chart = src.execute("SELECT id FROM charts WHERE slug = ?", (slug,)).fetchone()["id"]
    issue = src.execute(
        "SELECT * FROM chart_issues WHERE chart_id = ? AND chart_date = ?", (b_chart, date)
    ).fetchone()
    cols = [c for c in issue.keys() if c not in ("id", "chart_id")]
    cur = db.execute(
        f"INSERT INTO chart_issues (chart_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
        (chart_id, *[issue[c] for c in cols]),
    )
    new_issue = cur.lastrowid
    entries = src.execute("SELECT * FROM chart_entries WHERE issue_id = ? ORDER BY rank", (issue["id"],)).fetchall()
    for e in entries:
        ecols = [c for c in e.keys() if c not in ("id", "issue_id")]
        db.execute(
            f"INSERT INTO chart_entries (issue_id, {', '.join(ecols)}) VALUES (?{', ?' * len(ecols)})",
            (new_issue, *[e[c] for c in ecols]),
        )
    src.close()
    return len(entries)


def run(db_path: Path, csv_path: Path, apply: bool) -> int:
    for p in (db_path, csv_path):
        if not p.exists():
            print(f"Not found: {p}")
            return 1
    dates = csv_dates(csv_path)
    db = sqlite3.connect(db_path)
    wrong, weekly = find_charts(db, dates)
    if wrong is None:
        print("No chart holds the imported weeks - nothing to undo.")
    else:
        print(f"Weeks were imported into:  {describe(db, wrong)}")
    if weekly is None:
        print("Couldn't find the Weekly chart.")
        return 1
    print(f"They belong in:            {describe(db, weekly)}")

    collisions = year_end_collisions(dates)
    backup = None
    if wrong is not None:
        slug = db.execute("SELECT slug FROM charts WHERE id = ?", (wrong,)).fetchone()[0]
        present = {d for (d,) in db.execute(
            "SELECT chart_date FROM chart_issues WHERE chart_id = ?", (wrong,)) if d in dates}
        overwritten = [d for d in collisions if d in present]
        print(f"\nEditions to remove from it: {len(present)}")
        if overwritten:
            backup = find_backup(db_path, slug, dates, overwritten)
            if backup is None:
                print(f"Year-end edition(s) overwritten: {', '.join(overwritten)} - and no backup in "
                      f"{db_path.parent / 'backups'} still has them. Stopping; nothing changed.")
                return 1
            print(f"Year-end edition(s) to put back: {', '.join(overwritten)}  (from {backup.name})")

    weekly_slug = db.execute("SELECT slug FROM charts WHERE id = ?", (weekly,)).fetchone()[0]
    already = db.execute(
        "SELECT count(*) FROM chart_issues WHERE chart_id = ? AND chart_date IN (%s)"
        % ",".join("?" * len(dates)), (weekly, *sorted(dates))).fetchone()[0]
    print(f"Weeks to add to Weekly: {len(dates) - already} (of {len(dates)} in the file)")

    if not apply:
        print("\nDry run - nothing changed. Close MusicMgr and add --apply to do it.")
        return 0

    safe = db_path.parent / "backups" / f"library-before-weekly-fix-{time.strftime('%Y%m%d-%H%M%S')}.db"
    safe.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(safe)
    db.backup(out)
    out.close()
    print(f"\nBackup: {safe}")

    if wrong is not None:
        with db:
            ids = [i for (i, d) in db.execute(
                "SELECT id, chart_date FROM chart_issues WHERE chart_id = ?", (wrong,)) if d in dates]
            for i in ids:
                db.execute("DELETE FROM chart_entries WHERE issue_id = ?", (i,))
                db.execute("DELETE FROM chart_issues WHERE id = ?", (i,))
            print(f"Removed {len(ids)} editions from the Year End chart.")
            for d in [d for d in collisions if backup is not None]:
                n = restore_issue(db, backup, slug, wrong, d)
                print(f"Put back the {d[:4]} year-end edition ({n} rows).")
    db.close()

    # the import itself, through MusicMgr's own importer (same matching as the app)
    sys.path.insert(0, str(REPO))
    from musicmgr.db import session as db_session
    from musicmgr.services import charts as chart_svc

    db_session.init_engine(db_path=db_path)
    print("Importing into Weekly (matching against your library - this can take a few minutes)...")
    with db_session.session_scope() as s:
        result = chart_svc.import_chart_csv(s, csv_path, chart_name=weekly_slug)
    print(result.summary())
    if result.chart_id != weekly:
        print("WARNING: the import went to a different chart than expected - restore the backup above.")
        return 1
    print("Done. Open MusicMgr > Charts > Billboard > Pop > Weekly.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    return run(args.db, args.csv, args.apply)


if __name__ == "__main__":
    sys.exit(main())
