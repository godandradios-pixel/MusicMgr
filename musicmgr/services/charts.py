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
    """Cheap prefilter: tracks with exactly this title, then tracks sharing
    a leading token with it.

    2026-10-01 - the exact-title tracks come first and always: the token
    search is capped at 400 rows, and a common first word ("baby" is in
    over a thousand of James's titles) could crowd out the very record the
    chart entry is about."""
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
        entry.track_id, entry.match_score = hit[0], hit[1]
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

    `progress` (2026-09-22, part of rematch_all_charts below) is called
    `(done, total, "")` per entry - fuzzy title/artist matching
    (`matching.best_match`) against every candidate track is real CPU work
    per entry, not a cheap lookup, so a chart with a few thousand entries
    is worth reporting progress through rather than leaving Settings
    looking frozen for however long that takes.

    2026-09-22 same-day follow-up (James: "add a real cancel button that
    stops any process running within Settings") - `should_stop`, checked
    once per entry, `break`s rather than raising. Nothing here commits at
    all (the whole multi-chart run shares rematch_all_charts' one
    session_scope, committed once at the very end), so a break just means
    fewer entries get re-matched this run - never a rollback, since there
    was never anything to roll back mid-loop in the first place."""
    stmt = (
        select(ChartEntry)
        .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
        .where(ChartIssue.chart_id == chart_id)
    )
    return _rematch_entries(session, session.scalars(stmt).all(), threshold,
                            progress, should_stop)


def rematch_issue(session: Session, issue_id: int, threshold: float = 0.72) -> tuple[int, int]:
    """Re-match just one edition's rows - what the Charts page's "Re-match
    library" button does since 2026-09-30 (James: "I'm on a specific
    [edition], what [does] Re-match Library do on this page? It's taking a
    very long time" - it was re-matching all ~355,000 rows of the whole
    Weekly chart, on the UI thread). Returns (matched, rows)."""
    entries = session.scalars(select(ChartEntry).where(ChartEntry.issue_id == issue_id)).all()
    return _rematch_entries(session, entries, threshold), len(entries)


def _rematch_entries(
    session: Session,
    entries: Sequence[ChartEntry],
    threshold: float = 0.72,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Match each row, but each distinct title + artist only once
    (2026-09-30): a weekly chart repeats the same song for weeks - the
    Hot 100 has ~30,000 distinct songs across ~355,000 rows - so a whole-
    chart re-match does roughly a tenth of the fuzzy lookups it used to."""
    matched = 0
    seen: dict[tuple[str, str], tuple[Optional[int], Optional[float]]] = {}
    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        if should_stop and should_stop():
            break
        key = (entry.title_key, entry.artist_key)
        if entry.match_locked:
            hit = entry.track_id is not None
        elif key in seen:
            entry.track_id, entry.match_score = seen[key]
            hit = entry.track_id is not None
        else:
            hit = match_entry(session, entry, threshold)
            seen[key] = (entry.track_id, entry.match_score)
        if hit:
            matched += 1
        if progress and (i % 50 == 0 or i == total):
            progress(i, total, "")
    return matched


