"""Ask MusicMgr - play music from a spoken request on the phone.

A natural-language request ("play some 1972 number ones") is answered from
the library's artists, profiles, albums, top songs, charts and playlists.

* An AI model understands the request - Claude (the Anthropic API) or
  Google Gemini, whose free tier needs only a Google AI Studio key - with
  an offline fallback for plain phrasings when there's no key or no
  internet. Settings › Phone remote picks the model; each provider keeps
  its own key.
* Voice comes from the iPhone keyboard's own dictation mic in the phone
  remote's Ask box - it works on the remote's plain http:// address, where
  Safari won't allow a web page to use the microphone itself.
* The answer plays at once (replacing the queue); the phone then offers
  "Add to queue instead", which puts back what was playing and queues the
  picks after it.

How a request runs (`Asker.ask`):

1. With a key for the chosen model (Gemini or Claude), the AI gets the
   request, today's date, a short summary of the library (genres, year
   span, the charts that exist) and the read-only tools in `LibraryTools` -
   artists and their profiles, top songs, similar artists, albums and
   songs, charts, playlists. It looks things up, then calls `play_songs`
   with track ids it was given by those tools (ids it made up are dropped),
   or `say` when there's nothing to play.
2. Without a key, or when the AI can't be reached, `offline_ask` handles the
   plain shapes - "play <artist>", "play the top 20 of 1972", "play number
   ones from 1965", "play playlist <name>", "play the album <name>",
   "play <song> by <artist>" - and otherwise searches the library.

Threading: the web remote runs this on its request thread (the AI can take
several seconds), but every library lookup is handed to the Qt main thread
through `run_main` - the same rule as the rest of the web remote: the
database is only touched from the main thread.

The model and both keys are stored per PC in the `settings` table
(Settings › Phone remote). Claude's and Gemini's loops are the same shape -
only the wire format differs (`Asker._ask_claude` / `Asker._ask_gemini`).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from ..db.models import (
    Artist,
    ArtistAlias,
    ArtistMembership,
    ArtistSimilar,
    ArtistTopPick,
    ArtistTopTrack,
    Chart,
    ChartEntry,
    ChartFolder,
    ChartIssue,
    Genre,
    MediaFile,
    Playlist,
    Release,
    Setting,
    Style,
    Track,
    release_genres,
    release_styles,
)
from .matching import normalize
from .search_text import fold_ilike
from .top_songs import distinct_chart, song_key

log = logging.getLogger(__name__)

KEY_API_KEY = "ask_anthropic_api_key"
KEY_GEMINI_KEY = "ask_gemini_api_key"
KEY_MODEL = "ask_model"

CLAUDE = "claude"
GEMINI = "gemini"
PROVIDER_NAMES = {CLAUDE: "Claude", GEMINI: "Gemini"}

#: (model id, label in Settings, provider) - Gemini first: it's free
MODELS = (
    ("gemini-3.8-flash", "Gemini Flash - free (Google AI Studio key)", GEMINI),
    ("gemini-3.5-flash-lite", "Gemini Flash-Lite - free, fastest", GEMINI),
    ("claude-sonnet-5-5", "Claude Sonnet - best understanding (paid)", CLAUDE),
    ("claude-haiku-5-5", "Claude Haiku - faster, cheaper (paid)", CLAUDE),
)
DEFAULT_MODEL = MODELS[0][0]

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
#: the AI's turns per request (each is one look-up round)
MAX_ROUNDS = 10
#: the whole request, so the phone isn't left waiting forever
TIME_BUDGET_S = 60.0
#: one HTTP call to the AI
HTTP_TIMEOUT_S = 40.0


def provider_of(model: str) -> str:
    for model_id, _label, provider in MODELS:
        if model_id == model:
            return provider
    return GEMINI if (model or "").startswith("gemini") else CLAUDE

#: a request plays at most this many songs
MAX_PLAY = 200
DEFAULT_PLAY = 25


# --------------------------------------------------------------------------
# preferences
# --------------------------------------------------------------------------


@dataclass
class AskPrefs:
    #: Anthropic (Claude)
    api_key: str = ""
    model: str = DEFAULT_MODEL
    #: Google AI Studio (Gemini)
    gemini_key: str = ""

    @property
    def provider(self) -> str:
        return provider_of(self.model)

    @property
    def active_key(self) -> str:
        return self.gemini_key if self.provider == GEMINI else self.api_key


def load_prefs(session) -> AskPrefs:
    prefs = AskPrefs()
    row = session.get(Setting, KEY_API_KEY)
    if row is not None and row.value:
        prefs.api_key = row.value.strip()
    row = session.get(Setting, KEY_GEMINI_KEY)
    if row is not None and row.value:
        prefs.gemini_key = row.value.strip()
    row = session.get(Setting, KEY_MODEL)
    if row is not None and row.value and row.value in {m[0] for m in MODELS}:
        prefs.model = row.value
    return prefs


def save_prefs(session, prefs: AskPrefs) -> None:
    for key, value in ((KEY_API_KEY, prefs.api_key.strip()),
                       (KEY_GEMINI_KEY, prefs.gemini_key.strip()),
                       (KEY_MODEL, prefs.model)):
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    session.flush()


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------


@dataclass
class AskResult:
    #: library track ids, in play order
    track_ids: list[int] = field(default_factory=list)
    #: a short name for what's playing ("Top 20 of 1972")
    title: str = ""
    #: one or two sentences for the phone
    message: str = ""
    shuffle: bool = False
    #: "claude", "gemini" or "offline"
    engine: str = "offline"


# --------------------------------------------------------------------------
# library look-ups (main thread; each takes an open session)
# --------------------------------------------------------------------------


def _like(text: str) -> str:
    return f"%{(text or '').strip()}%"


def _playable(stmt):
    """Only tracks with a file that isn't marked missing."""
    return stmt.where(
        Track.id.in_(select(MediaFile.track_id).where(MediaFile.is_missing.is_(False)))
    )


