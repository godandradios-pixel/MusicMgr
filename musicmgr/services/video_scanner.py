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
import re
from pathlib import Path, PurePath, PureWindowsPath
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


def _parse_video_title_artist(path: PurePath) -> tuple[str, str]:
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
            return _move_featuring_to_title(title, artist)
    parent = path.parent.name
    if parent and parent not in (".", "/"):
        return stem.strip() or "Untitled", parent
    return stem.strip() or "Untitled", "Unknown Artist"


#: "Artist ft. Guest" / "Artist feat. Guest" / "Artist featuring Guest" /
#: "Artist (feat. Guest)" in the artist half of a video's file name
_ARTIST_FEAT_RE = re.compile(
    r"\s*(?:\(\s*(?:feat\.?|ft\.?|featuring)\s+(?P<b>[^)]+)\)|"
    r"\s(?:feat\.?|ft\.?|featuring)\s+(?P<a>.+))\s*$",
    re.IGNORECASE,
)
_TITLE_HAS_FEAT_RE = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\s", re.IGNORECASE)


def _move_featuring_to_title(title: str, artist: str) -> tuple[str, str]:
    """Keep a guest singer out of the video's artist.

    "for KING & COUNTRY ft. Courtney - Without You.mp4" files under
    "for KING & COUNTRY" with the title "Without You (feat. Courtney)", so
    nothing in the name is lost and no made-up artist is created per guest.
    Only an explicit feat./ft./
    featuring is split off: "Simon & Garfunkel - The Boxer.mp4" stays with
    Simon & Garfunkel (the music side's split_artists would credit Simon
    and Garfunkel separately, which a video, with one artist, can't do)."""
    match = _ARTIST_FEAT_RE.search(artist)
    if not match or match.start() == 0:
        return title, artist
    guests = (match.group("a") or match.group("b") or "").strip()
    main = artist[: match.start()].strip()
    if not main or not guests:
        return title, artist
    if not _TITLE_HAS_FEAT_RE.search(title):
        title = f"{title} (feat. {guests})"
    return title, main


def repair_video_artists(session: Session) -> int:
    """File every video under the artist its file name gives with the
    current rules - guests moved to the title (see _move_featuring_to_title)
    and merged spellings ("for KING + COUNTRY") resolved through
    library.get_or_create_artist's aliases. No file is opened; only rows
    whose title or artist comes out different change. Artists left with
    nothing pointing at them are removed. Returns how many videos changed.
    One-time per data folder (ui/app.py)."""
    changed = 0
    left: set[int] = set()
    for video in list(session.scalars(select(Video))):
        # a library made on Windows keeps its "D:\\Videos\\..." paths on the
        # Mint PC too - read those as Windows paths there
        path = PureWindowsPath(video.path) if "\\" in video.path else Path(video.path)
        title, artist_name = _parse_video_title_artist(path)
        artist = lib.get_or_create_artist(session, artist_name)
        if video.title == title and video.artist_id == artist.id and video.artist_display == artist.name:
            continue
        if video.artist_id is not None and video.artist_id != artist.id:
            left.add(video.artist_id)
        video.title = title
        video.title_key = normalize(title)
        video.artist_display = artist.name
        video.artist_id = artist.id
        changed += 1
    session.flush()
    if left:
        _remove_unused_artists(session, left)
    return changed


#: a folder with one of these names (any depth, any capitalization) holds
#: movies - full-length films, concert films and DVD rips - rather than
#: music videos. D:\\Movies is where James keeps them.
MOVIE_FOLDER_NAMES = frozenset({"movies"})


def _as_path(path: PurePath | str) -> PurePath:
    """A stored path read the way it was written - a library made on
    Windows keeps its "D:\\Movies\\..." paths on the Mint PC too."""
    text = str(path)
    return PureWindowsPath(text) if "\\" in text else PurePath(text)


def media_type_for_path(path: PurePath | str) -> str:
    """Video.MEDIA_MOVIE for a file anywhere under a "Movies" folder,
    Video.MEDIA_MUSIC_VIDEO otherwise. Decided by folder alone so it's the
    same answer on every PC and after every USB sync, with nothing to keep
    in step - moving a file between Videos and Movies re-files it on the
    next scan."""
    folders = _as_path(path).parts[:-1]
    if any(part.lower() in MOVIE_FOLDER_NAMES for part in folders):
        return Video.MEDIA_MOVIE
    return Video.MEDIA_MUSIC_VIDEO


def classify_media_types(session: Session) -> int:
    """Set every video's media_type from its folder (media_type_for_path).
    No file is opened. Returns how many rows changed. One-time per data
    folder for libraries scanned before the column existed (ui/app.py);
    scans keep it right from then on."""
    changed = 0
    for video in session.scalars(select(Video)):
        kind = media_type_for_path(video.path)
        if video.media_type != kind:
            video.media_type = kind
            changed += 1
    session.flush()
    return changed


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

    `force=True` ("Re-read all tags" scan option in
    ui/views/settings/ - audio and video scan together under one
    `ScanThread`) bypasses the unchanged-file shortcut below, mirroring
    services/scanner.py:import_file's own `force` flag.
    """
    stat = path.stat()
    existing = session.scalar(select(Video).where(Video.path == str(path)))
    if existing is None and config.CASE_INSENSITIVE_PATHS:
        # same file under another capitalization (see
        # scanner.find_media_file) - keep the row, follow the new spelling
        existing = session.scalar(
            select(Video).where(path_matches(Video.path, path)).limit(1)
        )
        if existing is not None:
            existing.path = str(path)
    if existing is not None:
        # cheap (no file read), so kept up to date even on the unchanged-file
        # shortcut below
        existing.media_type = media_type_for_path(path)
        if not force and existing.mtime == stat.st_mtime and existing.size_bytes == stat.st_size:
            existing.is_missing = False
            existing.last_seen_at = _now()
            result.unchanged += 1
            return existing
        result.updated += 1
        # a changed file gets its thumbnail found again
        # (services/video_details.py)
        existing.thumbnail_path = None
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
    video.media_type = media_type_for_path(path)
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
    """Import every video file under `root`. `should_stop` (Settings'
    Cancel button), checked once per file, `break`s rather than raising,
    like scanner.py:scan_folder: there's no per-file savepoint (see the
    `session.rollback()` comment below), so a clean break lets the
    end-of-scan bookkeeping (WatchedFolder timestamp, flush) run over
    whatever was imported before the stop."""
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
    bookkeeping. The video twin of scanner.import_paths (used after a USB
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
    of scanner.remove_skipped_folder_files. Cleans up what an older scan
    imported from those folders (e.g. GoodSync's _gsdata_\\_saved_ old
    versions, sometimes filed under an artist called "_saved_").

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
    same convention, `progress` and safe-via-break `should_stop` as
    scanner.py:mark_missing_files."""
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
    one is wired to a manual, confirmed Settings button ("Purge missing
    videos…"): it's irreversible, and a library carried over from another
    OS's paths can show every video "missing" at once.
    Call `mark_missing_videos` first (or run a scan) so `is_missing` is
    actually current before purging by it.
    """
    candidates = list(session.scalars(select(Video).where(Video.is_missing.is_(True))))
    for video in candidates:
        session.delete(video)
    if candidates:
        session.flush()
    return len(candidates)
