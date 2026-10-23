"""Filesystem scanner: audio files -> Artist / Release / Track / MediaFile rows.

Tag reading goes through mutagen's `File(easy=True)` where possible and falls
back to format-specific frames for the fields Easy mode omits (album artist on
some formats, embedded art, disc totals).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from sqlalchemy import delete, func, or_, select
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
from . import radio_folder
from . import replaygain
from .tracknum import is_side, parse_side
from .matching import normalize, split_artists

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]

#: album-artist names (post normalize()) that mark a release as a compilation
#: even when the file's own "compilation" flag was never set
COMPILATION_ARTIST_KEYS = {"various artists", "various", "va", "various performers"}


def is_various_artists(album_artist: Optional[str], performers: Iterable[Optional[str]] = (),
                       flagged: bool = False) -> bool:
    """Is this album a various-artists compilation - one that belongs to no
    single artist's page?

    An album artist of "Various Artists" (or the like) is always a
    compilation. The Compilation flag (TCMP) only counts when the album
    artist isn't the one performing the tracks: MusicBrainz types a
    single-artist "Very Best of" as "Album, Compilation" and Picard writes
    the flag for it, so the flag alone doesn't mean various artists."""
    key = normalize(album_artist)
    if key in COMPILATION_ARTIST_KEYS:
        return True
    if not flagged:
        return False
    keys = {normalize(p) for p in performers if p}
    return bool(keys) and keys != {key}


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    #: tracks deleted outright by `purge_orphaned_tracks` - see its docstring
    removed: int = 0
    #: files that had been folded into another artist's track and now have
    #: their own (see import_file's track lookup)
    separated: int = 0
    #: the tracks those files left - their chart matches get looked at again
    separated_from: set[int] = field(default_factory=set)
    #: old track id -> new, for every re-read file that now belongs to a
    #: different track (a retag changed its title or album)
    retracked: dict[int, int] = field(default_factory=dict)
    #: of those, songs whose history (rating, plays, playlists, jukebox,
    #: chart matches) moved over because no file was left on the old track
    history_moved: dict[int, int] = field(default_factory=dict)
    #: old-time radio files taken back out of the music library - they
    #: belong to the Radio page (see remove_radio_files)
    radio_removed: int = 0
    #: file rows inside a folder scans skip (GoodSync's _gsdata_ and the
    #: like) taken back out - see remove_skipped_folder_files
    skipped_removed: int = 0
    #: rows for a file already in the library under another capitalization
    #: (see merge_case_duplicate_files)
    case_duplicates_removed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        separated = (f", {self.separated} recordings separated from another artist's"
                     if self.separated else "")
        radio = (f", {self.radio_removed} old-time radio files left to the Radio page"
                 if self.radio_removed else "")
        skipped = (f", {self.skipped_removed} copies in sync/system folders removed"
                   if self.skipped_removed else "")
        if self.case_duplicates_removed:
            skipped += (f", {self.case_duplicates_removed} duplicate entries "
                        "differing only in capitalization removed")
        return (
            f"{self.scanned} files scanned - {self.added} added, "
            f"{self.updated} updated, {self.unchanged} unchanged, "
            f"{self.missing} missing, {self.removed} removed, "
            f"{len(self.errors)} errors{separated}{radio}{skipped}"
        )


@dataclass
class TrackTags:
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    track_no: Optional[int] = None
    #: a record side written as the track number ("A", "B", "A1")
    side: Optional[str] = None
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
    #: True if the title came from the file's own title tag, not its
    #: filename - `_fallback_from_path` only strips a track-number prefix
    #: from a filename title
    had_title_tag: bool = False
    duration_ms: Optional[int] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    codec: str = ""
    cover: Optional[bytes] = None
    #: ReplayGain tags, if the file has them (services/replaygain.py)
    gain: Optional["replaygain.GainInfo"] = None


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


def _all_values(mapping, *keys) -> str:
    """Every value of the first of `keys` the file has, joined with "; " -
    for genre, which a file may hold as several values."""
    for key in keys:
        try:
            val = mapping.get(key)
        except Exception:
            val = None
        if not val:
            continue
        vals = val if isinstance(val, (list, tuple)) else [val]
        parts = [_text_of(v) for v in vals]
        parts = [p for p in parts if p]
        if parts:
            return "; ".join(parts)
    return ""


def _text_of(val) -> str:
    if isinstance(val, bytes):  # an MP4 freeform atom
        return val.decode("utf-8", errors="replace").strip()
    text = getattr(val, "text", None)  # an ID3 frame
    if text is not None:
        return _text_of(text[0]) if text else ""
    return str(val).strip() if val is not None else ""


def _raw_first(raw, *keys) -> str:
    """A tag the easy wrapper can't see, read from the raw tags (MP4
    freeform atoms, ID3 TXXX frames in a .wav, ...)."""
    tags = getattr(raw, "tags", None)
    if tags is None:
        return ""
    for key in keys:
        try:
            val = tags.get(key)
        except Exception:
            val = None
        if not val:
            continue
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
        text = _text_of(val)
        if text:
            return text
    return ""


_FF = "----:com.apple.iTunes:"


def _first_id3_comment(raw) -> str:
    """Read a COMM (comment) frame straight off the raw ID3 tags.

    EasyID3 has no "comment" key, so `_first(src, "comment", ...)` never
    finds an MP3's comment through the easy wrapper (FLAC/Vorbis and MP4
    expose it natively). Matches any COMM frame regardless of language or
    description, since real-world taggers are inconsistent about both.
    """
    id3_tags = getattr(raw, "tags", None)
    if id3_tags is None:
        return ""
    try:
        keys = list(id3_tags.keys())
    except Exception:
        return ""
    # a plain comment (no description) first; iTunes' own "iTunNORM"/
    # "iTunSMPB" comments hold volume and gapless figures, never the comment
    def desc(key) -> str:
        return str(getattr(id3_tags[key], "desc", "") or "")

    def lang(key) -> str:
        return str(getattr(id3_tags[key], "lang", "") or "")

    comm = [k for k in keys if str(k).startswith("COMM")]
    comm = [k for k in comm if not desc(k).lower().startswith("itun")]
    # the same order the tag editor reads in (tag_writer._id3_comment):
    # plain comments first, English ones before other languages
    comm.sort(key=lambda k: (0 if not desc(k) else 1,
                             0 if lang(k) in ("eng", "XXX", "\x00\x00\x00", "") else 1))
    for key in comm:
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


_NO_CATALOG = {"none", "[none]", "n/a"}


def _distinct_catalog_numbers(text: str) -> str:
    """'986113-7, 986113-7, 986113-7' -> '986113-7'. Some taggers write one
    catalog number per label on a co-released album; since a release is
    looked up by its catalog number, the repeated form would miss the album
    already in the library and make a second copy. Different numbers are
    kept, in order."""
    seen: list[str] = []
    for part in (p.strip() for p in (text or "").split(",")):
        if part and part.lower() not in _NO_CATALOG and part.lower() not in (s.lower() for s in seen):
            seen.append(part)
    return ", ".join(seen)


