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
import re
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    Chart,
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


# --------------------------------------------------------------------------
# tile artwork (2026-09-26 - the Playlists page's drill-down tile grid)
# --------------------------------------------------------------------------

#: how many different album covers a playlist/folder tile's mosaic uses
MOSAIC_COVERS = 4


def cover_paths_for_tracks(tracks: Iterable[Track], limit: int = MOSAIC_COVERS) -> list[str]:
    """The first `limit` *different* album covers among `tracks`, in
    playlist order - what a playlist tile's 2x2 mosaic is built from
    (James: "Maybe have a folder allow an image to press"). One cover per
    release, so a playlist that opens with three songs from one album
    doesn't show that album three times."""
    seen_releases: set[int] = set()
    seen_paths: set[str] = set()
    paths: list[str] = []
    for track in tracks:
        release = track.release
        if release is None or not release.cover_path:
            continue
        if release.id in seen_releases or release.cover_path in seen_paths:
            continue
        seen_releases.add(release.id)
        seen_paths.add(release.cover_path)
        paths.append(release.cover_path)
        if len(paths) >= limit:
            break
    return paths


def _store_tile_image(file_path: str) -> str:
    """Copies an image James picked into the artwork folder under a name
    derived from its bytes (so picking the same file twice reuses one
    copy) and returns the stored path. Raises ValueError for anything that
    isn't a readable, non-empty image file."""
    import hashlib
    from pathlib import Path

    from .. import config

    src = Path(file_path)
    ext = src.suffix.lower()
    if ext not in config.IMAGE_EXTENSIONS:
        raise ValueError("that isn't an image file")
    try:
        data = src.read_bytes()
    except OSError as exc:
        raise ValueError(f"couldn't read that file: {exc}") from exc
    if not data:
        raise ValueError("that file is empty")
    config.ART_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.ART_DIR / f"playlist_{hashlib.sha1(data).hexdigest()[:16]}{ext}"
    if not dest.exists():
        dest.write_bytes(data)
    return str(dest)


def set_playlist_image(session: Session, playlist_id: int, file_path: Optional[str]) -> None:
    """Use `file_path` as this playlist's tile image, or pass None to go
    back to the automatic cover mosaic. Raises ValueError for a bad file."""
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        return
    playlist.cover_path = _store_tile_image(file_path) if file_path else None
    session.flush()


def set_folder_image(session: Session, folder_id: int, file_path: Optional[str]) -> None:
    """Folder counterpart of `set_playlist_image`."""
    folder = session.get(PlaylistFolder, folder_id)
    if folder is None:
        return
    folder.cover_path = _store_tile_image(file_path) if file_path else None
    session.flush()


def folder_path(session: Session, folder_id: Optional[int]) -> list[PlaylistFolder]:
    """Root-first chain of folders ending at `folder_id` - backs the
    Playlists page breadcrumb. Empty for the top level (None) or a folder
    that no longer exists; stops if the parent chain ever loops."""
    chain: list[PlaylistFolder] = []
    seen: set[int] = set()
    current = session.get(PlaylistFolder, folder_id) if folder_id is not None else None
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        current = current.parent
    chain.reverse()
    return chain


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