def _song(t: Track, **extra) -> dict:
    rel = t.release
    out = {
        "id": t.id,
        "title": t.title,
        "artist": t.artist_display or (rel.artist_display if rel else "") or "",
        "album": rel.title if rel else "",
        "year": rel.year if rel else None,
    }
    if t.rating:
        out["rating"] = t.rating
    if t.play_count:
        out["plays"] = t.play_count
    out.update(extra)
    return out


def _dedupe(tracks: Sequence[Track]) -> list[Track]:
    """One copy of each song by each artist. The same recording often sits on
    the album and again in a chart or hits folder; keep the album's
    (non-compilation) copy, then the most played - playlists should point
    at album songs rather than the chart-folder copies."""
    best: dict[tuple[str, str], Track] = {}
    order: list[tuple[str, str]] = []

    def rank(t: Track):
        rel = t.release
        return (0 if rel is not None and not rel.is_compilation else 1, -(t.play_count or 0))

    for t in tracks:
        key = (song_key(t.title), normalize(t.artist_display or ""))
        cur = best.get(key)
        if cur is None:
            best[key] = t
            order.append(key)
        elif rank(t) < rank(cur):
            best[key] = t
    return [best[k] for k in order]


def _find_artist(session, name: str) -> Optional[Artist]:
    """The library artist best matching a spoken name ("the beatles", "elvis")."""
    name = (name or "").strip()
    if not name:
        return None
    key = normalize(name)
    artist = session.scalars(select(Artist).where(Artist.name_key == key).limit(1)).first()
    if artist is not None:
        return artist
    alias = session.scalars(
        select(ArtistAlias).where(ArtistAlias.alias_name_key == key).limit(1)
    ).first()
    if alias is not None:
        return alias.artist
    # a partial name: the one with the most songs wins ("Elvis" -> Elvis Presley)
    candidates = session.scalars(
        select(Artist).where(or_(fold_ilike(Artist.name, _like(name)), fold_ilike(Artist.name_key, _like(key))))
        .limit(25)
    ).all()
    if not candidates:
        return None
    counts = _track_counts(session, candidates)
    return max(candidates, key=lambda a: (counts.get(a.id, 0), -len(a.name)))


def _artist_track_clause(artist: Artist):
    """Tracks credited to the artist (album artist or the track's own
    artist line), aliases included."""
    names = [artist.name] + [a.alias_name for a in artist.aliases if a.alias_name]
    parts = [Release.album_artist_id == artist.id]
    for n in names:
        parts.append(fold_ilike(Track.artist_display, n))
        parts.append(fold_ilike(Track.artist_display, f"{n} feat%"))
        parts.append(fold_ilike(Track.artist_display, f"{n} &%"))
        parts.append(fold_ilike(Track.artist_display, f"{n} and %"))
        parts.append(fold_ilike(Track.artist_display, f"{n} with %"))
    return or_(*parts)


def _artist_tracks(session, artist: Artist, limit: Optional[int] = None) -> list[Track]:
    stmt = _playable(
        select(Track).join(Release, Track.release_id == Release.id)
        .options(selectinload(Track.release))
        .where(_artist_track_clause(artist))
    ).order_by(Release.year, Release.title, Track.disc_no, Track.track_no)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).unique())


def _track_counts(session, artists: Sequence[Artist]) -> dict[int, int]:
    out = {}
    for a in artists:
        out[a.id] = session.scalar(
            _playable(select(func.count(Track.id)).select_from(Track)
                      .join(Release, Track.release_id == Release.id)
                      .where(_artist_track_clause(a)))
        ) or 0
    return out


def _artist_genres(session, artist: Artist, limit: int = 6) -> list[str]:
    rows = session.execute(
        select(Genre.name, func.count())
        .select_from(Release)
        .join(release_genres, release_genres.c.release_id == Release.id)
        .join(Genre, Genre.id == release_genres.c.genre_id)
        .where(Release.album_artist_id == artist.id)
        .group_by(Genre.name).order_by(func.count().desc()).limit(limit)
    ).all()
    styles = session.execute(
        select(Style.name, func.count())
        .select_from(Release)
        .join(release_styles, release_styles.c.release_id == Release.id)
        .join(Style, Style.id == release_styles.c.style_id)
        .where(Release.album_artist_id == artist.id)
        .group_by(Style.name).order_by(func.count().desc()).limit(limit)
    ).all()
    return [r[0] for r in rows] + [r[0] for r in styles]


def _top_song_titles(session, artist: Artist) -> list[str]:
    """The user's own top-song picks if set, else Last.fm's chart folded
    to one row per song (the artist page's Top songs)."""
    picks = list(session.scalars(
        select(ArtistTopPick.title).where(ArtistTopPick.artist_id == artist.id)
        .order_by(ArtistTopPick.position)
    ))
    if picks:
        return picks
    rows = session.scalars(
        select(ArtistTopTrack).where(ArtistTopTrack.artist_id == artist.id)
        .order_by(ArtistTopTrack.rank)
    ).all()
    return [s.title for s in distinct_chart(rows)]


def _match_titles(tracks: Sequence[Track], titles: Sequence[str]) -> list[Track]:
    """The owned track for each title, in the titles' order."""
    by_key: dict[str, list[Track]] = {}
    for t in tracks:
        by_key.setdefault(song_key(t.title), []).append(t)
    out = []
    for title in titles:
        found = by_key.get(song_key(title))
        if found:
            out.append(_dedupe(found)[0])
    return out


