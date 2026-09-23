"""Filesystem scanner: audio files -> Artist / Release / Track / MediaFile rows.

Tag reading goes through mutagen's `File(easy=True)` where possible and falls
back to format-specific frames for the fields Easy mode omits (album artist on
some formats, embedded art, disc totals).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import (
    ChartEntry,
    Credit,
    JukeboxSlot,
    MediaFile,
    PlayEvent,
    PlaylistItem,
    Release,
    ReleaseFormat,
    Track,
    WatchedFolder,
)
from . import library as lib
from .matching import normalize, split_artists

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]

#: album-artist names (post normalize()) that mark a release as a compilation
#: even when the file's own "compilation" flag was never set
COMPILATION_ARTIST_KEYS = {"various artists", "various", "va", "various performers"}


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    #: tracks deleted outright by `purge_orphaned_tracks` - see its
    #: docstring (2026-09-07 follow-up)
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.scanned} files scanned - {self.added} added, "
            f"{self.updated} updated, {self.unchanged} unchanged, "
            f"{self.missing} missing, {self.removed} removed, "
            f"{len(self.errors)} errors"
        )


@dataclass
class TrackTags:
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_no: Optional[int] = None
    track_total: Optional[int] = None
    disc_no: int = 1
    disc_total: Optional[int] = None
    year: Optional[int] = None
    released: Optional[str] = None
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    label: str = ""
    catalog_number: str = ""
    barcode: str = ""
    country: str = ""
    media_format: str = "File"
    isrc: str = ""
    bpm: Optional[float] = None
    musical_key: str = ""
    comment: str = ""
    compilation: bool = False
    #: True if the file itself carried an album-artist tag, before the
    #: single-file fallback below overwrites `album_artist` with a guess.
    #: `_folder_album_artist` needs this to tell "every track in this folder
    #: genuinely says Various Artists" apart from "no track said anything, so
    #: each one silently fell back to its own performer".
    had_explicit_album_artist: bool = False
    duration_ms: Optional[int] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    codec: str = ""
    cover: Optional[bytes] = None


# --------------------------------------------------------------------------
# tag extraction
# --------------------------------------------------------------------------


def _first(mapping, *keys) -> str:
    for key in keys:
        try:
            val = mapping.get(key)
        except Exception:
            val = None
        if not val:
            continue
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
        if val is None:
            continue
        return str(val).strip()
    return ""


def _first_id3_comment(raw) -> str:
    """Read a COMM (comment) frame straight off the raw ID3 tags.

    `_first(src, "comment", "COMM::eng", ...)` above never finds a comment on
    an MP3, no matter what is actually tagged. `src` is the *easy* wrapper
    whenever one loads (true for virtually every MP3), and EasyID3 simply
    has no "comment" key at all - and "COMM::eng" is a raw-ID3 HashKey, which
    isn't a valid EasyID3 key either, so it can't be found through the easy
    wrapper. FLAC/Vorbis and MP4 comments work fine (both expose "comment"
    natively through their easy wrappers); only ID3/MP3 needed this.

    Falls back to the raw (non-easy) tags object and matches any COMM frame
    regardless of language or description, since real-world taggers are
    inconsistent about both.
    """
    id3_tags = getattr(raw, "tags", None)
    if id3_tags is None:
        return ""
    try:
        keys = list(id3_tags.keys())
    except Exception:
        return ""
    for key in keys:
        if not str(key).startswith("COMM"):
            continue
        try:
            text = id3_tags[key].text
        except Exception:
            continue
        if not text:
            continue
        val = text[0] if isinstance(text, (list, tuple)) else text
        if val:
            return str(val).strip()
    return ""


def _int_of(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", str(text))
    return int(m.group()) if m else None


def _split_pair(text: str) -> tuple[Optional[int], Optional[int]]:
    """'3/12' -> (3, 12)."""
    if not text:
        return None, None
    parts = str(text).split("/")
    return _int_of(parts[0]), (_int_of(parts[1]) if len(parts) > 1 else None)


def _extract_cover(audio, path: Path) -> Optional[bytes]:
    try:
        if isinstance(audio, FLAC) and audio.pictures:
            return audio.pictures[0].data
        if isinstance(audio, MP4):
            covers = audio.tags.get("covr") if audio.tags else None
            if covers:
                return bytes(covers[0])
        tags = getattr(audio, "tags", None)
        if tags is not None:
            for key in getattr(tags, "keys", lambda: [])():
                if str(key).startswith("APIC"):
                    return tags[key].data
        # last resort: ID3 attached to a non-mp3 container
        if path.suffix.lower() == ".mp3":
            id3 = ID3(str(path))
            for key in id3.keys():
                if key.startswith("APIC"):
                    return id3[key].data
    except Exception:
        pass
    return None


def read_tags(path: Path) -> TrackTags:
    """Best-effort metadata read. Never raises for a merely tagless file."""
    tags = TrackTags()
    audio = mutagen.File(str(path), easy=True)
    raw = mutagen.File(str(path))

    info = getattr(raw, "info", None) or getattr(audio, "info", None)
    if info is not None:
        if getattr(info, "length", None):
            tags.duration_ms = int(info.length * 1000)
        tags.bitrate = getattr(info, "bitrate", None)
        tags.sample_rate = getattr(info, "sample_rate", None)
        tags.channels = getattr(info, "channels", None)
        tags.codec = type(info).__module__.split(".")[-1]

    src = audio if audio is not None else raw
    if src is None:
        tags.title = path.stem
        return tags

    tags.title = _first(src, "title", "TIT2", "\xa9nam") or path.stem
    tags.artist = _first(src, "artist", "TPE1", "\xa9ART")
    tags.album_artist = _first(
        src, "albumartist", "album artist", "TPE2", "aART", "ALBUMARTIST"
    )
    tags.had_explicit_album_artist = bool(tags.album_artist)
    tags.album = _first(src, "album", "TALB", "\xa9alb")
    tags.track_no, tags.track_total = _split_pair(
        _first(src, "tracknumber", "TRCK", "trkn")
    )
    disc, disc_total = _split_pair(_first(src, "discnumber", "TPOS", "disk"))
    tags.disc_no = disc or 1
    tags.disc_total = disc_total
    date = _first(src, "date", "originaldate", "year", "TDRC", "TYER", "\xa9day")
    tags.released = date or None
    tags.year = _int_of(date[:4]) if date else None
    genre = _first(src, "genre", "TCON", "\xa9gen")
    tags.genres = [g.strip() for g in re.split(r"[;/,]", genre) if g.strip()] if genre else []
    style = _first(src, "style", "STYLE")
    tags.styles = [s.strip() for s in re.split(r"[;/,]", style) if s.strip()] if style else []
    tags.label = _first(src, "organization", "label", "TPUB", "publisher", "LABEL")
    tags.catalog_number = _first(src, "catalognumber", "CATALOGNUMBER", "TXXX:CATALOGNUMBER")
    tags.barcode = _first(src, "barcode", "BARCODE")
    tags.country = _first(src, "releasecountry", "RELEASECOUNTRY")
    tags.media_format = _first(src, "media", "MEDIA") or "File"
    tags.isrc = _first(src, "isrc", "TSRC", "ISRC")
    bpm = _first(src, "bpm", "TBPM")
    tags.bpm = float(_int_of(bpm)) if _int_of(bpm) else None
    tags.musical_key = _first(src, "initialkey", "TKEY", "KEY")
    tags.comment = _first(src, "comment", "COMM::eng", "\xa9cmt") or _first_id3_comment(raw)
    comp = _first(src, "compilation", "TCMP", "cpil")
    tags.compilation = comp in ("1", "True", "true", "yes")

    tags.cover = _extract_cover(raw, path)

    if not tags.artist:
        tags.artist = tags.album_artist or "Unknown Artist"
    if not tags.album_artist:
        tags.album_artist = "Various Artists" if tags.compilation else tags.artist
    if not tags.album:
        tags.album = path.parent.name or "Unknown Album"
    return tags


def _fallback_from_path(path: Path, tags: TrackTags) -> TrackTags:
    """Fill obvious gaps from `Artist/Album/01 Title.ext` layouts.

    2026-09-07 follow-up (James: a live-bootleg WAV with no title tag at
    all showed up as "01 Midwest Midnight" instead of "Midwest Midnight").
    `read_tags()` falls back to the raw filename stem for `title` when a
    file carries no title tag, so an untagged "01 Midwest Midnight.wav"
    kept its leading track-number prefix baked right into the title. The
    same regex that already pulls `track_no` out of that prefix now also
    strips it from the title - but only when `tags.title == path.stem`,
    i.e. only when the title really did come from the raw filename and
    isn't a genuine tag that merely happens to start with a number.

    2026-09-13 fix (found while writing this module's first tests, not
    reported): the artist/album-artist half of this fallback never
    actually fired. `read_tags()` always runs first at both real call
    sites (`import_file`, `scan_folder`) and already upgrades a blank
    artist to the literal string "Unknown Artist" before returning it -
    so by the time this function saw it, `tags.artist` was never really
    falsy, and `tags.artist or grand.name` silently kept "Unknown Artist"
    instead of ever substituting the folder name. The guard above already
    correctly treated "Unknown Artist" as "no real artist"; the
    assignment below just never matched that guard. Every untagged file
    has been filed under "Unknown Artist" instead of its parent folder's
    name until now.
    """
    if tags.artist in ("", "Unknown Artist"):
        parent = path.parent
        grand = parent.parent
        if grand and grand.name and grand.name not in (".", "/"):
            tags.artist = grand.name
            if tags.album_artist in ("", "Unknown Artist"):
                tags.album_artist = grand.name
    m = re.match(r"^\s*(\d{1,3})[\s._-]+", path.stem)
    if m:
        if not tags.track_no:
            tags.track_no = int(m.group(1))
        if tags.title == path.stem:
            stripped = path.stem[m.end():].strip()
            tags.title = stripped or tags.title
    return tags


def _save_cover(data: bytes, release_id: int) -> Optional[str]:
    if not data:
        return None
    try:
        config.ensure_dirs()
        digest = hashlib.sha1(data).hexdigest()[:16]
        ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        out = config.ART_DIR / f"release_{release_id}_{digest}{ext}"
        if not out.exists():
            out.write_bytes(data)
        return str(out)
    except Exception as exc:  # pragma: no cover
        log.warning("cover save failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def iter_audio_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in config.AUDIO_EXTENSIONS:
                yield p


def _folder_album_artist(tags_by_path: dict[Path, TrackTags]) -> tuple[str, bool]:
    """Decide the one album artist an entire folder's tracks file under.

    An explicit album-artist tag (present on any file in the folder) always
    wins - the common case, and the most trustworthy signal, since it's how
    a properly tagged compilation announces itself. When several files
    disagree on it (rare, usually a tagging mistake on one track) the most
    common value wins.

    Failing that - no file in the folder has an album-artist tag at all -
    tracks that all share the same performer are an ordinary single-artist
    album, same as the old per-file fallback. Tracks that don't are exactly
    what an *untagged* various-artists compilation looks like: one folder,
    one album title, a different performer named on every file and nothing
    ever says "Various Artists" outright. Filing each track under its own
    performer there used to fragment one physical album into one release
    per performer (e.g. a 10-track "Best of Women of Christian Music" box
    set showing up as 10 near-identical album tiles) - so that case is
    treated as a compilation instead.
    """
    explicit = [
        t.album_artist for t in tags_by_path.values() if t.had_explicit_album_artist
    ]
    if explicit:
        winner = Counter(explicit).most_common(1)[0][0]
        is_compilation = any(t.compilation for t in tags_by_path.values()) or (
            normalize(winner) in COMPILATION_ARTIST_KEYS
        )
        return winner, is_compilation

    performers = {normalize(t.artist): t.artist for t in tags_by_path.values() if t.artist}
    if len(performers) <= 1:
        shared = next(iter(performers.values()), "Unknown Artist")
        return shared, any(t.compilation for t in tags_by_path.values())
    return "Various Artists", True


def _needs_import(session: Session, path: Path, force: bool = False) -> bool:
    """Cheap pre-check mirroring `import_file`'s own unchanged-file
    shortcut, used to decide which files are worth reading tags for at all
    before grouping them by folder. An unchanged file keeps whatever release
    it was already filed under - both for scan speed on a large library, and
    because it already went through this same folder logic the first time
    it was imported.

    `force=True` (2026-09-17, chasing why a Comment-tag fix in `read_tags`
    didn't do anything for James's real library - see `import_file`'s own
    `force` docstring) always says yes, regardless of mtime/size: it's the
    only way to make an existing, on-disk-unchanged file's tags ever get
    re-read at all."""
    if force:
        return True
    stat = path.stat()
    existing = session.scalar(select(MediaFile).where(MediaFile.path == str(path)))
    if existing is None:
        return True
    return not (existing.mtime == stat.st_mtime and existing.size_bytes == stat.st_size)


def _quality_score(tags: TrackTags) -> int:
    fields = [
        tags.title, tags.artist, tags.album, tags.album_artist,
        str(tags.track_no or ""), str(tags.year or ""),
        ",".join(tags.genres), tags.label, tags.catalog_number,
        tags.isrc, tags.country,
    ]
    filled = sum(1 for f in fields if f)
    return int(100 * filled / len(fields))


def import_file(
    session: Session,
    path: Path,
    result: ScanResult,
    *,
    tags: Optional[TrackTags] = None,
    album_artist_override: Optional[str] = None,
    is_compilation_override: Optional[bool] = None,
    force: bool = False,
) -> Optional[Track]:
    """Import or refresh a single audio file. Returns the Track.

    `tags` lets a caller that already read the file (namely `scan_folder`,
    which has to read every track in a folder up front to make the
    folder-wide album-artist call below) pass it in rather than this
    function reading the file a second time. `album_artist_override` /
    `is_compilation_override` carry that folder-wide decision
    (`_folder_album_artist`) in; without them this file decides alone from
    its own tags, exactly as before - which is what a direct call (as the
    tests and a single-file CLI import do) still gets.

    `force=True` (2026-09-17, James: "still blank after a rescan") skips
    the unchanged-file shortcut below even when the file's mtime/size on
    disk haven't moved. Fixing a tag-reading bug in `read_tags` (like the
    MP3 Comment-frame fix earlier this session) does nothing at all for a
    library that's already been scanned - every file everywhere is
    "unchanged" from the scanner's point of view, since nothing touched
    the files themselves - so there was previously no way to make an
    already-imported library pick up a tag-reading fix short of deleting
    and reimporting the whole thing. `force` re-runs this whole function
    for a file even when its `MediaFile` row already matches, re-deriving
    every tag-sourced field (including `track.comment`) from a fresh
    `read_tags()` call. Every write below this point (`get_or_create_*`,
    `add_credit`) is already idempotent - the same "changed file" path
    already re-runs them on every real edit - so forcing an unchanged file
    through is safe, just slower than skipping it.
    """
    stat = path.stat()
    existing = session.scalar(select(MediaFile).where(MediaFile.path == str(path)))
    if existing is not None:
        if not force and existing.mtime == stat.st_mtime and existing.size_bytes == stat.st_size:
            existing.is_missing = False
            existing.last_seen_at = dt.datetime.now(dt.timezone.utc)
            result.unchanged += 1
            return existing.track
        result.updated += 1
    else:
        result.added += 1

    tags = tags if tags is not None else _fallback_from_path(path, read_tags(path))

    if album_artist_override is not None:
        album_artist = album_artist_override
        is_compilation = bool(is_compilation_override)
    else:
        album_artist = tags.album_artist or tags.artist
        # a lot of taggers never set the ID3/Vorbis "compilation" flag at all,
        # so the album artist itself is the more reliable tell - a release
        # explicitly credited to "Various Artists" is a compilation whether or
        # not the flag made it into the file
        is_compilation = tags.compilation or normalize(album_artist) in COMPILATION_ARTIST_KEYS
    master = lib.get_or_create_master(session, tags.album, album_artist, tags.year)
    release = lib.get_or_create_release(
        session,
        title=tags.album,
        artist_display=album_artist,
        year=tags.year,
        catalog_number=tags.catalog_number or None,
        released=tags.released,
        country=tags.country or None,
        barcode=tags.barcode or None,
        is_compilation=is_compilation,
    )
    release.is_compilation = is_compilation
    release.master_id = master.id
    if master.main_release_id is None:
        master.main_release_id = release.id
    release.data_quality = max(release.data_quality, _quality_score(tags))
    if tags.label:
        label = lib.get_or_create_label(session, tags.label)
        if label is not None:
            release.label_id = label.id
    for gname in tags.genres:
        g = lib.get_or_create_genre(session, gname)
        if g is not None and g not in release.genres:
            release.genres.append(g)
    for sname in tags.styles:
        s = lib.get_or_create_style(session, sname)
        if s is not None and s not in release.styles:
            release.styles.append(s)
    if not release.formats:
        session.add(
            ReleaseFormat(
                release_id=release.id,
                name=tags.media_format or "File",
                qty=1,
                descriptions=path.suffix.lstrip(".").upper(),
            )
        )
    if tags.cover and not release.cover_path:
        release.cover_path = _save_cover(tags.cover, release.id)

    # release-level main credit, and the FK the Artists grid actually browses by
    aa = lib.get_or_create_artist(session, album_artist)
    lib.add_credit(session, aa, Credit.ROLE_MAIN, release=release)
    release.album_artist_id = aa.id

    # track
    title_key = normalize(tags.title)
    track = session.scalar(
        select(Track).where(
            Track.release_id == release.id,
            Track.title_key == title_key,
            Track.disc_no == tags.disc_no,
        )
    )
    if track is None:
        track = Track(release_id=release.id, title=tags.title, title_key=title_key)
        session.add(track)
        session.flush()
    track.track_no = tags.track_no
    track.disc_no = tags.disc_no
    track.position = (
        f"{tags.disc_no}-{tags.track_no:02d}"
        if tags.disc_total and tags.disc_total > 1 and tags.track_no
        else (str(tags.track_no) if tags.track_no else None)
    )
    track.artist_display = tags.artist
    track.duration_ms = tags.duration_ms
    track.isrc = tags.isrc or None
    track.bpm = tags.bpm
    track.musical_key = tags.musical_key or None
    track.comment = tags.comment or None

    names = split_artists(tags.artist)
    for idx, name in enumerate(names):
        artist = lib.get_or_create_artist(session, name)
        role = Credit.ROLE_MAIN if idx == 0 else Credit.ROLE_FEATURING
        lib.add_credit(session, artist, role, track=track, position=idx)

    mf = existing or MediaFile(track_id=track.id, path=str(path))
    mf.track_id = track.id
    mf.size_bytes = stat.st_size
    mf.mtime = stat.st_mtime
    mf.codec = tags.codec or path.suffix.lstrip(".")
    mf.bitrate = tags.bitrate
    mf.sample_rate = tags.sample_rate
    mf.channels = tags.channels
    mf.duration_ms = tags.duration_ms
    mf.is_missing = False
    mf.last_seen_at = dt.datetime.now(dt.timezone.utc)
    if existing is None:
        session.add(mf)
    return track


def scan_folder(
    session: Session,
    root: Path | str,
    progress: Optional[ProgressFn] = None,
    result: Optional[ScanResult] = None,
    force: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    root = Path(root).expanduser()
    result = result or ScanResult()
    if not root.exists():
        result.errors.append(f"{root} does not exist")
        return result

    files = list(iter_audio_files(root))
    total = len(files)

    # Only a new or changed file needs its tags read at all. Group just
    # those by folder so a folder-wide album-artist decision can be made
    # before any of them turn into a Release - see _folder_album_artist.
    # An unchanged file is left exactly alone: cheap on a big rescan, and it
    # already went through this same folder logic the first time around.
    # `force=True` (see import_file's docstring) makes every file "need"
    # import regardless, which is the only way an existing library ever
    # picks up a `read_tags()` bugfix - it has no other reason to re-read a
    # file whose mtime/size on disk never changed.
    to_process = [p for p in files if force or _needs_import(session, p)]
    by_folder: dict[Path, list[Path]] = {}
    for path in to_process:
        by_folder.setdefault(path.parent, []).append(path)

    tags_cache: dict[Path, TrackTags] = {}
    overrides: dict[Path, tuple[str, bool]] = {}
    for folder_files in by_folder.values():
        folder_tags = {p: _fallback_from_path(p, read_tags(p)) for p in folder_files}
        tags_cache.update(folder_tags)
        decision = _folder_album_artist(folder_tags)
        for p in folder_files:
            overrides[p] = decision

    for idx, path in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        try:
            if path in overrides:
                album_artist, is_compilation = overrides[path]
                import_file(
                    session,
                    path,
                    result,
                    tags=tags_cache[path],
                    album_artist_override=album_artist,
                    is_compilation_override=is_compilation,
                    force=force,
                )
            else:
                import_file(session, path, result, force=force)
        except Exception as exc:  # keep going on one bad file
            log.exception("failed to import %s", path)
            result.errors.append(f"{path.name}: {exc}")
            # A failed flush leaves the Session unusable (SQLAlchemy refuses
            # any further query or flush on it) until it's explicitly rolled
            # back - without this, "keep going on one bad file" was a lie:
            # every file after the first failure, every remaining watched
            # folder, and mark_missing_files/purge_orphaned_tracks below all
            # failed the same way, with the SAME generic "transaction has
            # been rolled back" message masking whatever actually went wrong
            # with THIS file (James, 2026-09-08 - a scan reported total
            # failure over one bad track).
            #
            # There's no per-file savepoint (see db/session.py - one
            # session_scope, one commit, at the very end of the whole
            # multi-folder scan), so this rollback discards every row
            # flushed-but-not-committed so far in THIS scan, not just the
            # bad file's - i.e. every earlier file already processed in this
            # same run. That's still strictly better than before (the old
            # behavior lost the entire scan, silently, with not even the
            # watched-folder timestamps updated) - anything rolled back here
            # just gets re-detected as needing import and picked back up
            # cleanly on the next scan, same as if it had never run.
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
    folder.last_scan_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return result


# 2026-09-22 - James: "add a real cancel button that stops any process
# running within Settings". `should_stop` above, when given, is checked
# once per file, *before* that file's own import starts - deliberately a
# `break`, never a raise, so this never takes the `session.rollback()` path
# a few lines up (that path exists only for a genuinely bad file, and
# throws away every row flushed-but-not-committed in the whole scan so
# far - see its own comment). A clean break instead leaves whatever's
# already been imported (flushed or not) to get picked up by the one
# `session.flush()`/commit this whole scan still ends with normally, same
# as if the folder's remaining files had simply not been reached yet -
# they'll just get picked back up, cleanly, on the next scan.


def rescan_all(
    session: Session, progress: Optional[ProgressFn] = None, force: bool = False
) -> ScanResult:
    result = ScanResult()
    folders = list(
        session.scalars(select(WatchedFolder).where(WatchedFolder.enabled.is_(True)))
    )
    for folder in folders:
        scan_folder(session, folder.path, progress, result, force=force)
    result.missing = mark_missing_files(session)
    result.removed = purge_orphaned_tracks(session)
    return result


#: how often mark_missing_files below reports progress - each item here is
#: just one os.path.exists() stat call (unlike the actual tag-reading work
#: scan_folder's own `idx % 5` throttles), so this can afford to be much
#: coarser - same reasoning and same value as metadata_health.py's own
#: _LYRICS_PROGRESS_EVERY, which walks a comparable per-track stat call.
_MISSING_FILES_PROGRESS_EVERY = 250


def mark_missing_files(
    session: Session,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Flag rows whose file has disappeared instead of deleting metadata.

    2026-09-22 - James: "does the progress bar also work on ... verify
    files" (it didn't - "Verify files" ran this entirely on the UI thread
    with no feedback, same problem class the "Missing metadata" dashboard
    had before its own progress bar). `progress`, when given, is called
    `(done, total, "")`.

    2026-09-22 same-day follow-up (James: "add a real cancel button...") -
    `should_stop`, checked once per row, `break`s rather than raising -
    each row's `mf.is_missing` flip is an independent in-memory mutation
    with no per-row commit, so a break just leaves the remaining rows
    unchecked for this run, same as `import_artist_images`'s own
    should_stop above."""
    total = session.scalar(select(func.count(MediaFile.id))) or 0
    count = 0
    for i, mf in enumerate(session.scalars(select(MediaFile)), start=1):
        if should_stop and should_stop():
            break
        exists = os.path.exists(mf.path)
        if not exists and not mf.is_missing:
            mf.is_missing = True
            count += 1
        elif exists and mf.is_missing:
            mf.is_missing = False
        if progress and (i % _MISSING_FILES_PROGRESS_EVERY == 0 or i == total):
            progress(i, total, "")
    return count


