"""Remove one song from the library completely - the song, its file rows,
its album and artist if nothing else uses them - even when it has plays,
playlist places, chart matches or a jukebox side.

2026-10-30 - James couldn't get rid of the artist "2000-09". An untagged
file, D:\\Music\\Hot Country\\2000-09\\2008\\Kenny Chesney & Mac McAnally
   Down The Road.mp3, came in with its folder names as artist ("2000-09")
and album ("2008"). The file was later renamed "Kenny Chesney & Mac
McAnally - Down The Road.mp3" and came back as a new, correct song. The old
one had played twice, so Settings' missing-file cleanup keeps it ("1 track
with no file left is kept (1 with play history)") and the artist page has
no way to remove it.

With --to NEW_ID the old song's plays, playlist places, chart matches and
jukebox sides move to that song first (the app's carry_history rules);
without it they are deleted along with the song.

Then: the album is removed if it is left empty (and its master if that
empties too), and every artist the song credited is removed if nothing
else points at it (credits, album artist, jukebox card, video). An
artist photo file of a removed artist is deleted too. Playlists that
changed get updated_at bumped so USB sync carries the change.

Stdlib only. CLOSE MUSICMGR FIRST. Without --apply everything is done and
then rolled back, so the report is exact. --apply writes a backup to
data\\backups\\library-before-remove-track-<time>.db first.

    python tools/remove_track.py --db D:\\MusicMgr\\data\\library.db --track 107835 --to 105869
    python tools/remove_track.py --db D:\\MusicMgr\\data\\library.db --track 107835 --to 105869 --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


def one(db, sql, *args):
    row = db.execute(sql, args).fetchone()
    return row[0] if row else None


def album_artist_of(db, track_id):
    return one(db, "SELECT r.album_artist_id FROM tracks t JOIN releases r ON r.id = t.release_id"
                   " WHERE t.id = ?", track_id)


def move_history(db, old, new, out):
    o = db.execute("SELECT rating, play_count, skip_count, last_played_at, added_at FROM tracks"
                   " WHERE id = ?", (old,)).fetchone()
    n = db.execute("SELECT rating, play_count, skip_count, last_played_at, added_at FROM tracks"
                   " WHERE id = ?", (new,)).fetchone()
    rating = n[0] if n[0] is not None else o[0]
    last = max([x for x in (o[3], n[3]) if x], default=None)
    added = min([x for x in (o[4], n[4]) if x], default=None)
    db.execute("UPDATE tracks SET rating = ?, play_count = ?, skip_count = ?, last_played_at = ?,"
               " added_at = ? WHERE id = ?",
               (rating, (n[1] or 0) + (o[1] or 0), (n[2] or 0) + (o[2] or 0), last, added, new))
    plays = db.execute("UPDATE play_events SET track_id = ? WHERE track_id = ?", (new, old)).rowcount
    charts = db.execute("UPDATE chart_entries SET track_id = ? WHERE track_id = ?", (new, old)).rowcount
    both = {r[0] for r in db.execute("SELECT playlist_id FROM playlist_items WHERE track_id = ?", (new,))}
    changed_pl = set()
    places = 0
    for item_id, pl in db.execute("SELECT id, playlist_id FROM playlist_items WHERE track_id = ?",
                                  (old,)).fetchall():
        if pl in both:   # already holds the good copy: drop the old place
            db.execute("DELETE FROM playlist_items WHERE id = ?", (item_id,))
        else:
            db.execute("UPDATE playlist_items SET track_id = ? WHERE id = ?", (new, item_id))
        changed_pl.add(pl)
        places += 1
    new_artist = album_artist_of(db, new)
    sides = 0
    for sid, a, b in db.execute("SELECT id, side_a_track_id, side_b_track_id FROM jukebox_slots"
                                " WHERE side_a_track_id = ? OR side_b_track_id = ?", (old, old)).fetchall():
        a = new if a == old else a
        b = new if b == old else b
        other = b if a == new else a
        other_artist = album_artist_of(db, other) if other else None
        db.execute("UPDATE jukebox_slots SET side_a_track_id = ?, side_b_track_id = ? WHERE id = ?",
                   (a, b, sid))
        if new_artist and other_artist in (None, new_artist):
            db.execute("UPDATE jukebox_slots SET artist_id = ? WHERE id = ?", (new_artist, sid))
        sides += 1
    out(f"  moved to {new}: {plays} plays, {places} playlist places, {charts} chart matches,"
        f" {sides} jukebox cards")
    return changed_pl


def artist_in_use(db, aid):
    return any(one(db, sql, aid) for sql in (
        "SELECT 1 FROM credits WHERE artist_id = ? LIMIT 1",
        "SELECT 1 FROM releases WHERE album_artist_id = ? LIMIT 1",
        "SELECT 1 FROM jukebox_slots WHERE artist_id = ? LIMIT 1",
        "SELECT 1 FROM videos WHERE artist_id = ? LIMIT 1",
    ))


def remove(db, track_id, to_id, out):
    t = db.execute("SELECT id, title, artist_display, release_id FROM tracks WHERE id = ?",
                   (track_id,)).fetchone()
    if t is None:
        raise SystemExit(f"no song with id {track_id}")
    out(f"song {t[0]}: {t[1]!r} - {t[2]}")
    for (path, missing) in db.execute("SELECT path, is_missing FROM media_files WHERE track_id = ?",
                                      (track_id,)):
        out(f"  file: {path}{'  (missing)' if missing else '  (PRESENT - stays on disk)'}")
    changed_pl = set()
    if to_id is not None:
        n = db.execute("SELECT id, title, artist_display FROM tracks WHERE id = ?", (to_id,)).fetchone()
        if n is None or to_id == track_id:
            raise SystemExit(f"--to {to_id}: no such other song")
        out(f"  history goes to {n[0]}: {n[1]!r} - {n[2]}")
        changed_pl = move_history(db, track_id, to_id, out)
    else:
        changed_pl = {r[0] for r in db.execute(
            "SELECT DISTINCT playlist_id FROM playlist_items WHERE track_id = ?", (track_id,))}
        db.execute("UPDATE jukebox_slots SET side_a_track_id = NULL WHERE side_a_track_id = ?", (track_id,))
        db.execute("UPDATE jukebox_slots SET side_b_track_id = NULL WHERE side_b_track_id = ?", (track_id,))
        db.execute("DELETE FROM jukebox_slots WHERE side_a_track_id IS NULL AND side_b_track_id IS NULL")

    artists = {r[0] for r in db.execute("SELECT artist_id FROM credits WHERE track_id = ?", (track_id,))}
    release_id = t[3]
    rel = db.execute("SELECT album_artist_id, master_id FROM releases WHERE id = ?", (release_id,)).fetchone()
    db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))   # cascades files, credits, plays, places
    out("  song removed")

    if rel is not None and not one(db, "SELECT 1 FROM tracks WHERE release_id = ? LIMIT 1", release_id):
        album_artist, master_id = rel
        if album_artist:
            artists.add(album_artist)
        artists |= {r[0] for r in db.execute("SELECT artist_id FROM credits WHERE release_id = ?",
                                             (release_id,))}
        db.execute("DELETE FROM releases WHERE id = ?", (release_id,))
        out(f"  empty album {release_id} removed")
        if master_id is not None:
            rest = one(db, "SELECT id FROM releases WHERE master_id = ? LIMIT 1", master_id)
            if rest is None:
                db.execute("DELETE FROM masters WHERE id = ?", (master_id,))
                out(f"  empty master {master_id} removed")
            else:
                db.execute("UPDATE masters SET main_release_id = ? WHERE id = ?"
                           " AND (main_release_id IS NULL OR main_release_id = ?)",
                           (rest, master_id, release_id))

    photos = []
    for aid in sorted(artists):
        name, image = db.execute("SELECT name, image_path FROM artists WHERE id = ?", (aid,)).fetchone()
        if artist_in_use(db, aid):
            out(f"  artist {aid} {name!r} kept (still used)")
            continue
        db.execute("DELETE FROM artists WHERE id = ?", (aid,))
        out(f"  artist {aid} {name!r} removed")
        if image:
            photos.append(image)

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for pl in changed_pl:
        db.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, pl))
    if changed_pl:
        out(f"  {len(changed_pl)} playlist(s) marked changed for USB sync")
    return photos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--track", required=True, type=int, help="id of the song to remove")
    ap.add_argument("--to", type=int, help="id of the song that gets its history")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    db = sqlite3.connect(args.db, isolation_level=None, timeout=2)
    db.execute("PRAGMA locking_mode = EXCLUSIVE")   # also lets WAL work with no -shm
    db.execute("PRAGMA foreign_keys = ON")
    try:
        db.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as e:
        print(f"can't lock the library ({e}) - close MusicMgr first", file=sys.stderr)
        return 1
    db.execute("ROLLBACK")

    if args.apply:
        backups = args.db.parent / "backups"
        backups.mkdir(exist_ok=True)
        dest = backups / f"library-before-remove-track-{time.strftime('%Y%m%d-%H%M%S')}.db"
        with sqlite3.connect(dest) as b:
            db.backup(b)
        print(f"backup: {dest}")

    db.execute("BEGIN IMMEDIATE")
    try:
        photos = remove(db, args.track, args.to, print)
        bad = db.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign key problems: {bad[:5]}")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    if not args.apply:
        db.execute("ROLLBACK")
        print("dry run - nothing changed (add --apply)")
        return 0
    db.execute("COMMIT")
    for p in photos:
        f = Path(p) if Path(p).is_absolute() else args.db.parent / p
        if f.is_file():
            f.unlink()
            print(f"  photo deleted: {f}")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
