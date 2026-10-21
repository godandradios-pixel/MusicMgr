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
from typing import Callable, Iterable, Optional

import mutagen
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import Video, WatchedFolder
from . import library as lib
from .matching import normalize
from .scanner import ProgressFn, ScanResult, _in_skipped_folder, path_matches

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iter_video_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not config.is_skipped_dir(d)]
        for name in sorted(filenames):
            if config.is_hidden_name(name):
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


def import_video_file(
    session: Session, path: Path, result: ScanResult, *, force: bool = False
) -> Optional[Video]:
    """Import or refresh a single video file. Returns the Video.

    `force=True` (2026-09-18, "Re-read all tags" scan option in
    ui/views/settings.py - audio and video scan together under one
    `ScanThread`) bypasses the unchanged-file shortcut below, mirroring
    services/scanner.py:import_file's own `force` flag.
    """
    stat = path.stat()
    existing = session.scalar(select(Video).where(Video.path == str(path)))
    if existing is None and config.CASE_INSENSITIVE_PATHS:
        # 2026-10-19: same file under another capitalization (see
        # scanner.find_media_file) - keep the row, follow the new spelling
        existing = session.scalar(
            select(Video).where(path_matches(Video.path, path)).limit(1)
        )
        if existing is not None:
            existing.path = str(path)
    if existing is not None:
        if not force and existing.mtime == stat.st_mtime and existing.size_bytes == stat.st_size:
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
    *,
    force: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings". `should_stop`, checked once per file before
    that file's own import, `break`s rather than raising - same
    safe-via-break shape scanner.py:scan_folder's own should_stop just
    adopted, and for the same reason: no per-file savepoint here either
    (see the matching `session.rollback()` comment below), so a clean break
    just leaves the folder's own end-of-scan bookkeeping (WatchedFolder
    timestamp, flush) to run over whatever got imported before the stop."""
    root = Path(root).expanduser()
    result = result or ScanResult()
    if not root.exists():
        result.errors.append(f"{root} does not exist")
        return result

    files = list(iter_video_files(root))
    total = len(files)
    for idx, path in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        try:
            import_video_file(session, path, result, force=force)
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



def import_video_paths(
    session: Session,
    paths: Iterable[Path | str],
    result: Optional[ScanResult] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Import just these video files - no folder walk, no WatchedFolder
    bookkeeping. The video twin of scanner.import_paths (2026-09-23, USB
    sync). Non-video paths are ignored; an unchanged file is skipped."""
    result = result or ScanResult()
    for p in paths:
        if should_stop and should_stop():
            break
        path = Path(p)
        if path.suffix.lower() not in config.VIDEO_EXTENSIONS or not path.exists():
            continue
        try:
            import_video_file(session, path, result)
        except Exception as exc:  # keep going on one bad file
            log.exception("failed to import %s", path)
            result.errors.append(f"{path.name}: {exc}")
            session.rollback()
        result.scanned += 1
    session.flush()
    return result

def rescan_all_videos(
    session: Session, progress: Optional[ProgressFn] = None, *, force: bool = False
) -> ScanResult:
    result = ScanResult()
    folders = list(
        session.scalars(select(WatchedFolder).where(WatchedFolder.enabled.is_(True)))
    )
    for folder in folders:
        scan_video_folder(session, folder.path, progress, result, force=force)
    result.skipped_removed = remove_skipped_folder_videos(session)
    result.missing = mark_missing_videos(session)
    return result


