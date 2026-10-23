"""Read and write a music file's own tags.

The scanner reads tags (services/scanner.py:read_tags); this module is the
write side, kept separate from the scanner so the
scanner stays read-only. `services/tag_editor.py` is what the app calls:
it writes through here, then has the scanner read the files again so the
library follows the files.

`TagValues` carries every field as text, with three meanings:

- ``None``  - leave that tag exactly as it is in the file
- ``""``    - remove that tag from the file
- anything else - set it

so an edit only ever touches the fields that were actually changed, and
everything else in the file (embedded pictures, ReplayGain, lyrics, tags
this app has never heard of) is left alone.

Formats, by tag container:

- **ID3** - .mp3, and the ID3 chunk inside .wav / .aiff. An existing
  ID3v2.3 tag is saved as v2.3 again (Windows Explorer and many car
  stereos still read 2.3 best); a file with no ID3 tag yet gets v2.3 too.
- **Vorbis comments** - .flac, .ogg, .oga, .opus.
- **MP4 atoms** - .m4a, .mp4. MP4 stores track/disc numbers as numbers,
  so a 78's side letter ("A") can't be written there.
- **ASF** - .wma.
- **APEv2** - .ape, .wv.

Raw .aac (ADTS) files have nowhere to keep tags at all; they're reported
as not editable rather than silently skipped.

Care taken with the files themselves:

- Tags are written into a copy beside the file, which then replaces it,
  so a crash mid-save can't leave a damaged file - and a song that's
  playing (locked) just reports "in use" with the file untouched.
- Only the plain comment is the Comment. Comments with a description
  (iTunes' "iTunSMPB" gapless data and "iTunNORM" volume figures, notes
  other programs keep) are never shown, changed or removed.
- Saving an ID3v2.3 tag puts back the frames mutagen's v2.3 conversion
  drops (sort artist / album / title and a few others).
- A genre held as several values shows as "Rock; Pop" and is written
  back as separate values.

Field keys match what scanner.read_tags looks for (and what MusicBrainz
Picard writes), so a written tag is read straight back by the next scan.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Optional

import mutagen
from mutagen.apev2 import APEv2File
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import (
    COMM,
    ID3,
    TALB,
    TCMP,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSRC,
    TXXX,
    UFID,
)
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

#: the extensions this module can write - .aac is deliberately missing
WRITABLE_EXTENSIONS = {
    ".mp3", ".wav", ".aiff", ".aif",
    ".flac", ".ogg", ".oga", ".opus",
    ".m4a", ".mp4",
    ".wma",
    ".ape", ".wv",
}


class TagWriteError(Exception):
    """A file couldn't be written. `str()` is a short reason fit to show
    the user ("in use by another program", "read-only", ...)."""


@dataclass
class TagValues:
    """One file's tags as text. None = leave alone, "" = remove."""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    #: "3", or a 78's side ("A", "B", "A1") - see services/tracknum.py
    track: Optional[str] = None
    track_total: Optional[str] = None
    disc: Optional[str] = None
    disc_total: Optional[str] = None
    #: "1984" or "1984-05-21"
    date: Optional[str] = None
    genre: Optional[str] = None
    label: Optional[str] = None
    catalog_number: Optional[str] = None
    barcode: Optional[str] = None
    country: Optional[str] = None
    isrc: Optional[str] = None
    comment: Optional[str] = None
    #: "1" for a compilation, "" to clear the flag
    compilation: Optional[str] = None
    # MusicBrainz identifiers, written by a lookup (Picard's tag names)
    mb_album_id: Optional[str] = None
    mb_album_artist_id: Optional[str] = None
    mb_artist_id: Optional[str] = None
    mb_release_group_id: Optional[str] = None
    mb_release_track_id: Optional[str] = None
    mb_recording_id: Optional[str] = None

    def changed_fields(self) -> dict[str, str]:
        """The fields that would be written (everything not None)."""
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) is not None}

    def is_empty(self) -> bool:
        return not self.changed_fields()


#: fields an edit dialog shows, in order, with their labels
FIELD_LABELS: dict[str, str] = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "album_artist": "Album artist",
    "track": "Track #",
    "track_total": "Tracks",
    "disc": "Disc",
    "disc_total": "Discs",
    "date": "Year / date",
    "genre": "Genre",
    "label": "Label",
    "catalog_number": "Catalog #",
    "barcode": "Barcode",
    "country": "Country",
    "isrc": "ISRC",
    "comment": "Comment",
    "compilation": "Compilation",
}

