"""Chart import (Billboard-style CSV).

The built-in "Most Played" playback charts that used to live in this module
(Chart.KIND_PLAYBACK, `playback_chart`/`playback_movers`/
`ensure_builtin_playback_charts`) moved to services/playlists.py on
2026-08-30 as Playlist.KIND_PLAYBACK - a most-played list is something you
queue up and play, which fits the Playlists domain's shape better than an
imported, dated, ranked chart. See playlists.py's "playback playlists"
section for the current home of that logic.

CSV import is deliberately forgiving about column names, because chart data
scraped from different places never agrees on headers:

    rank | position | pos | no             -> rank
    title | song | track                  -> title
    artist | performer | act              -> artist
    date | chart_date | week | weekending -> issue date
    last_week | lw | previous             -> last week's position
    peak | peak_pos | peak_rank           -> peak
    weeks | wks | weeks_on_board          -> weeks on chart

A file may hold many weeks (a date column) or a single week (pass `chart_date`).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    Chart,
    ChartEntry,
    ChartFolder,
    ChartIssue,
    Playlist,
    PlaylistItem,
    Track,
)
from .matching import best_match, normalize

log = logging.getLogger(__name__)

_COLUMN_ALIASES = {
    "rank": {"rank", "position", "pos", "no", "this_week", "thisweek", "tw", "current"},
    "title": {"title", "song", "track", "song_title", "track_title", "name"},
    "artist": {"artist", "performer", "act", "artists", "artist_name", "credit"},
    "date": {
        "date", "chart_date", "week", "week_of", "weekid", "issue_date",
        "chartweek", "year", "weekending", "week_ending",
    },
    "last_week": {"last_week", "lw", "previous", "prev", "last_week_position", "lastweek"},
    "peak": {"peak", "peak_pos", "peak_position", "peakpos", "peak_rank"},
    "weeks": {
        "weeks", "wks", "weeks_on_chart", "weeksonchart", "wks_on_chart",
        "weeks_on_board", "weeksonboard",
    },
}

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")


@dataclass
class ImportResult:
    chart_name: str = ""
    chart_id: Optional[int] = None  # added 2026-08-30, so the caller can
    # auto-expand this chart in the Charts table right after import
    issues: int = 0
    entries: int = 0
    matched: int = 0
    unmatched: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        pct = (100 * self.matched / self.entries) if self.entries else 0
        return (
            f"{self.chart_name}: {self.issues} issue(s), {self.entries} entries, "
            f"{self.matched} matched to library ({pct:.0f}%)"
        )


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s) or "chart"


def parse_date(value: str) -> Optional[dt.date]:
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d{4}", value):
        # a bare year, not a full date - the "year" alias exists for
        # Billboard-style Year-End charts (one row per year+rank rather than
        # per week+rank). Filed as December 31st of that year so each year
        # still gets its own distinct, correctly-ordered ChartIssue - see
        # test_year_end_charts_get_one_issue_per_year for the bug this fixes
        # (every row used to collapse into a single issue dated "today").
        year = int(value)
        return dt.date(year, 12, 31)
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _map_columns(fieldnames: Sequence[str]) -> dict[str, str]:
    """Map canonical field -> actual header present in the file."""
    mapping: dict[str, str] = {}
    lowered = {
        (name or "").strip().lower().replace(" ", "_").replace("-", "_"): name
        for name in fieldnames
    }
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                mapping[canonical] = lowered[alias]
                break
    return mapping


def _int_or_none(value) -> Optional[int]:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _read_csv_text(path: Path) -> str:
    """Billboard-style chart CSVs come from all over. Most are clean UTF-8,
    but some (Kaggle-style Windows exports, for one) carry stray
    Windows-1252 bytes - a curly quote, an accented letter in a title like
    "Mas" - that aren't valid UTF-8 at all. Try UTF-8 first, since it's the
    common case and rejects cleanly on anything that isn't; fall back to
    cp1252, which accepts any byte, rather than the previous plain
    `errors="replace"` silently turning a real character into a U+FFFD
    replacement mark. That matters here specifically because a mangled
    character in a title can be the difference between match_entry finding
    the track in the library and not."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# --------------------------------------------------------------------------
# matching chart entries to owned tracks
# --------------------------------------------------------------------------


