"""Chart import (Billboard-style CSV).

The built-in "Most Played" playback charts live in services/playlists.py
as Playlist.KIND_PLAYBACK (see its "playback playlists" section) - a
most-played list is something you queue and play, not an imported, dated,
ranked chart.

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
from typing import Callable, Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    Chart,
    ChartEntry,
    ChartFolder,
    ChartIssue,
    Playlist,
    PlaylistItem,
    Setting,
    Track,
)
from ..db.session import session_scope
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
    chart_id: Optional[int] = None  # so the caller can
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
        # Billboard-style Year-End charts (one row per year+rank). Filed as
        # December 31st so each year gets its own correctly-ordered
        # ChartIssue (see test_year_end_charts_get_one_issue_per_year).
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
    Windows-1252 bytes that aren't valid UTF-8. Try UTF-8 first, since it
    rejects cleanly on anything that isn't; fall back to cp1252, which
    accepts any byte, rather than `errors="replace"` turning a real
    character into U+FFFD - a mangled title character can stop match_entry
    finding the track."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# --------------------------------------------------------------------------
# matching chart entries to owned tracks
# --------------------------------------------------------------------------


def _candidate_tracks(session: Session, title_key: str) -> list[tuple[int, str, str]]:
    """Cheap prefilter: tracks with exactly this title, then tracks sharing
    a leading token with it.

    The exact-title tracks always come first: the token search is capped
    at 400 rows, and a common first word ("baby" is in over a thousand
    titles in a real library) could crowd out the very record the chart
    entry is about."""
    cols = select(Track.id, Track.title, Track.artist_display)
    rows: dict[int, tuple[int, str, str]] = {}
    if title_key:
        for r in session.execute(cols.where(Track.title_key == title_key).limit(400)):
            rows[r[0]] = (r[0], r[1], r[2] or "")
    first_word = title_key.split(" ")[0] if title_key else ""
    if len(first_word) >= 3:
        stmt = cols.where(Track.title_key.like(f"%{first_word}%"))
    else:
        stmt = cols.where(Track.title_key.like(f"{title_key}%"))
    for r in session.execute(stmt.limit(400)):
        rows.setdefault(r[0], (r[0], r[1], r[2] or ""))
    return list(rows.values())


def match_entry(session: Session, entry: ChartEntry, threshold: float = 0.72) -> bool:
    """Attach a library track to a chart entry. Returns True if matched."""
    if entry.match_locked:
        return entry.track_id is not None
    candidates = _candidate_tracks(session, entry.title_key)
    hit = best_match(entry.title, entry.artist_name, candidates, threshold)
    if hit:
        # playlists and charts point to the album copy of a song rather
        # than a chart-folder copy (Billboard/Hot Country/Oldies) when the
        # library has both (services/duplicates.py)
        from .duplicates import album_copy_for

        entry.track_id, entry.match_score = album_copy_for(session, hit[0]) or hit[0], hit[1]
        return True
    entry.track_id, entry.match_score = None, None
    return False


def rematch_chart(
    session: Session,
    chart_id: int,
    threshold: float = 0.72,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Re-run matching for a whole chart, e.g. after adding music.

    `progress` is called `(done, total, "")` per entry - fuzzy matching
    (`matching.best_match`) is real CPU work per entry, so a chart with
    thousands of entries reports progress rather than leaving Settings
    looking frozen.

    `should_stop`, checked once per entry, `break`s rather than raising.
    Nothing here commits (rematch_all_charts' one session_scope commits at
    the end), so a break just means fewer entries get re-matched this run -
    there's nothing to roll back."""
    stmt = (
        select(ChartEntry)
        .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
        .where(ChartIssue.chart_id == chart_id)
    )
    return _rematch_entries(session, session.scalars(stmt).all(), threshold,
                            progress, should_stop)


