"""Edit tags in the music files and bring the library along (2026-10-06).

James: "Tag editor with MusicBrainz lookup. Fix tags in the files
themselves, plus a 'look up this album' button. Better tags would also
improve chart matching."

The files are the source of truth, so an edit is: write the tags into each
of the song's files (services/tag_writer.py), then have the scanner read
those files again (scanner.import_file), as a rescan would after tagging in
another program. The next USB sync carries the edited files (their date
changed) to the other PCs, and their scanner reads them the same way.

Along the way:

- **A song is written whole or not at all.** An MP3 and a FLAC of one song
  are both written; if the second can't be (it's playing, say), the first
  is put back, so the two never disagree.
- **History stays with the file.** A new title or album files a song under
  a different track. The scanner keeps that song's rating, plays, playlist
  places, jukebox card and chart matches (hand-fixed ones too) with its
  file (scanner.hide_retitled / carry_history) - also when titles are
  shuffled between songs, and also on the other PCs and after a retag in
  Mp3tag or Picard, since it's the scanner doing it.
- **An album renamed as a whole stays the same album.** When every song of
  an album gets the same new title, album artist or catalog #, the album
  row itself is renamed, so its notes, format (Vinyl, LP, Reissue),
  credits and cover stay. A song moved to another album takes nothing with
  it from the album it left; an album every song left goes, and its cover
  goes with them to an album without one.
- **Albums follow their files.** The scanner only ever *adds* to an album
  and never changes its year, so every album touched is refreshed from its
  files: year, date, label, catalog #, barcode, country, genres and styles.
- **Charts are matched again** for the edited songs: chart rows matched to
  them (unless fixed by hand) and unmatched rows with the new title, so
  correcting "Dont Stop Believin" to "Don't Stop Believin'" can pick up a
  chart entry that never matched before.

The library is committed after each step, so the database isn't held for
the length of a whole album's save.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from sqlalchemy import select

from .. import config
from ..db.models import (
    ChartEntry,
    Credit,
    Master,
    MediaFile,
    Release,
    Track,
)
from ..db.session import session_scope
from . import library as lib
from . import scanner
from . import tag_writer as tw
from .matching import normalize

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


# --------------------------------------------------------------------------
# loading what the editor shows
# --------------------------------------------------------------------------


@dataclass
class EditRow:
    """One track as the editor shows it: its tags as the file has them."""

    track_id: int
    #: the file whose tags are shown (the first present one)
    path: Optional[str]
    #: every present file of the track (an MP3 and a FLAC of one song are
    #: both written)
    paths: list[str]
    values: tw.TagValues
    duration_ms: Optional[int] = None
    release_id: Optional[int] = None
    #: why this track can't be edited ("" when it can)
    problem: str = ""

    @property
    def editable(self) -> bool:
        return not self.problem

    @property
    def file_name(self) -> str:
        return Path(self.path).name if self.path else ""


def _db_values(track: Track) -> tw.TagValues:
    """What the library knows, for a track whose file can't be read."""
    rel = track.release
    return tw.TagValues(
        title=track.title or "",
        artist=track.artist_display or "",
        album=rel.title if rel else "",
        album_artist=rel.artist_display if rel else "",
        track=track.position if track.position and not track.position[:1].isdigit()
        else (str(track.track_no) if track.track_no else ""),
        track_total="",
        disc=str(track.disc_no) if track.disc_no else "",
        disc_total="",
        date=(rel.released or (str(rel.year) if rel.year else "")) if rel else "",
        genre=", ".join(g.name for g in rel.genres) if rel else "",
        label=rel.label.name if rel and rel.label else "",
        catalog_number=(rel.catalog_number or "") if rel else "",
        barcode=(rel.barcode or "") if rel else "",
        country=(rel.country or "") if rel else "",
        isrc=track.isrc or "",
        comment=track.comment or "",
        compilation="1" if rel and rel.is_compilation else "",
    )