def remove_skipped_folder_videos(session: Session) -> int:
    """Take videos inside a folder scans now skip (config.SKIP_SCAN_DIRS -
    GoodSync's _gsdata_ above all) back out of the library - the video twin
    of scanner.remove_skipped_folder_files (2026-10-21).

    The 2026-10-18 fix made video scans skip those folders, but only the
    music side got a cleanup for what an older scan had already imported.
    PC1 still had 15 videos from D:\\Movies\\_gsdata_\\_saved_ (GoodSync's
    old versions), four of them filed under an artist called "_saved_" -
    the folder name, since their file names had no "Artist - Title".

    Deleting a Video row takes nothing else with it (nothing has a foreign
    key to videos.id - see purge_missing_videos), so unlike the music side
    there's no orphan-track step. An artist the removed videos leave with
    nothing at all - no credits, releases, videos or jukebox slots - goes
    too, along with its top-track/similar-artist rows, so "_saved_" doesn't
    linger as an empty artist. The files on disk are never touched.
    Returns how many videos were removed."""
    doomed = [
        (video_id, artist_id)
        for video_id, path, artist_id in session.execute(select(Video.id, Video.path, Video.artist_id))
        if _in_skipped_folder(path or "")
    ]
    if not doomed:
        return 0
    ids = [video_id for video_id, _ in doomed]
    for start in range(0, len(ids), 500):
        session.execute(delete(Video).where(Video.id.in_(ids[start:start + 500])))
    session.flush()
    _remove_unused_artists(session, {a for _, a in doomed if a is not None})
    session.expire_all()
    return len(ids)


def _remove_unused_artists(session: Session, artist_ids: set[int]) -> int:
    """Delete those of `artist_ids` nothing points at any more - the same
    "still used?" test scanner.py's artist repairs use."""
    from ..db.models import (
        Artist, ArtistSimilar, ArtistTopPick, ArtistTopTrack, Credit, JukeboxSlot, Release,
    )

    removed = 0
    for artist_id in artist_ids:
        used = (
            session.scalar(select(Credit.id).where(Credit.artist_id == artist_id).limit(1))
            or session.scalar(select(Release.id).where(Release.album_artist_id == artist_id).limit(1))
            or session.scalar(select(Video.id).where(Video.artist_id == artist_id).limit(1))
            or session.scalar(select(JukeboxSlot.id).where(JukeboxSlot.artist_id == artist_id).limit(1))
        )
        if used:
            continue
        for table in (ArtistTopTrack, ArtistTopPick, ArtistSimilar):
            session.execute(delete(table).where(table.artist_id == artist_id))
        session.execute(delete(Artist).where(Artist.id == artist_id))
        removed += 1
    session.flush()
    return removed


def mark_missing_videos(
    session: Session,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Flag rows whose file has disappeared instead of deleting metadata -
    same convention as scanner.py:mark_missing_files, `progress` (2026-09-22,
    part of the same "does the progress bar also work on ... verify files"
    fix) included, and now `should_stop` too (2026-09-22 same-day
    follow-up, "add a real cancel button...") - same safe-via-break shape
    as scanner.py:mark_missing_files."""
    total = session.scalar(select(func.count(Video.id))) or 0
    count = 0
    for i, video in enumerate(session.scalars(select(Video)), start=1):
        if should_stop and should_stop():
            break
        exists = os.path.exists(video.path)
        if not exists and not video.is_missing:
            video.is_missing = True
            count += 1
        elif exists and video.is_missing:
            video.is_missing = False
        if progress and (i % 250 == 0 or i == total):
            progress(i, total, "")
    return count


def purge_missing_videos(session: Session) -> int:
    """Permanently delete every `Video` row currently flagged `is_missing`.

    Unlike scanner.py's `purge_orphaned_tracks`, this needs no has-history
    check before deleting - nothing in the schema has a foreign key to
    `videos.id` (a video is never a playlist item, a chart entry, a play
    event, or a jukebox slot side), so there is nothing a delete here could
    silently take down with it. That's also why, unlike the audio purge
    (which runs automatically on every scan - see its own docstring), this
    one is wired to a manual, confirmed Settings button instead: it's the
    one video-library action that's actually irreversible, so it gets a
    "are you sure" rather than running unattended (2026-09-15, James: a
    library carried over from another machine's Linux paths showed every
    video "missing" and duplicated after a Windows rescan - see
    mark_missing_videos above for why the paths themselves don't move, and
    settings.py's "Purge missing videos…" button for the cleanup step).
    Call `mark_missing_videos` first (or run a scan) so `is_missing` is
    actually current before purging by it.
    """
    candidates = list(session.scalars(select(Video).where(Video.is_missing.is_(True))))
    for video in candidates:
        session.delete(video)
    if candidates:
        session.flush()
    return len(candidates)
