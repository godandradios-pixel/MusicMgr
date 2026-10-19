"""Point chart positions held by file-less "ghost" songs at the real song,
and give songs back the numbers an old scanner cut off their titles.

2026-10-19 - James approved fixing "113 chart positions point at songs that
no longer have a file". Two old bugs together:

- Before 2026-10-16 (faadbf4) the scanner took a song's own number for a
  track number and cut it off the title, even when the title tag had it:
  "6 Inch" became "Inch", "99 Luftballons" "Luftballons", "777-9311"
  "9311". The fix only helps files read again, and an unchanged file never
  is, so those titles are still short.
- A rescan that found a file again under a new song (renamed, moved,
  retagged, or retitled by that bug) left the old song behind with no file.
  purge_orphaned_tracks hands such a song's history to its live copy only
  when that copy has the same title and artist and is on the same album or
  one by the same album artist. With a cut-off title, or the copy filed on
  another album, it never matched, so the old song stayed because chart
  positions still pointed at it. Charts then counted those songs as owned
  and "Play owned" couldn't play them.

This script, in order:

1. Titles: for each present song whose file's title tag is its library
   title with a number in front ("6 Inch" vs "Inch"), puts the tag's title
   back. Only files whose name holds the title after a number are read.
   Chart positions that were unmatched because of the short title get
   matched again.
2. Ghosts: for each song with no file at all that still has history
   (chart positions, plays, playlist places, jukebox sides), finds its live
   copy: same title and performer, not a video. If there's more than one, it
   prefers one on an album of the same name, then one by the same album
   artist. Everything moves over with the app's own carry_history, and the
   ghost is removed. A ghost with no single live copy is listed and left
   alone.
3. Removes albums left with no songs.

Uses the app's code, so run it with the repo's Python:

    venv\\Scripts\\python tools\\fix_ghost_chart_tracks.py --db D:\\MusicMgr\\data\\library.db
    venv\\Scripts\\python tools\\fix_ghost_chart_tracks.py --db D:\\MusicMgr\\data\\library.db --apply

CLOSE MUSICMGR FIRST. The first line only reports. --apply writes a backup
next to the database (data\\backups\\library-before-ghost-fix-<time>.db)
before changing anything.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NUMBER_PREFIX = re.compile(r"^\d+(?:[-\s]\d+)*[-\s]$")


def number_cut(tag_title: Optional[str], title: Optional[str]) -> bool:
    """True when `tag_title` is `title` with a number in front of it:
    ("6 Inch", "Inch"), ("777-9311", "9311"), ("5-10-15 Hours", "10-15 Hours")."""
    if not tag_title or not title or tag_title == title or not tag_title.endswith(title):
        return False
    return bool(NUMBER_PREFIX.match(tag_title[: len(tag_title) - len(title)]))


def _may_be_cut(path: str, title: str) -> bool:
    """Cheap filter before reading a file: its name holds the title right
    after a number ("6 Inch.mp3", "Artist - 777-9311.mp3")."""
    stem = Path(path.replace("\\", "/")).stem
    i = stem.rfind(title)
    if i <= 0 or stem[i - 1] not in " -":
        return False
    m = re.search(r"(\d+)$", stem[:i].rstrip(" -"))
    # "01 Song" / "1-01 Song": a zero-padded number is the track number
    return bool(m) and not m.group(1).startswith("0")


def fix_titles(session, read_title: Callable[[str], Optional[str]]) -> list[tuple]:
    from sqlalchemy import select

    from musicmgr import config
    from musicmgr.db.models import MediaFile, Track
    from musicmgr.services.matching import normalize

    video = {e.lower() for e in config.VIDEO_EXTENSIONS}
    fixed = []
    rows = session.execute(
        select(Track, MediaFile.path).join(MediaFile, MediaFile.track_id == Track.id)
        .where(MediaFile.is_missing.is_(False)).order_by(Track.id, MediaFile.id)
    ).all()
    seen = set()
    for track, path in rows:
        if track.id in seen or not track.title:
            continue
        seen.add(track.id)
        if Path(path).suffix.lower() in video or not _may_be_cut(path, track.title):
            continue
        tag = read_title(path)
        if number_cut(tag, track.title):
            fixed.append((track.id, track.title, tag, path))
            track.title = tag
            track.title_key = normalize(tag)
    session.flush()
    return fixed


def rematch_fixed_titles(session, fixed: list[tuple]) -> int:
    """Chart positions left unmatched because the song's title was short
    ("99 Luftballons" in a chart, "Luftballons" in the library) get another
    try now. Positions fixed by hand are never touched."""
    from sqlalchemy import select

    from musicmgr.db.models import ChartEntry
    from musicmgr.services import charts as chart_svc
    from musicmgr.services.matching import normalize

    keys = {normalize(new) for _tid, _old, new, _path in fixed}
    if not keys:
        return 0
    matched = 0
    for entry in session.scalars(select(ChartEntry).where(
            ChartEntry.track_id.is_(None), ChartEntry.match_locked.is_(False),
            ChartEntry.title_key.in_(keys))):
        if chart_svc.match_entry(session, entry):
            matched += 1
    session.flush()
    return matched


def _artist_key(name: Optional[str]) -> str:
    from musicmgr.services.matching import normalize

    key = re.sub(r"\b(the|and)\b", " ", normalize(name or ""))
    return re.sub(r"\s+", " ", key).strip()


def ghost_moves(session) -> tuple[dict[int, int], list[tuple]]:
    """{ghost id: live copy id}, plus the ghosts left alone and why."""
    from sqlalchemy import exists, or_, select

    from musicmgr import config
    from musicmgr.db.models import (ChartEntry, JukeboxSlot, MediaFile, PlayEvent,
                                    PlaylistItem, Release, Track)

    video = {e.lower() for e in config.VIDEO_EXTENSIONS}
    no_file = ~exists().where(MediaFile.track_id == Track.id)
    has_history = or_(
        exists().where(ChartEntry.track_id == Track.id),
        exists().where(PlaylistItem.track_id == Track.id),
        exists().where(PlayEvent.track_id == Track.id),
        exists().where(or_(JukeboxSlot.side_a_track_id == Track.id,
                           JukeboxSlot.side_b_track_id == Track.id)),
    )
    ghosts = session.scalars(select(Track).where(no_file, has_history).order_by(Track.id)).all()
    moves, left = {}, []
    for ghost in ghosts:
        live = []
        for track, path in session.execute(
            select(Track, MediaFile.path).join(MediaFile, MediaFile.track_id == Track.id)
            .where(Track.title_key == ghost.title_key, Track.id != ghost.id,
                   MediaFile.is_missing.is_(False))
        ):
            if Path(path).suffix.lower() in video:
                continue
            if _artist_key(track.artist_display) != _artist_key(ghost.artist_display):
                continue
            if track not in live:
                live.append(track)
        pick = None
        if len(live) == 1:
            pick = live[0]
        elif live:
            g_rel = session.get(Release, ghost.release_id) if ghost.release_id else None
            for test in (
                lambda t: g_rel is not None and t.release is not None
                and (t.release.title or "").casefold() == (g_rel.title or "").casefold(),
                lambda t: g_rel is not None and g_rel.album_artist_id is not None
                and t.release is not None and t.release.album_artist_id == g_rel.album_artist_id,
            ):
                hits = [t for t in live if test(t)]
                if len(hits) == 1:
                    pick = hits[0]
                    break
        if pick is None:
            left.append((ghost.id, ghost.title, ghost.artist_display,
                         "no live copy" if not live else f"{len(live)} live copies"))
        else:
            moves[ghost.id] = pick.id
    return moves, left


def _counts(session, track_id: int) -> str:
    from sqlalchemy import func, select

    from musicmgr.db.models import ChartEntry, PlayEvent, PlaylistItem

    c = session.scalar(select(func.count()).select_from(ChartEntry).where(ChartEntry.track_id == track_id))
    p = session.scalar(select(func.count()).select_from(PlayEvent).where(PlayEvent.track_id == track_id))
    pl = session.scalar(select(func.count()).select_from(PlaylistItem).where(PlaylistItem.track_id == track_id))
    return f"{c} chart, {p} plays, {pl} playlist"


def run(session, read_title: Callable[[str], Optional[str]], out=print) -> dict:
    from musicmgr.db.models import Track
    from musicmgr.services import scanner

    titles = fix_titles(session, read_title)
    out(f"Titles with their number put back: {len(titles)}")
    for tid, old, new, path in titles:
        out(f"  {tid}: {old!r} -> {new!r}   ({path})")
    rematched = rematch_fixed_titles(session, titles)
    out(f"Unmatched chart positions now matched to those songs: {rematched}")

    moves, left = ghost_moves(session)
    out(f"\nFile-less songs handed to their live copy: {len(moves)}")
    for gid, lid in moves.items():
        g, live = session.get(Track, gid), session.get(Track, lid)
        out(f"  {gid} {g.title!r} - {g.artist_display}  ({_counts(session, gid)})")
        out(f"      -> {lid} on {live.release.title if live.release else '?'!r}")
    if left:
        out(f"\nLeft alone: {len(left)}")
        for gid, title, artist, why in left:
            out(f"  {gid} {title!r} - {artist}: {why}")
    done = scanner.carry_history(session, moves)
    emptied = scanner.purge_empty_releases(session)
    session.flush()
    out(f"\nMoved {len(done)}; albums left empty and removed: {emptied}")
    return {"titles": titles, "rematched": rematched, "moves": done, "left": left,
            "emptied": emptied}


def _tag_title(path: str) -> Optional[str]:
    from musicmgr.services import scanner

    try:
        return scanner.read_tags(Path(path)).title
    except Exception as exc:  # unreadable file: leave its title alone
        print(f"  (can't read {path}: {exc})")
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, type=Path, help="path to library.db")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    args = ap.parse_args(argv)
    db_path = args.db.resolve()
    if not db_path.is_file():
        print(f"No database at {db_path}")
        return 1

    if args.apply:
        backups = db_path.parent / "backups"
        backups.mkdir(exist_ok=True)
        dest = backups / f"library-before-ghost-fix-{time.strftime('%Y%m%d-%H%M%S')}.db"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        print(f"Backup written: {dest}\n")

    from musicmgr.db import session as dbs

    dbs.init_engine(db_path=db_path)
    session = dbs.new_session()
    try:
        run(session, _tag_title)
        if args.apply:
            session.commit()
            print("\nDone. Start MusicMgr; Charts' coverage now counts only songs you can play.")
        else:
            session.rollback()
            print("\nReport only - nothing changed. Run again with --apply to make these changes.")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
