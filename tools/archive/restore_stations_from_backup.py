"""Put back Radio page stations that are in a backup but no longer in the
library.

2026-10-14 - all 21 AM/FM presets (11 AM, 10 FM) were removed from
D:\\MusicMgr by the 08:22 USB sync: a pre-1.7.1 MusicMgr had rewritten the
drive's state without stations, a PC with no sync history then wrote "no
stations" to it, and this PC read that as every station deleted on the
other PC (fixed in services/library_state.py the same day). The backup
MusicMgr made before the first run of 2.1.5 still has them.

Matching is by stream address, never by id, so a station already in the
library is left alone. Name, band, dial position, homepage, logo address,
country, tags, codec and bitrate come back; the logo picture itself is
fetched again by the Radio page (the sync deleted the files).

After this, sync this PC FIRST: the stations then count as new here and go
out to the USB, so the other PCs pick them up too.

Stdlib only. CLOSE MUSICMGR FIRST. A backup is written next to the database
before anything changes.

    python tools/restore_stations_from_backup.py --db D:\\MusicMgr\\data\\library.db --from D:\\MusicMgr\\data\\backups\\library-before-2.1.5.db
    python tools/restore_stations_from_backup.py --db D:\\MusicMgr\\data\\library.db --from D:\\MusicMgr\\data\\backups\\library-before-2.1.5.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

#: copied as they were; favicon_path (this PC's logo file) is left empty
FIELDS = ("name", "stream_url", "homepage", "favicon_url", "country", "tags", "codec",
          "bitrate", "rb_uuid", "dial_order", "band", "created_at", "off_air_since")


def missing_stations(db: sqlite3.Connection) -> list[dict]:
    have = {(r[0] or "").strip() for r in db.execute("SELECT stream_url FROM main.radio_stations")}
    src_cols = {r[1] for r in db.execute("PRAGMA src.table_info(radio_stations)")}
    cols = [f for f in FIELDS if f in src_cols]
    out, seen = [], set()
    for row in db.execute(
            f"SELECT {', '.join(cols)} FROM src.radio_stations ORDER BY band, dial_order, id"):
        st = dict(zip(cols, row))
        url = (st.get("stream_url") or "").strip()
        if url and url not in have and url not in seen:
            seen.add(url)
            out.append(st)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path, help="library.db to restore into")
    ap.add_argument("--from", dest="src", required=True, type=Path, help="backup .db to restore from")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args(argv)
    for p in (a.db, a.src):
        if not p.is_file():
            print(f"not found: {p}", file=sys.stderr)
            return 2

    db = sqlite3.connect(str(a.db))
    db.execute("ATTACH DATABASE ? AS src", (str(a.src),))
    todo = missing_stations(db)
    if not todo:
        print("Nothing to restore - every station in the backup is already in the library.")
        return 0
    for band in ("am", "fm"):
        rows = [s for s in todo if (s.get("band") or "fm") == band]
        if rows:
            print(f"{band.upper()} ({len(rows)}):")
            for s in rows:
                print(f"  {s.get('name')}")
    others = [s for s in todo if (s.get("band") or "fm") not in ("am", "fm")]
    for s in others:
        print(f"  {s.get('name')} ({s.get('band')})")
    if not a.apply:
        print(f"\n{len(todo)} station(s) would be restored. Run again with --apply.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = a.db.with_name(f"library.before-station-restore-{stamp}.db")
    dst = sqlite3.connect(str(backup))
    db.backup(dst)
    dst.close()
    print(f"\nBackup: {backup}")
    with db:
        for s in todo:
            cols = list(s)
            db.execute(
                f"INSERT INTO main.radio_stations ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [s[c] for c in cols])
    print(f"Restored {len(todo)} station(s). Start MusicMgr and sync this PC first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
