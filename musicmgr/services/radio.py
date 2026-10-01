"""Radio - an endless mix seeded by one song or artist (2026-10-01).

James picked this off the feature list: "pick a seed song and it keeps
queuing similar tracks you own, using Last.fm's similar-artists data
together with your ratings and play counts" (Plexamp's track radio).

**Which artists fit.** Each library artist gets an affinity, 0..1, from
whichever of these is strongest:

- **Last.fm similar artists** (`artist.getSimilar`, cached in the
  `artist_similar` table for 30 days; fetched in the background the first
  time an artist seeds or plays on a radio, so a radio starts instantly and
  gets smarter a batch later). The seed artist's list counts fully; the
  *currently playing* artist's list counts at half strength, so a long
  session drifts gently the way Plexamp's does.
- **Chart neighbours** - artists that share chart weeks with the seed
  (cosine of their chart-issue sets). James's library has ~400k chart
  entries, and two artists charting in the same weeks is a strong sign of
  the same era and audience - the best signal available offline.
- **Playlist neighbours** - artists that share his own playlists.
- **Same genre**, weakly, as a last resort for a thin pool.

The seed artist itself is in the mix too, but spaced out.

**Which of their tracks.** Each candidate track is weighted by
affinity x your rating (unrated = neutral; 1-2 stars mostly kept out,
4-5 stars favoured) x a light boost for tracks you play x a penalty for
tracks you usually skip x freshness (not something played in the last day
or week) x era (release year close to the seed's). Picks are a weighted
random draw, not a top-N, so two radios from the same song differ; no
artist repeats within three tracks, and the same song (any pressing) never
repeats in a session.

`RadioController` (bottom of this module) keeps the player's queue topped
up: whenever three or fewer tracks remain after the current one it adds
another batch. Playing anything else ends the radio.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import (
    Artist,
    ArtistSimilar,
    ChartEntry,
    Credit,
    MediaFile,
    PlaylistItem,
    Release,
    Track,
    release_genres,
)
from .matching import normalize

log = logging.getLogger(__name__)

RADIO_SOURCE = "radio"
BATCH_SIZE = 10
#: top the queue up when this many tracks (or fewer) are left after the current one
REFILL_WHEN_LEFT = 3
SIMILAR_TTL_DAYS = 30
#: no artist twice within this many picks
ARTIST_SPACING = 3
#: the seed artist at most once per this many picks
SEED_ARTIST_EVERY = 4
#: candidate pool below this many tracks gets widened by genre, then era
MIN_POOL = 40

W_SEED_ARTIST = 0.8
W_CURRENT_SIMILAR = 0.5
W_CHART = 0.6
W_PLAYLIST = 0.4
W_GENRE = 0.12
W_FILLER = 0.03
#: chart neighbours must share this many chart weeks, and only the closest
#: this many count - a long-charting seed (Elvis: ~3,000 artists share at
#: least one week with him) otherwise makes everyone a neighbour
CHART_MIN_SHARED = 3
CHART_TOP_N = 150
PLAYLIST_TOP_N = 100
#: this many similar artists in the library = Last.fm data is rich enough to lead
LASTFM_LEADS_AT = 8
#: artists considered per batch, strongest first
MAX_ARTISTS = 300
#: songs that charted (or are Last.fm top tracks) are this much likelier -
#: a radio should mostly play songs people know, with deep cuts mixed in
HIT_BOOST = 3.0
#: era: weight falls off with years between a song's original release and the seed's
ERA_HALF_SPAN = 8.0


# --------------------------------------------------------------------------
# seeds
# --------------------------------------------------------------------------


@dataclass
class RadioSeed:
    artist_id: int
    artist_name: str
    track_id: Optional[int] = None
    track_title: str = ""
    release_id: Optional[int] = None
    year: Optional[int] = None

    @property
    def label(self) -> str:
        return f"“{self.track_title}”" if self.track_id else self.artist_name


def main_artist_id(session: Session, track_id: int) -> Optional[int]:
    ta = _track_artist_subquery()
    row = session.execute(select(ta.c.artist_id).where(ta.c.track_id == track_id)).first()
    if row is not None:
        return int(row[0])
    track = session.get(Track, track_id)
    if track is not None and track.release is not None:
        return track.release.album_artist_id
    return None


def seed_from_track(session: Session, track_id: int) -> Optional[RadioSeed]:
    track = session.get(Track, track_id)
    if track is None:
        return None
    artist_id = main_artist_id(session, track_id)
    if artist_id is None:
        return None
    artist = session.get(Artist, artist_id)
    # the song's original year: its earliest release by this artist
    ta = _track_artist_subquery()
    year = session.scalar(
        select(func.min(Release.year))
        .join(Track, Track.release_id == Release.id)
        .join(ta, ta.c.track_id == Track.id)
        .where(ta.c.artist_id == artist_id, Track.title_key == track.title_key)
    )
    return RadioSeed(
        artist_id=artist_id,
        artist_name=artist.name if artist else "",
        track_id=track.id,
        track_title=track.title,
        release_id=track.release_id,
        year=year or (track.release.year if track.release else None),
    )


def seed_from_artist(session: Session, artist_id: int) -> Optional[RadioSeed]:
    artist = session.get(Artist, artist_id)
    if artist is None:
        return None
    # median of the artist's songs' *original* years (earliest release of
    # each title) - a crooner's catalogue is mostly reissued on modern
    # compilations, so raw release years would put Bing Crosby in the 1990s
    ta = _track_artist_subquery()
    years = sorted(
        y for (y,) in session.execute(
            select(func.min(Release.year))
            .join(Track, Track.release_id == Release.id)
            .join(ta, ta.c.track_id == Track.id)
            .where(ta.c.artist_id == artist_id, Release.year.is_not(None))
            .group_by(Track.title_key)
        )
    )
    return RadioSeed(
        artist_id=artist_id,
        artist_name=artist.name,
        year=years[len(years) // 2] if years else None,
    )


# --------------------------------------------------------------------------
# Last.fm similar artists
# --------------------------------------------------------------------------


def needs_similar_fetch(session: Session, artist_id: int, now: Optional[dt.datetime] = None) -> bool:
    artist = session.get(Artist, artist_id)
    if artist is None:
        return False
    if artist.similar_fetched_at is None:
        return True
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    fetched = artist.similar_fetched_at.replace(tzinfo=None)
    return (now - fetched).days >= SIMILAR_TTL_DAYS


def fetch_similar(artist_id: int, http=None, api_key: Optional[str] = None) -> str:
    """Fetch and store Last.fm's similar artists for one library artist.
    Returns "updated", "not_found", "not_configured", "auth_error" or
    "error" (network trouble - nothing stored, so it's tried again later)."""
    import requests

    from ..db.session import session_scope
    from . import lastfm_popularity as lfm

    with session_scope() as session:
        artist = session.get(Artist, artist_id)
        if artist is None:
            return "not_found"
        name = artist.name
        api_key = api_key or lfm.get_api_key(session)
    if not api_key:
        return "not_configured"
    http = http or requests.Session()
    try:
        response = http.get(
            lfm.LASTFM_API_URL,
            params={
                "method": "artist.getsimilar",
                "artist": name,
                "autocorrect": 1,
                "limit": 100,
                "api_key": api_key,
                "format": "json",
            },
            timeout=10,
        )
        payload = response.json()
    except Exception as exc:
        log.info("Last.fm similar for %s: %s", name, exc)
        return "error"
    error_code = payload.get("error") if isinstance(payload, dict) else None
    if error_code in lfm._AUTH_ERROR_CODES:
        return "auth_error"
    entries = []
    if error_code is None:
        raw = ((payload.get("similarartists") or {}).get("artist")) or []
        if isinstance(raw, dict):
            raw = [raw]
        for entry in raw:
            sim_name = (entry.get("name") or "").strip()
            try:
                match = float(entry.get("match") or 0)
            except (TypeError, ValueError):
                match = 0.0
            key = normalize(sim_name)
            if key and match > 0:
                entries.append((sim_name, key, min(1.0, match)))
    with session_scope() as session:
        artist = session.get(Artist, artist_id)
        if artist is None:
            return "not_found"
        session.query(ArtistSimilar).filter(ArtistSimilar.artist_id == artist_id).delete()
        for sim_name, key, match in entries:
            session.add(ArtistSimilar(artist_id=artist_id, name=sim_name, name_key=key, match=match))
        artist.similar_fetched_at = dt.datetime.now(dt.timezone.utc)
    return "updated" if entries else "not_found"


def similar_library_artists(session: Session, artist_id: int) -> dict[int, float]:
    """Cached Last.fm similar artists that are in the library -> match."""
    rows = session.execute(
        select(Artist.id, ArtistSimilar.match)
        .join(Artist, Artist.name_key == ArtistSimilar.name_key)
        .where(ArtistSimilar.artist_id == artist_id)
    )
    out: dict[int, float] = {}
    for aid, match in rows:
        if aid != artist_id:
            out[aid] = max(out.get(aid, 0.0), float(match))
    return out


# --------------------------------------------------------------------------
# local neighbours
# --------------------------------------------------------------------------


_KEY_SPAN = 1_000_000_000


def _track_artist_subquery():
    """track_id -> its main artist. About 1.5% of James's tracks carry more
    than one "Main" credit; the one whose name appears in the track's own
    artist text wins (else the lowest artist id)."""
    from sqlalchemy import case

    preferred = case(
        (func.instr(func.lower(func.coalesce(Track.artist_display, "")), func.lower(Artist.name)) > 0, 0),
        else_=1,
    )
    keyed = (
        select(
            Credit.track_id.label("track_id"),
            func.min(preferred * _KEY_SPAN + Credit.artist_id).label("k"),
        )
        .join(Track, Track.id == Credit.track_id)
        .join(Artist, Artist.id == Credit.artist_id)
        .where(Credit.role == Credit.ROLE_MAIN)
        .group_by(Credit.track_id)
        .subquery()
    )
    return select(
        keyed.c.track_id.label("track_id"), (keyed.c.k % _KEY_SPAN).label("artist_id")
    ).subquery()


def _cosine_neighbours(
    session: Session, artist_id: int, group_col, link_track_col, table,
    min_shared: int = 1, top_n: int = 100,
) -> dict[int, float]:
    """Artists sharing groups (chart issues, playlists) with `artist_id`,
    scored by cosine similarity of their group sets, scaled so the closest
    neighbour is 1.0, keeping the `top_n` closest."""
    ta = _track_artist_subquery()
    seed_groups = (
        select(group_col)
        .join(ta, ta.c.track_id == link_track_col)
        .where(ta.c.artist_id == artist_id)
        .distinct()
        .subquery()
    )
    shared = dict(
        session.execute(
            select(ta.c.artist_id, func.count(func.distinct(group_col)))
            .select_from(table)
            .join(ta, ta.c.track_id == link_track_col)
            .where(group_col.in_(select(seed_groups)))
            .group_by(ta.c.artist_id)
        ).all()
    )
    if not shared or artist_id not in shared:
        return {}
    totals = dict(
        session.execute(
            select(ta.c.artist_id, func.count(func.distinct(group_col)))
            .select_from(table)
            .join(ta, ta.c.track_id == link_track_col)
            .where(ta.c.artist_id.in_(list(shared)))
            .group_by(ta.c.artist_id)
        ).all()
    )
    seed_total = totals.get(artist_id) or 1
    scores = {}
    for aid, count in shared.items():
        if aid == artist_id or count < min_shared:
            continue
        scores[aid] = count / math.sqrt(seed_total * (totals.get(aid) or count))
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:top_n]
    if not top:
        return {}
    best = top[0][1]
    return {aid: score / best for aid, score in top}


