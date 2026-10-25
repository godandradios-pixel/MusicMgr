"""Unlink chart positions matched to the wrong song by the old matcher.

2026-10-25 - chart matching scored title and artist together and counted
one title found anywhere inside another (even inside a word) as 0.9, so
positions linked to other songs by the same act ("Hurricane" by MS MR ->
"Beautiful Hurricane") and to songs whose title merely contained the
chart's ("Men" -> "Midnight Train to Georgia (instrumental)").
matching.score_pair now requires the title itself to match
(MIN_TITLE_SIMILARITY, title_similarity), but positions matched before
keep their link until a chart is re-matched, and Settings > Charts >
Re-match over every chart takes well over half an hour.

This script only re-scores each existing link - the chart's title and
artist against the linked song's - with the current score_pair, and
unlinks those now under the match threshold. It doesn't search for new
matches (Re-match a chart for that). Positions fixed by hand (Fix match...)
are left alone. Playlists already saved from a chart keep their songs
until saved again.

    venv\\Scripts\\python tools\\unlink_loose_chart_matches.py --db D:\\MusicMgr\\data\\library.db
    venv\\Scripts\\python tools\\unlink_loose_chart_matches.py --db D:\\MusicMgr\\data\\library.db --apply

CLOSE MUSICMGR FIRST. Without --apply it only reports. --apply writes a
backup (data\\backups\\library-before-unlink-<time>.db) first.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicmgr.services.matching import score_pair  # noqa: E402

THRESHOLD = 0.72  # charts.match_entry's default


def loose_links(con: sqlite3.Connection) -> list[tuple[int, str, str, str, str, str]]:
    """(entry id, chart, chart title, chart artist, song title, song artist)
    for every unlocked link that no longer scores as a match."""
    rows = con.execute(
        """select e.id, c.name, e.title, e.artist_name, t.title, t.artist_display
           from chart_entries e
           join tracks t on t.id = e.track_id
           join chart_issues i on i.id = e.issue_id
           join charts c on c.id = i.chart_id
           where coalesce(e.match_locked, 0) = 0"""
    ).fetchall()
    scores: dict[tuple, float] = {}
    out = []
    for eid, chart, et, ea, tt, ta in rows:
        key = (et, ea, tt, ta)
        if key not in scores:
            scores[key] = score_pair(et or "", ea or "", tt or "", ta or "")
        if scores[key] < THRESHOLD:
            out.append((eid, chart, et, ea, tt, ta))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    con = sqlite3.connect(str(args.db))
    loose = loose_links(con)
    pairs = sorted({(r[2], r[3], r[4], r[5]) for r in loose})
    print(f"{len(loose)} chart positions ({len(pairs)} different song pairs) are linked "
          "to a song they no longer match")
    for chart, n in Counter(r[1] for r in loose).most_common():
        print(f"  {n:6}  {chart}")
    for et, ea, tt, ta in pairs[:40]:
        print(f"    {et} - {ea}  ->  {tt} - {ta}")
    if not args.apply:
        print("Nothing changed (run again with --apply).")
        return 0
    backups = args.db.parent / "backups"
    backups.mkdir(exist_ok=True)
    dest = backups / f"library-before-unlink-{time.strftime('%Y%m%d-%H%M%S')}.db"
    with sqlite3.connect(str(dest)) as dst:
        con.backup(dst)
    print(f"Backup: {dest}")
    with con:
        con.executemany(
            "update chart_entries set track_id = null, match_score = null where id = ?",
            [(r[0],) for r in loose],
        )
    print(f"Unlinked {len(loose)} positions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
