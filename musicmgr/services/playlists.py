"""Manual, smart, and playback ("Most Played") playlists.

A smart playlist stores JSON rules and resolves to tracks on demand:

    {
      "match": "all",                     # or "any"
      "limit": 100,
      "order_by": "play_count_desc",
      "rules": [
        {"field": "genre",      "op": "is",       "value": "Soul"},
        {"field": "year",       "op": "between",  "value": [1965, 1975]},
        {"field": "play_count", "op": "gte",      "value": 3}
      ]
    }
"""

from __future__ import annotations

import datetime as dt
import json
import random
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    Credit,
    Genre,
    MediaFile,
    PlayEvent,
    Playlist,
    PlaylistFolder,
    PlaylistItem,
    Release,
    Style,
    Track,
    release_genres,
    release_styles,
)
from .matching import normalize

ORDERINGS = {
    "title": Track.title,
    "artist": Track.artist_display,
    "added_desc": Track.added_at.desc(),
    "play_count_desc": Track.play_count.desc(),
    "last_played_desc": Track.last_played_at.desc(),
    "rating_desc": Track.rating.desc(),
    "random": func.random(),
}

#: how many days back each playback window looks; None = no lower bound.
#: Moved here from services/charts.py (2026-08-30) along with the built-in
#: "Most Played" playlists that use it - see the "playback playlists" section
#: below.
PLAYBACK_WINDOWS = {
    "today": 1,
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
    "all": None,
}

#: position count of each built-in "Most Played" playlist, same as the old
#: playback charts' Chart.size.
PLAYBACK_PLAYLIST_LIMIT = 100


# --------------------------------------------------------------------------
# manual playlists
# --------------------------------------------------------------------------


def create_playlist(
    session: Session,
    name: str,
    description: str = "",
    kind: str = Playlist.KIND_MANUAL,
    folder_id: Optional[int] = None,
) -> Playlist:
    playlist = Playlist(
        name=name, description=description or None, kind=kind, folder_id=folder_id
    )
    session.add(playlist)
    session.flush()
    return playlist


def rename_playlist(session: Session, playlist_id: int, name: str) -> None:
    playlist = session.get(Playlist, playlist_id)
    if playlist is not None and name.strip():
        playlist.name = name.strip()


def move_playlist(session: Session, playlist_id: int, folder_id: Optional[int]) -> None:
    playlist = session.get(Playlist, playlist_id)
    if playlist is not None:
        playlist.folder_id = folder_id


def list_playlists(session: Session) -> list[Playlist]:
    return list(
        session.scalars(
            select(Playlist).order_by(
                Playlist.is_pinned.desc(), func.lower(Playlist.name)
            )
        )
    )


# --------------------------------------------------------------------------
# playlist folders
# --------------------------------------------------------------------------


def create_folder(
    session: Session, name: str, parent_id: Optional[int] = None
) -> PlaylistFolder:
    folder = PlaylistFolder(name=name.strip() or "New folder", parent_id=parent_id)
    session.add(folder)
    session.flush()
    return folder


def rename_folder(session: Session, folder_id: int, name: str) -> None:
    folder = session.get(PlaylistFolder, folder_id)
    if folder is not None and name.strip():
        folder.name = name.strip()


def list_folders(session: Session) -> list[PlaylistFolder]:
    return list(
        session.scalars(select(PlaylistFolder).order_by(func.lower(PlaylistFolder.name)))
    )


def folder_and_descendant_ids(session: Session, folder_id: int) -> set[int]:
    """`folder_id` plus every folder nested under it, direct or not - used to
    keep a folder from being moved inside its own subtree."""
    by_parent: dict[Optional[int], list[int]] = {}
    for fid, parent_id in session.execute(
        select(PlaylistFolder.id, PlaylistFolder.parent_id)
    ):
        by_parent.setdefault(parent_id, []).append(fid)
    ids = {folder_id}
    stack = [folder_id]
    while stack:
        for child_id in by_parent.get(stack.pop(), []):
            if child_id not in ids:
                ids.add(child_id)
                stack.append(child_id)
    return ids


def move_folder(session: Session, folder_id: int, new_parent_id: Optional[int]) -> None:
    if new_parent_id is not None and new_parent_id in folder_and_descendant_ids(
        session, folder_id
    ):
        raise ValueError(
            "a folder can't be moved inside itself or one of its own subfolders"
        )
    folder = session.get(PlaylistFolder, folder_id)
    if folder is not None:
        folder.parent_id = new_parent_id


