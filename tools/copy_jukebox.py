#!/usr/bin/env python3
"""Copy Jukebox cards from one MusicMgr library.db into another.

2026-09-23 - James: "I wanted the Jukebox cards with the artist and 2 songs
to be copied into the Linux library.db" - i.e. carry the board built on the
Windows PC (on the USB stick, MusicMgr/data/library.db) over to the Linux
Mint install, whose library was scanned separately from ~/Music.

Track and artist ids differ between the two databases (each was scanned on
its own), so every card is re-matched by what it *is*, not by id:

1. the track's file path - the last folders + file name
   ("Artist/Album/01 Song.mp3"), which survive the D:\\Music -> ~/Music move;
2. failing that, its title plus artist (the same normalized keys the app
   itself uses: tracks.title_key / artists.name_key).

A card is copied with its genre chip and, if that number is still free, its
original slot number. A card whose tracks are already on the target board is
skipped, so running this twice is harmless. Genre chips missing from the
target are appended. Nothing else in either database is touched.

Safety: the source is copied to a temp folder and read from there (the USB
is never written to - that also folds in any leftover -wal file), and the
target gets a backup next to it before anything is written.

Stdlib only - runs with the system python3 on Mint, no MusicMgr install
needed. CLOSE MUSICMGR before running it.

    python3 copy_jukebox.py --dry-run          # report only
    python3 copy_jukebox.py                    # do it
    python3 copy_jukebox.py --source /media/jrs58/USB/MusicMgr/data/library.db \\
                            --target ~/MusicMgr/data/library.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Optional

GENRES_KEY = "jukebox_genres"
NEXT_SLOT_KEY = "jukebox_next_slot_number"
DEFAULT_GENRE = "Rock"


def default_source() -> Path:
    return Path(f"/media/{os.environ.get('USER', '')}/USB/MusicMgr/data/library.db")


def default_target() -> Path:
    return Path.home() / "MusicMgr" / "data" / "library.db"


# -- matching helpers ---------------------------------------------------------


def path_parts(path: str) -> list[str]:
    """Windows or POSIX path -> lowercased, accent-folded components."""
    folded = unicodedata.normalize("NFC", path or "").lower()
    return [p for p in re.split(r"[\\/]+", folded) if p and not re.fullmatch(r"[a-z]:", p)]


def suffix(parts: list[str], n: int) -> Optional[tuple]:
    return tuple(parts[-n:]) if len(parts) >= n else None


class TargetIndex:
    """Everything needed to find a target track/artist, built once."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.by_suffix: dict[int, dict[tuple, set[int]]] = {3: {}, 2: {}, 1: {}}
        for track_id, path in db.execute("SELECT track_id, path FROM media_files"):
            parts = path_parts(path)
            for n in (3, 2, 1):
                key = suffix(parts, n)
                if key:
                    self.by_suffix[n].setdefault(key, set()).add(track_id)

        self.tracks_by_title: dict[str, list[tuple[int, str, str]]] = {}
        for track_id, title_key, display, album_artist_key in db.execute(
            """SELECT t.id, t.title_key, COALESCE(t.artist_display, ''), COALESCE(a.name_key, '')
               FROM tracks t
               JOIN releases r ON r.id = t.release_id
               LEFT JOIN artists a ON a.id = r.album_artist_id"""
        ):
            self.tracks_by_title.setdefault(title_key or "", []).append(
                (track_id, display.lower().strip(), album_artist_key)
            )

        self.artist_by_key: dict[str, int] = {}
        for artist_id, name_key in db.execute("SELECT id, name_key FROM artists ORDER BY id"):
            self.artist_by_key.setdefault(name_key or "", artist_id)

        self.track_album_artist: dict[int, Optional[int]] = dict(
            db.execute("SELECT t.id, r.album_artist_id FROM tracks t JOIN releases r ON r.id = t.release_id")
        )

    def match_track(self, src_paths: list[str], title_key: str, artist_key: str, display: str) -> tuple[Optional[int], str]:
        """(target track id, how it matched) or (None, reason)."""
        for n in (3, 2, 1):
            for path in src_paths:
                key = suffix(path_parts(path), n)
                hits = self.by_suffix[n].get(key) if key else None
                if hits and len(hits) == 1:
                    return next(iter(hits)), "file"
        candidates = self.tracks_by_title.get(title_key or "", [])
        if len(candidates) == 1:
            return candidates[0][0], "title"
        by_artist = [c for c in candidates if c[2] == artist_key or (display and c[1] == display)]
        if by_artist:
            return by_artist[0][0], "title+artist"
        return None, "not in the target library"