class LibraryTools:
    """What the AI (and the offline parser) can look up. Every method takes
    an open session and returns plain JSON-able data. Main thread only."""

    # -- artists ----------------------------------------------------------

    def find_artists(self, s, query: str = "", genre: str = "", limit: int = 15) -> dict:
        limit = max(1, min(int(limit or 15), 40))
        stmt = select(Artist)
        if query:
            stmt = stmt.where(or_(fold_ilike(Artist.name, _like(query)),
                                  Artist.profile.ilike(_like(query)),
                                  fold_ilike(Artist.real_name, _like(query))))
        if genre:
            stmt = stmt.where(Artist.id.in_(
                select(Release.album_artist_id)
                .join(release_genres, release_genres.c.release_id == Release.id, isouter=True)
                .join(Genre, Genre.id == release_genres.c.genre_id, isouter=True)
                .join(release_styles, release_styles.c.release_id == Release.id, isouter=True)
                .join(Style, Style.id == release_styles.c.style_id, isouter=True)
                .where(or_(fold_ilike(Genre.name, _like(genre)), fold_ilike(Style.name, _like(genre))))
            ))
        artists = s.scalars(stmt.limit(200)).all()
        counts = _track_counts(s, artists)
        artists = [a for a in artists if counts.get(a.id)]
        artists.sort(key=lambda a: -counts[a.id])
        out = []
        for a in artists[:limit]:
            years = s.execute(
                select(func.min(Release.year), func.max(Release.year))
                .where(Release.album_artist_id == a.id, Release.year.is_not(None))
            ).one()
            out.append({
                "name": a.name,
                "songs": counts[a.id],
                "years": f"{years[0]}-{years[1]}" if years[0] else None,
                "genres": _artist_genres(s, a, 4),
                "profile": (a.profile or "")[:240] or None,
            })
        return {"artists": out}

    def artist_profile(self, s, name: str) -> dict:
        a = _find_artist(s, name)
        if a is None:
            return {"error": f"No artist called {name!r} in the library"}
        members = [m.name for m in s.scalars(
            select(Artist).join(ArtistMembership, ArtistMembership.member_id == Artist.id)
            .where(ArtistMembership.group_id == a.id))]
        groups = [g.name for g in s.scalars(
            select(Artist).join(ArtistMembership, ArtistMembership.group_id == Artist.id)
            .where(ArtistMembership.member_id == a.id))]
        albums = s.scalars(
            select(Release).where(Release.album_artist_id == a.id).order_by(Release.year)
        ).all()
        return {
            "name": a.name,
            "real_name": a.real_name,
            "profile": (a.profile or "")[:2500] or None,
            "members": members or None,
            "member_of": groups or None,
            "genres": _artist_genres(s, a),
            "albums": [{"title": r.title, "year": r.year} for r in albums[:60]],
            "top_songs": _top_song_titles(s, a)[:15],
        }

    def artist_top_songs(self, s, name: str, limit: int = 10) -> dict:
        a = _find_artist(s, name)
        if a is None:
            return {"error": f"No artist called {name!r} in the library"}
        limit = max(1, min(int(limit or 10), 50))
        titles = _top_song_titles(s, a)
        owned = _match_titles(_artist_tracks(s, a), titles)
        return {
            "artist": a.name,
            "songs": [_song(t, top_rank=i + 1) for i, t in enumerate(owned[:limit])],
            "note": None if titles else "No top-songs list fetched for this artist yet",
        }

    def similar_artists(self, s, name: str) -> dict:
        a = _find_artist(s, name)
        if a is None:
            return {"error": f"No artist called {name!r} in the library"}
        rows = s.scalars(
            select(ArtistSimilar).where(ArtistSimilar.artist_id == a.id)
            .order_by(ArtistSimilar.match.desc())
        ).all()
        keys = [r.name_key for r in rows]
        owned = {x.name_key: x.name for x in s.scalars(select(Artist).where(Artist.name_key.in_(keys)))}
        names = [owned[k] for k in keys if k in owned]
        return {"artist": a.name, "similar_in_library": names[:25],
                "note": None if rows else "No similar-artists list fetched for this artist yet"}

    # -- songs and albums -------------------------------------------------

    def search_songs(
        self, s, artist: str = "", title: str = "", album: str = "", genre: str = "",
        year_from: Optional[int] = None, year_to: Optional[int] = None,
        min_rating: Optional[int] = None, order: str = "random", limit: int = 40,
    ) -> dict:
        limit = max(1, min(int(limit or 40), MAX_PLAY))
        stmt = _playable(select(Track).join(Release, Track.release_id == Release.id)
                         .options(selectinload(Track.release)))
        if artist:
            a = _find_artist(s, artist)
            stmt = stmt.where(_artist_track_clause(a) if a is not None
                              else fold_ilike(Track.artist_display, _like(artist)))
        if title:
            stmt = stmt.where(fold_ilike(Track.title, _like(title)))
        if album:
            stmt = stmt.where(fold_ilike(Release.title, _like(album)))
        if genre:
            stmt = stmt.where(or_(
                Release.id.in_(select(release_genres.c.release_id)
                               .join(Genre, Genre.id == release_genres.c.genre_id)
                               .where(fold_ilike(Genre.name, _like(genre)))),
                Release.id.in_(select(release_styles.c.release_id)
                               .join(Style, Style.id == release_styles.c.style_id)
                               .where(fold_ilike(Style.name, _like(genre)))),
            ))
        if year_from:
            stmt = stmt.where(Release.year >= int(year_from))
        if year_to:
            stmt = stmt.where(Release.year <= int(year_to))
        if min_rating:
            stmt = stmt.where(Track.rating >= int(min_rating))
        if order == "most_played":
            stmt = stmt.order_by(Track.play_count.desc(), func.random())
        elif order == "top_rated":
            stmt = stmt.order_by(Track.rating.desc().nulls_last(), Track.play_count.desc())
        elif order == "recently_added":
            stmt = stmt.order_by(Track.added_at.desc())
        elif order == "oldest":
            stmt = stmt.order_by(Release.year.asc().nulls_last(), Release.title, Track.track_no)
        elif order == "newest":
            stmt = stmt.order_by(Release.year.desc().nulls_last(), Release.title, Track.track_no)
        elif order == "album_order":
            stmt = stmt.order_by(Release.year, Release.title, Track.disc_no, Track.track_no)
        else:
            stmt = stmt.order_by(func.random())
        tracks = _dedupe(list(s.scalars(stmt.limit(limit * 3)).unique()))[:limit]
        return {"songs": [_song(t) for t in tracks]}

    def find_albums(self, s, query: str = "", artist: str = "", year_from: Optional[int] = None,
                    year_to: Optional[int] = None, limit: int = 20) -> dict:
        limit = max(1, min(int(limit or 20), 50))
        stmt = select(Release)
        if query:
            stmt = stmt.where(fold_ilike(Release.title, _like(query)))
        if artist:
            a = _find_artist(s, artist)
            stmt = stmt.where(Release.album_artist_id == a.id if a is not None
                              else fold_ilike(Release.artist_display, _like(artist)))
        if year_from:
            stmt = stmt.where(Release.year >= int(year_from))
        if year_to:
            stmt = stmt.where(Release.year <= int(year_to))
        rels = s.scalars(stmt.order_by(Release.year).limit(limit)).all()
        return {"albums": [{"album_id": r.id, "title": r.title, "artist": r.artist_display,
                            "year": r.year, "songs": len(r.tracks),
                            "compilation": bool(r.is_compilation)} for r in rels]}

    def album_songs(self, s, album_id: int) -> dict:
        r = s.get(Release, int(album_id))
        if r is None:
            return {"error": "No such album"}
        return {"title": r.title, "artist": r.artist_display,
                "songs": [_song(t) for t in r.tracks]}

    # -- charts -----------------------------------------------------------

    def list_charts(self, s, query: str = "") -> dict:
        charts = s.scalars(select(Chart).order_by(Chart.name)).all()
        folders = {f.id: f for f in s.scalars(select(ChartFolder))}

        def path(fid):
            parts = []
            while fid is not None and fid in folders and len(parts) < 8:
                parts.append(folders[fid].name)
                fid = folders[fid].parent_id
            return " > ".join(reversed(parts))

        out = []
        for c in charts:
            where = path(c.folder_id)
            if query and query.lower() not in f"{c.name} {where}".lower():
                continue
            first, last, n = s.execute(
                select(func.min(ChartIssue.chart_date), func.max(ChartIssue.chart_date),
                       func.count(ChartIssue.id)).where(ChartIssue.chart_id == c.id)
            ).one()
            if not n:
                continue
            out.append({
                "chart_id": c.id, "name": c.name, "folder": where or None,
                "editions": n,
                "type": "Year End" if n <= (last.year - first.year + 1) else "Weekly",
                "from": str(first), "to": str(last),
            })
        return {"charts": out}

    def chart_songs(
        self, s, chart_id: int, year: Optional[int] = None, date_from: str = "",
        date_to: str = "", max_rank: Optional[int] = None, number_ones_only: bool = False,
        limit: int = 40,
    ) -> dict:
        """Owned songs from a chart, best (peak) position first. For a weekly
        chart over a span, each song appears once at its best position."""
        limit = max(1, min(int(limit or 40), MAX_PLAY))
        chart = s.get(Chart, int(chart_id))
        if chart is None:
            return {"error": "No such chart - call list_charts for ids"}
        cond = [ChartIssue.chart_id == chart.id]
        if year:
            cond.append(ChartIssue.chart_date >= dt.date(int(year), 1, 1))
            cond.append(ChartIssue.chart_date <= dt.date(int(year), 12, 31))
        for value, op in ((date_from, ">="), (date_to, "<=")):
            if value:
                try:
                    d = dt.date.fromisoformat(str(value)[:10])
                except ValueError:
                    return {"error": f"Bad date {value!r} - use YYYY-MM-DD"}
                cond.append(ChartIssue.chart_date >= d if op == ">=" else ChartIssue.chart_date <= d)
        if number_ones_only:
            cond.append(ChartEntry.rank == 1)
        elif max_rank:
            cond.append(ChartEntry.rank <= int(max_rank))
        rows = s.execute(
            select(ChartEntry.track_id, func.min(ChartEntry.rank), func.count(ChartEntry.id),
                   func.min(ChartIssue.chart_date))
            .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
            .where(and_(*cond), ChartEntry.track_id.is_not(None))
            .group_by(ChartEntry.track_id)
            .order_by(func.min(ChartEntry.rank), func.count(ChartEntry.id).desc(),
                      func.min(ChartIssue.chart_date))
            .limit(limit * 2)
        ).all()
        tracks = {t.id: t for t in s.scalars(
            _playable(select(Track).options(selectinload(Track.release))
                      .where(Track.id.in_([r[0] for r in rows])))
        ).unique()}
        out = []
        for tid, best, weeks, first in rows:
            t = tracks.get(tid)
            if t is not None:
                out.append(_song(t, chart_peak=best, editions=weeks, first_charted=str(first)))
        total = s.scalar(
            select(func.count(func.distinct(ChartEntry.title_key)))
            .join(ChartIssue, ChartEntry.issue_id == ChartIssue.id)
            .where(and_(*cond))
        ) or 0
        return {"chart": chart.name, "songs": out[:limit],
                "chart_songs_in_range": total, "owned_in_range": len(out)}

    # -- playlists --------------------------------------------------------

    def list_playlists(self, s, query: str = "") -> dict:
        stmt = select(Playlist).order_by(func.lower(Playlist.name))
        if query:
            stmt = stmt.where(fold_ilike(Playlist.name, _like(query)))
        lists = s.scalars(stmt.limit(80)).all()
        return {"playlists": [{"playlist_id": p.id, "name": p.name, "kind": p.kind,
                               "folder": p.folder.name if p.folder else None} for p in lists]}

    def playlist_songs(self, s, playlist_id: int, limit: int = 100) -> dict:
        from . import playlists as pl_svc

        p = s.get(Playlist, int(playlist_id))
        if p is None:
            return {"error": "No such playlist"}
        tracks = pl_svc.playlist_tracks(s, p.id)[: max(1, min(int(limit or 100), MAX_PLAY))]
        return {"name": p.name, "songs": [_song(t) for t in tracks]}

    # -- library overview (for Claude's system prompt) ---------------------

    def overview(self, s) -> dict:
        songs = s.scalar(_playable(select(func.count(Track.id)))) or 0
        artists = s.scalar(select(func.count(Artist.id))) or 0
        albums = s.scalar(select(func.count(Release.id))) or 0
        y0, y1 = s.execute(select(func.min(Release.year), func.max(Release.year))
                           .where(Release.year > 1800)).one()
        genres = [r[0] for r in s.execute(
            select(Genre.name, func.count()).join(release_genres, release_genres.c.genre_id == Genre.id)
            .group_by(Genre.name).order_by(func.count().desc()).limit(20))]
        styles = [r[0] for r in s.execute(
            select(Style.name, func.count()).join(release_styles, release_styles.c.style_id == Style.id)
            .group_by(Style.name).order_by(func.count().desc()).limit(30))]
        charts = [f"{c['name']} ({c['type']}, {c['from'][:4]}-{c['to'][:4]}, id {c['chart_id']})"
                  for c in self.list_charts(s)["charts"]]
        return {"songs": songs, "artists": artists, "albums": albums,
                "years": f"{y0}-{y1}" if y0 else None, "genres": genres, "styles": styles,
                "charts": charts[:80]}


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SPECS: list[dict] = [
    {"name": "find_artists",
     "description": "Find artists in the library by name, words in their biography/profile, or genre/style. Returns song counts, years active, genres and a profile snippet.",
     "input_schema": {"type": "object", "properties": {
         "query": {**_STR, "description": "name or profile words, e.g. 'outlaw' or 'Nashville'"},
         "genre": {**_STR, "description": "genre or style, e.g. 'Country', 'Doo Wop'"},
         "limit": _INT}}},
    {"name": "artist_profile",
     "description": "One artist's full biography/profile, real name, band members or bands they were in, genres, albums owned and their top songs.",
     "input_schema": {"type": "object", "properties": {"name": _STR}, "required": ["name"]}},
    {"name": "artist_top_songs",
     "description": "An artist's most popular songs (the artist page's Top songs: James's own picks, else Last.fm's chart) that are in the library, best first.",
     "input_schema": {"type": "object", "properties": {"name": _STR, "limit": _INT}, "required": ["name"]}},
    {"name": "similar_artists",
     "description": "Artists similar to this one (Last.fm) that are in the library.",
     "input_schema": {"type": "object", "properties": {"name": _STR}, "required": ["name"]}},
    {"name": "search_songs",
     "description": "Songs in the library filtered by artist, title words, album words, genre/style and album year range, optionally by James's star rating. order: random (default), most_played, top_rated, recently_added, oldest, newest, album_order.",
     "input_schema": {"type": "object", "properties": {
         "artist": _STR, "title": _STR, "album": _STR, "genre": _STR,
         "year_from": _INT, "year_to": _INT, "min_rating": {**_INT, "description": "1-5 stars"},
         "order": {"type": "string", "enum": ["random", "most_played", "top_rated", "recently_added",
                                              "oldest", "newest", "album_order"]},
         "limit": _INT}}},
    {"name": "find_albums",
     "description": "Albums in the library by title words, artist and year range.",
     "input_schema": {"type": "object", "properties": {
         "query": _STR, "artist": _STR, "year_from": _INT, "year_to": _INT, "limit": _INT}}},
    {"name": "album_songs",
     "description": "The songs on one album, in order.",
     "input_schema": {"type": "object", "properties": {"album_id": _INT}, "required": ["album_id"]}},
    {"name": "list_charts",
     "description": "The imported charts (Billboard Hot 100, Hot Country, Year End lists, etc.) with ids, folders, Year End/Weekly and date span. Optional query filters by name/folder.",
     "input_schema": {"type": "object", "properties": {"query": _STR}}},
    {"name": "chart_songs",
     "description": "Songs from one chart that James owns, best chart position first (each song once). Narrow by year or date range (YYYY-MM-DD), top N positions, or number ones only. Also returns how many chart songs exist in that range vs. owned.",
     "input_schema": {"type": "object", "properties": {
         "chart_id": _INT, "year": _INT, "date_from": _STR, "date_to": _STR,
         "max_rank": _INT, "number_ones_only": {"type": "boolean"}, "limit": _INT},
         "required": ["chart_id"]}},
    {"name": "list_playlists",
     "description": "James's playlists (optional name filter).",
     "input_schema": {"type": "object", "properties": {"query": _STR}}},
    {"name": "playlist_songs",
     "description": "The songs in one playlist.",
     "input_schema": {"type": "object", "properties": {"playlist_id": _INT, "limit": _INT},
                      "required": ["playlist_id"]}},
    {"name": "play_songs",
     "description": "FINISH: play these songs on the PC now, replacing the queue. Use only song ids returned by the other tools, in the order to play.",
     "input_schema": {"type": "object", "properties": {
         "song_ids": {"type": "array", "items": _INT},
         "title": {**_STR, "description": "a short name for the selection, e.g. 'Top 20 of 1972'"},
         "message": {**_STR, "description": "one short sentence for the phone about what's playing (mention notable gaps)"},
         "shuffle": {"type": "boolean"}},
         "required": ["song_ids", "title", "message"]}},
    {"name": "say",
     "description": "FINISH without playing anything: a short reply when nothing in the library fits or the request isn't about playing music.",
     "input_schema": {"type": "object", "properties": {"message": _STR}, "required": ["message"]}},
]