def chart_neighbours(session: Session, artist_id: int) -> dict[int, float]:
    """Artists that chart in the same weeks/years as `artist_id`.

    Worked out per chart, then blended by how present the seed is on each
    chart (share of that chart's issues it appears in). James's charts are
    organised Source > Genre > Type, so a country artist's neighbours come
    mostly from the Country charts it lives on rather than from the one
    big weekly Hot 100 where it crosses over now and then - which would
    otherwise pair Eric Church with Drake."""
    from ..db.models import ChartIssue

    ta = _track_artist_subquery()
    base = (
        select(ChartIssue.chart_id, ChartEntry.issue_id, ta.c.artist_id)
        .join(ChartIssue, ChartIssue.id == ChartEntry.issue_id)
        .join(ta, ta.c.track_id == ChartEntry.track_id)
        .subquery()
    )
    seed_rows = session.execute(
        select(base.c.chart_id, func.count(func.distinct(base.c.issue_id)))
        .where(base.c.artist_id == artist_id)
        .group_by(base.c.chart_id)
    ).all()
    if not seed_rows:
        return {}
    sizes = dict(
        session.execute(
            select(ChartIssue.chart_id, func.count(ChartIssue.id))
            .where(ChartIssue.chart_id.in_([c for c, _ in seed_rows]))
            .group_by(ChartIssue.chart_id)
        ).all()
    )
    seed_issues = select(base.c.issue_id).where(base.c.artist_id == artist_id).distinct()
    shared_rows = session.execute(
        select(base.c.chart_id, base.c.artist_id, func.count(func.distinct(base.c.issue_id)))
        .where(base.c.issue_id.in_(seed_issues))
        .group_by(base.c.chart_id, base.c.artist_id)
    ).all()
    candidates = {aid for _, aid, n in shared_rows if n >= CHART_MIN_SHARED and aid != artist_id}
    if not candidates:
        return {}
    totals: dict[tuple[int, int], int] = {}
    cand = list(candidates)
    for i in range(0, len(cand), 900):
        for chart_id, aid, n in session.execute(
            select(base.c.chart_id, base.c.artist_id, func.count(func.distinct(base.c.issue_id)))
            .where(base.c.artist_id.in_(cand[i : i + 900]))
            .group_by(base.c.chart_id, base.c.artist_id)
        ):
            totals[(chart_id, aid)] = n
    seed_counts = dict(seed_rows)
    weights = {c: n / max(1, sizes.get(c, n)) for c, n in seed_counts.items()}
    weight_sum = sum(weights.values()) or 1.0
    scores: dict[int, float] = defaultdict(float)
    for chart_id, aid, n in shared_rows:
        if aid not in candidates:
            continue
        cos = n / math.sqrt(seed_counts[chart_id] * (totals.get((chart_id, aid)) or n))
        scores[aid] += weights[chart_id] * cos / weight_sum
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:CHART_TOP_N]
    best = top[0][1] if top else 0
    return {aid: sc / best for aid, sc in top} if best > 0 else {}


