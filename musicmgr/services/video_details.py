"""Video thumbnails and years (2026-10-22 - James picked the deferred
"Video.thumbnail_path and Video.year: nothing populates them").

**Year**, first that answers:

1. the file's own date tag (mp4 `©day`) - a plain year or an ISO date. A
   bare 8-digit `YYYYMMDD` is what yt-dlp writes for the *upload* date
   (Thriller "2009"), so it's ignored;
2. the earliest Billboard chart this song by this artist appears on - the
   year it was a hit, which is what a music video's year means;
3. the earliest year of a non-compilation album in the library that has the
   song by this artist.

The title is cleaned of "(Official Music Video)"-style noise before
matching (`clean_title`).

**Thumbnail**, first that answers:

1. a picture next to the video with the same name (`Song.jpg` beside
   `Song.mp4`);
2. the cover picture embedded in the mp4 (yt-dlp `--embed-thumbnail`,
   iTunes videos) - saved to data\\video_thumbs;
3. a frame from about a third of the way in (at most 30 s), grabbed by
   `grab` - ffmpeg when it's on this PC's PATH, otherwise Qt's own player
   (see ui/video_details_worker.py).

Thumbnails live in data\\video_thumbs, outside the artwork folders, so they
never travel in USB sync - each PC makes its own. `Video.thumbnail_path`
is NULL until a video has been tried and "" when nothing could be found,
so a video is only ever tried once; a changed file is tried again
(video_scanner.import_video_file clears it).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import mutagen
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import (
    Artist, ChartEntry, ChartIssue, Credit, Release, Track, Video,
)
from .matching import normalize, split_artists

log = logging.getLogger(__name__)

THUMB_DIR_NAME = "video_thumbs"
#: thumbnail_path for "tried, nothing found"
NO_THUMB = ""
GRAB_WIDTH = 480

#: (video path, output .jpg) -> True grabbed / False no picture / None stuck
GrabFn = Callable[[Path, Path, Optional[int]], Optional[bool]]


def thumb_dir() -> Path:
    return config.DATA_DIR / THUMB_DIR_NAME


def thumb_file(video_path: str | Path, suffix: str = ".jpg") -> Path:
    key = str(video_path).casefold() if config.CASE_INSENSITIVE_PATHS else str(video_path)
    return thumb_dir() / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + suffix)


# --------------------------------------------------------------------------
# year
# --------------------------------------------------------------------------

_NOISE_GROUP = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:official|video|lyrics?|audio|visuali[sz]er|hd|hq|4k|"
    r"remaster(?:ed)?|clip|explicit|uncensored)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_TRAILING_FEAT = re.compile(r"\s+(?:ft\.?|feat\.?|featuring)\s+.*$", re.IGNORECASE)
_TRAILING_PIPE = re.compile(r"\s*\|.*$")


def clean_title(title: str) -> str:
    """'Back In Black (Official Video)' -> 'Back In Black'."""
    t = _TRAILING_PIPE.sub("", title or "")
    t = _NOISE_GROUP.sub("", t)
    t = _TRAILING_FEAT.sub("", t)
    return t.strip() or (title or "").strip()


def year_from_tag(value) -> Optional[int]:
    """'1980' / '1983-11-11T08:00:00Z' -> the year; yt-dlp's upload-date
    style '20091027' -> None."""
    if value is None:
        return None
    text = str(value[0] if isinstance(value, (list, tuple)) and value else value).strip()
    if re.fullmatch(r"\d{8}", text):
        return None
    m = re.match(r"(\d{4})(?:$|[-/T ])", text)
    if not m:
        return None
    year = int(m.group(1))
    return year if 1900 <= year <= 2100 else None


def read_file_details(path: Path) -> tuple[Optional[int], Optional[int], Optional[bytes], str]:
    """(duration ms, tag year, embedded cover bytes, cover suffix) - one
    mutagen read; anything it can't read comes back None."""
    duration = year = cover = None
    suffix = ".jpg"
    try:
        f = mutagen.File(str(path))
    except Exception:
        return None, None, None, suffix
    if f is None:
        return None, None, None, suffix
    length = getattr(getattr(f, "info", None), "length", None)
    if length:
        duration = int(length * 1000)
    tags = getattr(f, "tags", None)
    if tags is not None:
        try:
            year = year_from_tag(tags.get("\xa9day"))
            covers = tags.get("covr")
            if covers:
                cover = bytes(covers[0])
                if getattr(covers[0], "imageformat", None) == 14:  # MP4Cover.FORMAT_PNG
                    suffix = ".png"
        except Exception:
            pass
    return duration, year, cover, suffix


def _artist_matches(video_artist_key: str, credited: str) -> bool:
    keys = {normalize(n) for n in split_artists(credited)} | {normalize(credited)}
    return video_artist_key in keys


def chart_year(session: Session, artist: str, title: str) -> Optional[int]:
    """Earliest chart date for this song by this artist (any chart)."""
    title_key = normalize(clean_title(title))
    artist_key = normalize(artist)
    if not title_key or not artist_key:
        return None
    best = None
    rows = session.execute(
        select(ChartEntry.artist_name, func.min(ChartIssue.chart_date))
        .join(ChartIssue, ChartIssue.id == ChartEntry.issue_id)
        .where(ChartEntry.title_key == title_key)
        .group_by(ChartEntry.artist_name)
    )
    for name, first in rows:
        if first is None or not _artist_matches(artist_key, name or ""):
            continue
        year = int(str(first)[:4])
        best = year if best is None else min(best, year)
    return best