def search_tracks(
    session: Session,
    artist_query: str = "",
    track_query: str = "",
    limit: int = 50,
) -> list[dict]:
    """Artist + title search for the playlist "Add songs…" / "Replace
    song…" picker (2026-09-27 - James: "how can I edit a playlist by
    replacing a song with a search for another song", then "please build
    both"). Feeds the same `JukeboxPickerDialog` the Jukebox uses, so it
    returns the same dict shape as `services/jukebox.py:
    search_addable_tracks` and matches the same way (`artist_display
    ilike`, `title_key like`, ANDed, both blank -> nothing).

    Unlike the jukebox search it keeps tracks with no resolvable album
    artist (compilation tracks etc.) - a playlist can hold any track, it
    doesn't need an artist card to file it under. `artist_id` is the
    album artist when there is one, else 0; the playlist view ignores it.
    `artist_name` is the track's own artist, which is what the playlist's
    track list shows. Sorted by (artist, album, title)."""
    artist_query = artist_query.strip()
    track_query = track_query.strip()
    if not artist_query and not track_query:
        return []
    stmt = (
        select(Track)
        .options(selectinload(Track.release))
        .order_by(Track.title)
        .limit(limit)
    )
    if artist_query:
        stmt = stmt.where(Track.artist_display.ilike(f"%{artist_query}%"))
    if track_query:
        stmt = stmt.where(Track.title_key.like(f"%{normalize(track_query)}%"))
    results: list[dict] = []
    for track in session.scalars(stmt).unique():
        release = track.release
        results.append(
            {
                "track_id": track.id,
                "title": track.title,
                "artist_name": track.artist_display or "",
                "artist_id": (release.album_artist_id if release is not None else None) or 0,
                "album": release.title if release is not None else "",
            }
        )
    results.sort(
        key=lambda r: (r["artist_name"].lower(), r["album"].lower(), r["title"].lower())
    )
    return results


def replace_track_at(
    session: Session, playlist_id: int, index: int, old_track_id: int, new_track_id: int
) -> bool:
    """Swap the song at 0-based `index` of a manual playlist for
    `new_track_id`, keeping its position (2026-09-27 "Replace song…").
    `old_track_id` guards against the list having changed underneath the
    view: if the item at `index` isn't that track, the first item that is
    gets replaced instead. Returns False if nothing matched."""
    playlist = session.get(Playlist, playlist_id)
    if playlist is None or playlist.kind != Playlist.KIND_MANUAL:
        return False
    items = list(playlist.items)
    target = None
    if 0 <= index < len(items) and items[index].track_id == old_track_id:
        target = items[index]
    else:
        target = next((i for i in items if i.track_id == old_track_id), None)
    if target is None:
        return False
    target.track_id = new_track_id
    target.added_at = dt.datetime.now()
    session.flush()
    return True


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
        #: 2026-09-17 (James: matching MusicBee's own "Comment contains
        #: [Billboard]" auto-playlist rules) - Track.comment already
        #: exists and gets populated from the file's own tag on scan (see
        #: services/scanner.py's `track.comment = tags.comment or None`);
        #: it just had no smart-playlist field to reach it through until
        #: now.
        "comment": Track.comment,
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
        # "artist"/"comment" skip normalize() - normalize() strips
        # punctuation entirely (see services/matching.py), which would
        # mangle a bracketed tag like "[Billboard][1946]" into unmatchable
        # mush before it ever reaches the ILIKE below; title/album go
        # through it because those columns (*_key) are themselves
        # normalized, so the search term has to match that same shape.
        needle = normalize(value) if field not in ("artist", "comment") else str(value)
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


