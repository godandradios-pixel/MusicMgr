"""Video library queries.

The video-scanning counterpart to services/library.py, kept as its own
module since `Video` is a flat table rather than the Master/Release/Track
hierarchy the audio side browses - there's no get-or-create discography to
build up here, just a row per file. Watched-folder bookkeeping used to live
here too (`list_video_folders`, a thin wrapper around the now-removed
`VideoFolder` table) - see `db.models.WatchedFolder`'s docstring for the
2026-09-07 merge that folded video and audio watched folders into one
table/list, tracked in `db.models.WatchedFolder` and queried directly by
`ui/views/settings.py` rather than through a per-module helper.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Artist, Video


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def list_videos(
    session: Session, artist_id: Optional[int] = None, limit: int = 5000
) -> Sequence[Video]:
    stmt = select(Video)
    if artist_id is not None:
        stmt = stmt.where(Video.artist_id == artist_id)
    stmt = stmt.order_by(func.lower(Video.title)).limit(limit)
    return list(session.scalars(stmt))


def list_videos_for_search(session: Session) -> list[dict]:
    """Every video, shaped for weaving into the Library search surfaces -
    Albums, Artists, Tracks, and Title Details (2026-09-06) - the same way
    each of those already searches its own native content. Returns plain
    dicts rather than ORM objects, matching `services.library.
    list_track_details`'s own reasoning: each caller only needs a handful
    of fields, and a video library is small (hundreds, not tens of
    thousands of tracks) so fetching every video up front and filtering
    client-side - the same shape every other Library search already uses -
    costs nothing worth optimizing away.

    `artist_name` prefers the linked `Artist` row (so a rename there is
    reflected immediately) and falls back to the denormalised
    `artist_display` string, the same fallback rule
    `list_track_details` already uses for a release predating
    `album_artist_id`. `artist_image_path` is the artist's own portrait, if
    one has been imported - the nearest thing to "cover art" a video has,
    used in place of a generated placeholder tile where a caller wants one
    (see `ui/views/library.py:_video_tiles`)."""
    stmt = (
        select(Video, Artist.name, Artist.image_path)
        .outerjoin(Artist, Artist.id == Video.artist_id)
        .order_by(func.lower(Video.title))
    )
    rows: list[dict] = []
    for video, artist_name, artist_image_path in session.execute(stmt):
        rows.append({
            "video_id": video.id,
            "title": video.title,
            "artist_name": artist_name or video.artist_display or "",
            "artist_image_path": artist_image_path,
            "year": video.year,
            "duration_ms": video.duration_ms,
            "sort_key": video.title_key or video.title.lower(),
        })
    return rows


def video_counts(session: Session) -> dict:
    """Mirrors the shape of services/library.py:library_stats() but stays
    separate rather than folding video rows into it - see Video's docstring
    on why videos are deliberately not part of the audio hierarchy."""
    return {
        "videos": session.scalar(select(func.count(Video.id))) or 0,
        "video_missing": session.scalar(
            select(func.count(Video.id)).where(Video.is_missing.is_(True))
        )
        or 0,
        "video_total_ms": session.scalar(select(func.sum(Video.duration_ms))) or 0,
    }


def record_watch(session: Session, video_id: int) -> None:
    """Bump play_count/last_played_at once a watch has gone on long enough to
    count - see services/video_player.py:VideoController for the threshold."""
    video = session.get(Video, video_id)
    if video is not None:
        video.play_count = (video.play_count or 0) + 1
        video.last_played_at = _now()


#: `list_video_folders` used to live here (a thin `select(VideoFolder)`
#: wrapper), unused anywhere in the app - removed with the 2026-09-07
#: WatchedFolder merge (see db.models.WatchedFolder's docstring) rather
#: than updated to a concept ("a folder watched specifically for video")
#: that no longer exists. `services.videos.video_counts` below is the
#: query this module's callers actually use.
