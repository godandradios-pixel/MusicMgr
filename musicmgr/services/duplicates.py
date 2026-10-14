"""Duplicate finder (2026-10-13).

James: "Let's work on Duplicate finder. More from an identical file
standpoint. Playlist should point to songs in albums not chart folders in
the music. For example, all playlists that point to billboard should try to
point to the album songs." Then, asked: the chart folders are Billboard,
Hot Country and Oldies; a playlist song moves to an album copy holding the
identical recording first, otherwise to the same song by the same artist
whose length is within 2 seconds.

Two results, from one run (Settings > Duplicates):

* **Playlist songs to switch** - every playlist entry that points at a song
  in a chart folder (D:\\Music\\Billboard\\1965\\..., Hot Country, Oldies)
  for which the library also has that song on an album. `switch_playlists`
  moves those entries to the album song, in place.
* **Identical files** - groups of files holding exactly the same recording,
  whatever they're called or tagged. Nothing is deleted; the list is for
  James to act on.

"Identical" is judged by the audio itself, tags ignored:

* FLAC stores an MD5 of its decoded audio in its header (STREAMINFO) - two
  FLACs with the same, non-zero MD5 are the same recording. A header read.
* MP3: the audio is everything between the ID3v2 tag at the front and the
  ID3v1/APE tags at the back. Same-recording copies have exactly the same
  audio length; for files that share one (and the same playing time) a
  fingerprint is taken from 3 x 128 KB spread through the audio - about
  400 KB read per file, instead of whole files (the library is ~1 TB).
* Other formats (AAC/ALAC .m4a, ...) aren't compared.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db.models import (
    Chart, ChartEntry, ChartIssue, MediaFile, Playlist, PlaylistItem, Release, Track,
    WatchedFolder,
)
from .matching import artist_similarity, normalize, primary_artist

log = logging.getLogger(__name__)

#: top-level folders of a watched music folder that hold chart copies of
#: songs (D:\Music\Billboard\1960-69\1965\Them - Here Comes The Night.mp3)
CHART_FOLDERS = ("billboard", "hot country", "oldies")
#: "the same song" when it isn't the identical recording: same title, same
#: artist, and playing times this close. James chose 2 seconds; the
#: results dialog can widen it to the larger steps to show what that would
#: add (on PC1 ~500 more entries within 5 s are mostly the same recording
#: with a longer fade; beyond that it's mostly single edits vs album cuts).
SAME_SONG_MAX_DIFF_MS = 2000
LENGTH_STEPS_MS = (2000, 5000, 15000)
_SAMPLE = 128 * 1024

ProgressFn = Callable[[int, int, str], None]


# --------------------------------------------------------------------------
# where a file lives
# --------------------------------------------------------------------------


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def watched_roots(session: Session) -> list[str]:
    return sorted({_norm(p) for p in session.scalars(select(WatchedFolder.path)) if p},
                  key=len, reverse=True)


def chart_folder(path: str, roots: Sequence[str]) -> Optional[str]:
    """"Billboard"/"Hot Country"/"Oldies" when the file is in one of those
    folders at the top of a watched folder, else None."""
    p = _norm(path)
    for root in roots:
        if p.startswith(root + "/"):
            first = p[len(root) + 1:].split("/", 1)[0]
            return first.title() if first in CHART_FOLDERS else None
    return None


# --------------------------------------------------------------------------
# audio fingerprints
# --------------------------------------------------------------------------


def _mp3_audio_span(f, size: int) -> Optional[tuple[int, int]]:
    """(start, end) byte offsets of an MP3's audio - inside its tags."""
    head = f.read(10)
    start = 0
    if head[:3] == b"ID3" and len(head) == 10:
        b = head[6:10]
        start = 10 + ((b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3])
        if head[5] & 0x10:
            start += 10
    end = size
    tail_len = min(size, 256)
    f.seek(size - tail_len)
    tail = f.read(tail_len)
    if tail[-128:-125] == b"TAG":
        end -= 128
    # an APEv2 tag (footer 32 bytes) just before the end
    foot_at = end - 32 - (size - tail_len)
    if foot_at >= 0 and tail[foot_at:foot_at + 8] == b"APETAGEX":
        tag_size = struct.unpack("<I", tail[foot_at + 12:foot_at + 16])[0]
        flags = struct.unpack("<I", tail[foot_at + 20:foot_at + 24])[0]
        end -= tag_size + (32 if flags & 0x80000000 else 0)
    if end <= start:
        return None
    # the first frame sync after the tag (some taggers pad with zeros)
    f.seek(start)
    probe = f.read(4096)
    i = 0
    while i < len(probe) - 1 and not (probe[i] == 0xFF and probe[i + 1] & 0xE0 == 0xE0):
        i += 1
    if i < len(probe) - 1:
        start += i
    return start, end