def load_rows(session, track_ids: Sequence[int]) -> list[EditRow]:
    """The editor's rows for these tracks, in disc/track order within each
    album (albums in the order first given). Each row's values are read
    from the file itself, so the editor shows the real tags - not the
    scanner's fill-ins (a missing album tag shows empty, not the folder
    name)."""
    rows: list[EditRow] = []
    seen: set[int] = set()
    tracks = []
    for tid in track_ids:
        if tid in seen:
            continue
        seen.add(tid)
        track = session.get(Track, tid)
        if track is not None:
            tracks.append(track)
    order = {t.id: i for i, t in enumerate(tracks)}
    first_of_release: dict[int, int] = {}
    for t in tracks:
        first_of_release.setdefault(t.release_id, order[t.id])
    tracks.sort(key=lambda t: (first_of_release[t.release_id], t.disc_no or 1,
                               t.track_no if t.track_no is not None else 10_000,
                               (t.title or "").lower()))
    for track in tracks:
        present = [f.path for f in track.files if not f.is_missing and Path(f.path).exists()]
        row = EditRow(
            track_id=track.id,
            path=present[0] if present else None,
            paths=present,
            values=_db_values(track),
            duration_ms=track.duration_ms,
            release_id=track.release_id,
        )
        if not present:
            row.problem = "The file isn't there"
        elif not all(tw.can_write(p) for p in present):
            bad = next(p for p in present if not tw.can_write(p))
            row.problem = f"MusicMgr can't write tags into {Path(bad).suffix.lower()} files"
        else:
            try:
                row.values = tw.read_values(present[0])
            except tw.TagWriteError as exc:
                row.problem = str(exc)
        rows.append(row)
    return rows


def load_rows_for_release(session, release_id: int) -> list[EditRow]:
    ids = list(session.scalars(select(Track.id).where(Track.release_id == release_id)))
    return load_rows(session, ids)


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------


@dataclass
class SaveResult:
    #: files whose tags were changed
    written: int = 0
    #: files that already said what was asked (left untouched)
    unchanged: int = 0
    #: (file name, reason) for each file that couldn't be written
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: songs that moved to another album or track, history and all
    moved: int = 0
    #: albums removed because every song left them
    albums_removed: int = 0
    #: chart rows newly matched (or re-matched) to an edited song
    charts_matched: int = 0
    cancelled: bool = False
    #: old track id -> the track id its file belongs to now (for the player
    #: queue and any open page holding the old id)
    moves: dict[int, int] = field(default_factory=dict)
    #: every track id that was edited, after the moves
    track_ids: set[int] = field(default_factory=set)
    #: album ids touched (new ones included)
    release_ids: set[int] = field(default_factory=set)

    def summary(self) -> str:
        parts = []
        if self.written:
            parts.append(f"{self.written} file{'s' if self.written != 1 else ''} updated")
        if not self.written and not self.failed:
            parts.append("Nothing needed changing")
        if self.moved:
            parts.append(f"{self.moved} song{'s' if self.moved != 1 else ''} moved with "
                         "ratings, plays and playlists")
        if self.charts_matched:
            parts.append(f"{self.charts_matched} chart row{'s' if self.charts_matched != 1 else ''} matched")
        if self.failed:
            parts.append(f"{len(self.failed)} couldn't be written")
        if self.cancelled:
            parts.append("stopped early")
        return " · ".join(parts)


def save_edits(
    edits: dict[int, tw.TagValues],
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    cover: Optional[bytes] = None,
) -> SaveResult:
    """Write `edits` (track id -> the fields to change) into every present
    file of each track, then update the library from the files. Opens its
    own session (it runs on a worker thread) and commits after each step,
    so the database is never held for the whole save.

    `cover` (a MusicBrainz lookup's Cover Art Archive picture) becomes the
    cover of the album the edited songs end up on."""
    with session_scope() as session:
        return save_edits_in(session, edits, progress=progress, should_stop=should_stop,
                             cover=cover, commit=session.commit)


@dataclass
class _FileJob:
    track_id: int
    path: str
    values: tw.TagValues
    before: Optional[tw.TagValues] = None
    written: bool = False