MB_FIELDS = (
    "mb_album_id", "mb_album_artist_id", "mb_artist_id",
    "mb_release_group_id", "mb_release_track_id", "mb_recording_id",
)


def can_write(path: Path | str) -> bool:
    return Path(path).suffix.lower() in WRITABLE_EXTENSIONS


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _split_pair(text: str) -> tuple[str, str]:
    """'3/12' -> ('3', '12'); '3' -> ('3', ''); 'A' -> ('A', '')."""
    if not text:
        return "", ""
    text = str(text).strip()
    if "/" in text:
        a, b = text.split("/", 1)
        return a.strip(), b.strip()
    return text, ""


def _join_pair(num: str, total: str) -> str:
    if not num:
        return ""
    return f"{num}/{total}" if total else num


#: fields that can hold several values (two artists, two genres): shown
#: joined with "; " and written back as separate values
MULTI_FIELDS = {"artist", "album_artist", "genre"}
MULTI_SEP = "; "


def _all_text(value) -> str:
    """Every value of a tag, joined with "; " ('Rock; Pop')."""
    if value is None:
        return ""
    if not isinstance(value, (list, tuple)):
        return _first_text(value)
    parts = [_first_text(v) for v in value]
    return MULTI_SEP.join(p for p in parts if p)


#: of those, the ones written back as separate values. Only genre: the
#: scanner reads every genre value but just the first artist value, so an
#: artist typed as "A; B" is kept as that one piece of text
MULTI_WRITE_FIELDS = {"genre"}


def _values_of(name: str, text: str) -> list[str]:
    """What to write for a field: several values for a multi field."""
    if name in MULTI_WRITE_FIELDS and ";" in text:
        return [p.strip() for p in text.split(";") if p.strip()]
    return [text]


def _first_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip()


def _pair_update(current: tuple[str, str], num: Optional[str],
                 total: Optional[str]) -> Optional[tuple[str, str]]:
    """Merge a "n/total" edit into what the file has. None = no change."""
    if num is None and total is None:
        return None
    cur_num, cur_total = current
    new_num = cur_num if num is None else num.strip()
    new_total = cur_total if total is None else total.strip()
    return new_num, new_total


def _open_error(exc: Exception, path: Path) -> TagWriteError:
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (
        errno.EACCES, errno.EPERM,
    ):
        if path.exists() and not _is_writable(path):
            return TagWriteError("the file is read-only")
        return TagWriteError("the file is in use by another program, or access was denied")
    if isinstance(exc, FileNotFoundError):
        return TagWriteError("the file isn't there any more")
    return TagWriteError(str(exc) or type(exc).__name__)


def _is_writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


# --------------------------------------------------------------------------
# ID3 (.mp3, .wav, .aiff)
# --------------------------------------------------------------------------

_ID3_TEXT = {
    "title": TIT2,
    "artist": TPE1,
    "album": TALB,
    "album_artist": TPE2,
    "genre": TCON,
    "label": TPUB,
    "isrc": TSRC,
}
_ID3_TXXX = {
    "catalog_number": "CATALOGNUMBER",
    "barcode": "BARCODE",
    "country": "MusicBrainz Album Release Country",
    "mb_album_id": "MusicBrainz Album Id",
    "mb_album_artist_id": "MusicBrainz Album Artist Id",
    "mb_artist_id": "MusicBrainz Artist Id",
    "mb_release_group_id": "MusicBrainz Release Group Id",
    "mb_release_track_id": "MusicBrainz Release Track Id",
}
_UFID_OWNER = "http://musicbrainz.org"


def _id3_tags(audio) -> ID3:
    if audio.tags is None:
        audio.add_tags()
    return audio.tags