def audio_size(path: str) -> Optional[int]:
    """How many bytes of audio an MP3 holds (tags excluded), from its first
    and last few hundred bytes. None for anything else or unreadable."""
    if not path.lower().endswith(".mp3"):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            span = _mp3_audio_span(f, size)
    except OSError:
        return None
    return (span[1] - span[0]) if span else None


def fingerprint(path: str) -> Optional[str]:
    """A key that's the same for two files holding the identical recording
    (see the module docstring), or None when it can't be worked out."""
    low = path.lower()
    try:
        if low.endswith(".flac"):
            from mutagen.flac import FLAC

            md5 = FLAC(path).info.md5_signature
            return f"flac:{md5:032x}" if md5 else None
        if low.endswith(".mp3"):
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                span = _mp3_audio_span(f, size)
                if not span:
                    return None
                start, end = span
                length = end - start
                h = hashlib.sha1(str(length).encode())
                for at in (start, start + length // 2 - _SAMPLE // 2, end - _SAMPLE):
                    at = max(start, min(at, end - _SAMPLE))
                    f.seek(at)
                    h.update(f.read(min(_SAMPLE, end - at)))
                return f"mp3:{length}:{h.hexdigest()}"
    except Exception:
        log.debug("couldn't fingerprint %s", path, exc_info=True)
    return None


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class FileInfo:
    media_file_id: int
    track_id: int
    path: str
    title: str
    artist: str
    album: str
    duration_ms: int
    size: int
    lossless: bool
    bitrate: int
    chart: Optional[str]
    compilation: bool
    own_album: bool


@dataclass
class Switch:
    """One playlist entry to move from a chart copy to an album song."""
    item_id: int
    playlist_id: int
    playlist: str
    old: FileInfo
    new: FileInfo
    #: "identical" (same recording) or "same song" (title, artist, length)
    how: str
    #: how far apart the two playing times are
    diff_ms: int = 0


@dataclass
class ChartSwitch:
    """A chart-folder song the Charts page's entries point at, and the
    album copy they'd move to (2026-10-13 follow-up - James: "proceed with
    option 1": the Charts page's matches too). One per song, however many
    weekly rows it has."""
    old: FileInfo
    new: FileInfo
    how: str
    diff_ms: int
    #: chart rows pointing at the chart copy
    entries: int
    #: the charts they're in ("Billboard Hot 100", ...)
    charts: list[str] = field(default_factory=list)


@dataclass
class DuplicateGroup:
    """Files holding the identical recording."""
    files: list[FileInfo]

    @property
    def wasted_bytes(self) -> int:
        sizes = sorted((f.size for f in self.files), reverse=True)
        return sum(sizes[1:])


@dataclass
class DuplicateReport:
    #: every possible switch up to the widest length step - see
    #: switches_within() for the ones at a chosen step
    switches: list[Switch] = field(default_factory=list)
    groups: list[DuplicateGroup] = field(default_factory=list)
    #: chart-copy playlist entries with no album song in the library
    unmatched: int = 0
    #: the Charts page's matches on chart-folder songs (see ChartSwitch)
    chart_switches: list[ChartSwitch] = field(default_factory=list)
    #: chart rows on chart-folder songs with no album copy
    chart_unmatched: int = 0
    #: files the identical-file search looked inside
    examined: int = 0
    cancelled: bool = False

    def chart_switches_within(self, max_diff_ms: int = SAME_SONG_MAX_DIFF_MS) -> list[ChartSwitch]:
        return [s for s in self.chart_switches if s.diff_ms <= max_diff_ms or s.how == "identical"]

    def switches_within(self, max_diff_ms: int = SAME_SONG_MAX_DIFF_MS) -> list[Switch]:
        return [s for s in self.switches if s.diff_ms <= max_diff_ms or s.how == "identical"]

    @property
    def identical_switches(self) -> int:
        return sum(1 for s in self.switches if s.how == "identical")

    def summary(self, max_diff_ms: int = SAME_SONG_MAX_DIFF_MS) -> str:
        chosen = self.switches_within(max_diff_ms)
        identical = sum(1 for s in chosen if s.how == "identical")
        parts = [f"{len(chosen)} playlist song{'s' if len(chosen) != 1 else ''} "
                 f"can move to an album copy ({identical} identical)"]
        unmatched = self.unmatched + len(self.switches) - len(chosen)
        if unmatched:
            parts.append(f"{unmatched} chart songs have no album copy")
        charts = self.chart_switches_within(max_diff_ms)
        if self.chart_switches or self.chart_unmatched:
            rows = sum(c.entries for c in charts)
            parts.append(f"{rows:,} chart row{'s' if rows != 1 else ''} ({len(charts):,} "
                         f"song{'s' if len(charts) != 1 else ''}) can move to an album copy")
        if self.groups:
            wasted = sum(g.wasted_bytes for g in self.groups) / 1e9
            parts.append(f"{len(self.groups)} sets of identical files ({wasted:.1f} GB in extra copies)")
        return " · ".join(parts)


# --------------------------------------------------------------------------
# loading the library
# --------------------------------------------------------------------------


def _artist_key(artist: str) -> str:
    return normalize(primary_artist(re.sub(r"\([^)]*\)", " ", artist or "")))


def _load(session: Session) -> list[FileInfo]:
    roots = watched_roots(session)
    rows = session.execute(
        select(MediaFile.id, MediaFile.track_id, MediaFile.path, MediaFile.duration_ms,
               MediaFile.size_bytes, MediaFile.codec, MediaFile.bitrate,
               Track.title, Track.artist_display, Track.duration_ms,
               Release.title, Release.artist_display, Release.is_compilation)
        .join(Track, Track.id == MediaFile.track_id)
        .join(Release, Release.id == Track.release_id)
        .where(MediaFile.is_missing.is_(False))
    ).all()
    out = []
    for (mf_id, track_id, path, mf_ms, size, codec, bitrate, title, artist, t_ms,
         album, album_artist, is_comp) in rows:
        artist = artist or album_artist or ""
        codec = (codec or "").lower()
        out.append(FileInfo(
            media_file_id=mf_id, track_id=track_id, path=path, title=title or "",
            artist=artist, album=album or "", duration_ms=int(mf_ms or t_ms or 0),
            size=int(size or 0), lossless=codec in ("flac", "wave", "aiff", "alac"),
            bitrate=int(bitrate or 0), chart=chart_folder(path, roots),
            compilation=bool(is_comp),
            own_album=bool(album_artist) and _artist_key(album_artist) == _artist_key(artist),
        ))
    return out


def _preference(f: FileInfo, identical: bool) -> tuple:
    """Bigger is better: the identical recording, then the artist's own
    album (not a compilation), then lossless, then bitrate."""
    return (identical, f.own_album and not f.compilation, not f.compilation,
            f.lossless, f.bitrate)


# --------------------------------------------------------------------------
# the search
# --------------------------------------------------------------------------


class _Fingerprints:
    def __init__(self, should_stop: Optional[Callable[[], bool]]):
        self._cache: dict[str, Optional[str]] = {}
        self.should_stop = should_stop
        self.read = 0

    def __call__(self, path: str) -> Optional[str]:
        if path not in self._cache:
            self._cache[path] = fingerprint(path)
            self.read += 1
        return self._cache[path]


def _choose(old: FileInfo, cands: list[FileInfo],
            fp: Optional["_Fingerprints"]) -> Optional[tuple[FileInfo, str]]:
    """The album copy to use for chart-folder song `old` among `cands`
    (same title, same artist, not in a chart folder): the closest length
    step that has any decides; within it the identical recording (only
    checked when `fp` is given - it reads the files), then the artist's own
    album, then quality."""
    best = None
    old_fp = None
    for step in LENGTH_STEPS_MS:
        close = [c for c in cands
                 if old.duration_ms and c.duration_ms
                 and abs(c.duration_ms - old.duration_ms) <= step]
        for c in close:
            same = False
            # identical audio means an identical length - only those are
            # worth opening
            if (fp is not None and step == LENGTH_STEPS_MS[0]
                    and c.path.lower().rsplit(".", 1)[-1] == old.path.lower().rsplit(".", 1)[-1]):
                old_fp = old_fp if old_fp is not None else (fp(old.path) or "")
                same = bool(old_fp) and fp(c.path) == old_fp
            rank = _preference(c, same)
            if best is None or rank > best[0]:
                best = (rank, c, "identical" if same else "same song")
        if best:
            break
    return (best[1], best[2]) if best else None


def _same_song(old: FileInfo, cands: Iterable[FileInfo]) -> list[FileInfo]:
    key = _artist_key(old.artist)
    return [c for c in cands
            if c.track_id != old.track_id
            and (_artist_key(c.artist) == key or artist_similarity(c.artist, old.artist) >= 0.75)]


def _plan_switches(session: Session, files: list[FileInfo], fp: _Fingerprints,
                   report: DuplicateReport, progress: Optional[ProgressFn]) -> None:
    by_track: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        by_track[f.track_id].append(f)
    # a track counts as a chart copy only when none of its files is
    # outside a chart folder
    chart_tracks = {t for t, fs in by_track.items() if all(f.chart for f in fs)}

    if not chart_tracks:
        return
    # filtered here rather than with a 16,000-id IN (...) in SQL
    items = [
        row for row in session.execute(
            select(PlaylistItem.id, PlaylistItem.playlist_id, PlaylistItem.track_id, Playlist.name)
            .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
            .order_by(Playlist.name, PlaylistItem.position)
        )
        if row[2] in chart_tracks
    ]
    if not items:
        return

    album_by_title: dict[str, list[FileInfo]] = defaultdict(list)
    for f in files:
        if not f.chart:
            album_by_title[normalize(f.title)].append(f)

    choice: dict[int, Optional[tuple[FileInfo, FileInfo, str]]] = {}
    wanted = sorted({row[2] for row in items})
    for n, track_id in enumerate(wanted):
        if fp.should_stop and fp.should_stop():
            report.cancelled = True
            return
        old = by_track[track_id][0]
        if progress and n % 25 == 0:
            progress(n, len(wanted), f"Playlists: {old.artist} - {old.title}")
        cands = _same_song(old, album_by_title.get(normalize(old.title), []))
        if not cands:
            choice[track_id] = None
            continue
        picked = _choose(old, cands, fp)
        choice[track_id] = (old, picked[0], picked[1]) if picked else None

    for item_id, playlist_id, track_id, playlist_name in items:
        picked = choice.get(track_id)
        if picked is None:
            report.unmatched += 1
            continue
        old, new, how = picked
        report.switches.append(Switch(item_id, playlist_id, playlist_name, old, new, how,
                                      abs(new.duration_ms - old.duration_ms)))


def _find_identical(files: list[FileInfo], fp: _Fingerprints, report: DuplicateReport,
                    progress: Optional[ProgressFn]) -> None:
    """Groups of files with the identical recording - see the docstring."""
    flacs = [f for f in files if f.path.lower().endswith(".flac")]
    mp3s = [f for f in files if f.path.lower().endswith(".mp3")]

    # FLAC: the header MD5 is the answer outright - but only FLACs the
    # same length can match, so only those are opened
    by_len: dict[int, list[FileInfo]] = defaultdict(list)
    for f in flacs:
        if f.duration_ms:
            by_len[f.duration_ms // 1000].append(f)
    flac_cands = [f for g in by_len.values() if len(g) > 1 for f in g]

    # MP3: same playing time and same file size give or take tags first
    # (no reading at all), then the exact audio size, then the fingerprint
    by_ms: dict[int, list[FileInfo]] = defaultdict(list)
    for f in mp3s:
        if f.duration_ms:
            by_ms[f.duration_ms].append(f)
    mp3_cands: list[FileInfo] = []
    for group in by_ms.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda f: f.size)
        # tags rarely differ by more than a few MB (cover art)
        run = [group[0]]
        for f in group[1:]:
            if f.size - run[-1].size <= 4_000_000:
                run.append(f)
            else:
                if len(run) > 1:
                    mp3_cands.extend(run)
                run = [f]
        if len(run) > 1:
            mp3_cands.extend(run)

    total = len(flac_cands) + len(mp3_cands)
    done = 0
    keys: dict[str, list[FileInfo]] = defaultdict(list)

    def step(label: str) -> bool:
        nonlocal done
        done += 1
        if progress and (done % 20 == 0 or done == total):
            progress(done, total, label)
        if fp.should_stop and fp.should_stop():
            report.cancelled = True
            return False
        return True

    for f in flac_cands:
        if not step(f"Identical files: {f.artist} - {f.title}"):
            return
        key = fp(f.path)
        if key:
            keys[key].append(f)

    sized: dict[tuple[int, int], list[FileInfo]] = defaultdict(list)
    for f in mp3_cands:
        if not step(f"Identical files: {f.artist} - {f.title}"):
            return
        n = audio_size(f.path)
        if n:
            sized[(f.duration_ms, n)].append(f)
    for group in sized.values():
        if len(group) < 2:
            continue
        for f in group:
            key = fp(f.path)
            if key:
                keys[key].append(f)

    report.examined = total
    for group in keys.values():
        if len(group) > 1:
            group.sort(key=lambda f: _preference(f, False), reverse=True)
            report.groups.append(DuplicateGroup(group))
    report.groups.sort(key=lambda g: (-g.wasted_bytes, g.files[0].artist.lower()))


def _plan_chart_switches(session: Session, files: list[FileInfo], report: DuplicateReport,
                         progress: Optional[ProgressFn],
                         should_stop: Optional[Callable[[], bool]]) -> None:
    """The Charts page's matches on chart-folder songs, one row per song.
    No files are read here - a weekly chart can point at tens of thousands
    of songs - so these are matched by title, artist and length only."""
    by_track: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        by_track[f.track_id].append(f)
    chart_tracks = {t for t, fs in by_track.items() if all(f.chart for f in fs)}
    if not chart_tracks:
        return
    counts: dict[int, int] = defaultdict(int)
    names: dict[int, set[str]] = defaultdict(set)
    for track_id, chart_name, n in session.execute(
        select(ChartEntry.track_id, Chart.name, func.count(ChartEntry.id))
        .join(ChartIssue, ChartIssue.id == ChartEntry.issue_id)
        .join(Chart, Chart.id == ChartIssue.chart_id)
        .where(ChartEntry.track_id.is_not(None))
        .group_by(ChartEntry.track_id, Chart.name)
    ):
        if track_id in chart_tracks:
            counts[track_id] += n
            names[track_id].add(chart_name)
    if not counts:
        return
    album_by_title: dict[str, list[FileInfo]] = defaultdict(list)
    for f in files:
        if not f.chart:
            album_by_title[normalize(f.title)].append(f)
    wanted = sorted(counts)
    for n, track_id in enumerate(wanted):
        if should_stop and should_stop():
            report.cancelled = True
            return
        old = by_track[track_id][0]
        if progress and n % 200 == 0:
            progress(n, len(wanted), f"Charts: {old.artist} - {old.title}")
        picked = _choose(old, _same_song(old, album_by_title.get(normalize(old.title), [])), None)
        if picked is None:
            report.chart_unmatched += counts[track_id]
            continue
        new, how = picked
        report.chart_switches.append(ChartSwitch(
            old, new, how, abs(new.duration_ms - old.duration_ms), counts[track_id],
            sorted(names[track_id])))
    report.chart_switches.sort(key=lambda c: (-c.entries, c.old.artist.lower(), c.old.title.lower()))


def find_duplicates(
    session: Session,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    identical_files: bool = True,
) -> DuplicateReport:
    """All three results - see the module docstring. Read-only."""
    report = DuplicateReport()
    if progress:
        progress(0, 0, "Reading the library…")
    files = _load(session)
    fp = _Fingerprints(should_stop)
    _plan_switches(session, files, fp, report, progress)
    if not report.cancelled:
        _plan_chart_switches(session, files, report, progress, should_stop)
    if identical_files and not report.cancelled:
        _find_identical(files, fp, report, progress)
    return report


def switch_playlists(session: Session, switches: Iterable[Switch]) -> int:
    """Point these playlist entries at their album songs, keeping each
    entry's place in its playlist. Returns how many changed. An entry that
    no longer points at the chart copy it was found on (edited since) is
    left alone."""
    changed = 0
    for s in switches:
        item = session.get(PlaylistItem, s.item_id)
        if item is None or item.track_id != s.old.track_id:
            continue
        item.track_id = s.new.track_id
        changed += 1
    session.flush()
    return changed


def switch_charts(session: Session, switches: Iterable[ChartSwitch]) -> int:
    """Point every chart row on each chart-folder song at its album copy.
    A match James fixed by hand keeps its lock - it's still the song he
    chose, just the album copy of it. Returns how many rows changed."""
    changed = 0
    for s in switches:
        result = session.execute(
            update(ChartEntry)
            .where(ChartEntry.track_id == s.old.track_id)
            .values(track_id=s.new.track_id)
        )
        changed += result.rowcount or 0
    session.flush()
    session.expire_all()
    return changed


# --------------------------------------------------------------------------
# for chart matching: the album copy of one song
# --------------------------------------------------------------------------


def _file_infos(session: Session, where, roots: Sequence[str]) -> list[FileInfo]:
    rows = session.execute(
        select(MediaFile.id, MediaFile.track_id, MediaFile.path, MediaFile.duration_ms,
               MediaFile.size_bytes, MediaFile.codec, MediaFile.bitrate,
               Track.title, Track.artist_display, Track.duration_ms,
               Release.title, Release.artist_display, Release.is_compilation)
        .join(Track, Track.id == MediaFile.track_id)
        .join(Release, Release.id == Track.release_id)
        .where(MediaFile.is_missing.is_(False), where)
    ).all()
    out = []
    for (mf_id, track_id, path, mf_ms, size, codec, bitrate, title, artist, t_ms,
         album, album_artist, is_comp) in rows:
        artist = artist or album_artist or ""
        codec = (codec or "").lower()
        out.append(FileInfo(
            media_file_id=mf_id, track_id=track_id, path=path, title=title or "",
            artist=artist, album=album or "", duration_ms=int(mf_ms or t_ms or 0),
            size=int(size or 0), lossless=codec in ("flac", "wave", "aiff", "alac"),
            bitrate=int(bitrate or 0), chart=chart_folder(path, roots),
            compilation=bool(is_comp),
            own_album=bool(album_artist) and _artist_key(album_artist) == _artist_key(artist),
        ))
    return out


def album_copy_for(session: Session, track_id: int) -> Optional[int]:
    """When `track_id` is a song in a chart folder (Billboard, Hot Country,
    Oldies), the track id of its album copy - same title and artist, length
    within 2 seconds - or None (not a chart copy, or no album copy). Used by
    chart matching so the Charts page prefers album songs (2026-10-13).
    Cached per session: a re-match asks about the same songs many times."""
    cache = session.info.setdefault("album_copy_for", {})
    if track_id in cache:
        return cache[track_id]
    roots = session.info.get("watched_roots")
    if roots is None:
        roots = session.info["watched_roots"] = watched_roots(session)
    answer = None
    mine = _file_infos(session, MediaFile.track_id == track_id, roots)
    if mine and all(f.chart for f in mine):
        old = mine[0]
        title_key = session.scalar(select(Track.title_key).where(Track.id == track_id)) or ""
        others = [f for f in _file_infos(session, Track.title_key == title_key, roots)
                  if not f.chart]
        cands = [c for c in _same_song(old, others)
                 if old.duration_ms and c.duration_ms
                 and abs(c.duration_ms - old.duration_ms) <= SAME_SONG_MAX_DIFF_MS]
        picked = _choose(old, cands, None)
        answer = picked[0].track_id if picked else None
    cache[track_id] = answer
    return answer