def _distinct_label(text: str) -> str:
    """'RCA, RCA' -> 'RCA'. Only a label that is one name repeated is
    shortened - a comma can be part of a real name ("BMG Direct Marketing,
    Inc.") and co-labels ("Def Jam South, Disturbing tha Peace") stay as
    they are."""
    parts = [p.strip() for p in re.split(r"[,;]", text or "")]
    if len(parts) > 1 and all(p and p.lower() == parts[0].lower() for p in parts):
        return parts[0]
    return text


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

    # "Title", "Author" and the "WM/..." keys are Windows Media (.wma),
    # whose keys are capitalised and namespaced unlike every other format's
    tags.title = _first(src, "title", "TIT2", "\xa9nam", "Title")
    tags.had_title_tag = bool(tags.title)
    tags.title = tags.title or path.stem
    tags.artist = _first(src, "artist", "TPE1", "\xa9ART", "Author")
    tags.album_artist = _first(
        src, "albumartist", "album artist", "TPE2", "aART", "ALBUMARTIST", "WM/AlbumArtist"
    )
    tags.had_explicit_album_artist = bool(tags.album_artist)
    tags.album = _first(src, "album", "TALB", "\xa9alb", "WM/AlbumTitle")
    raw_track = _first(src, "tracknumber", "TRCK", "trkn", "WM/TrackNumber")
    side = parse_side(raw_track)
    if side is not None:
        # a record side ("A", "B", "A1") - see services/tracknum.py
        tags.track_no, tags.side = side
        tags.track_total = None
    else:
        tags.track_no, tags.track_total = _split_pair(raw_track)
    disc, disc_total = _split_pair(_first(src, "discnumber", "TPOS", "disk", "WM/PartOfSet"))
    tags.disc_no = disc or 1
    tags.disc_total = disc_total
    date = _first(src, "date", "originaldate", "year", "TDRC", "TYER", "\xa9day", "WM/Year")
    tags.released = date or None
    tags.year = _int_of(date[:4]) if date else None
    genre = _all_values(src, "genre", "TCON", "\xa9gen", "WM/Genre")
    tags.genres = [g.strip() for g in re.split(r"[;/,]", genre) if g.strip()] if genre else []
    style = _first(src, "style", "STYLE")
    tags.styles = [s.strip() for s in re.split(r"[;/,]", style) if s.strip()] if style else []
    # the _raw_first fallbacks are the tags the easy wrappers
    # don't map: an .m4a's freeform atoms, a .wav/.aiff's TXXX frames - all
    # written by the tag editor and MusicBrainz Picard
    tags.label = (_first(src, "organization", "label", "TPUB", "publisher", "LABEL", "WM/Publisher")
                  or _raw_first(raw, _FF + "LABEL"))
    tags.label = _distinct_label(tags.label)
    tags.catalog_number = _distinct_catalog_numbers(
        _first(src, "catalognumber", "CATALOGNUMBER", "TXXX:CATALOGNUMBER", "WM/CatalogNo")
        or _raw_first(raw, _FF + "CATALOGNUMBER"))
    tags.barcode = (_first(src, "barcode", "BARCODE", "WM/Barcode")
                    or _raw_first(raw, "TXXX:BARCODE", _FF + "BARCODE"))
    tags.country = (_first(src, "releasecountry", "RELEASECOUNTRY")
                    or _raw_first(raw, "TXXX:MusicBrainz Album Release Country",
                                  _FF + "MusicBrainz Album Release Country",
                                  "MusicBrainz/Album Release Country"))
    tags.media_format = _first(src, "media", "MEDIA") or "File"
    tags.isrc = _first(src, "isrc", "TSRC", "ISRC", "WM/ISRC") or _raw_first(raw, _FF + "ISRC")
    bpm = _first(src, "bpm", "TBPM")
    tags.bpm = float(_int_of(bpm)) if _int_of(bpm) else None
    tags.musical_key = _first(src, "initialkey", "TKEY", "KEY")
    tags.comment = _first(src, "comment", "COMM::eng", "\xa9cmt", "Description") or _first_id3_comment(raw)
    comp = (_first(src, "compilation", "TCMP", "cpil", "WM/IsCompilation")
            or _raw_first(raw, "cpil"))
    tags.compilation = comp in ("1", "True", "true", "yes")

    tags.cover = _extract_cover(raw, path)
    try:
        tags.gain = replaygain.read_gain_tags(raw)
    except Exception:  # pragma: no cover - never let levelling break a scan
        tags.gain = None

    if not tags.artist:
        tags.artist = tags.album_artist or "Unknown Artist"
    if not tags.album_artist:
        tags.album_artist = "Various Artists" if tags.compilation else tags.artist
    if not tags.album:
        tags.album = path.parent.name or "Unknown Album"
    return tags


#: the "(Official Music Video)" / "[Official Visualizer]" / "- Official
#: Visualizer" tails download tools leave on a video's filename, and a
#: trailing "(0)"/"(1)" copy number
_VIDEO_NOISE = re.compile(
    r"\s*(?:[(\[][^)\]]*\b(?:official|visuali[sz]er|video|lyrics?|audio|hd|4k)\b[^)\]]*[)\]]"
    r"|[-\u2013\u2014]\s*official\b.*$|\(\d\)\s*$)",
    re.IGNORECASE,
)


def _artist_title_from_stem(stem: str) -> Optional[tuple[str, str]]:
    """("Ella Langley", "20-20") from "Ella Langley - 20-20 (Official
    Visualizer)", or None when the name isn't "Artist - Title" - including
    "01 - Title", whose left side is a track number, not an artist."""
    if " - " not in stem:
        return None
    artist, _, title = stem.partition(" - ")
    artist = artist.strip()
    if not artist or not re.search(r"[^\W\d_]", artist):
        return None
    cleaned = title
    while True:
        shorter = _VIDEO_NOISE.sub("", cleaned).strip()
        if shorter == cleaned:
            break
        cleaned = shorter
    cleaned = cleaned or title.strip()
    return (artist, cleaned) if cleaned else None


def _fallback_from_path(path: Path, tags: TrackTags) -> TrackTags:
    """Fill obvious gaps from `Artist/Album/01 Title.ext` layouts.

    `read_tags()` has already turned a blank artist into "Unknown Artist",
    so that string counts as "no artist" here and is replaced by the
    grandparent folder's name.

    A leading track number ("01 Midwest Midnight") is taken as `track_no`
    and stripped from the title only when the title came from the filename
    (no title tag - `had_title_tag`). A file named after its own song
    ("99 Red Balloons.mp3" titled "99 Red Balloons") keeps its number, and
    so does a number run into another with a hyphen ("777-9311", "20-20",
    "5-10-15 Hours"), which is part of the title.
    """
    if tags.artist in ("", "Unknown Artist") and not tags.had_title_tag:
        # an untagged "Artist - Title.ext" (how music videos are usually
        # named) - the filename says who it is
        named = _artist_title_from_stem(path.stem)
        if named is not None:
            tags.artist, tags.title = named
            if tags.album_artist in ("", "Unknown Artist"):
                tags.album_artist = tags.artist
    if tags.artist in ("", "Unknown Artist"):
        parent = path.parent
        grand = parent.parent
        if grand and grand.name and grand.name not in (".", "/"):
            tags.artist = grand.name
            if tags.album_artist in ("", "Unknown Artist"):
                tags.album_artist = grand.name
    m = re.match(r"^\s*(\d{1,3})[\s._-]+", path.stem)
    if m and m.group().endswith("-") and path.stem[m.end():m.end() + 1].isdigit():
        m = None  # "777-9311": one title, not track 777
    if m and tags.had_title_tag and tags.title == path.stem:
        m = None  # "99 Red Balloons.mp3" titled "99 Red Balloons": the song's own number
    if m:
        if not tags.track_no:
            tags.track_no = int(m.group(1))
        if tags.title == path.stem:
            stripped = path.stem[m.end():].strip()
            tags.title = stripped or tags.title
    return tags


def _save_cover(data: bytes, release: "Release") -> Optional[str]:
    """Save a tag-embedded cover under its name-based file
    (`artwork\\<Album Artist> - <Title>.jpg` - see services/artwork_names.py).
    Never overwrites a picture that's already there: a
    downloaded or hand-picked cover (or one that arrived by USB sync) must
    survive a rescan."""
    if not data:
        return None
    from . import artwork_names

    return artwork_names.save_release_cover(release, data, overwrite=False)


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def iter_audio_files(root: Path, skip: Sequence[str] = ()) -> Iterable[Path]:
    """Every audio file under `root`. `skip` - normalised folders from
    services/radio_folder.roots - leaves the old-time radio folder out."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not config.is_skipped_dir(d)
            and not (skip and radio_folder.is_radio_path(os.path.join(dirpath, d), skip))
        ]
        for name in sorted(filenames):
            if config.is_hidden_name(name):
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
    album. Tracks that don't are what an *untagged* various-artists
    compilation looks like (one folder, one album title, a different
    performer on every file), so that case is treated as a compilation
    rather than split into one release per performer.
    """
    explicit = [
        t.album_artist for t in tags_by_path.values() if t.had_explicit_album_artist
    ]
    if explicit:
        winner = Counter(explicit).most_common(1)[0][0]
        is_compilation = is_various_artists(
            winner, (t.artist for t in tags_by_path.values()),
            flagged=any(t.compilation for t in tags_by_path.values()))
        return winner, is_compilation

    performers = {normalize(t.artist): t.artist for t in tags_by_path.values() if t.artist}
    if len(performers) <= 1:
        # one performer throughout: their album, whatever the flag says
        shared = next(iter(performers.values()), "Unknown Artist")
        return shared, normalize(shared) in COMPILATION_ARTIST_KEYS
    return "Various Artists", True