def rematch_issue(session: Session, issue_id: int, threshold: float = 0.72) -> tuple[int, int]:
    """Re-match just one edition's rows - what the Charts page's "Re-match
    library" button does (re-matching a whole weekly chart's ~355,000 rows
    on the UI thread took far too long). Returns (matched, rows)."""
    entries = session.scalars(select(ChartEntry).where(ChartEntry.issue_id == issue_id)).all()
    return _rematch_entries(session, entries, threshold), len(entries)


def _rematch_entries(
    session: Session,
    entries: Sequence[ChartEntry],
    threshold: float = 0.72,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Match each row, but each distinct title + artist only once: a weekly
    chart repeats the same song for weeks - the Hot 100 has ~30,000
    distinct songs across ~355,000 rows - so this does roughly a tenth of
    the fuzzy lookups."""
    matched = 0
    seen: dict[tuple[str, str], tuple[Optional[int], Optional[float]]] = {}
    changed: dict[int, list[tuple[ChartEntry, Optional[int]]]] = {}
    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        if should_stop and should_stop():
            break
        before = entry.track_id
        key = (entry.title_key, entry.artist_key)
        if entry.match_locked:
            hit = entry.track_id is not None
        elif key in seen:
            entry.track_id, entry.match_score = seen[key]
            hit = entry.track_id is not None
        else:
            hit = match_entry(session, entry, threshold)
            seen[key] = (entry.track_id, entry.match_score)
        if entry.track_id != before:
            changed.setdefault(entry.issue_id, []).append((entry, before))
        if hit:
            matched += 1
        if progress and (i % 50 == 0 or i == total):
            progress(i, total, "")
    if changed:
        # editions already saved as playlists pick up the new matches
        session.flush()
        for issue_id, changes in changed.items():
            sync_edition_playlist(session, issue_id, changes)
    return matched


def rematch_all_charts(
    threshold: float = 0.72,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Settings' bulk "Re-match" button, run on a background thread. Opens
    its own session, same as every other bulk action's service-level entry
    point (download_bios_for_artists/update_popularity_for_artists/
    search_artwork_for_releases).

    `progress` gets one `(0, 0, "<chart name>")` tick per chart (mirroring
    metadata_health.scan_missing_metadata's phase-announcement shape),
    then rematch_chart's own per-entry ticks for that chart.

    `should_stop` is threaded into every rematch_chart call (so a stop
    lands within a few dozen entries even mid-chart) and re-checked here
    between charts, so a chart not yet started when Cancel is pressed is
    never begun."""
    with session_scope() as session:
        chart_ids_and_names = [
            (chart.id, chart.name)
            for chart in session.scalars(
                select(Chart).where(Chart.kind == Chart.KIND_EXTERNAL)
            )
        ]
        total = 0
        for chart_id, name in chart_ids_and_names:
            if should_stop and should_stop():
                break
            if progress:
                progress(0, 0, f"Re-matching {name}…")
            total += rematch_chart(
                session, chart_id, threshold, progress=progress, should_stop=should_stop
            )
        return total


def search_tracks_for_match(
    session: Session, artist_query: str = "", track_query: str = "", limit: int = 50
) -> list[dict]:
    """Search by artist and track for the "Fix match..." dialog
    (ui/views/charts.py:MatchTrackDialog) - manually picking which library
    track a chart entry matches. Same two-box ANDed search as
    `services/jukebox.py:search_addable_tracks` (either box can be blank,
    both blank returns nothing), but a plain library search: no
    album-artist resolution and no exclusions, since a chart legitimately
    includes compilation tracks and a track can be matched elsewhere too.

    Returns plain dicts (`track_id`, `title`, `artist_name`, `album`), not
    ORM rows, since the session may close before the dialog does."""
    artist_query = artist_query.strip()
    track_query = track_query.strip()
    if not artist_query and not track_query:
        return []
    stmt = (
        select(Track)
        .options(selectinload(Track.release))
        .order_by(Track.title)
        .limit(limit)
    )
    if artist_query:
        stmt = stmt.where(Track.artist_display.ilike(f"%{artist_query}%"))
    if track_query:
        stmt = stmt.where(Track.title_key.like(f"%{normalize(track_query)}%"))
    return [
        {
            "track_id": track.id,
            "title": track.title,
            "artist_name": track.artist_display or "",
            "album": track.release.title if track.release else "",
        }
        for track in session.scalars(stmt).unique()
    ]


def set_entry_track(session: Session, entry_id: int, track_id: Optional[int]) -> ChartEntry:
    """Manually assign (or clear) which library track a chart entry
    matches - the "Fix match..." dialog's write side. Assigning a track
    locks the match (`match_locked = True`, `match_score = 1.0`) so a later
    "Re-match library" never overwrites a manual pick (`match_entry` skips
    locked entries). Clearing it (`track_id=None`) unlocks the entry - "not
    this one", not "never try again" - so automatic re-matching considers
    it again.

    If this edition was already saved as a playlist ("Save as playlist"),
    that playlist gets the same fix - see sync_edition_playlist."""
    entry = session.get(ChartEntry, entry_id)
    if entry is None:
        raise ValueError(f"no chart entry {entry_id}")
    old_track_id = entry.track_id
    entry.track_id = track_id
    entry.match_score = 1.0 if track_id is not None else None
    entry.match_locked = track_id is not None
    session.flush()
    # a playlist already saved from this edition gets the same fix
    sync_edition_playlist(session, entry.issue_id, [(entry, old_track_id)])
    return entry


# --------------------------------------------------------------------------
# editing positions by hand
# --------------------------------------------------------------------------
#
# Imported CSVs are sometimes short a row or carry a typo; these let the
# Charts page fix one edition in place instead of editing the CSV and
# re-importing it.


class RankTaken(ValueError):
    """The position asked for already holds another song. `holder` is that
    song's "Title — Artist", for the question the Charts page then asks."""

    def __init__(self, rank: int, holder: str) -> None:
        super().__init__(f"#{rank} is already {holder}")
        self.rank = rank
        self.holder = holder


def _issue_rows(session: Session, issue_id: int) -> list[ChartEntry]:
    return list(session.scalars(
        select(ChartEntry).where(ChartEntry.issue_id == issue_id).order_by(ChartEntry.rank)
    ))


def _renumber(session: Session, moves: dict[ChartEntry, int]) -> None:
    """Give several rows new ranks without tripping the (issue, rank)
    unique constraint mid-way: park them all on negative ranks first."""
    if not moves:
        return
    for entry in moves:
        entry.rank = -entry.id
    session.flush()
    for entry, rank in moves.items():
        entry.rank = rank
    session.flush()


def _after_edit(session: Session, issue_id: int, rank: Optional[int] = None) -> None:
    issue = session.get(ChartIssue, issue_id)
    if issue is None:
        return
    chart = session.get(Chart, issue.chart_id)
    if chart is not None and rank and (chart.size is None or rank > chart.size):
        chart.size = rank
    session.flush()
    from . import chart_sync  # chart_sync imports this module

    chart_sync.mark_edited(session, issue.chart_id)


def suggested_rank(session: Session, issue_id: int) -> int:
    """The first missing position in an edition (#40 when 1-39 and 41-100
    are there), or one past the last when nothing is missing."""
    ranks = set(session.scalars(select(ChartEntry.rank).where(ChartEntry.issue_id == issue_id)))
    rank = 1
    while rank in ranks:
        rank += 1
    return rank


def add_entry(
    session: Session,
    issue_id: int,
    rank: int,
    title: str,
    artist: str,
    last_week: Optional[int] = None,
    peak_pos: Optional[int] = None,
    weeks_on_chart: Optional[int] = None,
    shift: bool = False,
    threshold: float = 0.72,
) -> ChartEntry:
    """Add a song at `rank` and match it to the library. If that position
    is taken, raises RankTaken - unless `shift`, which moves that song and
    every one below it down a place. A missing peak defaults to the rank,
    same as an import."""
    title, artist = title.strip(), artist.strip()
    if rank < 1 or not title:
        raise ValueError("a position needs a rank of 1 or more and a title")
    if session.get(ChartIssue, issue_id) is None:
        raise ValueError(f"no chart edition {issue_id}")
    rows = _issue_rows(session, issue_id)
    holder = next((e for e in rows if e.rank == rank), None)
    if holder is not None:
        if not shift:
            raise RankTaken(rank, f"{holder.title} — {holder.artist_name}")
        _renumber(session, {e: e.rank + 1 for e in rows if e.rank >= rank})
    entry = ChartEntry(
        issue_id=issue_id, rank=rank, title=title, artist_name=artist,
        title_key=normalize(title), artist_key=normalize(artist),
        last_week=last_week, peak_pos=peak_pos or rank, weeks_on_chart=weeks_on_chart,
    )
    session.add(entry)
    session.flush()
    match_entry(session, entry, threshold)
    _after_edit(session, issue_id, max([rank] + [e.rank for e in rows]))
    return entry


def update_entry(
    session: Session,
    entry_id: int,
    rank: int,
    title: str,
    artist: str,
    last_week: Optional[int] = None,
    peak_pos: Optional[int] = None,
    weeks_on_chart: Optional[int] = None,
    shift: bool = False,
    threshold: float = 0.72,
) -> ChartEntry:
    """Change one position. Moving it onto a position another song holds
    raises RankTaken unless `shift`, which slides the songs in between up
    or down a place (like dragging a row in a list). A changed title or
    artist is re-matched, unless the match was fixed by hand."""
    entry = session.get(ChartEntry, entry_id)
    if entry is None:
        raise ValueError(f"no chart entry {entry_id}")
    title, artist = title.strip(), artist.strip()
    if rank < 1 or not title:
        raise ValueError("a position needs a rank of 1 or more and a title")
    if rank != entry.rank:
        rows = [e for e in _issue_rows(session, entry.issue_id) if e.id != entry.id]
        holder = next((e for e in rows if e.rank == rank), None)
        if holder is not None and not shift:
            raise RankTaken(rank, f"{holder.title} — {holder.artist_name}")
        moves: dict[ChartEntry, int] = {entry: rank}
        if holder is not None:
            old = entry.rank
            if rank < old:      # moving up: rank..old-1 slide down one
                moves.update({e: e.rank + 1 for e in rows if rank <= e.rank < old})
            else:               # moving down: old+1..rank slide up one
                moves.update({e: e.rank - 1 for e in rows if old < e.rank <= rank})
        _renumber(session, moves)
    renamed = (title, artist) != (entry.title, entry.artist_name)
    old_track_id = entry.track_id
    entry.title, entry.artist_name = title, artist
    entry.title_key, entry.artist_key = normalize(title), normalize(artist)
    entry.last_week, entry.peak_pos, entry.weeks_on_chart = last_week, peak_pos, weeks_on_chart
    session.flush()
    if renamed and not entry.match_locked:
        match_entry(session, entry, threshold)
        session.flush()
        if entry.track_id != old_track_id:
            sync_edition_playlist(session, entry.issue_id, [(entry, old_track_id)])
    _after_edit(session, entry.issue_id, rank)
    return entry


def delete_entry(session: Session, entry_id: int) -> None:
    """Remove one position. The ranks below keep their numbers - a gap is
    honest about a chart's missing row, and "Add song…" fills it back in.
    The matched track itself is untouched."""
    entry = session.get(ChartEntry, entry_id)
    if entry is None:
        return
    issue_id = entry.issue_id
    session.delete(entry)
    session.flush()
    _after_edit(session, issue_id)


#: Setting.key row this module owns (the generic key/value table - see
#: db.models.Setting; this app has no config file outside the library
#: database). Remembers where "Browse for file..." in MatchTrackDialog last
#: found something, so the next unmatched entry starts in the same folder.
LAST_BROWSE_DIR_KEY = "charts_last_browse_dir"


def get_last_browse_dir(session: Session) -> Optional[str]:
    row = session.get(Setting, LAST_BROWSE_DIR_KEY)
    return row.value if row else None


def set_last_browse_dir(session: Session, directory: str) -> None:
    row = session.get(Setting, LAST_BROWSE_DIR_KEY)
    if row is None:
        row = Setting(key=LAST_BROWSE_DIR_KEY, value=directory)
        session.add(row)
    else:
        row.value = directory


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


def create_chart(session: Session, name: str, folder_id: Optional[int] = None) -> Chart:
    """A brand-new chart, always - never an existing one that happens to
    share the name's slug (e.g. weekly "Billboard Hot 100" rows must not
    land in a Year End chart slugged `billboard-hot-100`). The slug gets
    -2, -3... when taken."""
    base = slugify(name)[:110] or "chart"
    slug, n = base, 2
    while session.scalar(select(Chart.id).where(Chart.slug == slug)) is not None:
        slug = f"{base}-{n}"
        n += 1
    chart = Chart(name=name.strip() or "Chart", slug=slug, folder_id=folder_id)
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
    chart_id: Optional[int] = None,
) -> ImportResult:
    """Import one CSV file. Rows are grouped into issues by their date column.

    `chart_id` adds the rows to that exact chart - what the Charts page's
    "Import chart CSV…" does with the selected chart - instead of finding a
    chart by `chart_name`'s slug.

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

    chart = session.get(Chart, chart_id) if chart_id is not None else None
    if chart_id is not None and chart is None:
        result.errors.append(f"chart {chart_id} no longer exists")
        return result
    if chart is None:
        name = chart_name or path.stem.replace("_", " ").replace("-", " ").title()
        chart = get_or_create_chart(session, name, source=str(path), folder_id=folder_id)
    elif chart.source is None:
        chart.source = str(path)
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
    """One-time cleanup for databases from before the "Most Played" charts
    moved to Playlists: deletes any Chart row still carrying the old
    "playback" kind. Those rows never had ChartIssue/ChartEntry data behind
    them (they were computed on the fly), so they'd show as empty entries
    in the Charts tree. A no-op once run, so it's cheap to leave on every
    startup next to backfill_album_artists/merge_compilation_duplicates."""
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
    touching the rest of the chart's history - for a single mis-dated or
    mis-imported edition. Owned tracks are untouched, same as
    delete_chart."""
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


