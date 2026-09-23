"""Aggregate "what's still missing" across the four enrichment sources this
app fetches per release/artist - album artwork (artwork_downloader.py),
artist biography (artist_bio_downloader.py), track popularity
(lastfm_popularity.py), and lyrics (lyrics_downloader.py/lyrics.py).

Each of those modules already has (or, for popularity, gets here) its own
id-only "missing" helper for its own bulk-action target
(`releases_missing_cover`, `artists_missing_bio`,
`all_artist_ids_with_releases`) - this module doesn't replace those. It adds
a *display* layer on top (names/titles, not just ids) purely for the
Settings "Missing metadata" dashboard (ui/widgets/common.py:
MissingMetadataDialog), kept separate so those other modules' own helpers
don't need to grow dashboard-specific fields they have no other use for.

Lyrics is the odd one out: there's no `Track`/`Release` column for it at
all (see lyrics.py's own docstring for why - a sidecar `.lrc` file next to
the audio is the only source of truth), so "missing" here means walking
every track's file path and checking for that sibling file, which is
filesystem I/O rather than an indexed lookup. `releases_missing_lyrics_rows`
takes an optional `progress` callback for exactly that reason - a library
of 150k+ tracks makes this the one genuinely slow query in this module, so
callers should run it off the UI thread (see common.py: MetadataScanThread)
and show progress rather than freezing on it.

`scan_missing_metadata` is the entry point the dashboard actually calls
(see MetadataScanThread) - all four queries run one after another *inside*
it, each announced with its own `progress(0, 0, "...")` tick first, so
every phase (including the three individually-fast ones) shows up on
screen instead of only the slow lyrics scan getting a progress bar while
the other three run silently on the UI thread first. James, 2026-09-22,
after the first version of this dashboard: "I click the option and it
looks like nothing is happening" - that silent multi-second stretch was
exactly those three "fast" queries running synchronously in the button's
own click handler, before the progress bar was even made visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy import func, select

from ..db.models import Artist, ArtistTopTrack, MediaFile, Release, Track
from ..db.session import session_scope
from . import artist_bio_downloader as bio_dl
from . import artwork_downloader as artwork_dl
from . import lyrics as lyrics_svc

#: how often the lyrics scan below reports progress - same throttling idea
#: as scanner.py's `idx % 5`, just coarser since this walks every track in
#: the library rather than one watched folder at a time.
_LYRICS_PROGRESS_EVERY = 250


@dataclass
class ScanResult:
    missing_covers: list = field(default_factory=list)
    missing_bios: list = field(default_factory=list)
    missing_popularity: list = field(default_factory=list)
    missing_lyrics: list = field(default_factory=list)


def scan_missing_metadata(
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Everything the "Missing metadata" dashboard needs, in one call - the
    three fast/indexed queries plus the one slow filesystem-bound one,
    each preceded by its own `progress(0, 0, "<phase>")` tick (0/0 rather
    than a real fraction - these three finish in well under a second each,
    there's nothing meaningful to show a fraction of) so a caller driving a
    progress bar (MetadataScanThread) has something to show throughout the
    whole scan, not just its last, slowest phase.

    2026-09-22 same-day follow-up (James: "add a real cancel button that
    stops any process running within Settings") - every query here is
    read-only (nothing in this whole module ever writes to the database),
    so there's no partial-write risk to guard against at all: `should_stop`
    just skips whichever phases haven't started yet the moment it's set,
    checked between phases and, for the one slow phase, inside its own row
    loop too (see releases_missing_lyrics_rows) - the dashboard then simply
    opens with fewer categories filled in than a full scan would have
    found."""

    def tick(name: str) -> None:
        if progress:
            progress(0, 0, name)

    if should_stop and should_stop():
        return ScanResult([], [], [], [])
    tick("Checking album artwork…")
    covers = releases_missing_cover_rows()
    if should_stop and should_stop():
        return ScanResult(covers, [], [], [])
    tick("Checking artist profiles…")
    bios = artists_missing_bio_rows()
    if should_stop and should_stop():
        return ScanResult(covers, bios, [], [])
    tick("Checking track popularity…")
    popularity = artists_missing_popularity_rows()
    if should_stop and should_stop():
        return ScanResult(covers, bios, popularity, [])
    tick("Checking lyrics…")
    lyrics = releases_missing_lyrics_rows(progress=progress, should_stop=should_stop)
    return ScanResult(covers, bios, popularity, lyrics)