def path_matches(column, path: Path | str):
    """`column == path`, ignoring letter case where the file system does
    (config.CASE_INSENSITIVE_PATHS). NOCASE uses the
    ix_media_files_path_nocase index, so a lookup stays an index seek."""
    if config.CASE_INSENSITIVE_PATHS:
        return column.collate("NOCASE") == str(path)
    return column == str(path)


def find_media_file(session: Session, path: Path | str) -> Optional[MediaFile]:
    """The library's row for `path`: the exact spelling first, then - on a
    case-insensitive file system - any row differing only in letter case."""
    mf = session.scalar(select(MediaFile).where(MediaFile.path == str(path)))
    if mf is None and config.CASE_INSENSITIVE_PATHS:
        mf = session.scalar(select(MediaFile).where(path_matches(MediaFile.path, path)).limit(1))
    return mf


def _needs_import(session: Session, path: Path, force: bool = False) -> bool:
    """Cheap pre-check mirroring `import_file`'s own unchanged-file
    shortcut, used to decide which files are worth reading tags for at all
    before grouping them by folder. An unchanged file keeps whatever release
    it was already filed under - both for scan speed on a large library, and
    because it already went through this same folder logic the first time
    it was imported.

    `force=True` always says yes, regardless of mtime/size - the only way
    to re-read an unchanged file's tags (see `import_file`'s `force`)."""
    if force:
        return True
    stat = path.stat()
    existing = find_media_file(session, path)
    if existing is None:
        return True
    return not (existing.mtime == stat.st_mtime and existing.size_bytes == stat.st_size)


def _shared_track_files(session: Session, files: list[Path]) -> list[Path]:
    """The files in `files` whose track also has another file."""
    shared = set(session.scalars(
        select(MediaFile.path).where(MediaFile.track_id.in_(
            select(MediaFile.track_id).group_by(MediaFile.track_id)
            .having(func.count(MediaFile.id) > 1)
        ))
    ))
    if not shared:
        return []
    return [p for p in files if str(p) in shared]


def import_single(session: Session, path: Path | str) -> Optional[Track]:
    """Import one file the user picked by hand (Charts' Fix match "Browse
    for file..."), and return its own track. A file already in the library
    whose track also holds other files is re-read, so a version folded into
    another artist's track comes out as its own track instead of handing
    back the other artist's."""
    path = Path(path)
    result = ScanResult()
    if _shared_track_files(session, [path]):
        _reimport_on_same_album(session, path, result)
        if result.separated_from:
            _rematch_separated(session, result)
        session.flush()
        mf = find_media_file(session, path)
        return mf.track if mf is not None else None
    return import_file(session, path, result)


def _reimport_on_same_album(session: Session, path: Path, result: ScanResult) -> None:
    """Re-read one unchanged file and file it again - on the album it's
    already on, so only its track can change."""
    mf = find_media_file(session, path)
    release = mf.track.release if mf is not None and mf.track is not None else None
    if release is None or not release.artist_display:
        import_file(session, path, result, force=True)
        return
    before = mf.track_id
    import_file(
        session, path, result, force=True,
        album_artist_override=release.artist_display,
        is_compilation_override=release.is_compilation,
    )
    session.flush()
    if mf.track_id != before:
        result.separated += 1
        result.separated_from.add(before)


def _quality_score(tags: TrackTags) -> int:
    fields = [
        tags.title, tags.artist, tags.album, tags.album_artist,
        str(tags.track_no or ""), str(tags.year or ""),
        ",".join(tags.genres), tags.label, tags.catalog_number,
        tags.isrc, tags.country,
    ]
    filled = sum(1 for f in fields if f)
    return int(100 * filled / len(fields))


#: track numbers that aren't a position on the album - see import_file
_PLACEHOLDER_TRACK_NOS = frozenset({0, 999})