_LOOKUPS = {spec["name"] for spec in TOOL_SPECS} - {"play_songs", "say"}

SYSTEM_PROMPT = """You are the DJ inside MusicMgr, James's personal music library app. He speaks a request into his iPhone (dictation, so expect missing punctuation and misheard names) and you choose songs from HIS library to play on the PC.

Rules:
- Only play songs whose ids came back from the tools in this conversation. Never invent ids.
- Look things up before choosing: use artist profiles for questions about who someone is, influences, band members, places or eras; charts for hits, number ones, "top N" and year/date requests; artist_top_songs for "greatest hits"/"best of"; similar_artists for "like X".
- If an exact chart isn't available, say so in the message and use the closest thing (e.g. a Year End list for "1972 hits", or Weekly entries in that year).
- Default to about {default} songs unless he gives a number or the request implies an album/playlist. Max {max}.
- Keep chart order for chart requests and album order for albums; shuffle when he asks for a mix, "some", or a vibe.
- Finish with exactly one play_songs or say call. The message is one short, friendly sentence (it shows on his phone), e.g. "18 of the 20 top hits of 1972 - you don't have #7 or #15."
- Be quick: use as few look-ups as you need.

Today is {today}.
The library: {overview}"""


class AIError(Exception):
    """The AI service answered with an error (or not at all)."""

    def __init__(self, status: int, detail: str = "", provider: str = CLAUDE) -> None:
        super().__init__(f"{PROVIDER_NAMES.get(provider, provider)} API {status}: {detail}")
        self.status = status
        self.detail = detail
        self.provider = provider

    def friendly(self) -> str:
        name = PROVIDER_NAMES.get(self.provider, "The AI")
        detail = self.detail.lower()
        if self.provider == GEMINI:
            if self.status in (401, 403) or "api key" in detail:
                return "The Google AI Studio key in Settings › Phone remote was refused"
            if self.status == 429:
                return "Gemini's free limit is used up for now - try again in a minute"
            if self.status == 404:
                return "That Gemini model isn't available - pick another in Settings › Phone remote"
        else:
            if self.status in (401, 403):
                return "The Anthropic API key in Settings › Phone remote was refused"
            if self.status == 429:
                return "Claude is rate-limiting requests right now"
            if self.status == 400 and "credit" in detail:
                return "The Anthropic account is out of credit"
        if self.status == 529 or self.status >= 500:
            return f"{name} is busy right now"
        return f"{name} couldn't answer ({self.status})"