def _id3_read(tags: Optional[ID3]) -> TagValues:
    v = TagValues()
    if tags is None:
        return v
    for name, frame in _ID3_TEXT.items():
        f = tags.get(frame.__name__)
        getter = _all_text if name in MULTI_FIELDS else _first_text
        setattr(v, name, getter(list(f.text)) if f is not None else "")
    for name, desc in _ID3_TXXX.items():
        f = tags.get(f"TXXX:{desc}")
        setattr(v, name, _first_text(f.text) if f is not None else "")
    v.track, v.track_total = _split_pair(_first_text(tags["TRCK"].text) if "TRCK" in tags else "")
    v.disc, v.disc_total = _split_pair(_first_text(tags["TPOS"].text) if "TPOS" in tags else "")
    date = ""
    for key in ("TDRC", "TYER", "TDOR"):
        if key in tags:
            date = _first_text(tags[key].text)
            if date:
                break
    v.date = date
    v.comment = _id3_comment(tags)
    v.compilation = "1" if ("TCMP" in tags and _first_text(tags["TCMP"].text) in ("1", "True", "true")) else ""
    ufid = tags.get(f"UFID:{_UFID_OWNER}")
    v.mb_recording_id = ufid.data.decode("ascii", errors="replace") if ufid is not None else ""
    return v


def _plain_comments(tags: ID3) -> list:
    """The ordinary comment frames - no description. Comments with a
    description are other programs' data (iTunes keeps its gapless and
    volume figures in "iTunSMPB"/"iTunNORM" comments) and are never shown
    or touched."""
    return [tags[k] for k in tags.keys()
            if str(k).startswith("COMM") and not (tags[k].desc or "")]


def _id3_comment(tags: ID3) -> str:
    plain = _plain_comments(tags)
    plain.sort(key=lambda f: 0 if (f.lang or "") in ("eng", "XXX", "\x00\x00\x00", "") else 1)
    for f in plain:
        text = _first_text(f.text)
        if text:
            return text
    return ""


def _id3_apply(tags: ID3, values: TagValues) -> None:
    def put(frame_id: str, frame) -> None:
        tags.delall(frame_id)
        if frame is not None:
            tags.add(frame)

    for name, frame in _ID3_TEXT.items():
        val = getattr(values, name)
        if val is None:
            continue
        put(frame.__name__, frame(encoding=3, text=_values_of(name, val)) if val else None)
    for name, desc in _ID3_TXXX.items():
        val = getattr(values, name)
        if val is None:
            continue
        put(f"TXXX:{desc}", TXXX(encoding=3, desc=desc, text=[val]) if val else None)

    current = _id3_read(tags)
    pair = _pair_update((current.track, current.track_total), values.track, values.track_total)
    if pair is not None:
        text = _join_pair(*pair)
        put("TRCK", TRCK(encoding=3, text=[text]) if text else None)
    pair = _pair_update((current.disc, current.disc_total), values.disc, values.disc_total)
    if pair is not None:
        text = _join_pair(*pair)
        put("TPOS", TPOS(encoding=3, text=[text]) if text else None)
    if values.date is not None:
        tags.delall("TYER")
        tags.delall("TDAT")
        put("TDRC", TDRC(encoding=3, text=[values.date]) if values.date else None)
    if values.comment is not None:
        # only the plain comments - see _plain_comments
        for key in [k for k in tags.keys()
                    if str(k).startswith("COMM") and not (tags[k].desc or "")]:
            del tags[key]
        if values.comment:
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[values.comment]))
    if values.compilation is not None:
        put("TCMP", TCMP(encoding=3, text=["1"]) if values.compilation else None)
    if values.mb_recording_id is not None:
        key = f"UFID:{_UFID_OWNER}"
        if key in tags:
            del tags[key]
        if values.mb_recording_id:
            tags.add(UFID(owner=_UFID_OWNER, data=values.mb_recording_id.encode("ascii", "ignore")))


#: frames mutagen's v2.3 conversion drops outright although players read
#: them from v2.3 tags all the same (MusicBee and iTunes write the sort
#: frames into v2.3 files) - put back after converting, as mutagen's own
#: docs suggest. Dates (TDRC) are converted to TYER/TDAT rather than kept.
_KEEP_IN_V23 = ("TSOP", "TSOA", "TSOT", "TSST", "TMOO", "TPRO", "TDRL", "RVA2")


def _id3_save(audio, keep_v24: bool) -> None:
    """Save as ID3v2.4 only if the file already was; otherwise v2.3 - with
    the frames the conversion would drop put back."""
    if keep_v24:
        audio.save(v2_version=4)
        return
    tags = audio.tags
    kept = []
    for frame_id in _KEEP_IN_V23:
        kept.extend(tags.getall(frame_id))
    tags.update_to_v23()
    for frame in kept:
        tags.add(frame)
    audio.save(v2_version=3)