#: how a chart's editions are dated - see edition_style
EDITION_YEAR, EDITION_DECADE, EDITION_DATE = "year", "decade", "date"


def edition_style(dates: Iterable[dt.date]) -> str:
    """How to name a chart's editions, from all their dates: a Year End
    chart files each year as 31 December (parse_date's bare-year rule), so
    "2025" says it better than "31 Dec 2025"; the One-Hit Wonders lists are
    one edition per decade dated 1 January 1980, 1990..., so "1980s".
    Anything else - a weekly chart - keeps its full date. 2026-10-25."""
    dates = list(dates)
    if not dates:
        return EDITION_DATE
    if all((d.month, d.day) == (12, 31) for d in dates):
        return EDITION_YEAR
    if all((d.month, d.day) == (1, 1) and d.year % 10 == 0 for d in dates):
        return EDITION_DECADE
    return EDITION_DATE


def edition_label(chart_date: dt.date, style: str) -> str:
    if style == EDITION_YEAR:
        return str(chart_date.year)
    if style == EDITION_DECADE:
        return f"{chart_date.year}s"
    return chart_date.strftime("%d %b %Y")


def chart_edition_style(session: Session, chart_id: int) -> str:
    return edition_style(session.scalars(
        select(ChartIssue.chart_date).where(ChartIssue.chart_id == chart_id)))