def save_edits_in(
    session,
    edits: dict[int, tw.TagValues],
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    cover: Optional[bytes] = None,
    commit: Optional[Callable[[], None]] = None,
) -> SaveResult:
    result = SaveResult()
    commit = commit or session.flush

    # every present file of every edited song, grouped by song
    by_track: dict[int, list[_FileJob]] = {}
    old_release: dict[int, int] = {}
    for tid, values in edits.items():
        if values is None or values.is_empty():
            continue
        track = session.get(Track, tid)
        if track is None:
            continue
        present = [mf for mf in track.files if not mf.is_missing]
        if not present:
            result.failed.append((track.title or f"track {tid}", "the file isn't there any more"))
            continue
        gone = [mf for mf in present if not Path(mf.path).exists()]
        if gone:
            # a song is written whole or not at all
            for mf in gone:
                result.failed.append((Path(mf.path).name, "the file isn't there any more"))
            continue
        by_track[tid] = [_FileJob(tid, mf.path, values) for mf in present]
        old_release[tid] = track.release_id
    total = sum(len(j) for j in by_track.values())

    # 1. write the files - a song at a time, all of its files or none
    done = 0
    written_tracks: list[int] = []
    for tid, jobs in by_track.items():
        if should_stop and should_stop():
            result.cancelled = True
            break
        failed = False
        for job in jobs:
            done += 1
            name = Path(job.path).name
            if progress:
                progress(done - 1, total, f"Writing {name}")
            try:
                job.before = tw.read_values(job.path)
                job.written = tw.write_values(job.path, job.values)
            except tw.TagWriteError as exc:
                result.failed.append((name, str(exc)))
                failed = True
                break
            except Exception as exc:  # never let one odd file stop the rest
                log.exception("tag write failed for %s", job.path)
                result.failed.append((name, str(exc) or type(exc).__name__))
                failed = True
                break
        if failed:
            # put back the song's files that were already written, so its
            # MP3 and FLAC can't end up saying different things
            for job in jobs:
                if job.written and job.before is not None:
                    undo = tw.TagValues(**{k: getattr(job.before, k) or ""
                                           for k in job.values.changed_fields()})
                    try:
                        tw.write_values(job.path, undo)
                    except Exception:  # pragma: no cover - reported below
                        log.exception("couldn't put back %s", job.path)
                        result.failed.append((Path(job.path).name,
                                              "changed, but the song's other file couldn't be - "
                                              "check this one"))
                    job.written = False
            continue
        n = sum(1 for j in jobs if j.written)
        result.written += n
        result.unchanged += len(jobs) - n
        if n:
            written_tracks.append(tid)
    if progress:
        progress(total, total, "Updating the library…")

    # 2. read the written files back into the library
    paths = [Path(j.path) for tid in written_tracks for j in by_track[tid] if j.written]
    tags_of = {}
    for p in paths:
        try:
            tags_of[p] = scanner._fallback_from_path(p, scanner.read_tags(p))
        except Exception as exc:
            log.exception("re-reading %s failed", p)
            result.failed.append((p.name, f"saved, but couldn't be read back ({exc})"))
    renamed = _rename_whole_albums(session, written_tracks, by_track, tags_of, old_release)
    hidden = scanner.hide_retitled(session, tags_of.items())
    scan = scanner.ScanResult()
    for p, tags in tags_of.items():
        mf = session.scalar(select(MediaFile).where(MediaFile.path == str(p)))
        rel = mf.track.release if mf is not None and mf.track is not None else None
        override, comp = None, None
        if (rel is not None and not tags.had_explicit_album_artist
                and normalize(tags.album) == rel.title_key):
            # no album artist in the file and the album didn't change: keep
            # the folder-wide decision the scan made (a various-artists
            # compilation stays one album), as a rescan does
            override, comp = rel.artist_display or None, bool(rel.is_compilation)
        try:
            track = scanner.import_file(session, p, scan, tags=tags, force=True,
                                        album_artist_override=override,
                                        is_compilation_override=comp)
        except Exception as exc:
            log.exception("re-reading %s after a tag edit failed", p)
            result.failed.append((p.name, f"saved, but the library couldn't take it in ({exc})"))
            continue
        if track is not None:
            # the file's own spelling, for a fix the title keys can't see
            # ("back in black" -> "Back in Black")
            if tags.title and track.title != tags.title:
                track.title = tags.title
            r = track.release
            if r is not None and tags.album and r.title != tags.album \
                    and normalize(tags.album) == r.title_key:
                r.title = tags.album
    scanner.restore_hidden(session, hidden)
    moved = scanner.carry_history(session, scan.retracked)
    result.moves.update(moved)
    result.moved = len(moved)
    session.flush()

    # where each edited song is now
    ended_on: set[int] = set()
    for tid in written_tracks:
        jobs = [j for j in by_track[tid] if j.written]
        mf = session.scalar(select(MediaFile).where(MediaFile.path == str(jobs[0].path))) if jobs else None
        if mf is not None:
            result.track_ids.add(mf.track_id)
            ended_on.add(mf.track.release_id)
    for tid in by_track:
        if tid not in result.moves and session.get(Track, tid) is not None:
            result.track_ids.add(tid)
    result.release_ids |= ended_on
    commit()

    # 3. albums every song left go; their cover goes with the songs
    went_to: dict[int, set[int]] = {}
    for old_tid in written_tracks:
        new_tid = moved.get(old_tid, old_tid)
        t = session.get(Track, new_tid)
        if t is not None:
            went_to.setdefault(old_release[old_tid], set()).add(t.release_id)
    for rid, targets in went_to.items():
        old_rel = session.get(Release, rid)
        if old_rel is None:
            continue
        if session.scalar(select(Track.id).where(Track.release_id == rid).limit(1)) is not None:
            result.release_ids.add(rid)
            continue
        _carry_cover(old_rel, [session.get(Release, r) for r in targets if r != rid])
        master = old_rel.master
        session.delete(old_rel)
        session.flush()
        result.albums_removed += 1
        _drop_empty_master(session, master)
    for rid in renamed:
        result.release_ids.add(rid)
    session.flush()

    # 4. albums follow their files; the looked-up cover goes on the album
    #    the songs ended up on (never on one they left)
    for rid in list(result.release_ids):
        rel = session.get(Release, rid)
        if rel is not None:
            _refresh_release(session, rel)
    if cover:
        from . import artwork_names
        for rid in ended_on:
            rel = session.get(Release, rid)
            if rel is not None:
                saved = artwork_names.save_release_cover(rel, cover, overwrite=True)
                if saved:
                    rel.cover_path = saved
    commit()

    # 5. charts
    result.charts_matched = _rematch_charts(session, result.track_ids, set(result.moves))
    commit()
    return result