def delete_folder(session: Session, folder_id: int) -> None:
    """Deletes just the folder. Its playlists and subfolders move up to its
    own parent (or the top level) rather than being deleted with it - a
    folder is organisation, never a container things should die with."""
    folder = session.get(PlaylistFolder, folder_id)
    if folder is None:
        return
    parent_id = folder.parent_id
    for child in session.scalars(
        select(PlaylistFolder).where(PlaylistFolder.parent_id == folder_id)
    ):
        child.parent_id = parent_id
    for playlist in session.scalars(
        select(Playlist).where(Playlist.folder_id == folder_id)
    ):
        playlist.folder_id = parent_id
    # flush the reparenting before deleting the folder - otherwise SQLAlchemy's
    # own relationship bookkeeping can re-query its (now stale) children and
    # null their FK out from under the reassignment above
    session.flush()
    session.delete(folder)
    session.flush()


def folder_counts(session: Session, folder_id: int) -> tuple[int, int]:
    """(direct playlists, direct subfolders) - not recursive; this backs the
    one-line summary shown when a folder itself is selected."""
    playlists = (
        session.scalar(
            select(func.count(Playlist.id)).where(Playlist.folder_id == folder_id)
        )
        or 0
    )
    subfolders = (
        session.scalar(
            select(func.count(PlaylistFolder.id)).where(
                PlaylistFolder.parent_id == folder_id
            )
        )
        or 0
    )
    return playlists, subfolders


def add_tracks(
    session: Session, playlist_id: int, track_ids: Iterable[int]
) -> int:
    start = (
        session.scalar(
            select(func.coalesce(func.max(PlaylistItem.position), -1)).where(
                PlaylistItem.playlist_id == playlist_id
            )
        )
        or -1
    ) + 1
    added = 0
    for offset, track_id in enumerate(track_ids):
        session.add(
            PlaylistItem(
                playlist_id=playlist_id, track_id=track_id, position=start + offset
            )
        )
        added += 1
    session.flush()
    return added


def remove_item(session: Session, item_id: int) -> None:
    item = session.get(PlaylistItem, item_id)
    if item is not None:
        session.delete(item)
        session.flush()


def reorder(session: Session, playlist_id: int, ordered_item_ids: Sequence[int]) -> None:
    for pos, item_id in enumerate(ordered_item_ids):
        item = session.get(PlaylistItem, item_id)
        if item is not None and item.playlist_id == playlist_id:
            item.position = pos
    session.flush()


def playlist_tracks(session: Session, playlist_id: int) -> list[Track]:
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        return []
    if playlist.kind == Playlist.KIND_SMART and playlist.rules:
        return resolve_smart(session, playlist.rules)
    if playlist.kind == Playlist.KIND_PLAYBACK:
        return [t for t, _plays, _ms in resolve_playback(session, playlist)]
    stmt = (
        select(Track)
        .options(selectinload(Track.files), selectinload(Track.release))
        .join(PlaylistItem, PlaylistItem.track_id == Track.id)
        .where(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position)
    )
    return list(session.scalars(stmt).unique())


def playlist_duration_ms(session: Session, playlist_id: int) -> int:
    return sum(t.duration_ms or 0 for t in playlist_tracks(session, playlist_id))


# --------------------------------------------------------------------------
# smart playlists
# --------------------------------------------------------------------------