def releases_missing_cover_rows(limit: Optional[int] = None) -> list[dict]:
    """Display rows for `artwork_downloader.releases_missing_cover` -
    `{"id", "title", "artist"}` per release with no cover saved yet."""
    with session_scope() as db:
        release_ids = artwork_dl.releases_missing_cover(limit=limit)
        if not release_ids:
            return []
        stmt = (
            select(Release.id, Release.title, Release.artist_display)
            .where(Release.id.in_(release_ids))
            .order_by(Release.title)
        )
        return [
            {"id": rid, "title": title or "", "artist": artist or ""}
            for rid, title, artist in db.execute(stmt).all()
        ]


def artists_missing_bio_rows(limit: Optional[int] = None) -> list[dict]:
    """Display rows for `artist_bio_downloader.artists_missing_bio` -
    `{"id", "name"}` per artist with no biography saved yet."""
    with session_scope() as db:
        artist_ids = bio_dl.artists_missing_bio(limit=limit)
        if not artist_ids:
            return []
        stmt = (
            select(Artist.id, Artist.name)
            .where(Artist.id.in_(artist_ids))
            .order_by(Artist.name)
        )
        return [{"id": aid, "name": name or ""} for aid, name in db.execute(stmt).all()]


def artists_missing_popularity_rows(limit: Optional[int] = None) -> list[dict]:
    """Every artist with at least one release who has no `ArtistTopTrack`
    rows yet - i.e. `update_popularity_for_artist`
    (services/lastfm_popularity.py) has never successfully run for them.
    Unlike `lastfm_popularity.all_artist_ids_with_releases` (deliberately
    unfiltered, since a playcount is meant to be periodically refreshed -
    see that function's own docstring), this dashboard cares only about
    artists that have *never* been fetched, the same "fetch once and
    surface as missing until it happens" framing `artists_missing_bio`
    already uses for biographies."""
    with session_scope() as db:
        fetched_ids = select(ArtistTopTrack.artist_id).distinct()
        stmt = (
            select(Artist.id, Artist.name)
            .where(Artist.album_releases.any())
            .where(Artist.id.not_in(fetched_ids))
            .order_by(Artist.name)
        )
        if limit:
            stmt = stmt.limit(limit)
        return [{"id": aid, "name": name or ""} for aid, name in db.execute(stmt).all()]


def releases_missing_lyrics_rows(
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[dict]:
    """Every release with at least one track missing a sibling `.lrc` file
    - `{"id", "title", "artist", "missing", "total"}`, `missing`/`total`
    counting only tracks that actually have a playable file (a track with
    no file at all has nothing to check a sidecar against).

    One query for every (release, track, path) triple - same "playable
    file per track" subquery `library.list_track_details` already uses -
    then the `.lrc` check itself happens in Python per row, since that's
    filesystem I/O SQL can't do. Grouped by release rather than returned
    as a flat per-track list: a release with 20,000 releases in the
    library and thousands of individually-missing tracks is still a list
    a person can actually scroll, the same reasoning the Settings dashboard
    (ui/widgets/common.py: MissingMetadataDialog) was built around.

    Streamed row by row (iterating the `Result` directly) rather than
    `.all()`'d up front - `.all()` would fetch and buffer every one of a
    150k-track library's rows before the per-row `.lrc` check (and its
    progress ticks) ever starts, which on a real library is exactly the
    kind of multi-second silent stretch this whole `progress` callback
    exists to avoid. The row count for a meaningful progress denominator
    comes from its own fast `COUNT(*)` first, over the same join, rather
    than `len()` on a list that no longer gets fully materialized.
    """
    with session_scope() as db:
        playable = (
            select(
                MediaFile.track_id.label("track_id"),
                func.min(MediaFile.id).label("file_id"),
            )
            .where(MediaFile.is_missing.is_(False))
            .group_by(MediaFile.track_id)
            .subquery()
        )
        base = (
            select(
                Release.id,
                Release.title,
                Release.artist_display,
                MediaFile.path,
            )
            .join(Track, Track.release_id == Release.id)
            .join(playable, playable.c.track_id == Track.id)
            .join(MediaFile, MediaFile.id == playable.c.file_id)
        )
        total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

        totals: dict[int, dict] = {}
        i = 0
        for release_id, title, artist, path in db.execute(base):
            if should_stop and should_stop():
                break
            i += 1
            entry = totals.setdefault(
                release_id, {"id": release_id, "title": title or "", "artist": artist or "",
                             "missing": 0, "total": 0}
            )
            entry["total"] += 1
            if lyrics_svc.find_lyrics_path(path) is None:
                entry["missing"] += 1
            if progress and (i % _LYRICS_PROGRESS_EVERY == 0 or i == total):
                progress(i, total, "")

        results = [entry for entry in totals.values() if entry["missing"] > 0]
        results.sort(key=lambda r: r["title"])
        return results