def library_year(session: Session, artist_id: Optional[int], title: str) -> Optional[int]:
    """Earliest year of a non-compilation album holding this song by this
    artist (as the album artist or credited on the track)."""
    if artist_id is None:
        return None
    title_key = normalize(clean_title(title))
    if not title_key:
        return None
    credited = select(Credit.track_id).where(Credit.artist_id == artist_id)
    return session.scalar(
        select(func.min(Release.year))
        .join(Track, Track.release_id == Release.id)
        .where(
            Track.title_key == title_key,
            Release.year.is_not(None), Release.year > 0,
            Release.is_compilation.is_(False),
            (Release.album_artist_id == artist_id) | Track.id.in_(credited),
        )
    )


def find_year(session: Session, video: Video, tag_year: Optional[int] = None) -> Optional[int]:
    if tag_year:
        return tag_year
    artist = video.artist_display or ""
    if video.artist_id is not None:
        name = session.scalar(select(Artist.name).where(Artist.id == video.artist_id))
        artist = name or artist
    return chart_year(session, artist, video.title) or library_year(session, video.artist_id, video.title)


# --------------------------------------------------------------------------
# thumbnail
# --------------------------------------------------------------------------


def sidecar_image(path: Path) -> Optional[Path]:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        for candidate in (path.with_suffix(ext), path.with_suffix(ext.upper())):
            if candidate.is_file():
                return candidate
    return None


def save_cover(video_path: str | Path, data: bytes, suffix: str = ".jpg") -> Optional[str]:
    out = thumb_file(video_path, suffix)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, out)
    except OSError:
        log.warning("couldn't save a thumbnail for %s", video_path)
        return None
    return str(out)


def ffmpeg_grabber() -> Optional[GrabFn]:
    """A frame grabber using ffmpeg on this PC's PATH, or None."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None

    def grab(path: Path, out: Path, duration_ms: Optional[int]) -> Optional[bool]:
        at = min(30.0, (duration_ms or 0) / 3000.0) if duration_ms else 10.0
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "-loglevel", "error", "-y", "-ss", f"{at:.1f}", "-i", str(path),
               "-frames:v", "1", "-vf", f"scale={GRAB_WIDTH}:-2", "-q:v", "4", str(out)]
        flags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, creationflags=flags, check=False)
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return None
        return out.is_file() and out.stat().st_size > 0

    return grab


# --------------------------------------------------------------------------
# filling in
# --------------------------------------------------------------------------


def pending_count(session: Session) -> int:
    """Videos still missing a thumbnail try or a year - cheap, one COUNT."""
    return session.scalar(
        select(func.count(Video.id)).where(
            Video.is_missing.is_(False),
            Video.thumbnail_path.is_(None) | Video.year.is_(None),
        )
    ) or 0


def fill_video_details(
    session: Session,
    grab: Optional[GrabFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int]:
    """Fill in years and thumbnails for every video still missing them.
    Returns (years found, thumbnails found). Each video is written and
    committed on its own, after its slow file work, so the app's own
    writes never wait behind this; a stop keeps what's done. A grabber that reports itself stuck (None) ends
    the frame grabs for this run; those videos are tried again next time."""
    ids = list(session.scalars(
        select(Video.id).where(
            Video.is_missing.is_(False),
            Video.thumbnail_path.is_(None) | Video.year.is_(None),
        ).order_by(Video.id)
    ))
    years = thumbs = 0
    grabbing = grab
    for n, vid in enumerate(ids, start=1):
        if should_stop and should_stop():
            break
        video = session.get(Video, vid)
        if video is None:
            continue
        path = Path(video.path)
        needs_thumb = video.thumbnail_path is None
        known_ms = video.duration_ms
        duration, tag_year, cover, suffix = (
            read_file_details(path) if path.is_file() else (None, None, None, ".jpg"))
        new_year = find_year(session, video, tag_year) if video.year is None else None
        # 2026-10-22: no write is pending while a frame is grabbed - the
        # first run held its write transaction through up to 25 grabs, and a
        # chart match James saved meanwhile timed out ("database is locked")
        session.rollback()
        new_thumb: Optional[str] = None
        if needs_thumb and path.is_file():
            found: Optional[str] = None
            settled = True  # False leaves it NULL, to be tried again next run
            side = sidecar_image(path)
            if side is not None:
                found = str(side)
            elif cover:
                found = save_cover(path, cover, suffix)
            elif grabbing is not None:
                out = thumb_file(path)
                ok = grabbing(path, out, duration or known_ms)
                if ok is None:
                    log.warning("video frame grab stuck on %s - no more grabs this run", path)
                    grabbing = None
                    settled = False
                elif ok:
                    found = str(out)
            elif grab is not None:
                settled = False  # the grabber got stuck earlier in this run
            if settled:
                new_thumb = found if found is not None else NO_THUMB
        else:
            needs_thumb = False
        # write and commit this video at once: each write holds the lock
        # for a moment only
        video = session.get(Video, vid)
        if video is not None and (new_year or (needs_thumb and new_thumb is not None)):
            if new_year:
                video.year = new_year
                years += 1
            if needs_thumb and new_thumb is not None:
                video.thumbnail_path = new_thumb
                if new_thumb:
                    thumbs += 1
            session.commit()
        if progress and n % 25 == 0:
            progress(n, len(ids))
    session.commit()
    if progress:
        progress(len(ids), len(ids))
    return years, thumbs