#: the old name, from when Claude was the only choice
ClaudeError = AIError


def _post(url: str, headers: dict, payload: dict, timeout: float, provider: str) -> dict:
    import requests

    r = requests.post(url, headers={**headers, "content-type": "application/json"},
                      data=json.dumps(payload), timeout=timeout)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = r.text[:200]
        raise AIError(r.status_code, detail, provider)
    return r.json()


def _anthropic_transport(api_key: str, payload: dict, timeout: float) -> dict:
    return _post(API_URL, {"x-api-key": api_key, "anthropic-version": API_VERSION},
                 payload, timeout, CLAUDE)


def _gemini_transport(api_key: str, model: str, payload: dict, timeout: float) -> dict:
    return _post(GEMINI_URL.format(model=model), {"x-goog-api-key": api_key},
                 payload, timeout, GEMINI)


def _jsonable(out: Any) -> Any:
    """Dates and the like as text, so any JSON encoder takes it."""
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


class Asker:
    """Turns a request into an AskResult.

    run_main(fn) runs fn(session) on the main thread with an open session and
    returns its result (the web remote passes its bridge; tests run inline).
    transport(api_key, payload, timeout) -> Claude's response;
    gemini_transport(api_key, model, payload, timeout) -> Gemini's (tests
    fake both).
    """

    def __init__(
        self,
        run_main: Callable[[Callable[[Any], Any]], Any],
        transport: Callable[[str, dict, float], dict] = _anthropic_transport,
        clock: Callable[[], float] = time.monotonic,
        gemini_transport: Callable[[str, str, dict, float], dict] = _gemini_transport,
    ) -> None:
        self.run_main = run_main
        self.transport = transport
        self.gemini_transport = gemini_transport
        self.clock = clock
        self.tools = LibraryTools()

    def ask(self, text: str, prefs: AskPrefs) -> AskResult:
        text = " ".join((text or "").split())
        if not text:
            return AskResult(message="Say or type what you'd like to hear")
        provider = prefs.provider
        name = PROVIDER_NAMES[provider]
        if not prefs.active_key:
            result = self._offline(text)
            if not result.track_ids:
                result.message += (
                    " (Only simple requests work offline - add a free Google AI Studio key"
                    " in Settings › Phone remote for anything you can say.)"
                    if provider == GEMINI else
                    " (Only simple requests work offline - add an Anthropic API key in"
                    " Settings › Phone remote for anything you can say.)")
            return result
        try:
            if provider == GEMINI:
                return self._ask_gemini(text, prefs)
            return self._ask_claude(text, prefs)
        except (AIError, OSError, ValueError, KeyError, TimeoutError) as exc:
            log.warning("Ask: %s failed (%s) - falling back to offline", name, exc)
            reason = exc.friendly() if isinstance(exc, AIError) else (
                f"{name} took too long" if isinstance(exc, TimeoutError) else f"Couldn't reach {name}")
            return self._offline(text, reason)
        except Exception as exc:  # requests' own exceptions, mostly
            if type(exc).__module__.startswith("requests"):
                log.warning("Ask: %s unreachable (%s) - falling back to offline", name, exc)
                return self._offline(text, f"Couldn't reach {name}")
            raise

    def _offline(self, text: str, reason: str = "") -> AskResult:
        result = self.run_main(lambda s: offline_ask(s, self.tools, text))
        if reason:
            result.message = f"{reason} - {result.message[:1].lower()}{result.message[1:]}"
        return result

    def _system(self) -> str:
        overview = self.run_main(self.tools.overview)
        return SYSTEM_PROMPT.format(
            default=DEFAULT_PLAY, max=MAX_PLAY, today=dt.date.today().isoformat(),
            overview=json.dumps(overview, ensure_ascii=False),
        )

    def _call(self, name: str, args: dict, seen_ids: set[int], engine: str):
        """One tool call: (AskResult, None) when it finishes the request,
        else (None, the look-up's data)."""
        if name == "play_songs":
            return self._finish_play(args, seen_ids, engine), None
        if name == "say":
            return AskResult(message=str(args.get("message") or ""), engine=engine), None
        out = _jsonable(self._run_tool(name, args))
        seen_ids.update(_ids_in(out))
        return None, out

    def _remaining(self, started: float) -> float:
        remaining = TIME_BUDGET_S - (self.clock() - started)
        if remaining <= 2:
            raise TimeoutError("The AI took too long")
        return min(HTTP_TIMEOUT_S, remaining)

    # -- Claude's tool-use loop ---------------------------------------------

    def _ask_claude(self, text: str, prefs: AskPrefs) -> AskResult:
        system = self._system()
        messages: list[dict] = [{"role": "user", "content": text}]
        seen_ids: set[int] = set()
        started = self.clock()
        for _round in range(MAX_ROUNDS):
            timeout = self._remaining(started)
            payload = {
                "model": prefs.model or DEFAULT_MODEL,
                "max_tokens": 2048,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "tools": TOOL_SPECS,
                "messages": messages,
            }
            reply = self.transport(prefs.api_key, payload, timeout)
            content = reply.get("content") or []
            messages.append({"role": "assistant", "content": content})
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                said = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                return AskResult(message=said or "I couldn't work out what to play", engine=CLAUDE)
            results = []
            for use in uses:
                done, out = self._call(use.get("name"), use.get("input") or {}, seen_ids, CLAUDE)
                if done is not None:
                    return done
                results.append({"type": "tool_result", "tool_use_id": use.get("id"),
                                "content": json.dumps(out, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        raise TimeoutError("Claude took too long")

    # -- Gemini's function-calling loop -------------------------------------

    def _ask_gemini(self, text: str, prefs: AskPrefs) -> AskResult:
        system = self._system()
        tools = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
            for t in TOOL_SPECS]}]
        contents: list[dict] = [{"role": "user", "parts": [{"text": text}]}]
        seen_ids: set[int] = set()
        started = self.clock()
        for _round in range(MAX_ROUNDS):
            timeout = self._remaining(started)
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "tools": tools,
                "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                "generationConfig": {"maxOutputTokens": 2048},
            }
            reply = self.gemini_transport(prefs.gemini_key, prefs.model, payload, timeout)
            candidates = reply.get("candidates") or []
            if not candidates:
                reason = (reply.get("promptFeedback") or {}).get("blockReason", "no answer")
                raise AIError(400, f"Gemini returned nothing ({reason})", GEMINI)
            # sent back exactly as it came - newer Gemini models attach a
            # thought signature to their calls that must come back with them
            content = candidates[0].get("content") or {}
            content.setdefault("role", "model")
            parts = content.get("parts") or []
            contents.append(content)
            calls = [p["functionCall"] for p in parts if isinstance(p.get("functionCall"), dict)]
            if not calls:
                said = " ".join(p.get("text", "") for p in parts
                                if p.get("text") and not p.get("thought")).strip()
                return AskResult(message=said or "I couldn't work out what to play", engine=GEMINI)
            responses = []
            for call in calls:
                name = call.get("name")
                done, out = self._call(name, call.get("args") or {}, seen_ids, GEMINI)
                if done is not None:
                    return done
                response = {"name": name, "response": {"result": out}}
                if call.get("id"):
                    response["id"] = call["id"]
                responses.append({"functionResponse": response})
            contents.append({"role": "user", "parts": responses})
        raise TimeoutError("Gemini took too long")

    # -- shared --------------------------------------------------------------

    def _run_tool(self, name: str, args: dict) -> dict:
        if name not in _LOOKUPS:
            return {"error": f"Unknown tool {name}"}
        method = getattr(self.tools, name)
        try:
            return self.run_main(lambda s: method(s, **args))
        except TypeError as exc:
            return {"error": f"Bad arguments: {exc}"}
        except (ValueError, LookupError) as exc:
            return {"error": str(exc)}

    def _finish_play(self, args: dict, seen_ids: set[int], engine: str = CLAUDE) -> AskResult:
        ids = []
        for raw in args.get("song_ids") or []:
            try:
                i = int(raw)
            except (TypeError, ValueError):
                continue
            if i in seen_ids and i not in ids:
                ids.append(i)
        return AskResult(
            track_ids=ids[:MAX_PLAY],
            title=str(args.get("title") or "")[:120],
            message=str(args.get("message") or "")[:400] if ids else "I couldn't find any of those in the library",
            shuffle=bool(args.get("shuffle")),
            engine=engine,
        )