def playlist_neighbours(session: Session, artist_id: int) -> dict[int, float]:
    return _cosine_neighbours(
        session, artist_id, PlaylistItem.playlist_id, PlaylistItem.track_id, PlaylistItem,
        top_n=PLAYLIST_TOP_N,
    )


def genre_ids_for_artist(session: Session, artist_id: int, release_id: Optional[int]) -> set[int]:
    if release_id is not None:
        ids = {
            g for (g,) in session.execute(
                select(release_genres.c.genre_id).where(release_genres.c.release_id == release_id)
            )
        }
        if ids:
            return ids
    ta = _track_artist_subquery()
    return {
        g for (g,) in session.execute(
            select(release_genres.c.genre_id)
            .join(Track, Track.release_id == release_genres.c.release_id)
            .join(ta, ta.c.track_id == Track.id)
            .where(ta.c.artist_id == artist_id)
            .distinct()
        )
    }


# --------------------------------------------------------------------------
# picking
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    track_id: int
    artist_id: int
    release_id: Optional[int]
    title_key: str
    year: Optional[int]
    rating: Optional[int]
    play_count: int
    skip_count: int
    last_played_at: Optional[dt.datetime]
    affinity: float = 0.0
    #: the song charted, or is one of the artist's Last.fm top tracks
    hit: bool = False