# --------------------------------------------------------------------------
# Vorbis comments (.flac, .ogg, .opus)
# --------------------------------------------------------------------------

_VORBIS = {
    "title": ("TITLE",),
    "artist": ("ARTIST",),
    "album": ("ALBUM",),
    "album_artist": ("ALBUMARTIST", "ALBUM ARTIST"),
    "genre": ("GENRE",),
    "label": ("LABEL", "ORGANIZATION"),
    "catalog_number": ("CATALOGNUMBER",),
    "barcode": ("BARCODE",),
    "country": ("RELEASECOUNTRY",),
    "isrc": ("ISRC",),
    "comment": ("COMMENT",),
    "mb_album_id": ("MUSICBRAINZ_ALBUMID",),
    "mb_album_artist_id": ("MUSICBRAINZ_ALBUMARTISTID",),
    "mb_artist_id": ("MUSICBRAINZ_ARTISTID",),
    "mb_release_group_id": ("MUSICBRAINZ_RELEASEGROUPID",),
    "mb_release_track_id": ("MUSICBRAINZ_RELEASETRACKID",),
    "mb_recording_id": ("MUSICBRAINZ_TRACKID",),
}


def _vc_get(tags, *keys: str, multi: bool = False) -> str:
    if tags is None:
        return ""
    for key in keys:
        try:
            val = tags.get(key)
        except Exception:
            val = None
        text = _all_text(val) if multi else _first_text(val)
        if text:
            return text
    return ""


def _vorbis_read(tags) -> TagValues:
    v = TagValues()
    for name, keys in _VORBIS.items():
        setattr(v, name, _vc_get(tags, *keys, multi=name in MULTI_FIELDS))
    if not v.comment:
        v.comment = _vc_get(tags, "DESCRIPTION")
    num, total = _split_pair(_vc_get(tags, "TRACKNUMBER"))
    v.track, v.track_total = num, total or _vc_get(tags, "TRACKTOTAL", "TOTALTRACKS")
    num, total = _split_pair(_vc_get(tags, "DISCNUMBER"))
    v.disc, v.disc_total = num, total or _vc_get(tags, "DISCTOTAL", "TOTALDISCS")
    v.date = _vc_get(tags, "DATE", "YEAR", "ORIGINALDATE")
    v.compilation = "1" if _vc_get(tags, "COMPILATION") in ("1", "True", "true") else ""
    return v


def _vc_set(tags, keys: tuple[str, ...], value, name: str = "") -> None:
    """Write `value` under the first key. An alternate key the file already
    has gets the same value (an old ORGANIZATION alongside LABEL is kept in
    step rather than left to disagree), or goes when the value is cleared."""
    values = _values_of(name, value) if value else []
    present = [k for k in keys[1:] if k in tags]
    for key in keys:
        if key in tags:
            del tags[key]
    if values:
        tags[keys[0]] = values
        for key in present:
            tags[key] = values


def _vorbis_apply(tags, values: TagValues) -> None:
    for name, keys in _VORBIS.items():
        val = getattr(values, name)
        if val is not None:
            _vc_set(tags, keys, val, name)
    current = _vorbis_read(tags)
    pair = _pair_update((current.track, current.track_total), values.track, values.track_total)
    if pair is not None:
        _vc_set(tags, ("TRACKNUMBER",), pair[0])
        _vc_set(tags, ("TRACKTOTAL", "TOTALTRACKS"), pair[1] if pair[0] else "")
    pair = _pair_update((current.disc, current.disc_total), values.disc, values.disc_total)
    if pair is not None:
        _vc_set(tags, ("DISCNUMBER",), pair[0])
        _vc_set(tags, ("DISCTOTAL", "TOTALDISCS"), pair[1] if pair[0] else "")
    if values.date is not None:
        _vc_set(tags, ("DATE", "YEAR"), values.date)
    if values.compilation is not None:
        _vc_set(tags, ("COMPILATION",), "1" if values.compilation else "")


# --------------------------------------------------------------------------
# MP4 (.m4a, .mp4)
# --------------------------------------------------------------------------