def _candidate_tracks(session: Session, title_key: str) -> list[tuple[int, str, str]]:
    """Cheap prefilter: tracks sharing a leading token with the chart title."""
    first_word = title_key.split(" ")[0] if title_key else ""
    stmt = select(Track.id, Track.title, Track.artist_display)
    if len(first_word) >= 3:
        stmt = stmt.where(Track.title_key.like(f"%{first_word}%"))
    else:
        stmt = stmt.where(Track.title_key.like(f"{title_key}%"))
    return [(r[0], r[1], r[2] or "") for r in session.execute(stmt.limit(400))]


def match_entry(session: Session, entry: ChartEntry, threshold: float = 0.72) -> bool:
    """Attach a library track to a chart entry. Returns True if matched."""
    if entry.match_locked:
        return entry.track_id is not None
    candidates = _candidate_tracks(session, entry.title_key)
    hit = best_match(entry.title, entry.artist_name, candidates, threshold)
    if hit:
        entry.track_id, entry.match_score = hit[0], hit[1]
        return True
    entry.track_id, entry.match_score = None, None
    return False


def rematch_chart(session: Session, chart_id: int, threshold: float = 0.72) -> int:
    """Re-run matching for a whole chart, e.g. after adding music."""
    matched = 0
    stmt = (
        select(ChartEntry)
        .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
        .where(ChartIssue.chart_id == chart_id)
    )
    for entry in session.scalars(stmt):
        if match_entry(session, entry, threshold):
            matched += 1
    return matched


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------


def get_or_create_chart(
    session: Session,
    name: str,
    kind: str = Chart.KIND_EXTERNAL,
    source: Optional[str] = None,
    size: Optional[int] = None,
    folder_id: Optional[int] = None,
) -> Chart:
    slug = slugify(name)
    chart = session.scalar(select(Chart).where(Chart.slug == slug))
    if chart is None:
        chart = Chart(
            name=name, slug=slug, kind=kind, source=source, size=size, folder_id=folder_id
        )
        session.add(chart)
        session.flush()
    return chart


def import_chart_csv(
    session: Session,
    path: Path | str,
    chart_name: Optional[str] = None,
    chart_date: Optional[dt.date] = None,
    match: bool = True,
    threshold: float = 0.72,
    folder_id: Optional[int] = None,
) -> ImportResult:
    """Import one CSV file. Rows are grouped into issues by their date column.

    `folder_id` only matters the first time a chart with this name is seen -
    re-importing into an existing chart never moves it out of wherever it was
    filed.
    """
    path = Path(path)
    result = ImportResult()
    text = _read_csv_text(path)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.errors.append("empty or unreadable CSV")
        return result

    cols = _map_columns(reader.fieldnames)
    missing = [k for k in ("rank", "title", "artist") if k not in cols]
    if missing:
        result.errors.append(
            f"missing required column(s): {', '.join(missing)}. "
            f"Found headers: {', '.join(reader.fieldnames)}"
        )
        return result

    name = chart_name or path.stem.replace("_", " ").replace("-", " ").title()
    chart = get_or_create_chart(session, name, source=str(path), folder_id=folder_id)
    result.chart_name = chart.name
    result.chart_id = chart.id

    fallback_date = chart_date or parse_date(path.stem) or dt.date.today()
    issues: dict[dt.date, ChartIssue] = {}
    seen_ranks: dict[int, set[int]] = {}
    max_rank = 0

    for lineno, row in enumerate(reader, start=2):
        try:
            rank = _int_or_none(row.get(cols["rank"]))
            title = (row.get(cols["title"]) or "").strip()
            artist = (row.get(cols["artist"]) or "").strip()
            if not rank or not title:
                continue
            max_rank = max(max_rank, rank)
            row_date = (
                parse_date(row.get(cols["date"], "")) if "date" in cols else None
            ) or fallback_date

            issue = issues.get(row_date)
            if issue is None:
                issue = session.scalar(
                    select(ChartIssue).where(
                        ChartIssue.chart_id == chart.id,
                        ChartIssue.chart_date == row_date,
                    )
                )
                if issue is None:
                    issue = ChartIssue(chart_id=chart.id, chart_date=row_date)
                    session.add(issue)
                    session.flush()
                    result.issues += 1
                issues[row_date] = issue
                seen_ranks[issue.id] = {e.rank for e in issue.entries}

            if rank in seen_ranks[issue.id]:
                existing = session.scalar(
                    select(ChartEntry).where(
                        ChartEntry.issue_id == issue.id, ChartEntry.rank == rank
                    )
                )
                entry = existing or ChartEntry(issue_id=issue.id, rank=rank)
            else:
                entry = ChartEntry(issue_id=issue.id, rank=rank)
                seen_ranks[issue.id].add(rank)

            entry.title = title
            entry.artist_name = artist
            entry.title_key = normalize(title)
            entry.artist_key = normalize(artist)
            entry.last_week = _int_or_none(row.get(cols.get("last_week", ""), ""))
            entry.peak_pos = _int_or_none(row.get(cols.get("peak", ""), "")) or rank
            entry.weeks_on_chart = _int_or_none(row.get(cols.get("weeks", ""), ""))
            if entry.id is None:
                session.add(entry)
            session.flush()

            if match and match_entry(session, entry, threshold):
                result.matched += 1
            else:
                result.unmatched += 1
            result.entries += 1
        except Exception as exc:  # pragma: no cover
            result.errors.append(f"line {lineno}: {exc}")

    if max_rank and (chart.size is None or max_rank > chart.size):
        chart.size = max_rank
    session.flush()
    return result