@dataclass
class RadioState:
    """What one radio session has done so far."""

    seed: RadioSeed
    played_track_ids: set[int] = field(default_factory=set)
    played_songs: set[tuple[int, str]] = field(default_factory=set)
    recent_artist_ids: list[int] = field(default_factory=list)
    picks_since_seed_artist: int = 0
    #: per-session caches (artist id -> neighbours)
    neighbour_cache: dict = field(default_factory=dict)

    def remember(self, c: "Candidate") -> None:
        self.played_track_ids.add(c.track_id)
        self.played_songs.add((c.artist_id, c.title_key))
        self.recent_artist_ids.append(c.artist_id)
        del self.recent_artist_ids[:-ARTIST_SPACING]
        if c.artist_id == self.seed.artist_id:
            self.picks_since_seed_artist = 0
        else:
            self.picks_since_seed_artist += 1


def _neighbours(session: Session, state: RadioState, kind: str, artist_id: int) -> dict[int, float]:
    key = (kind, artist_id)
    if key not in state.neighbour_cache:
        fn = {"chart": chart_neighbours, "playlist": playlist_neighbours,
              "similar": similar_library_artists}[kind]
        state.neighbour_cache[key] = fn(session, artist_id)
    return state.neighbour_cache[key]


def artist_affinities(
    session: Session, state: RadioState, current_artist_id: Optional[int] = None
) -> dict[int, float]:
    seed = state.seed.artist_id
    aff: dict[int, float] = defaultdict(float)

    def offer(scores: dict[int, float], weight: float) -> None:
        for aid, score in scores.items():
            aff[aid] = max(aff[aid], weight * score)

    similar = _neighbours(session, state, "similar", seed)
    offer(similar, 1.0)
    # once Last.fm knows enough of the library's artists it leads, and the
    # local signals fill in around it
    local = 0.5 if len(similar) >= LASTFM_LEADS_AT else 1.0
    offer(_neighbours(session, state, "chart", seed), W_CHART * local)
    offer(_neighbours(session, state, "playlist", seed), W_PLAYLIST * local)
    if current_artist_id is not None and current_artist_id != seed:
        offer(_neighbours(session, state, "similar", current_artist_id), W_CURRENT_SIMILAR)
    aff[seed] = max(aff[seed], W_SEED_ARTIST)
    strongest = sorted(aff.items(), key=lambda kv: -kv[1])[:MAX_ARTISTS]
    return {aid: w for aid, w in strongest if w > 0}