def issue_label(session: Session, issue: ChartIssue) -> str:
    """One edition's name as the Charts page shows it ("2025", "1980s" or
    "07 Oct 2026")."""
    return edition_label(issue.chart_date, chart_edition_style(session, issue.chart_id))


def chart_playlist_name(session: Session, issue: ChartIssue, chart: Chart) -> str:
    """Default name for an edition saved as a playlist: "Year End - 2025"."""
    return f"{chart.name} - {issue_label(session, issue)}"


def find_edition_playlist(session: Session, issue: ChartIssue, chart: Chart) -> Optional[Playlist]:
    """The playlist already saved from this edition, if any: by its link to
    the edition first - it may have been renamed ("2025" in a decade folder)
    - then by either default name (the full ISO date one used before
    2026-10-25, or chart_playlist_name)."""
    found = session.scalar(
        select(Playlist).where(Playlist.source_issue_id == issue.id,
                               Playlist.kind == Playlist.KIND_CHART)
        .order_by(Playlist.id).limit(1))
    if found is not None:
        return found
    names = {f"{chart.name} - {issue.chart_date.isoformat()}",
             chart_playlist_name(session, issue, chart)}
    return session.scalar(
        select(Playlist).where(Playlist.name.in_(names), Playlist.kind == Playlist.KIND_CHART)
        .order_by(Playlist.id).limit(1))