_MP4_TEXT = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_artist": "aART",
    "genre": "\xa9gen",
    "date": "\xa9day",
    "comment": "\xa9cmt",
}
_MP4_FREEFORM = {
    "label": "LABEL",
    "catalog_number": "CATALOGNUMBER",
    "barcode": "BARCODE",
    "country": "MusicBrainz Album Release Country",
    "isrc": "ISRC",
    "mb_album_id": "MusicBrainz Album Id",
    "mb_album_artist_id": "MusicBrainz Album Artist Id",
    "mb_artist_id": "MusicBrainz Artist Id",
    "mb_release_group_id": "MusicBrainz Release Group Id",
    "mb_release_track_id": "MusicBrainz Release Track Id",
    "mb_recording_id": "MusicBrainz Track Id",
}
_FF = "----:com.apple.iTunes:"


def _mp4_read(tags) -> TagValues:
    v = TagValues()
    for name, key in _MP4_TEXT.items():
        getter = _all_text if name in MULTI_FIELDS else _first_text
        setattr(v, name, getter(tags.get(key)) if tags is not None else "")
    for name, key in _MP4_FREEFORM.items():
        setattr(v, name, _first_text(tags.get(_FF + key)) if tags is not None else "")
    for attr, key in (("track", "trkn"), ("disc", "disk")):
        pair = tags.get(key) if tags is not None else None
        num, total = (pair[0] if pair else (0, 0))
        setattr(v, attr, str(num) if num else "")
        setattr(v, attr + "_total", str(total) if total else "")
    v.compilation = "1" if (tags is not None and tags.get("cpil")) else ""
    return v


def _int_or_error(text: str, what: str) -> int:
    text = (text or "").strip()
    if not text:
        return 0
    if not text.isdigit():
        raise TagWriteError(f"M4A files can only hold a number as the {what} (not \"{text}\")")
    return int(text)


def _mp4_apply(tags, values: TagValues) -> None:
    for name, key in _MP4_TEXT.items():
        val = getattr(values, name)
        if val is None:
            continue
        if key in tags:
            del tags[key]
        if name == "genre" and "gnre" in tags:
            del tags["gnre"]
        if val:
            tags[key] = _values_of(name, val)
    for name, key in _MP4_FREEFORM.items():
        val = getattr(values, name)
        if val is None:
            continue
        if _FF + key in tags:
            del tags[_FF + key]
        if val:
            tags[_FF + key] = [MP4FreeForm(val.encode("utf-8"))]
    current = _mp4_read(tags)
    for attr, key, what in (("track", "trkn", "track number"), ("disc", "disk", "disc number")):
        pair = _pair_update(
            (getattr(current, attr), getattr(current, attr + "_total")),
            getattr(values, attr), getattr(values, attr + "_total"),
        )
        if pair is None:
            continue
        num, total = _int_or_error(pair[0], what), _int_or_error(pair[1], what)
        if key in tags:
            del tags[key]
        if num:
            tags[key] = [(num, total)]
    if values.compilation is not None:
        if "cpil" in tags:
            del tags["cpil"]
        if values.compilation:
            tags["cpil"] = True


# --------------------------------------------------------------------------
# ASF (.wma)
# --------------------------------------------------------------------------

_ASF = {
    "title": "Title",
    "artist": "Author",
    "album": "WM/AlbumTitle",
    "album_artist": "WM/AlbumArtist",
    "genre": "WM/Genre",
    "date": "WM/Year",
    "label": "WM/Publisher",
    "catalog_number": "WM/CatalogNo",
    "barcode": "WM/Barcode",
    "country": "MusicBrainz/Album Release Country",
    "isrc": "WM/ISRC",
    "comment": "Description",
    "mb_album_id": "MusicBrainz/Album Id",
    "mb_album_artist_id": "MusicBrainz/Album Artist Id",
    "mb_artist_id": "MusicBrainz/Artist Id",
    "mb_release_group_id": "MusicBrainz/Release Group Id",
    "mb_release_track_id": "MusicBrainz/Release Track Id",
    "mb_recording_id": "MusicBrainz/Track Id",
}


def _asf_read(tags) -> TagValues:
    v = TagValues()
    for name, key in _ASF.items():
        getter = _all_text if name in MULTI_FIELDS else _first_text
        setattr(v, name, getter(tags.get(key)) if tags is not None else "")
    v.track, v.track_total = _split_pair(_first_text(tags.get("WM/TrackNumber")) if tags is not None else "")
    if not v.track_total and tags is not None:
        v.track_total = _first_text(tags.get("TotalTracks"))
    v.disc, v.disc_total = _split_pair(_first_text(tags.get("WM/PartOfSet")) if tags is not None else "")
    comp = _first_text(tags.get("WM/IsCompilation")) if tags is not None else ""
    v.compilation = "1" if comp in ("1", "True", "true") else ""
    return v