def _candidates(session: Session, artist_weights: dict[int, float]) -> list[Candidate]:
    if not artist_weights:
        return []
    ta = _track_artist_subquery()
    present = select(MediaFile.track_id).where(MediaFile.is_missing.is_(False))
    out: list[Candidate] = []
    ids = list(artist_weights)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        rows = session.execute(
            select(
                Track.id, ta.c.artist_id, Track.release_id, Track.title_key, Release.year,
                Track.rating, Track.play_count, Track.skip_count, Track.last_played_at,
            )
            .join(ta, ta.c.track_id == Track.id)
            .join(Release, Release.id == Track.release_id)
            .where(ta.c.artist_id.in_(chunk), Track.id.in_(present))
        )
        for tid, aid, rid, tkey, year, rating, plays, skips, last in rows:
            out.append(Candidate(tid, aid, rid, tkey or "", year, rating, plays or 0,
                                 skips or 0, last, artist_weights[aid]))
    return out


def _widen(session: Session, state: RadioState, have: set[int]) -> dict[int, float]:
    """Extra artists for a thin pool: same genre as the seed, then anyone
    (a library with no charts, playlists or Last.fm data yet)."""
    extra: dict[int, float] = {}
    ta = _track_artist_subquery()
    genres = genre_ids_for_artist(session, state.seed.artist_id, state.seed.release_id)
    if genres:
        for (aid,) in session.execute(
            select(ta.c.artist_id)
            .join(Track, Track.id == ta.c.track_id)
            .join(release_genres, release_genres.c.release_id == Track.release_id)
            .where(release_genres.c.genre_id.in_(genres))
            .distinct()
            .limit(3000)
        ):
            if aid not in have:
                extra[aid] = W_GENRE
    if len(extra) < 20:
        # anyone at all - the era weighting in track_weight still keeps
        # the picks close to the seed's years
        for (aid,) in session.execute(
            select(ta.c.artist_id).distinct().order_by(func.random()).limit(3000)
        ):
            if aid not in have and aid not in extra:
                extra[aid] = W_FILLER
    return extra


_RATING_FACTOR = {0: 0.05, 1: 0.1, 2: 0.4, 3: 1.0, 4: 1.6, 5: 2.2}


def track_weight(c: Candidate, seed_year: Optional[int], now: dt.datetime) -> float:
    w = c.affinity
    w *= _RATING_FACTOR.get(c.rating, 1.0) if c.rating is not None else 1.0
    w *= 1.0 + 0.12 * math.log1p(c.play_count)
    heard = c.play_count + c.skip_count
    if heard >= 2:
        w *= 1.0 - 0.7 * (c.skip_count / heard)
    if c.last_played_at is not None:
        last = c.last_played_at.replace(tzinfo=None)
        age_h = (now - last).total_seconds() / 3600
        if age_h < 24:
            w *= 0.1
        elif age_h < 24 * 7:
            w *= 0.5
    if c.hit:
        w *= HIT_BOOST
    if seed_year and c.year:
        w *= 0.04 + 0.96 * math.exp(-abs(c.year - seed_year) / ERA_HALF_SPAN)
    return max(w, 0.0)


