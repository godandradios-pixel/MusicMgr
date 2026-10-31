"""Library Health: one check-up of the library against the disk and itself.

2026-10-25 (James): what was done by hand that day - a scan for records
with no file, files the library never imported (the ".38 Special" videos),
chart positions linked to the wrong song, chart editions with missing
positions - as one Settings button (Files & data › Library health), with a
fix next to each finding that has one. Every fix saves a copy of the
library first (backups\\library-before-health-fix-<time>.db).

`check_library` is read-only and returns a `HealthReport` of `Finding`s;
`apply_fix(session, key)` runs one finding's fix. The checks:

- **missing_files** - song and video records whose file is gone. No fix
  here: Verify files › Verify… already removes them, keeping songs that
  still have history.
- **not_imported** - music and video files in the watched folders that
  the library doesn't have. Fix: import them.
- **no_file_songs** - songs with no file record at all (kept alive by a
  playlist, chart or play). Shown only.
- **broken_links** - chart positions, playlist entries, plays, credits and
  Jukebox sides pointing at a song (or playlist) that no longer exists.
  Fix: unlink or remove them.
- **loose_chart_matches** - chart positions linked to a song they no
  longer match by matching.score_pair (title *and* artist). Fix: unlink;
  positions fixed by hand are never touched.
- **stale_chart_playlists** - playlists saved from a chart edition whose
  songs no longer match it. Fix: save them again (name, folder and tile
  kept - charts.snapshot_to_playlist).
- **garbled_chart_text** - chart songs whose accented letters were
  mangled on import ("Expos\ufffd", "ExposÃ©"). Fix: fill the letters in from
  names the library already knows (text_repair) and match the fixed rows.
- **edition_gaps** - chart editions missing positions (rank 15 with no
  row). Shown only: the missing rows have to come from the source.
- **empty_albums** - albums with no songs. Fix: remove them.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import config
from ..db.models import (
    Chart, ChartEntry, ChartFolder, ChartIssue, MediaFile, Playlist, PlaylistItem,
    Release, Track, WatchedFolder,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]

#: how many examples each finding keeps to show
EXAMPLES = 200
MATCH_THRESHOLD = 0.72  # charts.match_entry's default


@dataclass
class Finding:
    key: str
    title: str
    #: what it means, one line
    detail: str
    count: int = 0
    examples: list[str] = field(default_factory=list)
    #: button text, or "" when there's nothing to fix here
    fix_label: str = ""
    #: shown when there's no fix button: where to fix it instead
    hint: str = ""
    #: what the fix works from (ids, paths) - not shown
    data: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.count == 0


@dataclass
class HealthReport:
    findings: list[Finding] = field(default_factory=list)
    seconds: float = 0.0
    cancelled: bool = False

    def get(self, key: str) -> Optional[Finding]:
        return next((f for f in self.findings if f.key == key), None)

    def problems(self) -> int:
        return sum(1 for f in self.findings if not f.ok)


def _key(path: str) -> str:
    return os.path.normcase(path) if config.CASE_INSENSITIVE_PATHS else path


def _add(finding: Finding, example: str) -> None:
    finding.count += 1
    if len(finding.examples) < EXAMPLES:
        finding.examples.append(example)


# -- files ---------------------------------------------------------------------


def _walk(session: Session, progress, should_stop) -> tuple[set[str], set[str], set[str]]:
    """(every file seen, music files, video files) under the watched
    folders, walked the way the scanners walk them."""
    from . import radio_folder
    from .scanner import iter_audio_files
    from .video_scanner import iter_video_files

    radio = radio_folder.roots(session)
    roots = [Path(p) for p in session.scalars(
        select(WatchedFolder.path).where(WatchedFolder.enabled.is_(True)))]
    audio: set[str] = set()
    video: set[str] = set()
    for root in roots:
        if should_stop and should_stop():
            break
        if not root.exists():
            continue
        if progress:
            progress(0, 0, f"Looking through {root}…")
        for n, p in enumerate(iter_audio_files(root, skip=radio), start=1):
            audio.add(str(p))
            if progress and n % 2000 == 0:
                progress(0, 0, f"Looking through {root}… {n:,} files")
        for p in iter_video_files(root):
            video.add(str(p))
    seen = {_key(p) for p in audio | video}
    return seen, audio, video


def _check_files(session: Session, report: HealthReport, progress, should_stop) -> None:
    from ..db.models import Video

    seen, audio, video = _walk(session, progress, should_stop)
    missing = Finding(
        "missing_files", "Records whose file is gone",
        "Songs and videos the library lists whose file can no longer be found.",
        hint="Remove them with Verify files › Verify… (songs with playlists, plays or "
             "chart places are kept).")
    known_audio: set[str] = set()
    rows = session.execute(select(MediaFile.path, Track.title, Track.artist_display)
                           .join(Track, Track.id == MediaFile.track_id)).all()
    for i, (path, title, artist) in enumerate(rows, start=1):
        k = _key(path)
        known_audio.add(k)
        if k not in seen and not os.path.isfile(path):
            _add(missing, f"{artist or '?'} - {title}   ({path})")
        if progress and i % 5000 == 0:
            progress(i, len(rows), "Checking song files")
    known_video: set[str] = set()
    for path, title in session.execute(select(Video.path, Video.title)):
        k = _key(path)
        known_video.add(k)
        if k not in seen and not os.path.isfile(path):
            _add(missing, f"Video: {title}   ({path})")
    report.findings.append(missing)

    new = Finding(
        "not_imported", "Files not in the library",
        "Music and video files in your watched folders that MusicMgr never imported.",
        fix_label="Import them")
    listed: set[str] = set()
    for kind, paths, known in (("audio", audio, known_audio), ("video", video, known_video)):
        for p in sorted(paths):
            if _key(p) in known:
                continue
            new.data.append((kind, p))
            if _key(p) not in listed:   # an .mp4 can be both: one line
                listed.add(_key(p))
                _add(new, p)
    report.findings.append(new)

    nofile = Finding(
        "no_file_songs", "Songs with no file",
        "Songs that have no file record at all - kept only because a playlist, chart or "
        "play history points at them.",
        hint="Add the file back and rescan, or remove the song from its playlists.")
    for title, artist in session.execute(
        select(Track.title, Track.artist_display)
        .where(~select(MediaFile.id).where(MediaFile.track_id == Track.id).exists())
    ):
        _add(nofile, f"{artist or '?'} - {title}")
    report.findings.append(nofile)


# -- links ---------------------------------------------------------------------

#: (table, column, what it is, fix: "null" or "delete")
_TRACK_LINKS = (
    ("chart_entries", "track_id", "chart positions", "null"),
    ("playlist_items", "track_id", "playlist entries", "delete"),
    ("play_events", "track_id", "plays", "delete"),
    ("credits", "track_id", "credits", "delete"),
    ("jukebox_slots", "side_a_track_id", "Jukebox A sides", "null"),
    ("jukebox_slots", "side_b_track_id", "Jukebox B sides", "null"),
)


def _columns(session: Session, table: str) -> set[str]:
    return {r[1] for r in session.execute(text(f"PRAGMA table_info({table})"))}


def _broken_link_sql(session: Session):
    """(where clause, table, column, label, fix) for each link that exists
    in this database."""
    out = []
    for table, col, label, fix in _TRACK_LINKS:
        if col in _columns(session, table):
            out.append((f"{col} IS NOT NULL AND {col} NOT IN (SELECT id FROM tracks)",
                        table, col, label, fix))
    out.append(("playlist_id NOT IN (SELECT id FROM playlists)",
                "playlist_items", "playlist_id", "playlist entries of a deleted playlist",
                "delete"))
    return out


def _check_links(session: Session, report: HealthReport) -> None:
    f = Finding(
        "broken_links", "Broken links",
        "Chart positions, playlist entries, plays, credits and Jukebox sides that point at "
        "a song or playlist that no longer exists.",
        fix_label="Clean up")
    for where, table, col, label, fix in _broken_link_sql(session):
        n = session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar() or 0
        if n:
            f.count += n
            f.examples.append(f"{n:,} {label}")
    report.findings.append(f)


# -- charts --------------------------------------------------------------------


def _chart_paths(session: Session) -> dict[int, str]:
    """chart id -> "Billboard › Country › Year End"."""
    folders = {f.id: f for f in session.scalars(select(ChartFolder))}

    def path(fid):
        names, seen = [], set()
        while fid is not None and fid in folders and fid not in seen:
            seen.add(fid)
            names.append(folders[fid].name)
            fid = folders[fid].parent_id
        return list(reversed(names))

    return {c.id: " › ".join(path(c.folder_id) + [c.name]) for c in session.scalars(select(Chart))}


def _check_loose_matches(session: Session, report: HealthReport, progress, should_stop) -> None:
    from .matching import score_pair

    f = Finding(
        "loose_chart_matches", "Chart positions linked to the wrong song",
        "Positions linked to a song whose title or artist no longer matches (for example "
        "\"Hurricane\" linked to \"Beautiful Hurricane\"). Matches you fixed by hand are "
        "left alone.",
        fix_label="Unlink them")
    rows = session.execute(
        select(ChartEntry.id, ChartEntry.title, ChartEntry.artist_name,
               Track.title, Track.artist_display)
        .join(Track, Track.id == ChartEntry.track_id)
        .where(ChartEntry.match_locked.is_not(True))
    ).all()
    scores: dict[tuple, float] = {}
    for i, (eid, et, ea, tt, ta) in enumerate(rows, start=1):
        if should_stop and should_stop():
            report.cancelled = True
            break
        k = (et, ea, tt, ta)
        first = k not in scores
        if first:
            scores[k] = score_pair(et or "", ea or "", tt or "", ta or "")
        if scores[k] < MATCH_THRESHOLD:
            f.count += 1
            f.data.append(eid)
            if first and len(f.examples) < EXAMPLES:
                # one line per song pair, however many weeks it charted
                f.examples.append(f"{et} - {ea}   →   {tt} - {ta}")
        if progress and i % 20000 == 0:
            progress(i, len(rows), "Checking chart matches")
    report.findings.append(f)


def _stale_chart_playlists(session: Session) -> list[tuple[Playlist, ChartIssue]]:
    out = []
    playlists = session.scalars(select(Playlist).where(
        Playlist.kind == Playlist.KIND_CHART, Playlist.source_issue_id.is_not(None))).all()
    for pl in playlists:
        issue = session.get(ChartIssue, pl.source_issue_id)
        if issue is None:
            continue
        want = list(session.scalars(
            select(ChartEntry.track_id).where(ChartEntry.issue_id == issue.id,
                                              ChartEntry.track_id.is_not(None))
            .order_by(ChartEntry.rank)))
        have = list(session.scalars(
            select(PlaylistItem.track_id).where(PlaylistItem.playlist_id == pl.id)
            .order_by(PlaylistItem.position)))
        if want != have:
            out.append((pl, issue))
    return out


def _check_chart_playlists(session: Session, report: HealthReport) -> None:
    from .charts import issue_label
    from .playlists import folder_path

    f = Finding(
        "stale_chart_playlists", "Chart playlists out of date",
        "Playlists saved from a chart whose songs no longer match the chart (songs added "
        "or matched since, or wrong matches removed).",
        fix_label="Update them")
    paths = _chart_paths(session)
    for pl, issue in _stale_chart_playlists(session):
        where = " › ".join(p.name for p in folder_path(session, pl.folder_id)) or "Playlists"
        _add(f, f"{where} › {pl.name}   (from {paths.get(issue.chart_id, '?')} "
                f"{issue_label(session, issue)})")
        f.data.append(issue.id)
    report.findings.append(f)


def _check_garbled_text(session: Session, report: HealthReport) -> None:
    from .text_repair import damaged_chart_rows

    f = Finding(
        "garbled_chart_text", "Chart songs with garbled letters",
        "Chart titles or artists whose accented letters were mangled when they were "
        "imported (\"Expos\ufffd\" or \"ExposÃ©\" for Exposé).",
        fix_label="Repair them")
    shown: set[tuple] = set()
    for eid, title, artist in damaged_chart_rows(session):
        f.count += 1
        f.data.append(eid)
        if (title, artist) not in shown and len(f.examples) < EXAMPLES:
            shown.add((title, artist))
            f.examples.append(f"{title} - {artist}")
    report.findings.append(f)


def _check_edition_gaps(session: Session, report: HealthReport) -> None:
    from .charts import chart_edition_style, edition_label

    f = Finding(
        "edition_gaps", "Chart editions with missing positions",
        "Editions where some positions have no row - the source they were imported from "
        "skipped them.",
        hint="Add the missing songs with Charts › Add song…, or import the edition again "
             "from a complete list.")
    paths = _chart_paths(session)
    styles: dict[int, str] = {}
    rows = session.execute(
        select(ChartIssue.id, ChartIssue.chart_id, ChartIssue.chart_date,
               func.count(ChartEntry.id), func.max(ChartEntry.rank))
        .join(ChartEntry, ChartEntry.issue_id == ChartIssue.id)
        .group_by(ChartIssue.id)
        .having(func.count(ChartEntry.id) < func.max(ChartEntry.rank))
        .order_by(ChartIssue.chart_id, ChartIssue.chart_date)
    ).all()
    for _iid, cid, date, n, top in rows:
        if cid not in styles:
            styles[cid] = chart_edition_style(session, cid)
        _add(f, f"{paths.get(cid, '?')}  {edition_label(date, styles[cid])}: "
                f"{n} of {top} positions")
    report.findings.append(f)


def _check_empty_albums(session: Session, report: HealthReport) -> None:
    f = Finding(
        "empty_albums", "Albums with no songs",
        "Albums left with no songs (usually after files were retagged or moved).",
        fix_label="Remove them")
    for (title,) in session.execute(select(Release.title).where(
            ~select(Track.id).where(Track.release_id == Release.id).exists())):
        _add(f, title or "(untitled)")
    report.findings.append(f)


# -- entry points --------------------------------------------------------------


def check_library(session: Session, progress: Optional[ProgressFn] = None,
                  should_stop: Optional[Callable[[], bool]] = None) -> HealthReport:
    """Run every check. Read-only."""
    started = time.monotonic()
    report = HealthReport()
    steps = (
        ("Checking files on disk…", lambda: _check_files(session, report, progress, should_stop)),
        ("Checking links…", lambda: _check_links(session, report)),
        ("Checking chart matches…",
         lambda: _check_loose_matches(session, report, progress, should_stop)),
        ("Checking chart playlists…", lambda: _check_chart_playlists(session, report)),
        ("Checking chart text…", lambda: _check_garbled_text(session, report)),
        ("Checking chart editions…", lambda: _check_edition_gaps(session, report)),
        ("Checking albums…", lambda: _check_empty_albums(session, report)),
    )
    for label, step in steps:
        if should_stop and should_stop():
            report.cancelled = True
            break
        if progress:
            progress(0, 0, label)
        step()
    report.seconds = time.monotonic() - started
    return report


def backup_before_fix(db_path: Optional[Path] = None, backup_dir: Optional[Path] = None) -> Path:
    """A copy of the library taken before any fix (raises if it can't be
    made - then nothing is fixed)."""
    from .library_backup import _copy_database, check_database

    db_path = db_path or config.DB_PATH
    backup_dir = backup_dir or (config.DATA_DIR / "backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"library-before-health-fix-{time.strftime('%Y%m%d-%H%M%S')}.db"
    _copy_database(db_path, dest)
    problem = check_database(dest)
    if problem:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"the backup failed its check ({problem})")
    return dest


def apply_fix(session: Session, finding: Finding,
              should_stop: Optional[Callable[[], bool]] = None) -> str:
    """Run `finding`'s fix in `session` (the caller commits). Returns what
    was done, in words."""
    key = finding.key
    if key == "not_imported":
        from .scanner import import_paths
        from .video_scanner import import_video_paths

        audio = [Path(p) for kind, p in finding.data if kind == "audio"]
        video = [Path(p) for kind, p in finding.data if kind == "video"]
        errors: list[str] = []
        if audio:
            errors += import_paths(session, audio, should_stop=should_stop).errors
        if video:
            errors += import_video_paths(session, video, should_stop=should_stop).errors
        done = len({p for _kind, p in finding.data})
        note = f" ({len(errors)} couldn't be read)" if errors else ""
        return f"Imported {done:,} file{'s' if done != 1 else ''}{note}"
    if key == "broken_links":
        done = 0
        for where, table, col, _label, fix in _broken_link_sql(session):
            if fix == "null":
                r = session.execute(text(f"UPDATE {table} SET {col} = NULL WHERE {where}"))
            else:
                r = session.execute(text(f"DELETE FROM {table} WHERE {where}"))
            done += r.rowcount or 0
        return f"Cleaned up {done:,} broken link{'s' if done != 1 else ''}"
    if key == "loose_chart_matches":
        ids = list(finding.data)
        for start in range(0, len(ids), 500):
            for e in session.scalars(select(ChartEntry).where(ChartEntry.id.in_(ids[start:start + 500]))):
                if not e.match_locked:
                    e.track_id, e.match_score = None, None
        return f"Unlinked {len(ids):,} chart position{'s' if len(ids) != 1 else ''}"
    if key == "stale_chart_playlists":
        from .charts import snapshot_to_playlist

        done = 0
        for issue_id in finding.data:
            if should_stop and should_stop():
                break
            snapshot_to_playlist(session, issue_id)
            done += 1
        return f"Updated {done:,} chart playlist{'s' if done != 1 else ''}"
    if key == "garbled_chart_text":
        from .text_repair import repair_chart_entries

        done, left = repair_chart_entries(session, finding.data)
        note = (f" ({left:,} couldn't be worked out - fix those with Charts › Edit song…)"
                if left else "")
        return f"Repaired {done:,} chart song{'s' if done != 1 else ''}{note}"
    if key == "empty_albums":
        from .scanner import purge_empty_releases

        done = purge_empty_releases(session)
        return f"Removed {done:,} empty album{'s' if done != 1 else ''}"
    raise ValueError(f"nothing to fix for {key}")
