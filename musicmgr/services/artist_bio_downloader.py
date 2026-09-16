"""Download a short artist biography from Wikipedia's public REST summary
API and save it to `Artist.profile` - the same free-text column
`ui/widgets/bio_panel.py` already knew how to display but that, until this
2026-09-16 follow-up, had no importer wired up to ever fill it in.

2026-09-16 follow-up - James, looking at a mocked-up "artist profile" style
page (photo + a short writeup + a top-tracks list): "Add a new menu option
called artist profile. Display the artist image, collect a brief writeup
of the artist and include a list of their top tracks in order of
popularity." Asked where the writeup itself should come from (there's
nothing resembling one anywhere in this local-files-only app), and where
it should be reachable from, James chose: a Settings bulk action ("like
lyrics" - see services/lyrics_downloader.py, the app's one other network
exception) for the whole library at once, plus a per-artist "fetch from
web" button right on the artist page itself for just that one artist - see
`ui/widgets/bio_panel.py`'s own follow-up note and `BioDownloadThread` in
`ui/widgets/common.py`, both shared by the two.

This is the same deliberate, requested exception to the rest of the app's
"read from local files, no network calls" stance that lyrics_downloader.py
already carved out, for the same reason: there is simply no local source
for a band biography, the way there is for tags/audio metadata. Unlike a
downloaded lyric (a standalone .lrc sidecar file, no database involved),
a fetched bio *is* a database write - `Artist.profile` - so every function
below takes (or opens) a real db session rather than staying DB-free the
way lyrics_downloader.py could.

Source and attribution: Wikipedia's REST `page/summary` endpoint returns a
short, plain-text lead-paragraph extract for a page (exactly the kind of
"brief writeup" the mockup showed) under Wikipedia's CC BY-SA license,
which requires attribution - so a successful fetch also appends that
page's canonical URL to `Artist.urls` (already a schema field, "newline
separated" - see db/models.py) as a source citation, alongside the text
itself.

Artist names are frequently ambiguous on Wikipedia (a band's plain name is
often a disambiguation page - "Rush", "Queen", "Kiss" all are), so
`_find_summary` below tries a short list of likely disambiguated titles
before falling back to Wikipedia's own search API and checking the top
result - ported down to the essentials from the same kind of
exact-then-fuzzy chain lyrics_downloader.py's `_find_lyrics` already uses
for the same reason (LRCLIB's catalogue is just as ambiguous by title).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import requests
from sqlalchemy import select

from ..db.models import Artist
from ..db.session import session_scope

log = logging.getLogger(__name__)

WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKI_SEARCH_URL = "https://en.wikipedia.org/w/api.php"

#: seconds between Wikipedia requests - polite pacing for a free public API,
#: the same self-imposed courtesy lyrics_downloader.py applies to LRCLIB
DEFAULT_RATE_LIMIT_S = 0.5

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5

_HEADERS = {
    "User-Agent": "MusicMgr-Bio-Downloader/1.0 (local desktop music library app)",
    "Accept": "application/json",
}

#: tried in order against the exact artist name before falling back to a
#: real search - covers the common "the plain name is a disambiguation
#: page" case without a network round trip for every candidate
_DISAMBIGUATION_SUFFIXES = ["", " (band)", " (musician)", " (singer)", " (rapper)"]


@dataclass
class ArtistBioOutcome:
    artist_id: int
    artist_name: str
    #: one of: downloaded, already_exists, not_found, error
    status: str
    detail: str = ""


@dataclass
class BioDownloadResult:
    downloaded: int = 0
    already_exists: int = 0
    not_found: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: list[ArtistBioOutcome] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.downloaded:
            parts.append(f"{self.downloaded} downloaded")
        if self.already_exists:
            parts.append(f"{self.already_exists} already had a biography")
        if self.not_found:
            parts.append(f"{self.not_found} not found")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " · ".join(parts) if parts else "Nothing to do"


# -- Wikipedia request/retry, the same shape as lyrics_downloader's --------


def _wiki_request(session: requests.Session, url: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET a Wikipedia endpoint with retry handling: 429/5xx and
    timeouts/connection errors are retried with backoff; 404 means "no
    such page", not an error - mirrors lyrics_downloader._lrclib_request."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=20)

            if response.status_code == 404:
                return None

            if response.status_code in _RETRY_STATUSES:
                if attempt >= _MAX_RETRIES:
                    log.warning(
                        "Wikipedia: HTTP %s after %d attempts", response.status_code, _MAX_RETRIES
                    )
                    return None
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    wait_seconds = 2**attempt
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            if attempt >= _MAX_RETRIES:
                log.warning("Wikipedia: request timed out after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.exceptions.ConnectionError:
            if attempt >= _MAX_RETRIES:
                log.warning("Wikipedia: connection failed after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.RequestException as exc:
            log.warning("Wikipedia: %s", exc)
            return None

    return None


def _is_usable(summary: Optional[dict]) -> bool:
    return bool(summary) and summary.get("type") != "disambiguation" and bool(summary.get("extract"))


def _fetch_summary(session: requests.Session, title: str) -> Optional[dict]:
    url = WIKI_SUMMARY_URL.format(title=requests.utils.quote(title.replace(" ", "_")))
    return _wiki_request(session, url)


def _search_titles(session: requests.Session, query: str, limit: int = 5) -> list[str]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "format": "json",
    }
    result = _wiki_request(session, WIKI_SEARCH_URL, params)
    if not result:
        return []
    hits = result.get("query", {}).get("search", [])
    return [h["title"] for h in hits if h.get("title")]


def _find_summary(session: requests.Session, artist_name: str) -> Optional[dict]:
    """Exact-title candidates first (cheap, usually right), then a real
    search as fallback - see the module docstring for why plain artist
    names collide with disambiguation pages so often."""
    for suffix in _DISAMBIGUATION_SUFFIXES:
        summary = _fetch_summary(session, artist_name + suffix)
        if _is_usable(summary):
            return summary

    for title in _search_titles(session, f"{artist_name} band musician"):
        summary = _fetch_summary(session, title)
        if _is_usable(summary):
            return summary

    return None


def _clean_extract(text: str) -> str:
    # Wikipedia extracts occasionally carry stray reference marker
    # leftovers ("[1]", "[citation needed]") even in the plain-text
    # summary - strip them rather than showing them in BioPanel.
    text = re.sub(r"\[[^\]]*\]", "", text)
    return " ".join(text.split()).strip()


def _add_source_url(artist: Artist, url: str) -> None:
    existing = (artist.urls or "").splitlines()
    if url not in existing:
        existing.append(url)
        artist.urls = "\n".join(line for line in existing if line.strip())


# -- public API ---------------------------------------------------------------


def download_bio_for_artist(
    http: requests.Session, db, artist: Artist, *, overwrite: bool = False
) -> ArtistBioOutcome:
    """Look up and save one artist's biography. `db` is an open SQLAlchemy
    session the caller commits (or rolls back) - this function only sets
    attributes on the given `artist`, already attached to that session."""
    if artist.profile and not overwrite:
        return ArtistBioOutcome(artist.id, artist.name, "already_exists")

    try:
        summary = _find_summary(http, artist.name)
    except Exception as exc:  # one bad artist must not stop a bulk run
        log.exception("bio lookup failed for %s", artist.name)
        return ArtistBioOutcome(artist.id, artist.name, "error", str(exc))

    if not summary:
        return ArtistBioOutcome(artist.id, artist.name, "not_found")

    extract = _clean_extract(summary.get("extract") or "")
    if not extract:
        return ArtistBioOutcome(artist.id, artist.name, "not_found")

    artist.profile = extract
    source_url = (summary.get("content_urls") or {}).get("desktop", {}).get("page")
    if source_url:
        _add_source_url(artist, source_url)

    return ArtistBioOutcome(artist.id, artist.name, "downloaded", source_url or "")


def download_bios_for_artists(
    artist_ids: Sequence[int],
    *,
    overwrite: bool = False,
    rate_limit_s: float = DEFAULT_RATE_LIMIT_S,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> BioDownloadResult:
    """Download biographies for a list of artist ids, one Wikipedia lookup
    per artist - used both by Settings' bulk "Download artist profiles…"
    button and, as a batch of one, the artist page's own per-artist "Fetch
    bio" button (see BioDownloadThread in ui/widgets/common.py).

    Opens its own db session (unlike lyrics_downloader, which never touches
    the database at all) and commits after each artist rather than only
    once at the end, so a long bulk run over hundreds of artists isn't
    all-or-nothing if it's interrupted partway through.
    """
    result = BioDownloadResult()
    http = requests.Session()
    http.headers.update(_HEADERS)

    total = len(artist_ids)
    with session_scope() as db:
        for i, artist_id in enumerate(artist_ids, start=1):
            artist = db.get(Artist, artist_id)
            if artist is None:
                continue

            if progress:
                progress(i - 1, total, artist.name)

            outcome = download_bio_for_artist(http, db, artist, overwrite=overwrite)
            result.outcomes.append(outcome)
            db.commit()

            if outcome.status == "downloaded":
                result.downloaded += 1
            elif outcome.status == "already_exists":
                result.already_exists += 1
            elif outcome.status == "not_found":
                result.not_found += 1
            elif outcome.status == "error":
                result.errors.append(f"{artist.name}: {outcome.detail}")

            # only pace ourselves when a request actually went out
            if outcome.status not in ("already_exists",) and i < total:
                time.sleep(rate_limit_s)

    if progress:
        progress(total, total, "")
    return result


def artists_missing_bio(limit: Optional[int] = None) -> list[int]:
    """Every artist id with no biography saved yet - what Settings' bulk
    "Download artist profiles…" button targets by default (overwrite=False
    would just skip these same rows one network round trip at a time
    otherwise, so filtering up front here saves the wasted lookups)."""
    with session_scope() as db:
        stmt = select(Artist.id).where(
            (Artist.profile.is_(None)) | (Artist.profile == "")
        )
        if limit:
            stmt = stmt.limit(limit)
        return [row[0] for row in db.execute(stmt).all()]