def _comment_rank_patterns(hint: Optional[str], year: Optional[str]) -> list[str]:
    """Regex source strings matching a chart rank tagged into `Track.comment`
    for the "chart_rank" smart-playlist ordering - see resolve_smart's own
    docstring for why this reads the comment text directly rather than
    joining through `ChartEntry`. James's library uses (at least) two
    conventions for this, both observed directly in his own data:

        "[Billboard] #002# [2020]"      - hint, then rank, then year
        "[Hot Country][2023] #096#"     - hint and year together, then rank

    `hint`/`year` are each optional (a playlist's rules might supply only
    one, or neither) - `None` becomes a permissive `.+?` placeholder rather
    than omitting that half of the pattern, since either convention above
    always has *something* in both positions. Every hint is regex-escaped;
    nothing here should ever be treated as a literal engine pattern from
    playlist rule text."""
    h = re.escape(hint) if hint else r"[^\[\]]+"
    y = re.escape(year) if year else r"\d{4}"
    return [
        rf"\[{h}\]\s*#(\d+)#\s*\[{y}\]",  # "[Billboard] #002# [2020]"
        rf"\[{h}\]\s*\[{y}\]\s*#(\d+)#",  # "[Hot Country][2023] #096#"
    ]


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
    order_by = spec.get("order_by", "title")
    # 2026-09-17 follow-up - the new form-based SmartPlaylistDialog
    # (ui/views/playlists.py) has an unchecked-by-default "limit to N
    # tracks" checkbox, same as MusicBee's own Auto-Playlist editor - so
    # omitting "limit" now means genuinely unlimited rather than the old
    # silent 500 cap, which nothing that already sets an explicit limit
    # (the three built-in smart playlists, and any spec saved through the
    # old JSON box) ever relied on.
    limit = spec.get("limit")
    if order_by == "chart_rank":
        # 2026-09-17 (James: "I need an option where it can be manual so
        # that I can sort them by ranking of the chart") - for a smart
        # playlist built around a chart-tagged Comment (e.g. "Comment
        # contains [Billboard]" + "Comment contains [2020]"), James wants
        # the list to read in the chart's own countdown order, not by
        # title/date/play count.
        #
        # 2026-09-17 same-day follow-up #1 (James: "the Playlist is out
        # of order") - the first version of this took MIN(rank) across
        # *every* `ChartEntry` a track has anywhere, with no notion of
        # "which chart, which edition" - so a track's *weekly* Hot 100
        # peak (which most Billboard #1s reach eventually) beat its
        # actual year-end position, and ties beyond that had no tiebreak
        # at all.
        #
        # 2026-09-17 same-day follow-up #2, chasing #1's fix - narrowing
        # to "the YE edition of a chart named like the rules' own
        # `[Billboard]` hint" ran straight into two more problems that
        # make `ChartEntry` fundamentally the wrong table for this:
        #   1. James's `charts` table has *three* separate chart rows all
        #      literally named "Billboard YE" (Hot 100, Country, and
        #      Christian, distinguished only by `Chart.slug` - a detail
        #      the smart-playlist rules have no way to reference), so
        #      "name the chart, then find its YE issue" still can't tell
        #      the Hot 100 list apart from the Country one; "The Bones"
        #      (a country song) was outranking Circles because its
        #      Country-chart YE rank of 2 was getting merged in.
        #   2. Worse: the *same song* sometimes exists as two separate
        #      `Track` rows (two different files/imports - confirmed for
        #      Circles: track 26171 with an unrelated 2019 tag, track
        #      26268 with the real `[Billboard] #002# [2020]` one), and
        #      `ChartEntry.track_id` - populated by the Charts page's own
        #      independent title/artist fuzzy-matcher ("Re-match
        #      library") - had linked to the *other* one. No amount of
        #      narrowing which chart/issue to look at fixes a rank stored
        #      against a different row than the one this query is
        #      actually sorting.
        #
        # The comment text itself sidesteps both: it's the exact same
        # field the rules already matched on, already carries the rank
        # number, and needs no cross-referencing to any other table at
        # all. `[Billboard] #002# [2020]` and `[Hot Country][2023] #096#`
        # are the two tagging conventions actually seen in James's
        # library - hint-then-rank-then-year, and hint-and-year-then-rank
        # - so both are tried. A non-numeric rule value like "[Billboard]"
        # or "[Hot Country]" is a chart-name hint; a 4-digit one like
        # "[2020]" is a year. A track whose comment doesn't actually
        # contain a matching "#rank#" for any hint/year combination (or a
        # playlist with no such rules at all) sorts after every ranked
        # one instead of raising or vanishing; `Track.title` breaks any
        # remaining tie deterministically.
        comment_values = [
            r.get("value", "")
            for r in spec.get("rules", [])
            if r.get("field") == "comment" and r.get("op") == "contains" and r.get("value")
        ]
        years = [
            m
            for v in comment_values
            for m in re.findall(r"(?:19|20)\d{2}", v)
        ]
        chart_hints = [
            v.strip("[]").strip()
            for v in comment_values
            if not re.fullmatch(r"(?:19|20)\d{2}", v.strip("[]").strip())
            and v.strip("[]").strip()
        ]
        # No hint/year at all (a playlist with no "Comment contains [...]"
        # rule) leaves `rank_patterns` empty rather than matching a wildcard
        # "any tag" pattern against every track's comment - safer than
        # guessing, and it still degrades gracefully: every track falls
        # through to the untagged (2**31-1) bucket below and the whole list
        # sorts alphabetically by title instead.
        rank_patterns = [
            re.compile(pattern)
            for hint in (chart_hints or [None])
            for year in (years or [None])
            for pattern in _comment_rank_patterns(hint, year)
        ] if (chart_hints or years) else []

        def _tagged_rank(track: Track) -> int:
            comment = track.comment or ""
            for pattern in rank_patterns:
                m = pattern.search(comment)
                if m:
                    return int(m.group(1))
            return 2**31 - 1

        tracks = list(session.scalars(stmt).unique())
        tracks.sort(key=lambda t: (_tagged_rank(t), t.title))
        return tracks[: int(limit)] if limit is not None else tracks

    order = ORDERINGS.get(order_by, Track.title)
    stmt = stmt.order_by(order)
    if limit is not None:
        stmt = stmt.limit(int(limit))
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