def sync_edition_playlist(
    session: Session,
    issue_id: int,
    changes: Sequence[tuple[ChartEntry, Optional[int]]],
) -> Optional[Playlist]:
    """Carry match changes in one edition over to the playlist already saved
    from it ("Save as playlist"), if there is one - so "Fix match…" and
    "Re-match library" don't leave that playlist playing the old, wrong
    song. Returns the playlist touched, or None when the edition was never
    saved as one (nothing is created here).

    `changes` is (entry, track_id before the change) for each entry whose
    match changed; each entry already holds its new track_id. Only those
    songs are touched - the playlist keeps its name, folder, tile and any
    songs added, removed or reordered by hand:

    * wrong song -> right song: swapped in place, same position
    * unmatched -> matched: inserted where its chart rank puts it
    * match cleared: that song is taken out

    The playlist row to change is found at the position the chart rank puts
    it first (so a song on the chart twice swaps the right copy), else by
    the old track anywhere in the playlist (it was reordered by hand)."""
    changes = [(e, old) for e, old in changes if e.track_id != old]
    if not changes:
        return None
    issue = session.get(ChartIssue, issue_id)
    chart = session.get(Chart, issue.chart_id) if issue is not None else None
    if issue is None or chart is None:
        return None
    playlist = find_edition_playlist(session, issue, chart)
    if playlist is None:
        return None

    items = list(
        session.scalars(
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist.id)
            .order_by(PlaylistItem.position, PlaylistItem.id)
        )
    )
    # every entry's match as it is now (the changes are already on the rows)
    ranks_tracks = list(
        session.execute(
            select(ChartEntry.rank, ChartEntry.track_id).where(ChartEntry.issue_id == issue_id)
        )
    )
    touched = False
    # top of the chart first, so the songs above each change are already in
    # their final state when its position is worked out
    for entry, old in sorted(changes, key=lambda c: c[0].rank):
        new = entry.track_id
        # where "Save as playlist" puts this song: one slot per matched
        # position above it
        expected = sum(1 for rank, tid in ranks_tracks
                       if rank < entry.rank and tid is not None)
        current = [item.track_id for item in items]
        idx = None
        if old is not None:
            if expected < len(items) and current[expected] == old:
                idx = expected
            elif old in current:
                idx = current.index(old)
        if idx is not None:
            if new is None or new in current:
                # cleared, or the right song is already in there (added by
                # hand) - just take the wrong one out
                session.delete(items.pop(idx))
            else:
                items[idx].track_id = new
            touched = True
        elif new is not None and new not in current:
            item = PlaylistItem(playlist_id=playlist.id, track_id=new)
            session.add(item)
            items.insert(min(expected, len(items)), item)
            touched = True

    if not touched:
        return None
    for pos, item in enumerate(items):
        item.position = pos
    # USB sync's playlist merge goes by updated_at when both PCs changed one
    playlist.updated_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    session.expire(playlist, ["items"])
    return playlist


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
            .options(
                selectinload(ChartEntry.track).selectinload(Track.files),
                # also load each track's release while the session is
                # open: ChartsView keeps matched tracks past its session
                # block (for "Play owned"/"Queue"/"Save as playlist"), and
                # `QueueItem.from_track` reads `track.release` -
                # otherwise DetachedInstanceError.
                selectinload(ChartEntry.track).selectinload(Track.release),
            )
            .where(ChartEntry.issue_id == issue_id)
            .order_by(ChartEntry.rank)
        ).unique()
    )