def _rename_whole_albums(session, written_tracks, by_track, tags_of, old_release) -> set[int]:
    """When every song of an album was edited to one new album title,
    album artist or catalog #, rename the album row itself rather than let
    the scanner make a new album and drop the old - so its notes, format
    (Vinyl, LP, Reissue), credits, cover and place in USB sync all stay.
    Returns the albums renamed."""
    from . import artwork_names

    renamed: set[int] = set()
    edited = set(written_tracks)
    by_rel: dict[int, list[int]] = {}
    for tid in written_tracks:
        by_rel.setdefault(old_release[tid], []).append(tid)
    for rid, tids in by_rel.items():
        rel = session.get(Release, rid)
        if rel is None:
            continue
        all_ids = set(session.scalars(select(Track.id).where(Track.release_id == rid)))
        if not all_ids <= edited:
            continue  # some songs stay behind - the scanner splits it
        # every file of every song must have been written and read back
        files = [Path(j.path) for t in tids for j in by_track[t]]
        if not all(f in tags_of for f in files):
            continue
        idents = set()
        for f in files:
            tags = tags_of[f]
            artist = tags.album_artist if tags.had_explicit_album_artist else (
                rel.artist_display if normalize(tags.album) == rel.title_key else tags.album_artist)
            idents.add((tags.album, artist, tags.catalog_number or None))
        if len(idents) != 1:
            continue
        album, artist, catalog = idents.pop()
        key = normalize(album)
        if (key, artist, catalog) == (rel.title_key, rel.artist_display, rel.catalog_number):
            continue  # nothing about the album's identity changed
        clash = session.scalar(select(Release.id).where(
            Release.title_key == key, Release.artist_display == artist, Release.id != rid))
        if clash is not None:
            continue  # it's joining an album that's already there
        old_cover = _cover_file(rel)
        rel.title, rel.title_key, rel.artist_display = album, key, artist
        rel.catalog_number = catalog
        # the release-level credit names the album artist; the scanner adds
        # the new one as it reads the files
        for credit in list(rel.credits):
            if credit.role == Credit.ROLE_MAIN:
                session.delete(credit)
        if old_cover is not None:
            try:
                saved = artwork_names.save_release_cover(
                    rel, old_cover.read_bytes(), ext_hint=old_cover.suffix, overwrite=False)
                if saved:
                    rel.cover_path = saved
            except OSError:
                pass
        old_master = rel.master
        session.flush()
        renamed.add(rid)
        if old_master is not None:
            master = lib.get_or_create_master(session, album, artist, rel.year)
            if master.id != old_master.id:
                rel.master_id = master.id
                if master.main_release_id is None:
                    master.main_release_id = rel.id
                session.flush()
                _drop_empty_master(session, old_master)
    return renamed