def _rule_clause(field: str, op: str, value: Any):
    numeric_fields = {
        "year": Release.year,
        "play_count": Track.play_count,
        "rating": Track.rating,
        "bpm": Track.bpm,
        "duration_ms": Track.duration_ms,
    }
    text_fields = {
        "title": Track.title_key,
        "artist": Track.artist_display,
        "album": Release.title_key,
    }

    if field in numeric_fields:
        col = numeric_fields[field]
        if op in ("is", "eq"):
            return col == value
        if op == "gte":
            return col >= value
        if op == "lte":
            return col <= value
        if op == "gt":
            return col > value
        if op == "lt":
            return col < value
        if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
            return and_(col >= value[0], col <= value[1])
        raise ValueError(f"unsupported numeric op {op!r}")

    if field in text_fields:
        col = text_fields[field]
        needle = normalize(value) if field != "artist" else str(value)
        if op in ("is", "eq"):
            return func.lower(col) == needle.lower()
        if op == "contains":
            return col.ilike(f"%{needle}%")
        if op == "startswith":
            return col.ilike(f"{needle}%")
        raise ValueError(f"unsupported text op {op!r}")

    if field == "genre":
        sub = (
            select(release_genres.c.release_id)
            .join(Genre, Genre.id == release_genres.c.genre_id)
            .where(func.lower(Genre.name) == str(value).lower())
        )
        clause = Release.id.in_(sub)
        return clause if op in ("is", "eq", "contains") else ~clause

    if field == "style":
        sub = (
            select(release_styles.c.release_id)
            .join(Style, Style.id == release_styles.c.style_id)
            .where(func.lower(Style.name) == str(value).lower())
        )
        clause = Release.id.in_(sub)
        return clause if op in ("is", "eq", "contains") else ~clause

    if field == "played_within_days":
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(value))
        return Track.last_played_at >= since

    if field == "not_played_within_days":
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(value))
        return or_(Track.last_played_at.is_(None), Track.last_played_at < since)

    raise ValueError(f"unknown smart-playlist field {field!r}")


def resolve_smart(session: Session, rules_json: str) -> list[Track]:
    spec = json.loads(rules_json) if isinstance(rules_json, str) else rules_json
    clauses = [
        _rule_clause(r["field"], r.get("op", "is"), r.get("value"))
        for r in spec.get("rules", [])
    ]
    combiner = and_ if spec.get("match", "all") == "all" else or_

    stmt = (
        select(Track)
        .options(selectinload(Track.files), selectinload(Track.release))
        .join(Release, Release.id == Track.release_id)
    )
    if clauses:
        stmt = stmt.where(combiner(*clauses))
    order = ORDERINGS.get(spec.get("order_by", "title"), Track.title)
    stmt = stmt.order_by(order).limit(int(spec.get("limit", 500)))
    return list(session.scalars(stmt).unique())


def create_smart_playlist(
    session: Session,
    name: str,
    spec: dict,
    description: str = "",
    folder_id: Optional[int] = None,
) -> Playlist:
    resolve_smart(session, json.dumps(spec))  # validate before saving
    playlist = Playlist(
        name=name,
        description=description or None,
        kind=Playlist.KIND_SMART,
        rules=json.dumps(spec, indent=2),
        folder_id=folder_id,
    )
    session.add(playlist)
    session.flush()
    return playlist


DEFAULT_SMART_PLAYLISTS = [
    (
        "Recently Added",
        {"match": "all", "rules": [], "order_by": "added_desc", "limit": 100},
        "The 100 newest tracks in your library",
    ),
    (
        "Heavy Rotation",
        {
            "match": "all",
            "rules": [{"field": "play_count", "op": "gte", "value": 3}],
            "order_by": "play_count_desc",
            "limit": 100,
        },
        "Anything you have played three times or more",
    ),
    (
        "Forgotten Favorites",
        {
            "match": "all",
            "rules": [
                {"field": "play_count", "op": "gte", "value": 2},
                {"field": "not_played_within_days", "op": "is", "value": 90},
            ],
            "order_by": "play_count_desc",
            "limit": 100,
        },
        "Loved once, untouched for three months",
    ),
]

#: Renamed 2026-09-17 (James: "remove any british english words. For
#: example, Forgotten Favourites") - anyone who already has the old
#: spelling gets it renamed in place the next time this runs, rather than
#: the exists-check loop below (which only matches by exact name, so it
#: has no way to know the old and new spellings are the same playlist)
#: leaving that old row orphaned next to a freshly created duplicate.
_RENAMED_DEFAULTS = {"Forgotten Favourites": "Forgotten Favorites"}


def ensure_default_playlists(session: Session) -> None:
    for old_name, new_name in _RENAMED_DEFAULTS.items():
        old = session.scalar(select(Playlist).where(Playlist.name == old_name))
        already_exists = session.scalar(select(Playlist).where(Playlist.name == new_name))
        if old is not None and already_exists is None:
            old.name = new_name
    for name, spec, desc in DEFAULT_SMART_PLAYLISTS:
        exists = session.scalar(select(Playlist).where(Playlist.name == name))
        if exists is None:
            create_smart_playlist(session, name, spec, desc)


# --------------------------------------------------------------------------
# playback playlists (derived from play history; moved from
# services/charts.py on 2026-08-30 - see Playlist.KIND_PLAYBACK)
# --------------------------------------------------------------------------