def _mark_hits(session: Session, pool: Sequence[Candidate]) -> None:
    """Flag every pressing of a song that has a matched chart entry or is
    in its artist's Last.fm top tracks."""
    from ..db.models import ArtistTopTrack

    hits: set[tuple[int, str]] = set()
    by_id = {c.track_id: c for c in pool}
    ids = list(by_id)
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for (tid,) in session.execute(
            select(ChartEntry.track_id).where(ChartEntry.track_id.in_(chunk)).distinct()
        ):
            c = by_id[tid]
            hits.add((c.artist_id, c.title_key))
    artist_ids = list({c.artist_id for c in pool})
    for i in range(0, len(artist_ids), 900):
        for aid, title in session.execute(
            select(ArtistTopTrack.artist_id, ArtistTopTrack.title)
            .where(ArtistTopTrack.artist_id.in_(artist_ids[i : i + 900]))
        ):
            hits.add((aid, normalize(title)))
    for c in pool:
        c.hit = (c.artist_id, c.title_key) in hits


def _original_years(pool: Iterable[Candidate]) -> None:
    """A song's year = the earliest release of it in the pool. A 1956 hit
    on a 2010 compilation counts as 1956, not 2010."""
    first: dict[tuple[int, str], int] = {}
    for c in pool:
        if c.year:
            song = (c.artist_id, c.title_key)
            first[song] = min(first.get(song, c.year), c.year)
    for c in pool:
        c.year = first.get((c.artist_id, c.title_key), c.year)


def build_batch(
    session: Session,
    state: RadioState,
    n: int = BATCH_SIZE,
    current_artist_id: Optional[int] = None,
    rng: Optional[random.Random] = None,
    now: Optional[dt.datetime] = None,
) -> list[int]:
    """Pick the next `n` track ids and record them in `state`."""
    rng = rng or random.Random()
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(tzinfo=None)
    weights = artist_affinities(session, state, current_artist_id)
    pool = _candidates(session, weights)
    fresh = [c for c in pool if c.track_id not in state.played_track_ids]
    if len(fresh) < MIN_POOL:
        extra = _widen(session, state, set(weights))
        pool += _candidates(session, extra)
    _original_years(pool)
    _mark_hits(session, pool)

    # one entry per song: the same title by the same artist on several
    # compilations counts once (best-weighted pressing wins)
    best: dict[tuple[int, str], tuple[float, Candidate]] = {}
    for c in pool:
        if c.track_id in state.played_track_ids:
            continue
        song = (c.artist_id, c.title_key)
        if song in state.played_songs:
            continue
        w = track_weight(c, state.seed.year, now)
        if w <= 0:
            continue
        if song not in best or w > best[song][0]:
            best[song] = (w, c)
    # two-stage draw: first an artist, then one of their songs - otherwise
    # an artist with a 300-track box set crowds out one with a 20-track
    # greatest-hits, whatever their affinity
    by_artist: dict[int, list[tuple[float, Candidate]]] = defaultdict(list)
    for w, c in best.values():
        by_artist[c.artist_id].append((w, c))

    picks: list[int] = []
    while len(picks) < n and by_artist:
        def artist_weight(aid: int) -> float:
            # the artist's affinity, scaled by how well their best-fitting
            # song suits this radio (era, rating, freshness)
            return max(w for w, _ in by_artist[aid])

        artists = list(by_artist)
        allowed = [
            a for a in artists
            if a not in state.recent_artist_ids
            and (a != state.seed.artist_id
                 or state.picks_since_seed_artist >= SEED_ARTIST_EVERY - 1)
        ]
        if not allowed:
            allowed = [a for a in artists
                       if not state.recent_artist_ids or a != state.recent_artist_ids[-1]]
        if not allowed:
            allowed = artists
        aid = rng.choices(allowed, weights=[artist_weight(a) for a in allowed], k=1)[0]
        songs = by_artist[aid]
        idx = rng.choices(range(len(songs)), weights=[w for w, _ in songs], k=1)[0]
        _, chosen = songs.pop(idx)
        if not songs:
            del by_artist[aid]
        picks.append(chosen.track_id)
        state.remember(chosen)
    return picks


