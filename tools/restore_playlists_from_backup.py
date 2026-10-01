"""Put back playlists and playlist folders that are in a backup but no longer
in the library.

2026-09-30 - the 78 Records folder (with its DECCA subfolder) and Personal
Favorites were removed from D:\\MusicMgr by a USB sync: the drive's state
didn't list them, this PC's last-sync copy did, so the sync read that as
"deleted on the other PC". The backup MusicMgr made before the first run
of 1.7.13 still has them.

Matching is by folder path and playlist name, never by id, so anything
already in the library is left alone. Only folders and playlists that are
missing get added, with their tracks in their saved order. A cover image is
put back only if its file is still on disk; missing ones are listed.

After this, sync this PC FIRST: the playlists then count as new here and go
out to the USB, so the other PC picks them up instead of removing them.

Stdlib only. CLOSE MUSICMGR FIRST. A backup is written next to the database
before anything changes.

    python tools/restore_playlists_from_backup.py --db D:\\MusicMgr\\data\\library.db --from D:\\MusicMgr\\data\\backups\\library-before-1.7.13.db
    python tools/restore_playlists_from_backup.py --db D:\\MusicMgr\\data\\library.db --from D:\\MusicMgr\\data\\backups\\library-before-1.7.13.db --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def folder_paths(db: sqlite3.Connection, schema: str) -> dict[int, str]:
    rows = {r[0]: (r[1], r[2]) for r in db.execute(
        f"SELECT id, name, parent_id FROM {schema}.playlist_folders")}
    out: dict[int, str] = {}

    def path(fid: int, seen=()) -> str:
        if fid in out:
            return out[fid]
        name, parent = rows[fid]
        if parent is None or parent not in rows or parent in seen:
            p = name
        else:
            p = path(parent, seen + (fid,)) + "/" + name
        out[fid] = p
        return p

    for fid in rows:
        path(fid)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path, help="library.db to restore into")
    ap.add_argument("--from", dest="src", required=True, type=Path, help="backup .db to restore from")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args(argv)
    for p in (a.db, a.src):
        if not p.is_file():
            print(f"not found: {p}")
            return 2

    db = sqlite3.connect(f"file:{a.db}", uri=True)
    db.execute("ATTACH DATABASE ? AS b", (f"file:{a.src.as_posix()}?mode=ro",))

    here = folder_paths(db, "main")
    there = folder_paths(db, "b")
    here_by_path = {p.casefold(): fid for fid, p in here.items()}

    # folders missing here, parents first
    new_folders = sorted((p for p in there.values() if p.casefold() not in here_by_path),
                         key=lambda p: p.count("/"))
    there_by_path = {p: fid for fid, p in there.items()}

    # playlists missing here (by folder path + name)
    have = {((here.get(f) or "").casefold(), n.casefold())
            for n, f in db.execute("SELECT name, folder_id FROM main.playlists")}
    missing = []
    for row in db.execute(
            "SELECT id, name, description, kind, rules, cover_path, is_pinned, source_issue_id,"
            " created_at, updated_at, folder_id, slug FROM b.playlists ORDER BY id"):
        fpath = there.get(row[10]) or ""
        if (fpath.casefold(), row[1].casefold()) not in have:
            missing.append((fpath, row))

    tracks_here = {r[0] for r in db.execute("SELECT id FROM main.tracks")}
    no_image: list[str] = []

    def cover(p):
        if p and not os.path.isfile(p):
            no_image.append(p)
            return None
        return p

    print(f"Folders to add: {len(new_folders)}")
    for p in new_folders:
        print(f"  {p}")
    print(f"Playlists to add: {len(missing)}")
    total = lost = 0
    for fpath, row in missing:
        items = db.execute("SELECT track_id FROM b.playlist_items WHERE playlist_id = ?",
                           (row[0],)).fetchall()
        gone = sum(1 for (t,) in items if t not in tracks_here)
        total += len(items) - gone
        lost += gone
        extra = f"  ({gone} track(s) no longer in the library)" if gone else ""
        print(f"  {fpath + '/' if fpath else ''}{row[1]}: {len(items)} tracks{extra}")
    print(f"Tracks: {total}" + (f", {lost} skipped" if lost else ""))

    if not new_folders and not missing:
        print("Nothing to restore.")
        return 0
    if not a.apply:
        print("\nDry run - nothing changed. Add --apply to write it.")
        return 0

    db.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = a.db.with_name(f"library.before-playlist-restore-{stamp}.db")
    src = sqlite3.connect(str(a.db))
    dst = sqlite3.connect(str(backup))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"\nBackup: {backup}")

    db = sqlite3.connect(f"file:{a.db}", uri=True)
    db.execute("ATTACH DATABASE ? AS b", (f"file:{a.src.as_posix()}?mode=ro",))
    now = time.strftime("%Y-%m-%d %H:%M:%S.000000", time.gmtime())
    with db:
        for p in new_folders:
            old = db.execute("SELECT name, created_at, cover_path FROM b.playlist_folders WHERE id = ?",
                             (there_by_path[p],)).fetchone()
            parent = here_by_path.get(p.rsplit("/", 1)[0].casefold()) if "/" in p else None
            cur = db.execute(
                "INSERT INTO main.playlist_folders (name, parent_id, created_at, cover_path)"
                " VALUES (?, ?, ?, ?)", (old[0], parent, old[1], cover(old[2])))
            here_by_path[p.casefold()] = cur.lastrowid
        for fpath, row in missing:
            fid = here_by_path.get(fpath.casefold()) if fpath else None
            cur = db.execute(
                "INSERT INTO main.playlists (name, description, kind, rules, cover_path, is_pinned,"
                " source_issue_id, created_at, updated_at, folder_id, slug)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (row[1], row[2], row[3], row[4], cover(row[5]), row[6], row[8], now, fid, row[11]))
            pid = cur.lastrowid
            n = 0
            for t, added in db.execute(
                    "SELECT track_id, added_at FROM b.playlist_items WHERE playlist_id = ?"
                    " ORDER BY position, id", (row[0],)).fetchall():
                if t in tracks_here:
                    db.execute("INSERT INTO main.playlist_items (playlist_id, track_id, position, added_at)"
                               " VALUES (?, ?, ?, ?)", (pid, t, n, added))
                    n += 1
    ok = db.execute("PRAGMA main.integrity_check").fetchone()[0]
    db.close()
    print(f"Restored {len(new_folders)} folder(s) and {len(missing)} playlist(s). Integrity: {ok}")
    if no_image:
        print(f"{len(no_image)} cover image(s) weren't on disk, so those tiles use the default:")
        for p in no_image:
            print(f"  {p}")
    print("\nNext: open MusicMgr and sync THIS PC to the USB first, then the other PC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
