"""Video library queries.

The video-scanning counterpart to services/library.py, kept as its own
module since `Video` is a flat table rather than the Master/Release/Track
hierarchy the audio side browses - there's no get-or-create discography to
build up here, just a row per file. Watched folders (audio and video) live
in `db.models.WatchedFolder`, queried directly by `ui/views/settings/`.
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
    Albums, Artists, Tracks, and Title Details. Returns plain dicts, like
    `services.library.list_track_details`: callers need only a few fields,
    and a video library is small enough to fetch whole and filter
    client-side.

    `artist_name` prefers the linked `Artist` row (so a rename shows
    immediately) and falls back to `artist_display`. `artist_image_path` is
    the artist's portrait, if imported, used in place of a placeholder tile
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
            # the video's own picture (services/video_details.py)
            "thumbnail_path": video.thumbnail_path or None,
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