# --------------------------------------------------------------------------
# reading charts back
# --------------------------------------------------------------------------


def list_charts(session: Session) -> list[Chart]:
    return list(session.scalars(select(Chart).order_by(Chart.kind, Chart.name)))


def remove_orphaned_playback_charts(session: Session) -> int:
    """One-time cleanup for a database that predates this feature's move to
    Playlists (2026-08-30): the built-in "Most Played" charts used to live
    here as Chart rows with kind "playback" (the constant they were seeded
    under, Chart.KIND_PLAYBACK, no longer exists on this model). Those rows
    never had real ChartIssue/ChartEntry data behind them - playback charts
    were computed on the fly, not stored - so an existing database is left
    with three empty, pointless entries in the Charts tree ("0 editions",
    nothing to show) once nothing in this module knows how to render them
    specially any more. Deletes any chart still carrying that old kind
    string; a no-op once run, so it's cheap to leave on every startup next
    to backfill_album_artists/merge_compilation_duplicates."""
    stale = list(session.scalars(select(Chart).where(Chart.kind == "playback")))
    for chart in stale:
        session.delete(chart)
    if stale:
        session.flush()
    return len(stale)


def rename_chart(session: Session, chart_id: int, name: str) -> None:
    chart = session.get(Chart, chart_id)
    if chart is not None and name.strip():
        chart.name = name.strip()


def move_chart(session: Session, chart_id: int, folder_id: Optional[int]) -> None:
    chart = session.get(Chart, chart_id)
    if chart is not None:
        chart.folder_id = folder_id


def delete_chart(session: Session, chart_id: int) -> None:
    """Deletes a chart and every issue/entry under it (cascade). Owned tracks
    are untouched - chart entries only ever point at tracks, never the other
    way round."""
    chart = session.get(Chart, chart_id)
    if chart is not None:
        session.delete(chart)
        session.flush()


def delete_issue(session: Session, issue_id: int) -> None:
    """Deletes one edition of a chart (and its entries, cascade) without
    touching the rest of the chart's history - the fix for a single
    mis-dated or mis-imported edition (see parse_date's bare-year handling
    for a case that used to produce exactly one of these) without having to
    delete and re-import the whole chart. Owned tracks are untouched, same
    as delete_chart."""
    issue = session.get(ChartIssue, issue_id)
    if issue is not None:
        session.delete(issue)
        session.flush()


# --------------------------------------------------------------------------
# chart folders
# --------------------------------------------------------------------------


def create_folder(
    session: Session, name: str, parent_id: Optional[int] = None
) -> ChartFolder:
    folder = ChartFolder(name=name.strip() or "New folder", parent_id=parent_id)
    session.add(folder)
    session.flush()
    return folder


def rename_folder(session: Session, folder_id: int, name: str) -> None:
    folder = session.get(ChartFolder, folder_id)
    if folder is not None and name.strip():
        folder.name = name.strip()


def list_folders(session: Session) -> list[ChartFolder]:
    return list(
        session.scalars(select(ChartFolder).order_by(func.lower(ChartFolder.name)))
    )