def _real_track_no(track_no: Optional[int]) -> Optional[int]:
    return track_no if track_no and track_no not in _PLACEHOLDER_TRACK_NOS else None


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

    `tags` lets a caller that already read the file (`scan_folder`, which
    reads a whole folder up front for the folder-wide album-artist call)
    pass it in. `album_artist_override` / `is_compilation_override` carry
    that folder-wide decision (`_folder_album_artist`); without them the
    file decides alone from its own tags.

    `force=True` skips the unchanged-file shortcut even when mtime/size
    haven't moved, re-deriving every tag-sourced field from a fresh
    `read_tags()` - the only way an already-scanned library picks up a
    tag-reading fix. Every write below (`get_or_create_*`, `add_credit`) is
    idempotent, so forcing an unchanged file through is safe, just slower.
    """
    stat = path.stat()
    existing = find_media_file(session, path)
    before_track_id = existing.track_id if existing is not None else None
    if existing is not None and existing.path != str(path):
        # same file, new capitalization (find_media_file) - keep the row
        # and its history, just follow the new spelling
        existing.path = str(path)
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
        # not the flag made it into the file (and a flagged "Best of" by the
        # artist performing it isn't one - see is_various_artists)
        is_compilation = is_various_artists(album_artist, [tags.artist], flagged=tags.compilation)
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
        release.cover_path = _save_cover(tags.cover, release)

    # release-level main credit, and the FK the Artists grid actually browses by
    aa = lib.get_or_create_artist(session, album_artist)
    lib.add_credit(session, aa, Credit.ROLE_MAIN, release=release)
    release.album_artist_id = aa.id

    # track
    # the same title on the same album is only the same track when it's by
    # the same artist - a compilation can hold several artists' versions of
    # one song. Two files by the same artist (an MP3 and a FLAC of one
    # recording) share a track.
    title_key = normalize(tags.title)
    artist_key = normalize(tags.artist or "")
    candidates = list(session.scalars(
        select(Track).where(
            Track.release_id == release.id,
            Track.title_key == title_key,
            Track.disc_no == tags.disc_no,
        ).order_by(Track.id)
    ))

    def same_artist(t: Track) -> bool:
        key = normalize(t.artist_display or "")
        return not key or not artist_key or key == artist_key

    # nor is it the same track when the two files sit at different track
    # numbers: the title key drops "(Live)", "(Remastered)", "(Mono)" and
    # the like, so a live or mono bonus version would otherwise fold into
    # the studio track. 999 and 0 aren't positions (extra copies are often
    # tagged 999), so they never split a track.
    this_no = _real_track_no(tags.track_no)

    def same_number(t: Track) -> bool:
        other = _real_track_no(t.track_no)
        if this_no is None or other is None or other == this_no:
            return True
        # this file alone on its own track: a renumbered file, same song
        return (existing is not None and existing.track is t
                and all(f is existing for f in t.files))

    def same_track(t: Track) -> bool:
        return same_artist(t) and same_number(t)

    track = None
    if existing is not None and existing.track in candidates and same_track(existing.track):
        track = existing.track
    if track is None:
        track = next((c for c in candidates if same_track(c)), None)
    if track is None:
        track = Track(release_id=release.id, title=tags.title, title_key=title_key)
        session.add(track)
        session.flush()
    # the file's own spelling of the title: a case-only or "(Remastered)"
    # fix keeps the same title key, so it has to be copied over here
    if tags.title and track.title != tags.title and existing is not None and existing.track is track:
        track.title = tags.title
    track.track_no = tags.track_no
    track.disc_no = tags.disc_no
    track.position = tags.side or (
        f"{tags.disc_no}-{tags.track_no:02d}"
        if tags.disc_total and tags.disc_total > 1 and tags.track_no
        else (str(tags.track_no) if tags.track_no else None)
    )
    track.artist_display = tags.artist
    # a 78 side whose file carries its own label keeps it
    if tags.cover and release.cover_path and is_side(track.position):
        from . import artwork_names

        artwork_names.save_side_cover(release.cover_path, track.position, tags.cover)
    track.duration_ms = tags.duration_ms
    track.isrc = tags.isrc or None
    track.bpm = tags.bpm
    track.musical_key = tags.musical_key or None
    track.comment = tags.comment or None

    names = split_artists(tags.artist)
    credited: set[int] = set()
    for idx, name in enumerate(names):
        artist = lib.get_or_create_artist(session, name)
        role = Credit.ROLE_MAIN if idx == 0 else Credit.ROLE_FEATURING
        lib.add_credit(session, artist, role, track=track, position=idx)
        credited.add(artist.id)
    if names:
        # drop performer credits left by other artists' files once folded
        # into this track (see the track lookup above)
        for credit in session.scalars(select(Credit).where(
            Credit.track_id == track.id,
            Credit.role.in_((Credit.ROLE_MAIN, Credit.ROLE_FEATURING)),
        )):
            if credit.artist_id not in credited:
                session.delete(credit)

    mf = existing or MediaFile(track_id=track.id, path=str(path))
    mf.track = track  # (relationship, so a file moving between tracks leaves its old one)
    mf.size_bytes = stat.st_size
    mf.mtime = stat.st_mtime
    mf.codec = tags.codec or path.suffix.lstrip(".")
    mf.bitrate = tags.bitrate
    mf.sample_rate = tags.sample_rate
    mf.channels = tags.channels
    mf.duration_ms = tags.duration_ms
    mf.is_missing = False
    mf.last_seen_at = dt.datetime.now(dt.timezone.utc)
    if tags.gain is not None:
        replaygain.apply_tags(mf, tags.gain)
    if existing is None:
        session.add(mf)
    if before_track_id is not None and before_track_id != track.id:
        # the first file to leave a track decides where its history goes
        result.retracked.setdefault(before_track_id, track.id)
    return track


# --------------------------------------------------------------------------
# retagged files: keep each song's history with its file
# --------------------------------------------------------------------------
#
# A retag that changes a song's title or album files it under a different
# track. Songs swapping titles could land on each other's old tracks (a
# track is found by its title), and an old track left with no file would
# keep the rating, plays, playlist places, jukebox card and chart matches -
# which USB sync, keyed by file path, would then see as deleted.
#
# `hide_retitled` keeps the edited songs' old tracks out of the title
# lookup while the files are read; `carry_history` moves everything from a
# track its last file left onto the track the file went to.

_HIDDEN = "\x00retitled:"


def hide_retitled(session: Session, items: Iterable[tuple[Path, "TrackTags"]]) -> dict[int, str]:
    """Hide from title lookups each track all of whose files are being
    read again with a different title. A track keeping a file under its
    old title (only the MP3 of an MP3 + FLAC pair was retitled) stays
    findable, so that file stays on it, history and all. Returns what to
    restore."""
    by_track: dict[int, list[str]] = {}
    tracks: dict[int, Track] = {}
    for path, tags in items:
        mf = find_media_file(session, path)
        if mf is None or mf.track is None:
            continue
        tracks[mf.track_id] = mf.track
        by_track.setdefault(mf.track_id, []).append(
            str(path) if normalize(tags.title) != mf.track.title_key else "")
    hidden: dict[int, str] = {}
    for tid, retitled in by_track.items():
        track = tracks[tid]
        present = {f.path for f in track.files if not f.is_missing}
        if "" in retitled or not present <= set(retitled):
            continue
        hidden[tid] = track.title_key
        track.title_key = f"{_HIDDEN}{tid}"
    if hidden:
        session.flush()
    return hidden


def restore_hidden(session: Session, hidden: dict[int, str]) -> None:
    for tid, key in hidden.items():
        track = session.get(Track, tid)
        if track is not None and (track.title_key or "").startswith(_HIDDEN):
            track.title_key = key
    if hidden:
        session.flush()


def carry_history(session: Session, moves: dict[int, int]) -> dict[int, int]:
    """Move rating, plays, playlist places, jukebox sides and chart matches
    from each old track whose last present file left it onto the track
    the file went to, and remove the old track. Returns the moves made."""
    from sqlalchemy import update

    done: dict[int, int] = {}
    for old_id, new_id in moves.items():
        if old_id == new_id:
            continue
        old = session.get(Track, old_id)
        new = session.get(Track, new_id)
        if old is None or new is None:
            continue
        if any(not f.is_missing for f in old.files):
            continue  # another file still holds it
        if new.rating is None and old.rating is not None:
            new.rating = old.rating
        new.play_count = (new.play_count or 0) + (old.play_count or 0)
        new.skip_count = (new.skip_count or 0) + (old.skip_count or 0)
        if old.last_played_at and (new.last_played_at is None
                                   or _naive(old.last_played_at) > _naive(new.last_played_at)):
            new.last_played_at = old.last_played_at
        if old.added_at and (new.added_at is None or _naive(old.added_at) < _naive(new.added_at)):
            new.added_at = old.added_at
        session.execute(update(PlayEvent).where(PlayEvent.track_id == old_id)
                        .values(track_id=new_id))
        # a playlist holding both: the old place goes rather than doubling up
        both = set(session.scalars(select(PlaylistItem.playlist_id)
                                   .where(PlaylistItem.track_id == new_id)))
        for item in list(session.scalars(select(PlaylistItem).where(PlaylistItem.track_id == old_id))):
            if item.playlist_id in both:
                session.delete(item)
            else:
                item.track_id = new_id
        session.execute(update(ChartEntry).where(ChartEntry.track_id == old_id)
                        .values(track_id=new_id))
        for slot in list(session.scalars(select(JukeboxSlot).where(
            or_(JukeboxSlot.side_a_track_id == old_id, JukeboxSlot.side_b_track_id == old_id)
        ))):
            if slot.side_a_track_id == old_id:
                slot.side_a_track_id = new_id
            if slot.side_b_track_id == old_id:
                slot.side_b_track_id = new_id
            # a card belongs to its record's artist; follow a changed one
            # when the other side doesn't say otherwise
            artist_id = lib.album_artist_id_for_track(session, new)
            other = slot.side_b_track_id if slot.side_a_track_id == new_id else slot.side_a_track_id
            other_artist = (lib.album_artist_id_for_track(session, session.get(Track, other))
                            if other and session.get(Track, other) is not None else None)
            if artist_id and other_artist in (None, artist_id):
                slot.artist_id = artist_id
        session.flush()
        session.expire(old)
        old = session.get(Track, old_id)
        if old is not None:
            session.delete(old)
        session.flush()
        done[old_id] = new_id
    # a chain (A's file went to B, B's to C) ends on the last track
    for old_id in list(done):
        seen = {old_id}
        while done[old_id] in done and done[old_id] not in seen:
            seen.add(done[old_id])
            done[old_id] = done[done[old_id]]
    return done


def _naive(value):
    return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value


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

    radio = radio_folder.roots(session)
    if radio_folder.is_radio_path(str(root), radio):
        # the watched folder *is* the old-time radio folder - its shows
        # belong to the Radio page, not the music library
        result.errors.append(f"{root} is the old-time radio folder - not scanned as music")
        return result
    files = list(iter_audio_files(root, skip=radio))
    _import_files(session, files, result, progress=progress, force=force, should_stop=should_stop)

    folder = session.scalar(select(WatchedFolder).where(WatchedFolder.path == str(root)))
    if folder is None:
        folder = WatchedFolder(path=str(root))
        session.add(folder)
    folder.last_scan_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return result


# `should_stop` (Settings' Cancel button), when given, is checked once per
# file before its import starts - deliberately a `break`, never a raise, so
# it never takes the `session.rollback()` path in `_import_files` (which
# discards every uncommitted row of the scan). What's already imported is
# committed normally; the remaining files are picked up on the next scan.


def import_paths(
    session: Session,
    paths: Iterable[Path | str],
    result: Optional[ScanResult] = None,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Import just these audio files - no folder walk, no WatchedFolder
    bookkeeping. Used after a USB sync (services/usb_sync.py) copies new
    tracks onto this PC, so only those files are read. Same per-folder album-artist decision as
    `scan_folder` (the copied files are grouped by their own folder), and
    the same unchanged-file shortcut, so passing an already-imported file
    is harmless. Non-audio paths are ignored."""
    result = result or ScanResult()
    radio = radio_folder.roots(session)
    files = [
        Path(p) for p in paths
        if Path(p).suffix.lower() in config.AUDIO_EXTENSIONS and Path(p).exists()
        and not radio_folder.is_radio_path(str(p), radio)
    ]
    _import_files(session, files, result, progress=progress, should_stop=should_stop)
    session.flush()
    return result