# -- source -------------------------------------------------------------------


def read_source_cards(db: sqlite3.Connection) -> tuple[list[dict], list[str]]:
    cards = []
    for slot_number, artist_name, artist_key, genre, a_id, b_id in db.execute(
        """SELECT j.slot_number, a.name, a.name_key, COALESCE(j.genre, ?), j.side_a_track_id, j.side_b_track_id
           FROM jukebox_slots j JOIN artists a ON a.id = j.artist_id
           ORDER BY j.slot_number""",
        (DEFAULT_GENRE,),
    ):
        sides = {}
        for side, track_id in (("a", a_id), ("b", b_id)):
            if track_id is None:
                sides[side] = None
                continue
            row = db.execute(
                "SELECT title, title_key, COALESCE(artist_display, '') FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()
            if row is None:
                sides[side] = None
                continue
            paths = [p for (p,) in db.execute("SELECT path FROM media_files WHERE track_id = ?", (track_id,))]
            sides[side] = {"title": row[0], "title_key": row[1], "display": row[2].lower().strip(), "paths": paths}
        cards.append({
            "slot_number": slot_number, "artist": artist_name, "artist_key": artist_key,
            "genre": genre, "a": sides["a"], "b": sides["b"],
        })
    genres = read_genres(db)
    return cards, genres


def read_genres(db: sqlite3.Connection) -> list[str]:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (GENRES_KEY,)).fetchone()
    if not row:
        return []
    try:
        value = json.loads(row[0])
        return [g for g in value if isinstance(g, str)] if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def open_source_copy(source: Path, workdir: Path) -> sqlite3.Connection:
    """Copy library.db (+ -wal/-shm if present) to workdir and open that, so
    the original is never written - SQLite folds the -wal into the copy."""
    for suffix_ in ("", "-wal", "-shm"):
        src = Path(str(source) + suffix_)
        if src.exists():
            shutil.copy2(src, workdir / ("library.db" + suffix_))
    return sqlite3.connect(workdir / "library.db")


# -- copy ---------------------------------------------------------------------