def folder_and_descendant_ids(session: Session, folder_id: int) -> set[int]:
    """`folder_id` plus every folder nested under it, direct or not - used to
    keep a folder from being moved inside its own subtree."""
    by_parent: dict[Optional[int], list[int]] = {}
    for fid, parent_id in session.execute(
        select(ChartFolder.id, ChartFolder.parent_id)
    ):
        by_parent.setdefault(parent_id, []).append(fid)
    ids = {folder_id}
    stack = [folder_id]
    while stack:
        for child_id in by_parent.get(stack.pop(), []):
            if child_id not in ids:
                ids.add(child_id)
                stack.append(child_id)
    return ids


def move_folder(session: Session, folder_id: int, new_parent_id: Optional[int]) -> None:
    if new_parent_id is not None and new_parent_id in folder_and_descendant_ids(
        session, folder_id
    ):
        raise ValueError(
            "a folder can't be moved inside itself or one of its own subfolders"
        )
    folder = session.get(ChartFolder, folder_id)
    if folder is not None:
        folder.parent_id = new_parent_id


def delete_folder(session: Session, folder_id: int) -> None:
    """Deletes just the folder. Its charts and subfolders move up to its own
    parent (or the top level) rather than being deleted with it - a folder is
    organisation, never a container things should die with."""
    folder = session.get(ChartFolder, folder_id)
    if folder is None:
        return
    parent_id = folder.parent_id
    for child in session.scalars(
        select(ChartFolder).where(ChartFolder.parent_id == folder_id)
    ):
        child.parent_id = parent_id
    for chart in session.scalars(select(Chart).where(Chart.folder_id == folder_id)):
        chart.folder_id = parent_id
    # flush the reparenting before deleting the folder - otherwise SQLAlchemy's
    # own relationship bookkeeping can re-query its (now stale) children and
    # null their FK out from under the reassignment above (see the identical
    # note on services/playlists.py:delete_folder, where this was first hit)
    session.flush()
    session.delete(folder)
    session.flush()


def folder_counts(session: Session, folder_id: int) -> tuple[int, int]:
    """(direct charts, direct subfolders) - not recursive; backs the one-line
    summary shown when a folder itself is selected."""
    charts = (
        session.scalar(select(func.count(Chart.id)).where(Chart.folder_id == folder_id))
        or 0
    )
    subfolders = (
        session.scalar(
            select(func.count(ChartFolder.id)).where(ChartFolder.parent_id == folder_id)
        )
        or 0
    )
    return charts, subfolders


def list_issues(session: Session, chart_id: int) -> list[ChartIssue]:
    return list(
        session.scalars(
            select(ChartIssue)
            .where(ChartIssue.chart_id == chart_id)
            .order_by(ChartIssue.chart_date.desc())
        )
    )


def issue_entries(session: Session, issue_id: int) -> list[ChartEntry]:
    return list(
        session.scalars(
            select(ChartEntry)
            .options(selectinload(ChartEntry.track).selectinload(Track.files))
            .where(ChartEntry.issue_id == issue_id)
            .order_by(ChartEntry.rank)
        ).unique()
    )


def chart_shape(chart: Chart) -> str:
    """"Year End" for a chart whose editions are one-per-calendar-year (the
    shape a bare-year import produces - see parse_date's bare-year handling),
    "Weekly" for anything with more than one edition in some year (an
    ordinary dated CSV), "—" for a chart with no editions imported yet.
    Purely a display label for the Charts table (added 2026-08-30) - it
    drives no matching logic of its own; match_by_comment_tag.py's own
    year-to-issue lookup already assumes one issue per year regardless of
    what this returns. Written without the hyphen ("Year End", not
    "Year-End") since 2026-08-30 - James named it that way asking for the
    Type column to stop truncating it, and the two spellings measure to the
    same pixel width in the app's font, so there was no reason to keep the
    hyphen once the truncation itself was fixed (see ChartTable.COL_TYPE)."""
    issues = chart.issues
    if not issues:
        return "—"
    years = [i.chart_date.year for i in issues]
    if len(issues) == len(set(years)):
        return "Year End"
    return "Weekly"


def chart_coverage(session: Session, chart_id: int) -> tuple[int, int]:
    """(owned, total) aggregated across every edition of a chart - the
    all-editions counterpart to issue_coverage's single-edition figure.
    Backs the Charts table's Coverage column (added 2026-08-30)."""
    total = (
        session.scalar(
            select(func.count(ChartEntry.id))
            .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
            .where(ChartIssue.chart_id == chart_id)
        )
        or 0
    )
    owned = (
        session.scalar(
            select(func.count(ChartEntry.id))
            .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
            .where(ChartIssue.chart_id == chart_id, ChartEntry.track_id.is_not(None))
        )
        or 0
    )
    return owned, total