def first_track_for_artist(session: Session, state: RadioState, rng=None) -> Optional[int]:
    """An artist radio opens with one of the artist's own best tracks."""
    rng = rng or random.Random()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    pool = _candidates(session, {state.seed.artist_id: 1.0})
    if not pool:
        return None
    weights = [track_weight(c, state.seed.year, now) ** 2 for c in pool]
    if sum(weights) <= 0:
        weights = None
    c = rng.choices(pool, weights=weights, k=1)[0]
    state.remember(c)
    return c.track_id


# --------------------------------------------------------------------------
# Qt controller
# --------------------------------------------------------------------------

from PySide6.QtCore import QObject, QThread, Signal  # noqa: E402


class SimilarFetchThread(QThread):
    done = Signal(int, str)  # artist_id, status

    def __init__(self, artist_id: int, parent=None) -> None:
        super().__init__(parent)
        self.artist_id = artist_id

    def run(self) -> None:  # pragma: no cover - network
        try:
            status = fetch_similar(self.artist_id)
        except Exception as exc:
            log.warning("similar-artist fetch failed: %s", exc)
            status = "error"
        self.done.emit(self.artist_id, status)


class BatchThread(QThread):
    """Builds one batch off the GUI thread - the first batch of a radio
    session works out its chart neighbours, which takes a second or two on
    a 100k-track library."""

    done = Signal(object, object)  # RadioState, list[int]

    def __init__(self, state: RadioState, n: int, current_artist_id: Optional[int],
                 rng: random.Random, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self.n = n
        self.current_artist_id = current_artist_id
        self.rng = rng

    def run(self) -> None:
        from ..db.session import session_scope

        picks: list[int] = []
        try:
            with session_scope() as session:
                picks = build_batch(session, self.state, n=self.n,
                                    current_artist_id=self.current_artist_id, rng=self.rng)
        except Exception as exc:  # pragma: no cover
            log.warning("radio batch failed: %s", exc)
        self.done.emit(self.state, picks)


_RUNNING: set = set()


def stop_background_work(wait_ms: int = 5000) -> None:
    """MainWindow.closeEvent: let any in-flight fetch/batch finish."""
    for thread in list(_RUNNING):
        thread.wait(wait_ms)


def _start_thread(thread: QThread) -> None:
    _RUNNING.add(thread)
    thread.finished.connect(lambda t=thread: _RUNNING.discard(t))
    thread.start()


class RadioController(QObject):
    """Keeps the player's queue topped up while a radio is on. One per app
    (`AppContext.radio`)."""

    #: active, label ("“Song”" / "Artist", or "" when off)
    stateChanged = Signal(bool, str)
    notified = Signal(str)

    #: Last.fm fetching on/off, and building batches on a worker thread -
    #: tests switch both off
    fetch_enabled = True
    threaded = True

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self.player = player
        self._state: Optional[RadioState] = None
        self._busy = False
        self._batch_pending = False
        self._fetching: set[int] = set()
        self._fetched_this_session: set[int] = set()
        self._rng = random.Random()
        player.queueChanged.connect(self._check)
        player.trackChanged.connect(lambda _item: self._check())

    @property
    def active(self) -> bool:
        return self._state is not None

    @property
    def label(self) -> str:
        return self._state.seed.label if self._state else ""

    # -- start / stop -----------------------------------------------------

    def start_from_track(self, track_id: int) -> bool:
        from ..db.session import session_scope

        with session_scope() as session:
            seed = seed_from_track(session, track_id)
            track = session.get(Track, track_id)
            if seed is None or track is None:
                self.notified.emit("Can't start a radio from that track")
                return False
            state = RadioState(seed=seed)
            state.remember(Candidate(
                track.id, seed.artist_id, track.release_id, track.title_key or "",
                seed.year, track.rating, track.play_count or 0, track.skip_count or 0,
                track.last_played_at,
            ))
            return self._start(session, state, [track_id])

    def start_from_artist(self, artist_id: int) -> bool:
        from ..db.session import session_scope

        with session_scope() as session:
            seed = seed_from_artist(session, artist_id)
            if seed is None:
                return False
            state = RadioState(seed=seed)
            first = first_track_for_artist(session, state, self._rng)
            if first is None:
                self.notified.emit(f"Nothing by {seed.artist_name} to play")
                return False
            return self._start(session, state, [first])

    def _start(self, session: Session, state: RadioState, opening: list[int]) -> bool:
        """Play the opening track straight away; the rest of the first
        batch follows a moment later from the worker thread."""
        from . import lastfm_popularity as lfm

        items = self._queue_items(session, opening)
        if not items:
            self.notified.emit("No playable files for that radio")
            return False
        pending = self._maybe_fetch(session, state.seed.artist_id)
        has_key = bool(lfm.get_api_key(session))
        self._state = state
        self._batch_pending = False
        self._busy = True
        try:
            self.player.set_shuffle(False)
            self.player.play_tracks(items, start=0, source=RADIO_SOURCE)
        finally:
            self._busy = False
        self.stateChanged.emit(True, state.seed.label)
        if has_key:
            self.notified.emit(f"Radio from {state.seed.label}")
        else:
            self.notified.emit(
                f"Radio from {state.seed.label} — add a Last.fm API key in Settings for better picks"
            )
        # a short first batch while Last.fm data is on its way, so the
        # better-informed picks start sooner
        self._request_batch(4 if pending else BATCH_SIZE)
        return True

    def stop(self) -> None:
        if self._state is None:
            return
        self._state = None
        self._batch_pending = False
        self.stateChanged.emit(False, "")

    # -- keeping the queue full -------------------------------------------

    def _check(self) -> None:
        if self._state is None or self._busy:
            return
        if self.player.source != RADIO_SOURCE or not self.player.queue:
            self.stop()
            return
        if self.player.remaining_count() <= REFILL_WHEN_LEFT:
            self._request_batch(BATCH_SIZE)

    def _request_batch(self, n: int) -> None:
        from ..db.session import session_scope

        if self._state is None or self._batch_pending:
            return
        current = self.player.current
        current_artist = None
        if current is not None:
            with session_scope() as session:
                current_artist = main_artist_id(session, current.track_id)
                if current_artist is not None:
                    self._maybe_fetch(session, current_artist)
        self._batch_pending = True
        if self.threaded:
            thread = BatchThread(self._state, n, current_artist, self._rng)
            thread.done.connect(self._on_batch)
            _start_thread(thread)
        else:
            with session_scope() as session:
                picks = build_batch(session, self._state, n=n,
                                    current_artist_id=current_artist, rng=self._rng)
            self._on_batch(self._state, picks)

    def _on_batch(self, state: RadioState, picks: list) -> None:
        from ..db.session import session_scope

        if state is not self._state:
            return  # radio stopped or restarted meanwhile
        self._batch_pending = False
        with session_scope() as session:
            items = self._queue_items(session, picks)
        if not items:
            if self.player.remaining_count() == 0:
                self.notified.emit("Radio ran out of new songs to play")
            return
        self._busy = True
        try:
            self.player.enqueue(items)
        finally:
            self._busy = False

    @staticmethod
    def _queue_items(session: Session, track_ids: Sequence[int]):
        from .player import queue_items_from_tracks

        ids = list(track_ids)
        if not ids:
            return []
        tracks = {t.id: t for t in session.scalars(select(Track).where(Track.id.in_(ids)))}
        return queue_items_from_tracks([tracks[i] for i in ids if i in tracks])

    # -- Last.fm ----------------------------------------------------------

    def _maybe_fetch(self, session: Session, artist_id: int) -> bool:
        """Start a background similar-artists fetch if this artist needs
        one. True if a fetch is in flight for it."""
        from . import lastfm_popularity as lfm

        if artist_id in self._fetching:
            return True
        if (
            not self.fetch_enabled
            or artist_id in self._fetched_this_session
            or not lfm.get_api_key(session)
            or not needs_similar_fetch(session, artist_id)
        ):
            return False
        self._fetching.add(artist_id)
        self._fetched_this_session.add(artist_id)
        thread = SimilarFetchThread(artist_id)
        thread.done.connect(self._on_fetched)
        _start_thread(thread)
        return True

    def _on_fetched(self, artist_id: int, status: str) -> None:
        self._fetching.discard(artist_id)
        if self._state is None:
            return
        # the next batch should use the fresh list
        self._state.neighbour_cache.pop(("similar", artist_id), None)
        if status == "auth_error":
            self.notified.emit(
                "Last.fm rejected the API key — radio is using your charts and playlists"
            )
