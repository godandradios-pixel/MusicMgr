"""Filesystem scanner: video files -> Video rows.

Deliberately much lighter than services/scanner.py's audio pipeline: there is
no reliable, dependency-free way to read title/artist tags out of an
arbitrary MP4/MKV/AVI/WebM container the way mutagen reads ID3/Vorbis tags
from audio, so this scanner leans on filename and folder conventions
instead - see `_parse_video_title_artist`. Duration is read where the
container happens to let mutagen do it (mp4-family files) and left NULL
otherwise; nothing here ever raises for a file it can't fully understand.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

import mutagen
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import Video, WatchedFolder
from . import library as lib
from .matching import normalize
from .scanner import ProgressFn, ScanResult

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iter_video_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in config.VIDEO_EXTENSIONS:
                yield p


def _parse_video_title_artist(path: Path) -> tuple[str, str]:
    """Best-effort title/artist split with no tag reading involved.

    Two conventions cover most real-world video collections: "Artist -
    Title.ext" (how most download/rip tools name a music video), and a plain
    "Title.ext" sitting inside an "Artist" folder - the same folder-as-artist
    layout scanner.py's own `_fallback_from_path` assumes for untagged audio.
    """
    stem = path.stem
    if " - " in stem:
        artist, _, title = stem.partition(" - ")
        artist, title = artist.strip(), title.strip()
        if artist and title:
            return title, artist
    parent = path.parent.name
    if parent and parent not in (".", "/"):
        return stem.strip() or "Untitled", parent
    return stem.strip() or "Untitled", "Unknown Artist"


def read_video_duration(path: Path) -> Optional[int]:
    """mutagen only understands the moov/atom layout of the mp4 family, so
    this comes back None for mkv/avi/webm/etc. - never a hard failure."""
    try:
        audio = mutagen.File(str(path))
        info = getattr(audio, "info", None) if audio is not None else None
        length = getattr(info, "length", None)
        if length:
            return int(length * 1000)
    except Exception:
        pass
    return None


def import_video_file(session: Session, path: Path, result: ScanResult) -> Optional[Video]:
    """Import or refresh a single video file. Returns the Video."""
    stat = path.stat()
    existing = session.scalar(select(Video).where(Video.path == str(path)))
    if existing is not None:
        if existing.mtime == stat.st_mtime and existing.size_bytes == stat.st_size:
            existing.is_missing = False
            existing.last_seen_at = _now()
            result.unchanged += 1
            return existing
        result.updated += 1
    else:
        result.added += 1

    title, artist_name = _parse_video_title_artist(path)
    artist = lib.get_or_create_artist(session, artist_name)

    video = existing or Video(path=str(path))
    video.title = title
    video.title_key = normalize(title)
    video.artist_display = artist.name
    video.artist_id = artist.id
    video.duration_ms = read_video_duration(path)
    video.size_bytes = stat.st_size
    video.mtime = stat.st_mtime
    video.codec = path.suffix.lstrip(".").lower()
    video.is_missing = False
    video.last_seen_at = _now()
    if existing is None:
        session.add(video)
        session.flush()
    return video


def scan_video_folder(
    session: Session,
    root: Path | str,
    progress: Optional[ProgressFn] = None,
    result: Optional[ScanResult] = None,
) -> ScanResult:
    root = Path(root).expanduser()
    result = result or ScanResult()
    if not root.exists():
        result.errors.append(f"{root} does not exist")
        return result

    files = list(iter_video_files(root))
    total = len(files)
    for idx, path in enumerate(files, start=1):
        try:
            import_video_file(session, path, result)
        except Exception as exc:  # keep going on one bad file
            log.exception("failed to import %s", path)
            result.errors.append(f"{path.name}: {exc}")
            # see the matching comment in services/scanner.py:scan_folder -
            # same bug, same fix: without this, one bad file poisons the
            # Session for the rest of the scan instead of just being skipped.
            session.rollback()
        result.scanned += 1
        if progress and (idx % 5 == 0 or idx == total):
            progress(idx, total, path.name)
        if idx % 200 == 0:
            session.flush()

    folder = session.scalar(select(WatchedFolder).where(WatchedFolder.path == str(root)))
    if folder is None:
        folder = WatchedFolder(path=str(root))
        session.add(folder)
    folder.last_scan_at = _now()
    session.flush()
    return result


def rescan_all_videos(session: Session, progress: Optional[ProgressFn] = None) -> ScanResult:
    result = ScanResult()
    folders = list(
        session.scalars(select(WatchedFolder).where(WatchedFolder.enabled.is_(True)))
    )
    for folder in folders:
        scan_video_folder(session, folder.path, progress, result)
    result.missing = mark_missing_videos(session)
    return result


def mark_missing_videos(session: Session) -> int:
    """Flag rows whose file has disappeared instead of deleting metadata -
    same convention as scanner.py:mark_missing_files."""
    count = 0
    for video in session.scalars(select(Video)):
        exists = os.path.exists(video.path)
        if not exists and not video.is_missing:
            video.is_missing = True
            count += 1
        elif exists and video.is_missing:
            video.is_missing = False
    return count