# --------------------------------------------------------------------------
# sort by album, then side / track (2026-09-30)
# --------------------------------------------------------------------------
# James, on his 78 Records playlists: "These are physical records I have in
# my collection. The track number is important in the playlist because I
# indicate Side A and Side B. I want the playlist to have the granularity
# of Album and then Track #." - so each record's A side is followed by its
# B side, records in catalogue order. Album titles like "Bluebird - B-6873"
# sort naturally (B-6873 before B-10096), not as text.

_NATURAL = re.compile(r"(\d+)")


def natural_key(text: Optional[str]) -> tuple:
    """'Bluebird - B-6873' < 'Bluebird - B-10096' (numbers compared as
    numbers, the rest case-insensitively)."""
    parts = _NATURAL.split((text or "").casefold())
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p != "")


def album_track_key(track: Track) -> tuple:
    album = track.release.title if track.release is not None else ""
    return (
        natural_key(album),
        track.disc_no or 1,
        track.track_no if track.track_no is not None else 9999,
        track.title_key or (track.title or "").casefold(),
    )


def sort_playlist_by_album_track(session: Session, playlist_id: int) -> bool:
    """Re-number a manual playlist's items in album, then disc, then
    side/track order. Returns True if the order changed."""
    playlist = session.get(Playlist, playlist_id)
    if playlist is None or playlist.kind != Playlist.KIND_MANUAL:
        return False
    items = list(session.scalars(
        select(PlaylistItem)
        .options(selectinload(PlaylistItem.track).selectinload(Track.release))
        .where(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position, PlaylistItem.id)
    ))
    ordered = sorted(items, key=lambda i: album_track_key(i.track))
    if [i.id for i in ordered] == [i.id for i in items] and all(
        i.position == n for n, i in enumerate(items)
    ):
        return False
    for n, item in enumerate(ordered):
        item.position = n
    playlist.updated_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return True


def sort_folder_by_album_track(session: Session, folder_id: int) -> tuple[int, int]:
    """`sort_playlist_by_album_track` for every manual playlist in a folder
    and its subfolders. Returns (playlists re-ordered, manual playlists)."""
    ids = folder_and_descendant_ids(session, folder_id)
    playlists = list(session.scalars(
        select(Playlist.id).where(Playlist.folder_id.in_(ids),
                                  Playlist.kind == Playlist.KIND_MANUAL)
    ))
    changed = sum(1 for pid in playlists if sort_playlist_by_album_track(session, pid))
    return changed, len(playlists)