def chart_shape(chart: Chart) -> str:
    """"Year End" for a chart whose editions are one-per-calendar-year (the
    shape a bare-year import produces - see parse_date), "Weekly" for
    anything with more than one edition in some year, "—" for a chart with
    no editions yet. Purely a display label for the Charts table's Type
    column (see ChartTable.COL_TYPE); it drives no matching logic -
    match_by_comment_tag.py's year-to-issue lookup assumes one issue per
    year regardless."""
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
    Backs the Charts table's Coverage column."""
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
    """A cheap COUNT of a chart's editions, used only to decide whether a
    chart gets an expand chevron in the Charts table (see chart_table.py).
    A real COUNT query rather than `len(chart.issues)`, which would load
    every ChartIssue row - a Hot 100 Weekly chart has thousands."""
    return (
        session.scalar(
            select(func.count(ChartIssue.id)).where(ChartIssue.chart_id == chart_id)
        )
        or 0
    )


def issue_coverage_by_chart(session: Session, chart_id: int) -> dict[int, tuple[int, int]]:
    """(owned, total) per issue_id, for every issue under one chart, each in
    a single grouped query - the batch counterpart to issue_coverage. So
    expanding a chart in the Charts table (editions populated on demand -
    see chart_table.py's ChartTable `issues_provider`) costs two queries
    regardless of how many editions it has (thousands, for a Hot 100
    Weekly chart)."""
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