def _ids_in(out: Any) -> set[int]:
    """Every song id a tool handed back - the only ids play_songs may use."""
    found: set[int] = set()
    if isinstance(out, dict):
        for song in out.get("songs") or []:
            if isinstance(song, dict) and isinstance(song.get("id"), int):
                found.add(song["id"])
    return found


# --------------------------------------------------------------------------
# offline: the plain shapes, no internet needed
# --------------------------------------------------------------------------

_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty five": 25,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy five": 75, "hundred": 100,
    "a hundred": 100, "one hundred": 100,
}
_LEAD = re.compile(r"^(?:hey\s+)?(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?"
                   r"(?:play|put on|queue up|queue|i want to hear|i'd like to hear|let'?s hear|give me|shuffle)\s+(?:me\s+)?(?:some\s+)?",
                   re.IGNORECASE)


def _year(text: str) -> Optional[int]:
    m = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(?:of|from|in)\s+'?([0-9]{2})\b(?!'?s)", text)
    if m:
        n = int(m.group(1))
        return 1900 + n if n > 30 else 2000 + n
    return None


def _decade(text: str) -> Optional[tuple[int, int]]:
    m = re.search(r"\b(?:the\s+)?(?:19)?([2-9]0)'?s\b", text)
    if not m:
        return None
    start = 1900 + int(m.group(1))
    return start, start + 9