def copy_cards(src_cards, src_genres, target: sqlite3.Connection, dry_run: bool) -> dict:
    index = TargetIndex(target)
    used_numbers = {n for (n,) in target.execute("SELECT slot_number FROM jukebox_slots")}
    on_board = set()
    for a_id, b_id in target.execute("SELECT side_a_track_id, side_b_track_id FROM jukebox_slots"):
        on_board.update(t for t in (a_id, b_id) if t is not None)
    next_row = target.execute("SELECT value FROM settings WHERE key = ?", (NEXT_SLOT_KEY,)).fetchone()
    next_number = max(
        int(next_row[0]) if next_row and str(next_row[0]).isdigit() else 1,
        max(used_numbers | {0}) + 1,
        max([c["slot_number"] for c in src_cards] + [0]) + 1,
    )

    report = {"copied": [], "skipped": [], "partial": []}
    for card in src_cards:
        label = f"#{card['slot_number']} {card['artist']}"
        matched = {}
        notes = []
        for side in ("a", "b"):
            info = card[side]
            if info is None:
                matched[side] = None
                continue
            track_id, how = index.match_track(info["paths"], info["title_key"], card["artist_key"], info["display"])
            matched[side] = track_id
            if track_id is None:
                notes.append(f"side {side.upper()} '{info['title']}' {how}")
        found = [t for t in matched.values() if t is not None]
        if not found:
            report["skipped"].append(f"{label}: no song found in this library ({'; '.join(notes)})")
            continue
        if any(t in on_board for t in found):
            report["skipped"].append(f"{label}: already on the board")
            continue

        artist_id = index.artist_by_key.get(card["artist_key"] or "")
        if artist_id is None:
            artist_id = index.track_album_artist.get(found[0])
        if artist_id is None:
            report["skipped"].append(f"{label}: artist '{card['artist']}' not in this library")
            continue

        number = card["slot_number"]
        if number in used_numbers:
            number = next_number
            next_number += 1
        used_numbers.add(number)
        on_board.update(found)

        if not dry_run:
            target.execute(
                """INSERT INTO jukebox_slots (slot_number, artist_id, side_a_track_id, side_b_track_id, genre, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (number, artist_id, matched["a"], matched["b"], card["genre"], time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        titles = " / ".join(card[s]["title"] for s in ("a", "b") if card[s] and matched[s])
        line = f"#{number} {card['artist']} [{card['genre']}]: {titles}"
        if notes:
            report["partial"].append(f"{line}  (missing: {'; '.join(notes)})")
        else:
            report["copied"].append(line)

    # genre chips: keep the target's order, append any the source has that it doesn't
    target_genres = read_genres(target)
    card_genres = [c["genre"] for c in src_cards]
    wanted = target_genres or list(src_genres)
    for g in list(src_genres) + card_genres:
        if g and g not in wanted:
            wanted.append(g)
    report["genres_added"] = [g for g in wanted if g not in target_genres]

    if not dry_run:
        if wanted != target_genres:
            target.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (GENRES_KEY, json.dumps(wanted)),
            )
        target.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (NEXT_SLOT_KEY, str(max(next_number, max(used_numbers | {0}) + 1))),
        )
        target.commit()
    return report


def backup_target(target: Path) -> Path:
    dest = target.with_name(f"library-before-jukebox-copy-{time.strftime('%Y%m%d-%H%M%S')}.db")
    src = sqlite3.connect(target)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=default_source(), help="library.db to copy cards FROM")
    parser.add_argument("--target", type=Path, default=default_target(), help="library.db to copy cards INTO")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied; change nothing")
    args = parser.parse_args(argv)
    source, target = args.source.expanduser(), args.target.expanduser()

    for label, path in (("Source", source), ("Target", target)):
        if not path.is_file():
            print(f"{label} database not found: {path}", file=sys.stderr)
            return 1
    if source.resolve() == target.resolve():
        print("Source and target are the same file.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        src_db = open_source_copy(source, Path(tmp))
        try:
            cards, genres = read_source_cards(src_db)
        finally:
            src_db.close()
    print(f"Source: {len(cards)} jukebox card(s) in {source}")
    if not cards:
        return 0

    tgt = sqlite3.connect(target, timeout=5)
    try:
        tracks = tgt.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        if tracks == 0:
            print("The target library has no tracks yet - open MusicMgr, add ~/Music under "
                  "Settings > Watched folders, Scan now, close MusicMgr, then run this again.", file=sys.stderr)
            return 1
        if not args.dry_run:
            print(f"Backup of target: {backup_target(target)}")
        report = copy_cards(cards, genres, tgt, args.dry_run)
    except sqlite3.OperationalError as exc:
        print(f"Couldn't write the target database ({exc}). Is MusicMgr still open? Close it and retry.",
              file=sys.stderr)
        return 1
    finally:
        tgt.close()

    verb = "Would copy" if args.dry_run else "Copied"
    print(f"\n{verb} {len(report['copied'])} card(s):")
    for line in report["copied"]:
        print("  " + line)
    if report["partial"]:
        print(f"\n{verb} {len(report['partial'])} card(s) with only one side found:")
        for line in report["partial"]:
            print("  " + line)
    if report["skipped"]:
        print(f"\nSkipped {len(report['skipped'])}:")
        for line in report["skipped"]:
            print("  " + line)
    if report["genres_added"]:
        print(f"\nGenre chips {'to add' if args.dry_run else 'added'}: {', '.join(report['genres_added'])}")
    if args.dry_run:
        print("\nDry run - nothing was changed. Run again without --dry-run to copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