def _drop_empty_master(session, master) -> None:
    if master is None:
        return
    session.expire(master)
    m = session.get(Master, master.id)
    if m is not None and session.scalar(select(Release.id).where(Release.master_id == m.id).limit(1)) is None:
        session.delete(m)
        session.flush()


def _cover_file(rel: Release) -> Optional[Path]:
    if not rel.cover_path:
        return None
    p = Path(rel.cover_path)
    if p.exists():
        return p
    alt = config.ART_DIR / p.name
    return alt if alt.exists() else None


def _carry_cover(old_rel: Release, targets: Sequence[Optional[Release]]) -> None:
    """The cover of an album every song left goes to where they went."""
    src = _cover_file(old_rel)
    if src is None:
        return
    from . import artwork_names
    try:
        data = src.read_bytes()
    except OSError:
        return
    for rel in targets:
        if rel is None or _cover_file(rel) is not None:
            continue
        saved = artwork_names.save_release_cover(rel, data, ext_hint=src.suffix, overwrite=False)
        if saved:
            rel.cover_path = saved


def _most_common(values: list) -> Optional[object]:
    values = [v for v in values if v not in (None, "")]
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def _refresh_release(session, rel: Release) -> None:
    """Set an album's year, date, label, numbers, country, genres and
    styles from what its files say now (the scanner only ever adds to an
    album and never changes its year)."""
    vals: list[tw.TagValues] = []
    styles: list[str] = []
    for track in session.scalars(select(Track).where(Track.release_id == rel.id)):
        mf = track.primary_file
        if mf is None or mf.is_missing or not Path(mf.path).exists():
            continue
        try:
            vals.append(tw.read_values(mf.path))
            for st in scanner.read_tags(Path(mf.path)).styles:
                if st not in styles:
                    styles.append(st)
        except Exception:
            continue
    if not vals:
        return
    date = _most_common([v.date for v in vals])
    year = int(date[:4]) if date and date[:4].isdigit() else None
    rel.released = date or None
    rel.year = year
    rel.catalog_number = _most_common([v.catalog_number for v in vals])
    rel.barcode = _most_common([v.barcode for v in vals])
    rel.country = _most_common([v.country for v in vals])
    label_name = _most_common([v.label for v in vals])
    if label_name:
        label = lib.get_or_create_label(session, label_name)
        rel.label_id = label.id if label is not None else None
    else:
        rel.label_id = None
    genres: list[str] = []
    for v in vals:
        for g in re.split(r"[;/,]", v.genre or ""):
            g = g.strip()
            if g and g not in genres:
                genres.append(g)
    rel.genres = [g for g in (lib.get_or_create_genre(session, n) for n in genres) if g is not None]
    rel.styles = [x for x in (lib.get_or_create_style(session, n) for n in styles) if x is not None]
    if rel.master is not None and rel.year and (rel.master.year is None or rel.year < rel.master.year):
        rel.master.year = rel.year


def _rematch_charts(session, track_ids: set[int], moved_from: set[int]) -> int:
    """Re-match chart rows for the edited songs. Returns how many rows
    point at one of them afterwards that didn't before."""
    if not track_ids:
        return 0
    from . import charts as chart_svc

    tracks = [session.get(Track, t) for t in track_ids]
    tracks = [t for t in tracks if t is not None]
    keys = {t.title_key for t in tracks if t.title_key}
    entries = list(session.scalars(select(ChartEntry).where(
        ChartEntry.match_locked.is_(False),
        (ChartEntry.track_id.in_(track_ids)) | (
            ChartEntry.track_id.is_(None) & ChartEntry.title_key.in_(keys)
        ),
    ))) if keys else list(session.scalars(select(ChartEntry).where(
        ChartEntry.match_locked.is_(False), ChartEntry.track_id.in_(track_ids))))
    before = {e.id: e.track_id for e in entries}
    seen: dict[tuple[str, str], tuple[Optional[int], Optional[float]]] = {}
    for entry in entries:
        key = (entry.title_key, entry.artist_key)
        if key in seen:
            entry.track_id, entry.match_score = seen[key]
        else:
            chart_svc.match_entry(session, entry)
            seen[key] = (entry.track_id, entry.match_score)
    return sum(1 for e in entries
               if e.track_id in track_ids and before.get(e.id) != e.track_id)