def rematch_all_charts(
    threshold: float = 0.72,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Settings' bulk "Re-match" button (2026-09-22, part of "does the
    progress bar also work on ... the other settings options that scan" -
    this used to be a plain loop inline in SettingsView.rematch_all,
    entirely on the UI thread with no feedback at all). Opens its own
    session, same as every other bulk action's service-level entry point
    (download_bios_for_artists/update_popularity_for_artists/
    search_artwork_for_releases) - this one just needed pulling out of
    SettingsView to have a session of its own to give a background thread.

    `progress` gets one `(0, 0, "<chart name>")` tick per chart (mirroring
    metadata_health.scan_missing_metadata's own phase-announcement shape),
    then rematch_chart's own real per-entry ticks for that chart.

    2026-09-22 same-day follow-up ("add a real cancel button...") -
    `should_stop` is threaded into every rematch_chart call (so a stop
    lands within a few dozen entries even mid-chart) and re-checked here
    between charts too, so a chart not yet started when Cancel is pressed
    is never begun at all."""
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
    """Search-by-artist-and-track for the "Fix match..." dialog
    (ui/views/charts.py:MatchTrackDialog) - manually picking which library
    track a chart entry matches, for the fraction `match_entry`'s fuzzy
    title/artist matching gets wrong or leaves unmatched entirely. Same
    two-box ANDed search `services/jukebox.py:search_addable_tracks`
    established for its own picker dialog (either box can be blank on its
    own, both blank returns nothing), but plain library search rather than
    that function's jukebox-specific one: no album-artist resolution and no
    exclusion of anything, since a chart legitimately charts
    various-artists compilation tracks and the same track can already be
    matched elsewhere (on another chart, or another entry being fixed) -
    every track in the library is a valid pick here.

    Returns plain dicts (`track_id`, `title`, `artist_name`, `album`), not
    ORM rows, same reasoning as `search_addable_tracks`: the dialog only
    ever reads these fields, and the session may close before it does."""
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
    locks the match (`match_locked = True`, `match_score = 1.0`, per the
    model's own "1.0 when confirmed by hand" comment on that column) so a
    later "Re-match library" pass never silently overwrites a manual pick -
    `match_entry` (and therefore `rematch_chart`) already skips any entry
    with `match_locked` set. Clearing it (`track_id=None`) unlocks the entry
    rather than leaving it permanently stuck unmatched: the person is
    saying "not this one," not "never try again," so it goes back into the
    pool the next automatic re-match considers."""
    entry = session.get(ChartEntry, entry_id)
    if entry is None:
        raise ValueError(f"no chart entry {entry_id}")
    entry.track_id = track_id
    entry.match_score = 1.0 if track_id is not None else None
    entry.match_locked = track_id is not None
    session.flush()
    return entry


# --------------------------------------------------------------------------
# editing positions by hand
# --------------------------------------------------------------------------
#
# 2026-10-03, James: "I would like to be able to edit my Chart songs. For
# example, the 2003 Billboard Top Country chart is missing #40 Trace Adkins
# - Then They Do." Imported CSVs are sometimes short a row or carry a typo;
# these let the Charts page fix one edition in place instead of editing the
# CSV and re-importing it.


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
    entry.title, entry.artist_name = title, artist
    entry.title_key, entry.artist_key = normalize(title), normalize(artist)
    entry.last_week, entry.peak_pos, entry.weeks_on_chart = last_week, peak_pos, weeks_on_chart
    session.flush()
    if renamed and not entry.match_locked:
        match_entry(session, entry, threshold)
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


#: Setting.key row this module owns - see db.models.Setting and
#: services/lastfm_popularity.py's API_KEY_KEY for the pattern this follows
#: (a generic key/value table, not QSettings - this app has no config file
#: outside the library database). Remembers where "Browse for file..." in
#: MatchTrackDialog last found something, so re-opening it for the next
#: unmatched entry doesn't always dump James back at his home folder when
#: he's clearly working through one album/box-set folder at a time.
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
    share the name's slug (2026-09-30: importing "Billboard Hot 100" weekly
    rows landed in the Pop Year End chart, whose slug was
    `billboard-hot-100`). The slug gets -2, -3... when taken."""
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

    `chart_id` (2026-09-30) adds the rows to that exact chart - what the
    Charts page's "Import chart CSV…" now does with the selected chart -
    instead of finding a chart by `chart_name`'s slug.

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
            .options(
                selectinload(ChartEntry.track).selectinload(Track.files),
                # 2026-09-17 fix - missing here (unlike
                # services/playlists.py:playlist_tracks, which already
                # loads both) meant `entry.track.release` was never
                # fetched while the session was still open. ChartsView
                # keeps its matched tracks around in `self._tracks` past
                # the `with self.ctx.session()` block that loaded them
                # (for "Play owned"/"Queue"/"Save as playlist"), and by
                # then the session is closed - so `QueueItem.from_track`'s
                # `track.release` access blew up with
                # sqlalchemy.orm.exc.DetachedInstanceError the moment
                # anyone actually tried to play a chart.
                selectinload(ChartEntry.track).selectinload(Track.release),
            )
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

    `folder_id` (2026-10-01) files the playlist in that playlist folder
    (None = top level) - the Charts page's "Save as playlist" asks James
    where to put it. Left out, a new playlist lands at the top level and an
    existing one stays where it is."""
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
    if folder_id is not _KEEP_FOLDER:
        playlist.folder_id = folder_id
    session.flush()
    return playlist