def _import_files(
    session: Session,
    files: list[Path],
    result: ScanResult,
    *,
    progress: Optional[ProgressFn] = None,
    force: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """The body of `scan_folder`, shared with `import_paths`."""
    total = len(files)

    # Only a new or changed file needs its tags read at all. Group just
    # those by folder so a folder-wide album-artist decision can be made
    # before any of them turn into a Release - see _folder_album_artist.
    # An unchanged file is left alone: cheap on a big rescan, and it already
    # went through this folder logic the first time. `force=True` (see
    # import_file) makes every file "need" import regardless.
    to_process = [p for p in files if force or _needs_import(session, p)]
    # unchanged files that share their track with another file get read once
    # more, so recordings folded together come apart (see import_file); they
    # stay on the album they're on
    process = set(to_process)
    regroup = [p for p in _shared_track_files(session, files) if p not in process]
    by_folder: dict[Path, list[Path]] = {}
    for path in to_process:
        by_folder.setdefault(path.parent, []).append(path)

    tags_cache: dict[Path, TrackTags] = {}
    overrides: dict[Path, tuple[str, bool]] = {}
    for folder_files in by_folder.values():
        folder_tags = {p: _fallback_from_path(p, read_tags(p)) for p in folder_files}
        tags_cache.update(folder_tags)
        # one decision per album in the folder, not per folder: a folder of
        # loose singles or 78s holds many albums, each tagged with its own
        # album artist
        by_album: dict[str, dict[Path, TrackTags]] = {}
        for p, t in folder_tags.items():
            by_album.setdefault(normalize(t.album), {})[p] = t
        for album_tags in by_album.values():
            decision = _folder_album_artist(album_tags)
            for p in album_tags:
                overrides[p] = decision

    hidden = hide_retitled(session, tags_cache.items())

    # the albums these files are on now - any a file leaves and leaves empty
    # is removed at the end (purge_empty_releases)
    paths = [str(p) for p in [*to_process, *regroup]]
    left_releases: set[int] = set()
    for start in range(0, len(paths), 500):
        left_releases.update(session.scalars(
            select(Track.release_id).join(MediaFile, MediaFile.track_id == Track.id)
            .where(MediaFile.path.in_(paths[start:start + 500]))
        ))

    regroup_set = set(regroup)
    for idx, path in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        try:
            if path in regroup_set:
                _reimport_on_same_album(session, path, result)
            elif path in overrides:
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
            # A failed flush leaves the Session unusable until it's rolled
            # back; without this every later file, folder and the
            # mark_missing_files/purge_orphaned_tracks passes would fail with
            # the same generic "transaction has been rolled back" error.
            #
            # There's no per-file savepoint (see db/session.py - one commit
            # at the end of the whole scan), so this discards every
            # uncommitted row of this scan, not just the bad file's. Those
            # files are re-detected as needing import on the next scan.
            session.rollback()
            # the rollback undid this scan's hiding and moves too - hide
            # again for the files still to come, and forget moves that
            # no longer happened
            result.retracked.clear()
            hidden = hide_retitled(session, [(p, tags_cache[p]) for p in files[idx:]
                                             if p in tags_cache])
        result.scanned += 1
        if progress and (idx % 5 == 0 or idx == total):
            progress(idx, total, path.name)
        if idx % 200 == 0:
            session.flush()
    restore_hidden(session, hidden)
    if result.retracked:
        result.history_moved.update(carry_history(session, result.retracked))
    if left_releases:
        purge_empty_releases(session, left_releases)
    if result.separated_from and not (should_stop and should_stop()):
        _rematch_separated(session, result, progress)


def _move_locked_to_version(session: Session, entry: ChartEntry, artist_similarity) -> None:
    current = session.get(Track, entry.track_id)
    if current is None:
        return
    siblings = session.scalars(select(Track).where(
        Track.release_id == current.release_id,
        Track.title_key == current.title_key,
        Track.id != current.id,
    ))
    here = artist_similarity(entry.artist_name or "", current.artist_display or "")
    best, best_score = None, max(here, 0.75)
    for track in siblings:
        score = artist_similarity(entry.artist_name or "", track.artist_display or "")
        if score > best_score:
            best, best_score = track, score
    if best is not None:
        entry.track_id = best.id


def _rematch_separated(session: Session, result: ScanResult,
                       progress: Optional[ProgressFn] = None) -> None:
    """Chart entries matched to a track that other artists' files have just
    been separated from may belong to one of those versions now. Re-match
    those entries.

    A match fixed by hand stays fixed, but may have been aimed at a version
    that was hidden inside the track: it moves to a separated version on the
    same album only when that version's artist is plainly the chart's
    artist."""
    from . import charts as chart_svc  # charts imports nothing from here
    from .matching import artist_similarity

    session.flush()
    entries = list(session.scalars(select(ChartEntry).where(
        ChartEntry.track_id.in_(result.separated_from),
    )))
    total = len(entries)
    for idx, entry in enumerate(entries, start=1):
        if entry.match_locked:
            _move_locked_to_version(session, entry, artist_similarity)
        else:
            chart_svc.match_entry(session, entry)
        if progress and (idx % 20 == 0 or idx == total):
            progress(idx, total, "Re-matching charts for separated recordings")
        if idx % 200 == 0:
            session.flush()


def rescan_all(
    session: Session, progress: Optional[ProgressFn] = None, force: bool = False
) -> ScanResult:
    result = ScanResult()
    folders = list(
        session.scalars(select(WatchedFolder).where(WatchedFolder.enabled.is_(True)))
    )
    for folder in folders:
        scan_folder(session, folder.path, progress, result, force=force)
    result.radio_removed = remove_radio_files(session)
    result.skipped_removed = remove_skipped_folder_files(session)
    result.case_duplicates_removed = merge_case_duplicate_files(session)
    result.missing = mark_missing_files(session)
    result.removed = purge_orphaned_tracks(session)
    tidy_after_scan(session)
    return result


def tidy_after_scan(session: Session) -> None:
    """Self-healing checks over every artist and album that only find work
    after a scan has changed the library - artist sort keys
    (library.fix_artist_sort_keys) and single-artist albums the
    Compilation flag hid (library.fix_single_artist_compilations). Every
    scan path calls this at its end rather than running it at startup, to
    keep launch fast. Both are idempotent."""
    lib.fix_artist_sort_keys(session)
    lib.fix_single_artist_compilations(session)


#: how often mark_missing_files below reports progress - each item is just
#: one os.path.exists() call, so much coarser than scan_folder's `idx % 5`
#: (same value as metadata_health.py's _LYRICS_PROGRESS_EVERY)
_MISSING_FILES_PROGRESS_EVERY = 250


def remove_radio_files(session: Session) -> int:
    """Take files in the old-time radio folder back out of the music
    library. A scan doesn't add them, but an older library (a watched
    folder with Radio inside it) may still have them. Only the file rows
    go here; purge_orphaned_tracks, which
    every scan runs next, then removes the tracks left with no file
    unless they have history (a playlist, plays, a chart, the jukebox)."""
    radio = radio_folder.roots(session)
    doomed = [
        mf_id for mf_id, path in session.execute(select(MediaFile.id, MediaFile.path))
        if radio_folder.is_radio_path(path or "", radio)
    ]
    for start in range(0, len(doomed), 500):
        session.execute(delete(MediaFile).where(MediaFile.id.in_(doomed[start:start + 500])))
    if doomed:
        session.flush()
        session.expire_all()
    return len(doomed)


def _in_skipped_folder(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")[:-1]
    return any(p and config.is_skipped_dir(p) for p in parts)


def remove_skipped_folder_files(session: Session) -> int:
    """Take files inside a folder scans skip (config.SKIP_SCAN_DIRS -
    GoodSync's _gsdata_ above all) back out of the library, the same way
    remove_radio_files does for the Radio folder. Only the
    file rows go: a song whose real file is also in the library just
    loses the stray copy, and purge_orphaned_tracks, which every scan
    runs next, removes a track left with no file - or hands its history
    to the real one (see _live_twin) - unless it has history of its own."""
    doomed = [
        mf_id for mf_id, path in session.execute(select(MediaFile.id, MediaFile.path))
        if _in_skipped_folder(path or "")
    ]
    for start in range(0, len(doomed), 500):
        session.execute(delete(MediaFile).where(MediaFile.id.in_(doomed[start:start + 500])))
    if doomed:
        session.flush()
        session.expire_all()
    return len(doomed)


def merge_case_duplicate_files(session: Session) -> int:
    """Drop library rows that are the same file as another row, differing
    only in letter case ("...Like A River.mp3" and "...Like a River.mp3",
    one file on disk). Only on a case-insensitive file system (config.CASE_INSENSITIVE_PATHS).
    The most recently added row - the spelling the last scan saw - stays;
    purge_orphaned_tracks, which every scan runs afterwards, tidies a
    track left without a file."""
    if not config.CASE_INSENSITIVE_PATHS:
        return 0
    groups: dict[str, list[int]] = {}
    for mf_id, path in session.execute(
        select(MediaFile.id, MediaFile.path).where(
            func.lower(MediaFile.path).in_(
                select(func.lower(MediaFile.path))
                .group_by(func.lower(MediaFile.path))
                .having(func.count(MediaFile.id) > 1)
            )
        )
    ):
        groups.setdefault((path or "").casefold(), []).append(mf_id)
    doomed = [i for ids in groups.values() if len(ids) > 1 for i in sorted(ids)[:-1]]
    for start in range(0, len(doomed), 500):
        session.execute(delete(MediaFile).where(MediaFile.id.in_(doomed[start:start + 500])))
    if doomed:
        session.flush()
        session.expire_all()
    return len(doomed)


def purge_stale_copies(session: Session) -> int:
    """Delete missing file rows whose track still has another file that's
    present - an MP3 beside the FLAC, a copy elsewhere.
    purge_orphaned_tracks only removes whole tracks, so without this a
    stale extra copy would stay "missing" forever. Nothing else is lost: the track, its history and
    its present file all stay. Settings' Purge button runs this; a scan
    doesn't, so a copy on a drive that's only unplugged isn't forgotten."""
    playable_track_ids = select(MediaFile.track_id).where(MediaFile.is_missing.is_(False))
    doomed = session.scalars(
        select(MediaFile.id).where(
            MediaFile.is_missing.is_(True),
            MediaFile.track_id.in_(playable_track_ids),
        )
    ).all()
    for start in range(0, len(doomed), 500):
        session.execute(delete(MediaFile).where(MediaFile.id.in_(doomed[start:start + 500])))
    if doomed:
        session.flush()
        session.expire_all()
    return len(doomed)


def mark_missing_files(
    session: Session,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Flag rows whose file has disappeared instead of deleting metadata.

    `progress`, when given, is called `(done, total, "")` (Settings'
    "Verify files" progress bar). `should_stop`, checked once per row,
    `break`s rather than raising: each `is_missing` flip is independent, so
    a break just leaves the remaining rows unchecked for this run."""
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

    A renamed or moved file gets a new path, and `import_file` matches a
    `MediaFile` by exact path, so the file comes in as a new track and the
    original track is left behind pointing at a missing path. This runs
    automatically on every scan to clear those.

    A track is deleted only when it has no history to lose: no playlist
    placement, chart match, play event or jukebox slot (see
    `_history_reason`). Those foreign keys cascade or null out, so deleting
    such a track would silently drop it from a playlist, unmatch a
    hand-placed chart entry, or empty a jukebox side (`JukeboxSlot`'s FK is
    `ondelete="SET NULL"`). A track with history stays, shown as missing.
    A track whose song is found again on another track first hands its
    history over (`_live_twin`, `carry_history`).
    """
    playable_track_ids = select(MediaFile.track_id).where(
        MediaFile.is_missing.is_(False)
    )
    candidates = session.scalars(
        select(Track).where(Track.id.not_in(playable_track_ids))
    ).all()
    # a track whose file now lives on another track - the same
    # song by the same artist, found again under a new path or a new album
    # (a rescan of a moved, renamed or retagged file) - hands its history
    # over and goes, instead of staying behind with no file forever
    moves = {}
    for track in candidates:
        twin = _live_twin(session, track, playable_track_ids)
        if twin is not None:
            moves[track.id] = twin.id
    removed = len(carry_history(session, moves)) if moves else 0
    if removed:
        candidates = session.scalars(
            select(Track).where(Track.id.not_in(playable_track_ids))
        ).all()
    for track in candidates:
        if _history_reason(session, track.id):
            continue
        session.delete(track)
        removed += 1
    if removed:
        session.flush()
    purge_empty_releases(session)
    return removed


def _history_reason(session: Session, track_id: int) -> Optional[str]:
    """Why a missing track is kept rather than purged - "playlist",
    "chart", "plays" or "jukebox" (the first that applies) - or None when
    it has no history and may go. Shared by purge_orphaned_tracks and
    missing_summary so the preview and the purge can never disagree."""
    if session.scalar(
        select(PlaylistItem.id).where(PlaylistItem.track_id == track_id).limit(1)
    ):
        return "playlist"
    if session.scalar(select(ChartEntry.id).where(ChartEntry.track_id == track_id).limit(1)):
        return "chart"
    if session.scalar(select(PlayEvent.id).where(PlayEvent.track_id == track_id).limit(1)):
        return "plays"
    if session.scalar(
        select(JukeboxSlot.id)
        .where(
            or_(
                JukeboxSlot.side_a_track_id == track_id,
                JukeboxSlot.side_b_track_id == track_id,
            )
        )
        .limit(1)
    ):
        return "jukebox"
    return None


@dataclass
class MissingSummary:
    """What "Purge missing files" would actually do, worked out without
    changing anything. The count of missing file rows is neither the number
    of tracks nor the number a purge removes: a missing file on a track
    that still has a present file takes nothing with it, and a track with
    history is kept by purge_orphaned_tracks anyway.

    - `files`: MediaFile rows currently flagged missing
    - `stale_copies`: missing files whose track still has another file
      present - a purge clears these (purge_stale_copies) and the track
      stays
    - `removable`: tracks with no present file that a purge would remove
      (including ones whose history first moves to their re-found twin)
    - `kept`: tracks with no present file kept for their history, by
      reason ("playlist" / "chart" / "plays" / "jukebox")"""

    files: int = 0
    stale_copies: int = 0
    removable: int = 0
    kept: Counter = field(default_factory=Counter)

    @property
    def kept_total(self) -> int:
        return sum(self.kept.values())


def missing_summary(session: Session) -> MissingSummary:
    """Preview purge_orphaned_tracks against the current is_missing flags
    without deleting anything - see MissingSummary. Run
    mark_missing_files first for an up-to-the-minute answer."""
    summary = MissingSummary(
        files=session.scalar(
            select(func.count(MediaFile.id)).where(MediaFile.is_missing.is_(True))
        ) or 0
    )
    playable_track_ids = select(MediaFile.track_id).where(MediaFile.is_missing.is_(False))
    summary.stale_copies = session.scalar(
        select(func.count(MediaFile.id)).where(
            MediaFile.is_missing.is_(True),
            MediaFile.track_id.in_(playable_track_ids),
        )
    ) or 0
    candidates = session.scalars(
        select(Track).where(Track.id.not_in(playable_track_ids))
    ).all()
    for track in candidates:
        if _live_twin(session, track, playable_track_ids) is not None:
            summary.removable += 1
            continue
        reason = _history_reason(session, track.id)
        if reason:
            summary.kept[reason] += 1
        else:
            summary.removable += 1
    return summary


def _live_twin(session: Session, track: Track, playable_track_ids) -> Optional[Track]:
    """The one playable track that is plainly the same song as `track`:
    same title, same performer, and on the same album - or failing that,
    on an album by the same album artist. None when there's no such track
    or more than one (two live versions: which one the history belongs to
    is a guess, so it's left alone)."""
    artist_key = normalize(track.artist_display or "")
    if not track.title_key or not artist_key:
        return None
    release = track.release
    same_song = [
        t for t in session.scalars(select(Track).where(
            Track.title_key == track.title_key,
            Track.id != track.id,
            Track.id.in_(playable_track_ids),
        ))
        if normalize(t.artist_display or "") == artist_key
    ]
    on_album = [t for t in same_song if t.release_id == track.release_id]
    if len(on_album) == 1:
        return on_album[0]
    if on_album or release is None or release.album_artist_id is None:
        return None
    by_artist = [t for t in same_song
                 if t.release is not None and t.release.album_artist_id == release.album_artist_id]
    return by_artist[0] if len(by_artist) == 1 else None


def save_side_covers(session: Session) -> int:
    """Give every 78 side its own label picture from its file. The scanner does
    this for new and changed files; this does it once for the files already
    in the library. Only songs with a side (A, B, A1...) are read. Returns
    how many side pictures were saved."""
    from . import artwork_names

    saved = 0
    rows = session.execute(
        select(Track.id, Track.position, Release.cover_path)
        .join(Release, Release.id == Track.release_id)
        .where(Track.position.is_not(None), Release.cover_path.is_not(None))
    ).all()
    for track_id, position, cover_path in rows:
        if not is_side(position):
            continue
        track = session.get(Track, track_id)
        mf = track.primary_file if track is not None else None
        if mf is None or not Path(mf.path).is_file():
            continue
        try:
            cover = read_tags(Path(mf.path)).cover
        except Exception:  # an unreadable file keeps what it has
            log.warning("side cover: couldn't read %s", mf.path, exc_info=True)
            continue
        if cover and artwork_names.save_side_cover(cover_path, position, cover):
            saved += 1
    return saved


def artist_key(name: str) -> str:
    """The Artist.name_key `library.get_or_create_artist` gives `name`."""
    key = normalize(name)
    if key:
        return key
    bare = (name or "").strip()
    return bare.casefold() if bare and bare.casefold() != "unknown artist" else "unknown artist"


def resplit_track_credits(session: Session) -> tuple[int, int]:
    """Recompute every track's performer credits from its artist text with
    the current `split_artists` rules (so "Blood, Sweat & Tears" stays one
    artist and "Shawn Mendes, Camila Cabello" becomes two). Works from Track.artist_display, which
    is the file's artist tag as last read, so no file is opened. Only tracks
    whose credits come out different are touched. Artists those tracks
    leave with nothing else pointing at them are deleted. Returns (tracks
    changed, artists removed). One-time (bootstrap_database)."""
    from sqlalchemy import insert

    from ..db.models import Artist, ArtistAlias, ArtistSimilar, ArtistTopPick, ArtistTopTrack, Video

    id_of = {key: aid for aid, key in session.execute(select(Artist.id, Artist.name_key))}
    # merged spellings (services/artist_merge.py) resolve to the kept artist
    for key, aid in session.execute(select(ArtistAlias.alias_name_key, ArtistAlias.artist_id)
                                    .where(ArtistAlias.alias_artist_id.is_(None))):
        id_of.setdefault(key, aid)
    current: dict[int, list[tuple[int, int, str]]] = {}
    for cid, track_id, artist_id, role in session.execute(
        select(Credit.id, Credit.track_id, Credit.artist_id, Credit.role).where(
            Credit.track_id.is_not(None),
            Credit.role.in_((Credit.ROLE_MAIN, Credit.ROLE_FEATURING)),
        )
    ):
        current.setdefault(track_id, []).append((cid, artist_id, role))

    changed = 0
    left: set[int] = set()
    doomed: list[int] = []
    rows: list[dict] = []
    for track_id, display in session.execute(select(Track.id, Track.artist_display)).all():
        names = split_artists(display)
        if not names:
            continue
        wanted = [(artist_key(n), Credit.ROLE_MAIN if i == 0 else Credit.ROLE_FEATURING)
                  for i, n in enumerate(names)]
        have = current.get(track_id, [])
        if all(k in id_of for k, _ in wanted) and \
                sorted((id_of[k], r) for k, r in wanted) == sorted((aid, r) for _, aid, r in have):
            continue
        for cid, aid, _ in have:
            doomed.append(cid)
            left.add(aid)
        for position, (name, (key, role)) in enumerate(zip(names, wanted)):
            aid = id_of.get(key)
            if aid is None:
                artist = lib.get_or_create_artist(session, name)
                aid = id_of[key] = id_of[artist.name_key] = artist.id
            rows.append({"artist_id": aid, "role": role, "track_id": track_id,
                         "release_id": None, "position": position})
        changed += 1
    for start in range(0, len(doomed), 500):
        session.execute(delete(Credit).where(Credit.id.in_(doomed[start:start + 500])))
    if rows:
        session.execute(insert(Credit), rows)
    session.flush()
    left -= {r["artist_id"] for r in rows}

    removed = 0
    for artist_id in left:
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
    return changed, removed


def repair_unknown_artist_merge(session: Session) -> int:
    """Re-read the files filed under a letterless artist name or "Unknown
    Artist". Older split/keying rules turned "? and the Mysterians" into
    "?" + "the Mysterians", keyed "?" as "unknown artist", and so filed
    every untagged file under that same "?".

    Re-reading those files with the current rules credits the band as itself
    and names each untagged "Artist - Title" file from its filename
    (`_artist_title_from_stem`); re-importing drops the old credits. Tracks
    the files leave are tidied by purge_orphaned_tracks / purge_empty_releases
    as after any scan, and the leftover "?" record is removed - or, if
    something still points at it, renamed "Unknown Artist". Returns how many
    files were re-read. One-time (bootstrap_database); running it again
    finds nothing new to fix."""
    from ..db.models import Artist, ArtistSimilar, ArtistTopPick, ArtistTopTrack, Video

    bad_artists = [
        a for a in session.scalars(select(Artist))
        if not normalize(a.name)
        or (a.name_key == "unknown artist" and a.name != "Unknown Artist")
    ]
    bad_ids = {a.id for a in bad_artists}
    track_ids: set[int] = set(session.scalars(
        select(Track.id).where(Track.artist_display.in_(("Unknown Artist", "?")))
    ))
    if bad_ids:
        track_ids.update(session.scalars(
            select(Credit.track_id).where(
                Credit.artist_id.in_(bad_ids), Credit.track_id.is_not(None)
            )
        ))
    files = [
        Path(p) for p in session.scalars(
            select(MediaFile.path).where(
                MediaFile.track_id.in_(track_ids), MediaFile.is_missing.is_(False)
            )
        ) if p and os.path.exists(p)
    ] if track_ids else []
    if files:
        _import_files(session, files, ScanResult(), force=True)
        session.flush()
        purge_orphaned_tracks(session)
    for artist in bad_artists:
        if session.get(Artist, artist.id) is None:
            continue
        used = (
            session.scalar(select(Credit.id).where(Credit.artist_id == artist.id).limit(1))
            or session.scalar(select(Release.id).where(Release.album_artist_id == artist.id).limit(1))
            or session.scalar(select(Video.id).where(Video.artist_id == artist.id).limit(1))
            or session.scalar(select(JukeboxSlot.id).where(JukeboxSlot.artist_id == artist.id).limit(1))
        )
        if not used:
            for table in (ArtistTopTrack, ArtistTopPick, ArtistSimilar):
                session.execute(delete(table).where(table.artist_id == artist.id))
            session.execute(delete(Artist).where(Artist.id == artist.id))
        elif artist.name_key == "unknown artist" and artist.name != "Unknown Artist":
            artist.name = "Unknown Artist"
            artist.name_sort = "Unknown Artist"
    session.flush()
    return len(files)


def repair_folder_album_artists(session: Session) -> int:
    """Put right the albums an older folder-wide album-artist rule
    mis-filed: every album in a multi-album folder (a folder of 78s, one
    album per record) got the folder's most common album artist, and its
    cover was then looked for under that artist's name and showed blank.
    The scanner decides per album now (see `_import_files`), but an
    unchanged file is never re-read, so existing rows need this.

    This looks only at folders holding more than one album, re-reads the
    tags of the files there, makes the same per-album decision the scanner
    now makes, and for each album that differs moves it to the right album
    artist (credit, master and all - or onto that artist's existing copy of
    the album). Each album there whose cover file is missing gets its
    picture back: the existing "<album artist> - <title>" image if there is
    one, else the picture embedded in its files. Returns how many albums
    changed. One-time (bootstrap_database runs it once per data folder);
    running it again changes nothing."""
    from . import artwork_names

    by_folder: dict[str, list[tuple[int, str, int]]] = {}
    for mf_path, track_id, release_id in session.execute(
        select(MediaFile.path, MediaFile.track_id, Track.release_id)
        .join(Track, Track.id == MediaFile.track_id)
        .where(MediaFile.is_missing.is_(False))
    ):
        folder = os.path.dirname(mf_path)
        by_folder.setdefault(folder, []).append((release_id, mf_path, track_id))

    # only folders where something looks wrong get their files read: an
    # album whose album artist performs none of its songs, or whose cover
    # file is gone - reading every multi-album folder (the Billboard year
    # folders hold thousands of songs) would make this slow for nothing
    suspect: set[int] = set()     # cover missing or album artist suspect
    misfiled: set[int] = set()    # album artist performs none of its songs
    multi = {folder: rows for folder, rows in by_folder.items()
             if len({r for r, _, _ in rows}) > 1}
    ids = {r for rows in multi.values() for r, _, _ in rows}
    for release in session.scalars(select(Release).where(Release.id.in_(ids))):
        if release.cover_path and not Path(release.cover_path).is_file():
            suspect.add(release.id)
        if release.is_compilation:
            continue
        owner = normalize(release.artist_display or "")
        performers = [normalize(a or "") for a in session.scalars(
            select(Track.artist_display).where(Track.release_id == release.id).distinct())]
        if owner and performers and not any(owner in p or p in owner for p in performers if p):
            suspect.add(release.id)
            misfiled.add(release.id)

    changed: set[int] = set()
    emptied: set[int] = set()
    for folder, rows in multi.items():
        if not suspect & {r for r, _, _ in rows}:
            continue
        releases = {r.id: r for r in session.scalars(
            select(Release).where(Release.id.in_({r for r, _, _ in rows})))}
        tags: dict[Path, TrackTags] = {}
        release_of: dict[Path, int] = {}
        for release_id, mf_path, _track_id in rows:
            path = Path(mf_path)
            if not path.is_file():
                continue
            try:
                tags[path] = _fallback_from_path(path, read_tags(path))
            except Exception:  # an unreadable file keeps what it has
                log.warning("repair: couldn't read %s", path, exc_info=True)
                continue
            release_of[path] = release_id
        by_album: dict[str, dict[Path, TrackTags]] = {}
        for path, t in tags.items():
            by_album.setdefault(normalize(t.album), {})[path] = t
        for album_tags in by_album.values():
            artist, is_compilation = _folder_album_artist(album_tags)
            # only an album artist the files themselves name - never one
            # guessed from folder names - and only for a mis-filed album
            explicit = any(t.had_explicit_album_artist for t in album_tags.values())
            for release_id in {release_of[p] for p in album_tags}:
                release = releases.get(release_id)
                if release is None or release.id in emptied or release.id not in suspect:
                    continue
                files = [p for p in album_tags if release_of[p] == release_id]
                target = release
                if (explicit and release.id in misfiled
                        and (release.artist_display or "") != artist):
                    target = _refile_release(session, release, artist, is_compilation)
                    if target is not release:
                        emptied.add(release.id)
                    changed.add(target.id)
                if _restore_cover(target, [album_tags[p] for p in files], artwork_names):
                    changed.add(target.id)
    if emptied:
        purge_empty_releases(session, emptied)
    session.flush()
    return len(changed)


def _refile_release(session: Session, release: Release, artist: str,
                    is_compilation: bool) -> Release:
    """Move `release` to album artist `artist`; if that artist already has
    this album, its tracks join that one instead (and the returned release
    is that one; `release` is left empty for purge_empty_releases)."""
    existing = session.scalar(
        select(Release).where(Release.id != release.id,
                              Release.title_key == release.title_key,
                              Release.artist_display == artist))
    if existing is not None:
        for track in list(release.tracks):
            track.release_id = existing.id
        session.flush()
        session.expire(release, ["tracks"])
        return existing
    old_artist_id = release.album_artist_id
    release.artist_display = artist
    release.is_compilation = is_compilation
    aa = lib.get_or_create_artist(session, artist)
    if old_artist_id is not None and old_artist_id != aa.id:
        session.execute(delete(Credit).where(
            Credit.release_id == release.id, Credit.track_id.is_(None),
            Credit.artist_id == old_artist_id, Credit.role == Credit.ROLE_MAIN))
    lib.add_credit(session, aa, Credit.ROLE_MAIN, release=release)
    release.album_artist_id = aa.id
    master = lib.get_or_create_master(session, release.title, artist, release.year)
    old_master_id = release.master_id
    release.master_id = master.id
    if master.main_release_id is None:
        master.main_release_id = release.id
    session.flush()
    if old_master_id and old_master_id != master.id:
        from ..db.models import Master

        rest = session.scalars(select(Release.id).where(Release.master_id == old_master_id)).first()
        old = session.get(Master, old_master_id)
        if old is not None:
            if rest is None:
                session.delete(old)
            elif old.main_release_id == release.id:
                old.main_release_id = rest
    return release


def _restore_cover(release: Release, tags: Sequence[TrackTags], artwork_names) -> bool:
    """Give `release` a cover file that exists: the image already saved
    under its "<album artist> - <title>" name, else one embedded in its
    files. A cover that's there is left alone. True if it changed."""
    current = Path(release.cover_path) if release.cover_path else None
    if current is not None and current.is_file():
        return False
    own = artwork_names.find_image(config.ART_DIR, artwork_names.release_stem(release))
    if own is not None and own.is_file():
        release.cover_path = str(own)
        return True
    for t in tags:
        if t.cover:
            saved = artwork_names.save_release_cover(release, t.cover, overwrite=False)
            if saved:
                release.cover_path = saved
                return True
    return False


def purge_empty_releases(session: Session, release_ids: Optional[Iterable[int]] = None) -> int:
    """Delete releases left with no tracks. A rescan that files an album's
    tracks under a different release (a retag changed its album artist or
    catalog number) moves every track off the old one, which would
    otherwise show on the artist page as an empty copy. Only the scanner makes
    releases, and only for files, so an empty one is never wanted. A master
    left with no releases goes too; one that still has some points at one
    of those instead.

    `release_ids` limits it to those releases - an import passes the ones
    its own files were on, so it never touches a release it had nothing
    to do with; the scan's closing `purge_orphaned_tracks` sweeps them all."""
    from ..db.models import Master

    stmt = select(Release).where(~select(Track.id).where(Track.release_id == Release.id).exists())
    if release_ids is not None:
        stmt = stmt.where(Release.id.in_(list(release_ids)))
    empty = session.scalars(stmt).all()
    if not empty:
        return 0
    master_ids = {r.master_id for r in empty if r.master_id is not None}
    empty_ids = {r.id for r in empty}
    for release in empty:
        session.delete(release)
    session.flush()
    for master in session.scalars(select(Master).where(Master.id.in_(master_ids))):
        rest = session.scalars(select(Release.id).where(Release.master_id == master.id)).first()
        if rest is None:
            session.delete(master)
        elif master.main_release_id is None or master.main_release_id in empty_ids:
            master.main_release_id = rest
    session.flush()
    return len(empty)