def purge_orphaned_tracks(session: Session) -> int:
    """Delete tracks left with no playable file after a scan.

    2026-09-07 follow-up. Renaming a file on disk (even just to fix a
    stray "01 " prefix that `_fallback_from_path` used to leave in an
    untagged title - see its docstring) gives it a new path, and
    `import_file` matches a `MediaFile` by exact path, so the renamed file
    is imported as a brand-new track while the *original* track - now
    pointing at a path that no longer exists, flagged missing by
    `mark_missing_files` above - is left behind. Nothing about the file
    system tells the scanner "this is the same song, just renamed", so
    without this cleanup that original track sits in the library forever,
    indistinguishable from a real duplicate in every search and browse
    view.

    James chose to run this automatically on every scan rather than
    behind a confirmation button. A track only qualifies for outright
    deletion when it has no history to lose: no playlist placement, no
    chart match, no play event, and no jukebox slot. All four of those
    foreign keys cascade or null out at the database level if a
    qualifying track were deleted without this check, which would
    otherwise silently drop a song from a playlist, unmatch a chart entry
    the user placed by hand, or empty a side of a jukebox title strip
    without any warning - this function exists specifically to make sure
    none of that happens automatically. A track with any of that history
    keeps showing up exactly as it does today (missing-file, visible, "no
    available file" if tapped) rather than being removed. In the ordinary
    rename/retag case this covers everything, since a fresh orphan has had
    no chance to accumulate any history yet.

    2026-09-07 fix: the jukebox check (`JukeboxSlot.side_a_track_id`/
    `side_b_track_id`) was missing entirely until James was about to move
    his whole music library to a new PC - the exact scenario that makes
    this matter, since every track's file goes "missing" under its old
    path the moment a rescan sees the new one, and a track sitting only on
    the jukebox board (no playlist/chart/play-event history of its own)
    would otherwise have been silently deleted, quietly emptying that side
    of its title strip (`JukeboxSlot`'s FK is `ondelete="SET NULL"`, so it
    wouldn't even have raised an error - just gone missing).
    """
    playable_track_ids = select(MediaFile.track_id).where(
        MediaFile.is_missing.is_(False)
    )
    candidates = session.scalars(
        select(Track).where(Track.id.not_in(playable_track_ids))
    ).all()
    removed = 0
    for track in candidates:
        has_history = (
            session.scalar(
                select(PlaylistItem.id)
                .where(PlaylistItem.track_id == track.id)
                .limit(1)
            )
            or session.scalar(
                select(ChartEntry.id).where(ChartEntry.track_id == track.id).limit(1)
            )
            or session.scalar(
                select(PlayEvent.id).where(PlayEvent.track_id == track.id).limit(1)
            )
            or session.scalar(
                select(JukeboxSlot.id)
                .where(
                    or_(
                        JukeboxSlot.side_a_track_id == track.id,
                        JukeboxSlot.side_b_track_id == track.id,
                    )
                )
                .limit(1)
            )
        )
        if has_history:
            continue
        session.delete(track)
        removed += 1
    if removed:
        session.flush()
    return removed