def playback_top_tracks(
    session: Session,
    window: str = "week",
    limit: int = 100,
    min_ms: int = 30_000,
) -> list[tuple[Track, int, int]]:
    """Top tracks by play count. Returns (track, plays, total_ms).

    A "play" only counts when at least `min_ms` was heard, so skipping
    through a playlist does not distort the list.
    """
    days = PLAYBACK_WINDOWS.get(window, 7)
    stmt = (
        select(
            Track,
            func.count(PlayEvent.id).label("plays"),
            func.sum(PlayEvent.ms_played).label("total_ms"),
        )
        .join(PlayEvent, PlayEvent.track_id == Track.id)
        .where(PlayEvent.ms_played >= min_ms)
        .group_by(Track.id)
        .order_by(func.count(PlayEvent.id).desc(), func.sum(PlayEvent.ms_played).desc())
        .limit(limit)
    )
    if days is not None:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        stmt = stmt.where(PlayEvent.started_at >= since)
    return [(row[0], row[1], row[2] or 0) for row in session.execute(stmt)]


def playback_movers(
    session: Session, window_days: int = 7, limit: int = 25
) -> list[tuple[Track, int, int, int]]:
    """Biggest position changes between this window and the previous one.

    Returns (track, current_rank, previous_rank_or_0, delta).
    """
    now = dt.datetime.now(dt.timezone.utc)
    cur_start = now - dt.timedelta(days=window_days)
    prev_start = now - dt.timedelta(days=window_days * 2)

    def ranked(start, end) -> dict[int, int]:
        stmt = (
            select(Track.id, func.count(PlayEvent.id).label("plays"))
            .join(PlayEvent, PlayEvent.track_id == Track.id)
            .where(PlayEvent.started_at >= start, PlayEvent.started_at < end)
            .group_by(Track.id)
            .order_by(func.count(PlayEvent.id).desc())
        )
        return {row[0]: idx for idx, row in enumerate(session.execute(stmt), start=1)}

    current = ranked(cur_start, now)
    previous = ranked(prev_start, cur_start)

    rows = []
    for track_id, rank in current.items():
        prev = previous.get(track_id, 0)
        delta = (prev - rank) if prev else 999  # new entry
        rows.append((track_id, rank, prev, delta))
    rows.sort(key=lambda r: (-r[3], r[1]))
    rows = rows[:limit]
    tracks = {
        t.id: t
        for t in session.scalars(
            select(Track).where(Track.id.in_([r[0] for r in rows]))
        )
    }
    return [(tracks[r[0]], r[1], r[2], r[3]) for r in rows if r[0] in tracks]


def resolve_playback(
    session: Session, playlist: Playlist, limit: int = PLAYBACK_PLAYLIST_LIMIT
) -> list[tuple[Track, int, int]]:
    """(track, plays, total_ms) rows for one KIND_PLAYBACK playlist, using the
    window stored in its `rules` field (a plain PLAYBACK_WINDOWS key, not
    JSON - see the field's docstring on the model)."""
    window = playlist.rules or "week"
    return playback_top_tracks(session, window=window, limit=limit)


#: (slug, name, window, description) for each built-in playback playlist -
#: same three windows the old built-in playback charts shipped with.
BUILTIN_PLAYBACK_PLAYLISTS = [
    ("most-played-week", "Most Played - This Week", "week", "Your top 100 of the last 7 days"),
    ("most-played-month", "Most Played - This Month", "month", "Your top 100 of the last 30 days"),
    ("most-played-all", "Most Played - All Time", "all", "Your top 100 ever"),
]


def ensure_builtin_playback_playlists(session: Session) -> None:
    """Re-seed the three built-in "Most Played" playlists, matched by slug so
    a rename (or a folder move) never produces a duplicate - the same
    protect-by-slug pattern services/charts.py used when this lived there as
    Chart.KIND_PLAYBACK."""
    for slug, name, window, desc in BUILTIN_PLAYBACK_PLAYLISTS:
        playlist = session.scalar(select(Playlist).where(Playlist.slug == slug))
        if playlist is None:
            session.add(
                Playlist(
                    name=name,
                    slug=slug,
                    kind=Playlist.KIND_PLAYBACK,
                    description=desc,
                    rules=window,
                )
            )
    session.flush()