def _asf_apply(tags, values: TagValues) -> None:
    def put(key: str, val: str, name: str = "") -> None:
        if key in tags:
            del tags[key]
        if val:
            tags[key] = _values_of(name, val)

    for name, key in _ASF.items():
        val = getattr(values, name)
        if val is not None:
            put(key, val, name)
    current = _asf_read(tags)
    pair = _pair_update((current.track, current.track_total), values.track, values.track_total)
    if pair is not None:
        put("WM/TrackNumber", pair[0])
        put("TotalTracks", pair[1] if pair[0] else "")
    pair = _pair_update((current.disc, current.disc_total), values.disc, values.disc_total)
    if pair is not None:
        put("WM/PartOfSet", _join_pair(*pair))
    if values.compilation is not None:
        put("WM/IsCompilation", "1" if values.compilation else "")


# --------------------------------------------------------------------------
# APEv2 (.ape, .wv)
# --------------------------------------------------------------------------

_APE = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "album_artist": "Album Artist",
    "genre": "Genre",
    "date": "Year",
    "label": "Label",
    "catalog_number": "CatalogNumber",
    "barcode": "Barcode",
    "country": "ReleaseCountry",
    "isrc": "ISRC",
    "comment": "Comment",
    "mb_album_id": "MUSICBRAINZ_ALBUMID",
    "mb_album_artist_id": "MUSICBRAINZ_ALBUMARTISTID",
    "mb_artist_id": "MUSICBRAINZ_ARTISTID",
    "mb_release_group_id": "MUSICBRAINZ_RELEASEGROUPID",
    "mb_release_track_id": "MUSICBRAINZ_RELEASETRACKID",
    "mb_recording_id": "MUSICBRAINZ_TRACKID",
}


def _ape_get(tags, key: str) -> str:
    if tags is None:
        return ""
    for k in tags.keys():
        if k.lower() == key.lower():
            return _first_text(str(tags[k]))
    return ""


def _ape_read(tags) -> TagValues:
    v = TagValues()
    for name, key in _APE.items():
        setattr(v, name, _ape_get(tags, key))
    v.track, v.track_total = _split_pair(_ape_get(tags, "Track"))
    v.disc, v.disc_total = _split_pair(_ape_get(tags, "Disc"))
    v.compilation = "1" if _ape_get(tags, "Compilation") in ("1", "True", "true") else ""
    return v


def _ape_apply(tags, values: TagValues) -> None:
    def put(key: str, val: str) -> None:
        for k in [k for k in tags.keys() if k.lower() == key.lower()]:
            del tags[k]
        if val:
            tags[key] = val

    for name, key in _APE.items():
        val = getattr(values, name)
        if val is not None:
            put(key, val)
    current = _ape_read(tags)
    pair = _pair_update((current.track, current.track_total), values.track, values.track_total)
    if pair is not None:
        put("Track", _join_pair(*pair))
    pair = _pair_update((current.disc, current.disc_total), values.disc, values.disc_total)
    if pair is not None:
        put("Disc", _join_pair(*pair))
    if values.compilation is not None:
        put("Compilation", "1" if values.compilation else "")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def _load(path: Path):
    try:
        audio = mutagen.File(str(path))
    except Exception as exc:  # mutagen raises its own error types too
        if isinstance(exc, OSError):
            raise _open_error(exc, path) from exc
        raise TagWriteError(f"MusicMgr couldn't read this file ({exc})") from exc
    if audio is None:
        raise TagWriteError("this kind of file can't hold tags")
    return audio


def _kind(audio) -> str:
    if isinstance(audio, (FLAC, OggVorbis, OggOpus)) or type(audio).__name__ in (
        "OggFLAC", "OggSpeex", "OggTheora",
    ):
        return "vorbis"
    if isinstance(audio, MP4):
        return "mp4"
    if isinstance(audio, ASF):
        return "asf"
    if isinstance(audio, APEv2File) or type(audio).__name__ in ("MonkeysAudio", "WavPack", "Musepack"):
        return "ape"
    if type(audio).__name__ in ("MP3", "WAVE", "AIFF", "EasyMP3", "TrueAudio"):
        return "id3"
    return ""


