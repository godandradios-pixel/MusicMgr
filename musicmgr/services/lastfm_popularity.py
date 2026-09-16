"""Fetch an artist's real Last.fm "top tracks" chart - see `ArtistTopTrack`
in db/models.py.

2026-09-16 same-day follow-up #2 - this module replaces a same-day first
attempt, spotify_popularity.py, which used Spotify's `GET /v1/artists/{id}/
top-tracks` endpoint instead. James went to actually set that up and found
Spotify's Developer Mode now requires the app owner to hold a Premium
subscription - and worse, a February 2026 Spotify change removed the
artist-top-tracks endpoint entirely for Development Mode apps, with no
replacement short of "Extended Quota Mode" (an approval process meant for
apps serving many outside users, not a personal single-user app like this
one). Spotify was a dead end regardless of the Premium question, so this
picks Last.fm's `artist.getTopTracks` instead: a free API key is all it
needs (no OAuth, no user login, no Premium anything - see
last.fm/api/account/create), and the response is Last.fm's own ranked
"top tracks" for the artist, each with a real `playcount`/`listeners`
figure - the same kind of authoritative outside signal James was after,
just from a source that's actually still reachable by a hobby app.

Same "read from local files, no network calls" exception the app already
made for lyrics (LRCLIB) and biographies (Wikipedia) - see
lyrics_downloader.py's module docstring for the precedent. The one API key
is stored in the existing generic `Setting` key/value table (see
services/jukebox.py for the only other user of that table) rather than a
new one built just for it - simpler than spotify_popularity.py's
client-id/secret pair, since Last.fm's key-only auth needs just the one
value.

2026-09-16 follow-up #4 (James: "I don't want the list to be constrained
by only copies I own, I want the top songs to be their top songs whether
they are in the library or not") - this module used to also match each
Last.fm track against the artist's locally-owned tracks (by normalized
title) and stamp a `Track.lastfm_popularity` score directly onto whichever
pressings matched. That meant the artist page's Top Tracks list could only
ever show songs James already owned - it wasn't really "this artist's top
tracks", just "this artist's top tracks, restricted to what's on your
drive". `update_popularity_for_artist` below now simply stores Last.fm's
whole returned chart as `ArtistTopTrack` rows (replacing whatever was
there from a previous fetch) - no ownership matching happens here at all
anymore. Whether a given entry is something James owns, and which pressing
represents it, is worked out later, at display time, by
ui/widgets/artist_panel.py:_refresh_top_tracks - see that function and
ArtistTopTrack's own docstring for why doing it there (rather than here,
once, at fetch time) means adding a missing song to the library is
recognized immediately, with no new fetch needed.

Unlike the Spotify attempt, there's no artist-id search/resolution step at
all: `artist.getTopTracks` takes the artist's name directly (with
`autocorrect=1` to paper over minor spelling differences), so there's
nothing to cache - no `Artist.lastfm_id` column exists or is needed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import requests
from sqlalchemy import delete, select

from ..db.models import Artist, ArtistTopTrack, Setting
from ..db.session import session_scope

log = logging.getLogger(__name__)

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

#: Setting.key row this module owns - see db.models.Setting and
#: services/jukebox.py's _NEXT_SLOT_NUMBER_KEY for the one other user of
#: that generic key/value table. Just one key (unlike spotify_popularity.py's
#: client id/secret pair) - Last.fm's API auth is a single API key.
API_KEY_KEY = "lastfm_api_key"

#: light, polite pacing between artists in a bulk run - same default the
#: Spotify attempt used; Last.fm has no officially documented per-second
#: cap, but there's no reason to hammer it either.
DEFAULT_RATE_LIMIT_S = 0.2

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5

#: Last.fm error codes that mean "your API key is bad", as opposed to
#: "this artist/request didn't turn up anything" - see
#: last.fm/api/errorcodes. Anything else in the "error" field (including an
#: artist Last.fm doesn't recognize) is treated as not_found further down,
#: rather than trying to enumerate every non-auth error code by hand.
_AUTH_ERROR_CODES = {10, 26}


@dataclass
class ArtistPopularityOutcome:
    artist_id: int
    artist_name: str
    #: one of: updated, not_found, not_configured, auth_error, error
    status: str
    detail: str = ""
    #: how many ArtistTopTrack rows this fetch stored - independent of how
    #: many of them James actually owns (see module docstring)
    stored: int = 0


@dataclass
class PopularityResult:
    updated: int = 0
    not_found: int = 0
    not_configured: int = 0
    auth_errors: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: list[ArtistPopularityOutcome] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.not_found:
            parts.append(f"{self.not_found} not found on Last.fm")
        if self.not_configured:
            parts.append("Last.fm API key not set up yet")
        if self.auth_errors:
            parts.append("Last.fm rejected that API key")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " · ".join(parts) if parts else "Nothing to do"


# -- credentials (Setting table) -------------------------------------------


def get_api_key(db) -> Optional[str]:
    row = db.get(Setting, API_KEY_KEY)
    return row.value if row else None


def set_api_key(db, api_key: str) -> None:
    row = db.get(Setting, API_KEY_KEY)
    if row is None:
        row = Setting(key=API_KEY_KEY, value=api_key)
        db.add(row)
    else:
        row.value = api_key


def has_api_key() -> bool:
    with session_scope() as db:
        return bool(get_api_key(db))


# -- Last.fm request/retry, the same shape as the app's other network
# integrations (lyrics_downloader._lrclib_request, artist_bio_downloader.
# _wiki_request, and the Spotify attempt's own _spotify_request) ---------


def _lastfm_request(http: requests.Session, params: dict):
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = http.get(LASTFM_API_URL, params=params, timeout=20)
            if response.status_code in _RETRY_STATUSES:
                if attempt >= _MAX_RETRIES:
                    log.warning(
                        "Last.fm: HTTP %s after %d attempts", response.status_code, _MAX_RETRIES
                    )
                    return None
                time.sleep(2**attempt)
                continue
            return response
        except requests.exceptions.Timeout:
            if attempt >= _MAX_RETRIES:
                log.warning("Last.fm: request timed out after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.exceptions.ConnectionError:
            if attempt >= _MAX_RETRIES:
                log.warning("Last.fm: connection failed after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.RequestException as exc:
            log.warning("Last.fm: %s", exc)
            return None
    return None


def _fetch_top_tracks(
    http: requests.Session, api_key: str, artist_name: str
) -> tuple[list[dict], Optional[str]]:
    """Returns (tracks, error_status) - error_status is None on success,
    else one of "auth_error"/"not_found" (see _AUTH_ERROR_CODES above)."""
    response = _lastfm_request(
        http,
        {
            "method": "artist.gettoptracks",
            "artist": artist_name,
            "autocorrect": 1,
            "api_key": api_key,
            "format": "json",
        },
    )
    if response is None:
        return [], "not_found"
    try:
        payload = response.json()
    except ValueError:
        return [], "not_found"

    error_code = payload.get("error")
    if error_code is not None:
        status = "auth_error" if error_code in _AUTH_ERROR_CODES else "not_found"
        return [], status

    tracks = ((payload.get("toptracks") or {}).get("track")) or []
    # a single result comes back as a bare object rather than a one-item
    # list - same quirk this kind of loosely-typed XML-turned-JSON API
    # tends to have across the board, guard it rather than assume a list
    if isinstance(tracks, dict):
        tracks = [tracks]
    return tracks, None


# -- public API ---------------------------------------------------------------


def update_popularity_for_artist(
    http: requests.Session, api_key: str, db, artist: Artist
) -> ArtistPopularityOutcome:
    """Fetch this artist's Last.fm top tracks and store the whole chart as
    `ArtistTopTrack` rows (replacing whatever this artist had from a
    previous fetch) - see that model's own docstring for why there's no
    ownership matching here at all anymore, and why there's no artist-id
    lookup/caching step the way the Spotify attempt needed."""
    top_tracks, error_status = _fetch_top_tracks(http, api_key, artist.name)
    if error_status is not None:
        return ArtistPopularityOutcome(artist.id, artist.name, error_status)
    if not top_tracks:
        return ArtistPopularityOutcome(artist.id, artist.name, "not_found")

    db.execute(delete(ArtistTopTrack).where(ArtistTopTrack.artist_id == artist.id))

    stored = 0
    for i, t in enumerate(top_tracks, start=1):
        name = (t.get("name") or "").strip()
        if not name:
            continue
        try:
            playcount = int(t.get("playcount") or 0)
        except (TypeError, ValueError):
            playcount = None
        db.add(ArtistTopTrack(artist_id=artist.id, rank=i, title=name, playcount=playcount))
        stored += 1

    if stored == 0:
        return ArtistPopularityOutcome(artist.id, artist.name, "not_found")
    return ArtistPopularityOutcome(artist.id, artist.name, "updated", stored=stored)


def update_popularity_for_artists(
    artist_ids: Sequence[int],
    *,
    rate_limit_s: float = DEFAULT_RATE_LIMIT_S,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> PopularityResult:
    """Update Last.fm popularity for a list of artist ids - used both by
    Settings' bulk "Update track popularity from Last.fm…" button and, as a
    batch of one, the artist page's own per-artist "Fetch popularity"
    button (see PopularityDownloadThread in ui/widgets/common.py)."""
    result = PopularityResult()
    total = len(artist_ids)

    with session_scope() as db:
        api_key = get_api_key(db)
    if not api_key:
        result.not_configured = total
        result.outcomes = [
            ArtistPopularityOutcome(aid, "", "not_configured") for aid in artist_ids
        ]
        if progress:
            progress(total, total, "")
        return result

    http = requests.Session()

    with session_scope() as db:
        for i, artist_id in enumerate(artist_ids, start=1):
            artist = db.get(Artist, artist_id)
            if artist is None:
                continue

            if progress:
                progress(i - 1, total, artist.name)

            try:
                outcome = update_popularity_for_artist(http, api_key, db, artist)
            except Exception as exc:  # one bad artist must not stop a bulk run
                log.exception("popularity lookup failed for %s", artist.name)
                outcome = ArtistPopularityOutcome(artist.id, artist.name, "error", str(exc))

            result.outcomes.append(outcome)
            db.commit()

            if outcome.status == "updated":
                result.updated += 1
            elif outcome.status == "not_found":
                result.not_found += 1
            elif outcome.status == "auth_error":
                result.auth_errors += 1
            elif outcome.status == "error":
                result.errors.append(f"{artist.name}: {outcome.detail}")

            if i < total:
                time.sleep(rate_limit_s)

    if progress:
        progress(total, total, "")
    return result


def all_artist_ids_with_releases() -> list[int]:
    """Every artist that's the album artist for at least one release -
    what Settings' bulk "Update track popularity from Last.fm…" button
    targets. Unlike artists_missing_bio (services/artist_bio_downloader.py
    - fetch-once-then-done), this isn't filtered down to "missing" artists:
    popularity is meant to be refreshed periodically as it changes, not
    fetched once and left alone."""
    with session_scope() as db:
        stmt = select(Artist.id).where(Artist.album_releases.any())
        return [row[0] for row in db.execute(stmt).all()]