#: Sentinel for snapshot_to_playlist's folder_id: "don't touch the folder"
#: (the CLI's behaviour) as distinct from None, which means the top level.
_KEEP_FOLDER = object()


def snapshot_to_playlist(
    session: Session,
    issue_id: int,
    owned_only: bool = True,
    folder_id=_KEEP_FOLDER,
) -> Playlist:
    """Freeze a chart week as a real playlist you can queue.

    `folder_id` files the playlist in that playlist folder (None = top
    level) - the Charts page's "Save as playlist" asks where to put it.
    Left out, a new playlist lands at the top level and an existing one
    stays where it is."""
    issue = session.get(ChartIssue, issue_id)
    if issue is None:
        raise ValueError(f"no chart issue {issue_id}")
    chart = session.get(Chart, issue.chart_id)
    # an edition saved before is updated in place - keeping its name,
    # folder and tile even if renamed - rather than saved a second time
    playlist = find_edition_playlist(session, issue, chart)
    if playlist is None:
        playlist = Playlist(name=chart_playlist_name(session, issue, chart),
                            kind=Playlist.KIND_CHART, source_issue_id=issue.id)
        session.add(playlist)
        session.flush()
    else:
        playlist.source_issue_id = issue.id
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
    # new songs alone don't touch the playlist row; USB sync's playlist
    # merge goes by updated_at when both PCs changed one
    playlist.updated_at = dt.datetime.now(dt.timezone.utc)
    if folder_id is not _KEEP_FOLDER:
        playlist.folder_id = folder_id
    session.flush()
    return playlist