def read_values(path: Path | str) -> TagValues:
    """Every field this module knows, as the file has it right now ("" for
    a tag the file doesn't carry). Raises TagWriteError for a file that
    can't be read."""
    path = Path(path)
    audio = _load(path)
    kind = _kind(audio)
    tags = getattr(audio, "tags", None)
    if kind == "id3":
        return _id3_read(tags)
    if kind == "vorbis":
        return _vorbis_read(tags)
    if kind == "mp4":
        return _mp4_read(tags)
    if kind == "asf":
        return _asf_read(tags)
    if kind == "ape":
        return _ape_read(tags)
    raise TagWriteError("this kind of file can't hold tags")


def write_values(path: Path | str, values: TagValues) -> bool:
    """Write the non-None fields of `values` into the file. Returns False
    when there was nothing to change (the file isn't touched at all, so
    its date doesn't move and USB sync won't see it as edited). Raises
    TagWriteError with a short reason on failure."""
    path = Path(path)
    if not path.exists():
        raise TagWriteError("the file isn't there any more")
    if path.suffix.lower() not in WRITABLE_EXTENSIONS:
        raise TagWriteError(f"MusicMgr can't write tags into {path.suffix.lower() or 'this'} files")
    audio = _load(path)
    kind = _kind(audio)
    if not kind:
        raise TagWriteError("this kind of file can't hold tags")
    before = read_values(path)
    wanted = values.changed_fields()
    if all(getattr(before, k) == (v or "") for k, v in wanted.items()):
        return False

    # Tag a copy beside the file, then swap it in: a crash or power cut
    # mid-save leaves the original untouched (and a song that's playing
    # makes the swap fail cleanly, rather than half-writing the file).
    tmp = path.with_name(f".{path.stem}.musicmgr-tagging{path.suffix}")
    try:
        shutil.copy2(path, tmp)
        audio = mutagen.File(str(tmp))
        if audio is None:
            raise TagWriteError("this kind of file can't hold tags")
        if kind == "id3":
            had = audio.tags
            keep_v24 = had is not None and getattr(had, "version", (2, 3, 0)) >= (2, 4, 0)
            _id3_apply(_id3_tags(audio), values)
            _id3_save(audio, keep_v24)
        elif kind == "vorbis":
            if audio.tags is None:
                audio.add_tags()
            _vorbis_apply(audio.tags, values)
            audio.save()
        elif kind == "mp4":
            if audio.tags is None:
                audio.add_tags()
            _mp4_apply(audio.tags, values)
            audio.save()
        elif kind == "asf":
            _asf_apply(audio.tags, values)
            audio.save()
        elif kind == "ape":
            if audio.tags is None:
                audio.add_tags()
            _ape_apply(audio.tags, values)
            audio.save()
        _swap_in(tmp, path)
    except TagWriteError:
        _discard(tmp)
        raise
    except OSError as exc:
        _discard(tmp)
        raise _open_error(exc, path) from exc
    except Exception as exc:
        _discard(tmp)
        raise TagWriteError(f"writing failed ({exc})") from exc
    return True


#: how long to keep trying when Windows says the file is busy - antivirus,
#: the search indexer or MusicMgr's own loudness measuring often hold a
#: file for a moment (e.g. a song just queued being measured)
SWAP_WAIT_S = 3.0
SWAP_STEP_S = 0.2


def _swap_in(tmp: Path, path: Path) -> None:
    """Replace `path` with the tagged copy, waiting out a short lock. A
    file that stays locked (the song that's playing) raises."""
    import time

    deadline = time.monotonic() + SWAP_WAIT_S
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(SWAP_STEP_S)


def _discard(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


def merge_values(base: TagValues, edits: TagValues) -> TagValues:
    """`base` with every non-None field of `edits` laid over it."""
    out = TagValues(**{f.name: getattr(base, f.name) for f in fields(base)})
    for name, val in edits.changed_fields().items():
        setattr(out, name, val)
    return out


def diff_values(before: TagValues, after: TagValues) -> TagValues:
    """Just the fields where `after` differs from `before` (as an edit)."""
    out = TagValues()
    for f in fields(before):
        new = getattr(after, f.name)
        if new is None:
            continue
        if (getattr(before, f.name) or "") != (new or ""):
            setattr(out, f.name, new)
    return out


ProgressFn = Callable[[int, int, str], None]