def issue_coverage(session: Session, issue_id: int) -> tuple[int, int]:
    """(owned, total) - how much of this chart week is in the library."""
    total = (
        session.scalar(
            select(func.count(ChartEntry.id)).where(ChartEntry.issue_id == issue_id)
        )
        or 0
    )
    owned = (
        session.scalar(
            select(func.count(ChartEntry.id)).where(
                ChartEntry.issue_id == issue_id, ChartEntry.track_id.is_not(None)
            )
        )
        or 0
    )
    return owned, total


def chart_edition_count(session: Session, chart_id: int) -> int:
    """A cheap COUNT of a chart's editions - added 2026-08-30 alongside the
    Charts table nesting editions as tree children (see chart_table.py):
    used only to decide whether a chart gets an expand chevron, so it's
    worth a real COUNT query rather than `len(chart.issues)`, which would
    load every ChartIssue row just to measure the collection - James's real
    Billboard Hot 100 Weekly chart alone has 3,393 of them."""
    return (
        session.scalar(
            select(func.count(ChartIssue.id)).where(ChartIssue.chart_id == chart_id)
        )
        or 0
    )


def issue_coverage_by_chart(session: Session, chart_id: int) -> dict[int, tuple[int, int]]:
    """(owned, total) per issue_id, for every issue under one chart, each in
    a single grouped query - the batch counterpart to issue_coverage's
    one-issue-at-a-time version. Added 2026-08-30 so expanding a chart in
    the Charts table (which nests editions as tree children, populated only
    on demand - see chart_table.py's ChartTable `issues_provider`) costs two
    queries regardless of how many editions that chart has, rather than one
    issue_coverage call per edition: James's real Billboard Hot 100 Weekly
    chart has 3,393 of them, so the per-issue version was never viable here."""
    totals: dict[int, int] = {
        issue_id: total
        for issue_id, total in session.execute(
            select(ChartEntry.issue_id, func.count(ChartEntry.id))
            .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
            .where(ChartIssue.chart_id == chart_id)
            .group_by(ChartEntry.issue_id)
        )
    }
    owned: dict[int, int] = {
        issue_id: count
        for issue_id, count in session.execute(
            select(ChartEntry.issue_id, func.count(ChartEntry.id))
            .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
            .where(ChartIssue.chart_id == chart_id, ChartEntry.track_id.is_not(None))
            .group_by(ChartEntry.issue_id)
        )
    }
    return {issue_id: (owned.get(issue_id, 0), total) for issue_id, total in totals.items()}


def chart_run(session: Session, chart_id: int, track_id: int) -> list[ChartEntry]:
    """Every week a given track appeared on a chart, oldest first."""
    stmt = (
        select(ChartEntry)
        .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
        .where(ChartIssue.chart_id == chart_id, ChartEntry.track_id == track_id)
        .order_by(ChartIssue.chart_date)
    )
    return list(session.scalars(stmt))


def snapshot_to_playlist(
    session: Session, issue_id: int, owned_only: bool = True
) -> Playlist:
    """Freeze a chart week as a real playlist you can queue."""
    issue = session.get(ChartIssue, issue_id)
    if issue is None:
        raise ValueError(f"no chart issue {issue_id}")
    chart = session.get(Chart, issue.chart_id)
    name = f"{chart.name} - {issue.chart_date.isoformat()}"
    playlist = session.scalar(
        select(Playlist).where(
            Playlist.name == name, Playlist.kind == Playlist.KIND_CHART
        )
    )
    if playlist is None:
        playlist = Playlist(name=name, kind=Playlist.KIND_CHART, source_issue_id=issue.id)
        session.add(playlist)
        session.flush()
    else:
        for item in list(playlist.items):
            session.delete(item)
        session.flush()

    pos = 0
    for entry in issue_entries(session, issue_id):
        if entry.track_id is None:
            if owned_only:
                continue
            continue
        session.add(
            PlaylistItem(playlist_id=playlist.id, track_id=entry.track_id, position=pos)
        )
        pos += 1
    playlist.description = f"Positions 1-{pos} from {chart.name}, {issue.chart_date}"
    session.flush()
    return playlist