def _count(text: str) -> Optional[int]:
    m = re.search(r"\btop\s+(\d{1,3})\b", text) or re.search(r"\b(\d{1,3})\s+(?:songs|hits|tracks)\b", text)
    if m:
        return int(m.group(1))
    for word, n in sorted(_NUMBERS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\btop\s+{word}\b|\b{word}\s+(?:songs|hits|tracks)\b", text):
            return n
    return None


def _pick_chart(tools: LibraryTools, s, text: str, year: Optional[int]) -> Optional[dict]:
    charts = tools.list_charts(s)["charts"]
    if year:
        charts = [c for c in charts if c["from"][:4] <= str(year) <= c["to"][:4]]
    if not charts:
        return None
    words = set(re.findall(r"[a-z0-9]+", text.lower()))

    def score(c):
        name = f"{c['name']} {c['folder'] or ''}".lower()
        hits = sum(1 for w in words if len(w) > 2 and w in name)
        prefer_ye = 1 if (c["type"] == "Year End") == ("number" not in text and "week" not in text) else 0
        hot100 = 1 if "hot 100" in name else 0
        return (hits, prefer_ye, hot100)

    return max(charts, key=score)


def offline_ask(s, tools: LibraryTools, text: str) -> AskResult:
    raw = text.strip()
    low = raw.lower().rstrip(".!?")
    body = _LEAD.sub("", low).strip()
    shuffle = low.startswith("shuffle") or " shuffle" in low or body.startswith(("some ", "a mix"))

    def done(songs, title, message, shuffle_=shuffle):
        ids = [x["id"] for x in songs]
        if not ids:
            return AskResult(message=message if not songs else "", engine="offline")
        return AskResult(track_ids=ids, title=title, message=message, shuffle=shuffle_, engine="offline")

    # playlists: "playlist road trip", "my road trip playlist"
    m = re.match(r"^(?:the\s+|my\s+)?playlist\s+(.+)$", body) or re.match(r"^(?:the\s+|my\s+)?(.+?)\s+playlist$", body)
    if m:
        lists = tools.list_playlists(s, m.group(1))["playlists"]
        if lists:
            p = lists[0]
            songs = tools.playlist_songs(s, p["playlist_id"], MAX_PLAY)["songs"]
            return done(songs, p["name"], f"Playing your playlist {p['name']}")
        return AskResult(message=f"No playlist called “{m.group(1)}”")

    # charts: "top 20 of 1972", "number ones from 1965", "the hits of 1980"
    year = _year(body)
    if year and re.search(r"\b(top|hits?|number ones?|#\s?1s?|no\.?\s?1s?|chart|billboard|countdown)\b", body):
        chart = _pick_chart(tools, s, body, year)
        if chart is None:
            return AskResult(message=f"There's no chart for {year} in the library")
        n = _count(body) or (None if re.search(r"number ones?|#\s?1|no\.?\s?1", body) else 40)
        ones = bool(re.search(r"number ones?|#\s?1s?\b|no\.?\s?1s?\b", body))
        got = tools.chart_songs(s, chart["chart_id"], year=year, max_rank=n,
                                number_ones_only=ones, limit=n or 60)
        songs = got.get("songs") or []
        label = f"Number ones of {year}" if ones else f"Top {n} of {year}"
        if not songs:
            return AskResult(message=f"You don't have any of the {label.lower()} ({chart['name']})")
        return done(songs, label, f"{len(songs)} songs from {chart['name']}, {year}", shuffle_=False)

    # "the album hysteria", "hysteria album"
    m = (re.match(r"^(?:the\s+)?album\s+(?P<t>.+?)(?:\s+by\s+(?P<a>.+))?$", body)
         or re.match(r"^(?:the\s+)?(?P<t>.+?)\s+(?:album|lp|record)(?:\s+by\s+(?P<a>.+))?$", body))
    if m:
        albums = tools.find_albums(s, m.group("t"), artist=m.group("a") or "")["albums"]
        albums = [a for a in albums if not a["compilation"]] or albums
        if albums:
            a = albums[0]
            songs = tools.album_songs(s, a["album_id"])["songs"]
            return done(songs, a["title"], f"Playing {a['title']} by {a['artist']}", shuffle_=False)

    # "<song> by <artist>"
    m = re.match(r"^(.+?)\s+by\s+(.+)$", body)
    if m:
        song, who = m.group(1).strip(), m.group(2).strip()
        if re.match(r"^(?:songs|music|hits|stuff|something|anything|the hits|greatest hits)$", song):
            body = who  # "songs by elvis" -> the artist branch below
        else:
            songs = tools.search_songs(s, artist=who, title=song, order="most_played", limit=1)["songs"]
            if songs:
                return done(songs, songs[0]["title"], f"Playing {songs[0]['title']} by {songs[0]['artist']}",
                            shuffle_=False)

    # "elvis", "greatest hits of elvis", "elvis from the 50s"
    m = re.match(r"^(?:the\s+)?(?:greatest hits|best|top songs|hits)\s+(?:of|by|from)\s+(.+)$", body)
    who = (m.group(1) if m else body).strip()
    decade = _decade(who)
    who_clean = re.sub(r"\s+(?:from|in)\s+(?:the\s+)?(?:19)?[2-9]0'?s\b.*$", "", who).strip()
    artist = _find_artist(s, who_clean) if who_clean else None
    if artist is not None:
        if m:
            top = tools.artist_top_songs(s, artist.name, 25)["songs"]
            if top:
                return done(top, f"{artist.name}: greatest hits", f"{artist.name}'s top songs", shuffle_=False)
        y0, y1 = decade if decade else (None, None)
        songs = tools.search_songs(s, artist=artist.name, year_from=y0, year_to=y1, limit=DEFAULT_PLAY * 2)["songs"]
        return done(songs, artist.name, f"A mix of {artist.name}", shuffle_=True)

    # "country from the 70s", "doo wop"
    genre_text = re.sub(r"\s+(?:from|in)\s+(?:the\s+)?(?:19)?[2-9]0'?s\b.*$", "", body).strip()
    genre_text = re.sub(r"\b(?:music|songs|hits)\b", "", genre_text).strip()
    if genre_text:
        y0, y1 = decade if decade else (year, year) if year else (None, None)
        songs = tools.search_songs(s, genre=genre_text, year_from=y0, year_to=y1, limit=DEFAULT_PLAY)["songs"]
        if songs:
            return done(songs, genre_text.title(), f"A mix of {genre_text}", shuffle_=True)
        songs = tools.search_songs(s, title=genre_text, limit=DEFAULT_PLAY)["songs"]
        if songs:
            return done(songs, genre_text.title(), f"Songs matching “{genre_text}”", shuffle_=False)

    return AskResult(message=f"I couldn't find anything for “{raw}”")
