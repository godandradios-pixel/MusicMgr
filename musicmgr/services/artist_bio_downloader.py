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

#: seconds between Wikipedia requests - polite pacing for a free public API.
#: 2026-10-13 (James: "can you speed that up, no reason it should take that
#: long hitting wikipedia"): was 0.5 s after every *artist*, on top of up to
#: a dozen one-title requests each. The bulk run now asks about many
#: artists per request (see _prefetch_summaries), one request at a time as
#: Wikipedia's API etiquette asks, so only a short gap is kept.
DEFAULT_RATE_LIMIT_S = 0.1

#: artists looked up per batched request: 8 artists x 6 candidate titles
#: stays under the action API's 50-titles-per-request limit
BATCH_ARTISTS = 8

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5

_HEADERS = {
    "User-Agent": "MusicMgr-Bio-Downloader/1.0 (local desktop music library app)",
    "Accept": "application/json",
}

#: tried in order against the exact artist name before falling back to a
#: real search - covers the common "the plain name is a disambiguation
#: page" case without a network round trip for every candidate.
#:
#: 2026-10-13 - James: "please include (band) to get a better match. For
#: example, the profile return on Boston was the city and not the band.
#: Similarly the search on the band .38 Special returned, the pistol." The
#: plain name used to be tried first, and "Boston" and ".38 Special" are
#: real articles (a city, a cartridge), not disambiguation pages - so they
#: were taken. Now the music-specific titles come first, and the plain
#: name (and any search hit) is only taken when the article is about a
#: musical act - see _is_music.
_MUSIC_SUFFIXES = [" (band)", " (musician)", " (singer)", " (rapper)", " (group)"]

#: words a music article's short description or opening uses ("American
#: rock band", "English singer-songwriter", "hip hop duo") - a city,
#: a cartridge or a ship has none of them
_MUSIC_WORDS = re.compile(
    r"\b(band|bands|singer|singers|singer-songwriter|songwriter|musician|musicians|"
    r"rapper|vocalist|guitarist|bassist|drummer|pianist|keyboardist|organist|"
    r"fiddler|composer|bandleader|crooner|yodeler|duo|trio|quartet|quintet|"
    r"orchestra|choir|musical|music|album|albums|recording artist|record producer|"
    r"(?:vocal|musical|girl|boy|gospel|rock|pop|doo-wop) group|rock and roll|"
    r"hip hop|rhythm and blues)\b",
    re.IGNORECASE,
)


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


def _is_music(summary: Optional[dict]) -> bool:
    """True when a Wikipedia summary is about a musical act - judged by its
    short description ("American rock band") and the start of its text."""
    if not summary:
        return False
    text = " ".join([summary.get("description") or "",
                     (summary.get("extract") or "")[:400]])
    return bool(_MUSIC_WORDS.search(text))


def _find_summary(session: requests.Session, artist_name: str) -> Optional[dict]:
    """"Boston (band)", "Prince (musician)" and the like first - a page
    with one of those titles is about a musical act by its name alone.
    Then the plain name, and then a search, both only if the article is
    about music (2026-10-13 - see _MUSIC_SUFFIXES)."""
    for suffix in _MUSIC_SUFFIXES:
        summary = _fetch_summary(session, artist_name + suffix)
        if _is_usable(summary):
            return summary

    summary = _fetch_summary(session, artist_name)
    if _is_usable(summary) and _is_music(summary):
        return summary

    for title in _search_titles(session, f"{artist_name} band musician"):
        summary = _fetch_summary(session, title)
        if _is_usable(summary) and _is_music(summary):
            return summary

    return None


# -- batched lookups for the bulk run (2026-10-13) ----------------------------
#
# The one-artist path above makes up to a dozen requests per artist (each
# "(band)"/"(musician)"/... guess, the plain name, a search, each hit) -
# fine for the artist page's own button, slow for a whole library, and
# slowest for exactly the artists Wikipedia doesn't have. The MediaWiki
# action API answers up to 50 titles in one request, so the bulk run asks
# about every guess for a batch of artists at once, then fetches the chosen
# pages' text in one more request. Only artists none of the guesses fit go
# on to a search - itself one request (generator=search returns the hits'
# text along with them). Same preference order and the same music check as
# _find_summary, so the same pages are picked.

_BATCH_UNSAFE = re.compile(r"[|#<>\[\]{}]")


def _pages(result: Optional[dict]) -> list[dict]:
    pages = ((result or {}).get("query") or {}).get("pages") or []
    return list(pages.values()) if isinstance(pages, dict) else list(pages)


def _title_map(result: Optional[dict]) -> dict[str, str]:
    """The renames a query reported: a requested title -> its normalised
    form, and a redirect -> its target. `_resolve` follows them."""
    query = (result or {}).get("query") or {}
    hops: dict[str, str] = {}
    for key in ("normalized", "redirects"):
        for row in query.get(key) or []:
            if row.get("from") and row.get("to"):
                hops[row["from"]] = row["to"]
    return hops


def _resolve(title: str, mapping: dict[str, str]) -> str:
    seen = set()
    while title in mapping and title not in seen:
        seen.add(title)
        title = mapping[title]
    return title


def _first_paragraph(text: str) -> str:
    """The REST summary the one-artist path uses is the article's opening
    paragraph; TextExtracts' exintro is the whole lead - keep its first
    paragraph so both paths save the same text."""
    paras = [p.strip() for p in (text or "").split("\n") if p.strip()]
    return paras[0] if paras else ""


def _as_summary(page: dict) -> Optional[dict]:
    """An action-API page shaped like a REST summary, so _is_usable,
    _is_music and download_bio_for_artist treat both the same."""
    if not page or page.get("missing") or page.get("invalid"):
        return None
    disambiguation = "disambiguation" in (page.get("pageprops") or {})
    url = page.get("fullurl") or (
        "https://en.wikipedia.org/wiki/" + requests.utils.quote(page.get("title", "").replace(" ", "_"))
    )
    return {
        "type": "disambiguation" if disambiguation else "standard",
        "title": page.get("title", ""),
        "description": page.get("description") or "",
        "extract": _first_paragraph(page.get("extract") or ""),
        "content_urls": {"desktop": {"page": url}},
    }


_PAGE_PROPS = {
    "prop": "extracts|description|pageprops|info",
    "exintro": 1,
    "explaintext": 1,
    "exlimit": "max",
    "ppprop": "disambiguation",
    "inprop": "url",
}


def _query(http: requests.Session, params: dict) -> dict:
    """One action-API query. Raises when there's no proper answer (no
    reply, or an API error such as too many titles) so the caller never
    mistakes a failed request for "Wikipedia has no such page"."""
    base = {"action": "query", "format": "json", "formatversion": 2, "redirects": 1}
    result = _wiki_request(http, WIKI_SEARCH_URL, base | params)
    if not isinstance(result, dict):
        raise ConnectionError("Wikipedia didn't answer")
    if result.get("error"):
        raise ConnectionError(f"Wikipedia: {result['error'].get('info') or result['error']}")
    if "titles" in params and not _pages(result):
        raise ConnectionError("Wikipedia sent no pages back")
    return result


def _summaries_for_titles(http: requests.Session, titles: Sequence[str]) -> dict[str, Optional[dict]]:
    """requested title -> summary (None when there's no such page), for at
    most 20 titles (TextExtracts' own per-request limit)."""
    if not titles:
        return {}
    result = _query(http, {"titles": "|".join(titles)} | _PAGE_PROPS)
    mapping = _title_map(result)
    by_title = {p.get("title"): p for p in _pages(result)}
    return {t: _as_summary(by_title.get(_resolve(t, mapping))) for t in titles}


def _existing_titles(http: requests.Session, titles: Sequence[str]) -> dict[str, Optional[dict]]:
    """requested title -> {"title", "disambiguation"} for pages that exist
    (None for missing ones) - up to 50 titles in one request, no text."""
    result = _query(http, {
        "titles": "|".join(titles), "prop": "pageprops", "ppprop": "disambiguation",
    })
    mapping = _title_map(result)
    by_title = {p.get("title"): p for p in _pages(result)}
    out: dict[str, Optional[dict]] = {}
    for t in titles:
        page = by_title.get(_resolve(t, mapping))
        if not page or page.get("missing") or page.get("invalid"):
            out[t] = None
        else:
            out[t] = {"title": page.get("title", ""),
                      "disambiguation": "disambiguation" in (page.get("pageprops") or {})}
    return out


def _search_summary(http: requests.Session, artist_name: str) -> Optional[dict]:
    """_find_summary's last step in one request: search, with each hit's
    text included, best hit first; the first one about music wins."""
    result = _query(http, {
        "generator": "search", "gsrsearch": f"{artist_name} band musician", "gsrlimit": 5,
    } | _PAGE_PROPS)
    pages = sorted(_pages(result), key=lambda p: p.get("index", 0))
    for page in pages:
        summary = _as_summary(page)
        if _is_usable(summary) and _is_music(summary):
            return summary
    return None


def _prefetch_summaries(http: requests.Session, names: Sequence[str]) -> dict[str, Optional[dict]]:
    """Bulk version of _find_summary for a batch of artist names: name ->
    summary, or None when Wikipedia has nothing about that act. A name
    missing from the result couldn't be settled here (unusual characters,
    a failed request) and is looked up the one-artist way instead."""
    names = [n for n in dict.fromkeys(names) if n and n.strip() and not _BATCH_UNSAFE.search(n)]
    if not names:
        return {}
    candidates = {n: [n + sfx for sfx in _MUSIC_SUFFIXES] + [n] for n in names}
    exists = _existing_titles(http, [t for ts in candidates.values() for t in ts])

    # the first guess that's a real, non-disambiguation page, in the same
    # order _find_summary tries them
    chosen: dict[str, str] = {}
    for n, titles in candidates.items():
        for t in titles:
            info = exists.get(t)
            if info and not info["disambiguation"]:
                chosen[n] = t
                break

    out: dict[str, Optional[dict]] = {}
    texts = _summaries_for_titles(http, list(dict.fromkeys(chosen.values())))
    for n in names:
        t = chosen.get(n)
        summary = texts.get(t) if t else None
        if t and _is_usable(summary) and (t != n or _is_music(summary)):
            out[n] = summary
            continue
        if t and t != n and not _is_usable(summary):
            # a "(band)" page with no text - rare; let the one-artist path
            # walk the rest of the guesses
            continue
        # the plain name wasn't about music, or nothing matched: search
        try:
            out[n] = _search_summary(http, n)
        except ConnectionError:
            continue  # looked up the one-artist way instead
    return out


def is_doubtful(artist: Artist) -> bool:
    """A biography fetched from Wikipedia (its page is in Artist.urls) that
    says nothing about music - the city of Boston for the band Boston,
    the cartridge for .38 Special. These are looked up again by the next
    "Download artist profiles…" run; a new lookup that finds nothing
    leaves the old text where it is."""
    text = (artist.profile or "").strip()
    if not text or "wikipedia.org/wiki/" not in (artist.urls or ""):
        return False
    return not _MUSIC_WORDS.search(text[:600])


def _clean_extract(text: str) -> str:
    # Wikipedia extracts occasionally carry stray reference marker
    # leftovers ("[1]", "[citation needed]") even in the plain-text
    # summary - strip them rather than showing them in BioPanel.
    text = re.sub(r"\[[^\]]*\]", "", text)
    return " ".join(text.split()).strip()


def _drop_wrong_sources(artist: Artist) -> None:
    """Forget the Wikipedia page a replaced biography came from (the
    city's page for the band Boston) - the new one is cited instead."""
    lines = [line for line in (artist.urls or "").splitlines()
             if line.strip() and "wikipedia.org/wiki/" not in line]
    artist.urls = "\n".join(lines) or None


def _add_source_url(artist: Artist, url: str) -> None:
    existing = (artist.urls or "").splitlines()
    if url not in existing:
        existing.append(url)
        artist.urls = "\n".join(line for line in existing if line.strip())


# -- public API ---------------------------------------------------------------


_NOT_LOOKED_UP = object()


def _needs_lookup(artist: Artist, overwrite: bool) -> bool:
    return not artist.profile or overwrite or is_doubtful(artist)


def download_bio_for_artist(
    http: requests.Session, db, artist: Artist, *, overwrite: bool = False,
    found=_NOT_LOOKED_UP,
) -> ArtistBioOutcome:
    """Look up and save one artist's biography. `db` is an open SQLAlchemy
    session the caller commits (or rolls back) - this function only sets
    attributes on the given `artist`, already attached to that session.
    `found` is a summary (or None for "nothing on Wikipedia") the bulk run
    already fetched in a batch; without it the artist is looked up here."""
    if not _needs_lookup(artist, overwrite):
        return ArtistBioOutcome(artist.id, artist.name, "already_exists")

    try:
        summary = found if found is not _NOT_LOOKED_UP else _find_summary(http, artist.name)
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
    _drop_wrong_sources(artist)
    if source_url:
        _add_source_url(artist, source_url)

    return ArtistBioOutcome(artist.id, artist.name, "downloaded", source_url or "")


def set_bio(artist_id: int, text: str) -> ArtistBioOutcome:
    """Save `text` as this artist's biography directly - the manual
    counterpart to download_bio_for_artist, for when a fetch matches the
    wrong band (Wikipedia name collisions - see the module docstring -
    mean a plain "Rush"/"Queen"/"Kiss" search can and does land on the
    wrong page) or when James would rather write one by hand than accept
    whatever Wikipedia has. An empty/whitespace-only `text` clears the
    saved biography (stored as NULL, same as an artist that's never had
    one) rather than saving an empty string. Unlike a fetch, this never
    touches Artist.urls - a hand-written or corrected bio has no
    Wikipedia page to cite as its source."""
    cleaned = text.strip()
    with session_scope() as db:
        artist = db.get(Artist, artist_id)
        if artist is None:
            return ArtistBioOutcome(artist_id, "", "error", "artist no longer exists")
        artist.profile = cleaned or None
        db.commit()
        return ArtistBioOutcome(artist.id, artist.name, "saved")


def download_bios_for_artists(
    artist_ids: Sequence[int],
    *,
    overwrite: bool = False,
    rate_limit_s: float = DEFAULT_RATE_LIMIT_S,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    http: Optional[requests.Session] = None,
) -> BioDownloadResult:
    """Download biographies for a list of artist ids, one Wikipedia lookup
    per artist - used both by Settings' bulk "Download artist profiles…"
    button and, as a batch of one, the artist page's own per-artist "Fetch
    bio" button (see BioDownloadThread in ui/widgets/common.py).

    Opens its own db session (unlike lyrics_downloader, which never touches
    the database at all) and commits after each artist rather than only
    once at the end, so a long bulk run over hundreds of artists isn't
    all-or-nothing if it's interrupted partway through.

    2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings". `should_stop`, when given, is checked once
    per artist, before that artist's own lookup starts - every artist
    already looked up this run has already been individually committed
    (see the `db.commit()` right below), so stopping here just means "don't
    start the next one", never a rollback of work already saved.
    """
    result = BioDownloadResult()
    if http is None:
        http = requests.Session()
        http.headers.update(_HEADERS)

    total = len(artist_ids)
    ids = list(artist_ids)
    done = 0
    stopped = False
    with session_scope() as db:
        for start in range(0, total, BATCH_ARTISTS):
            if stopped:
                break
            chunk = [a for a in (db.get(Artist, i) for i in ids[start:start + BATCH_ARTISTS])
                     if a is not None]
            done += (min(start + BATCH_ARTISTS, total) - start) - len(chunk)
            prefetched: Optional[dict[str, Optional[dict]]] = None

            for artist in chunk:
                if should_stop and should_stop():
                    stopped = True
                    break
                if prefetched is None:
                    # 2026-10-13: one batched lookup for the whole chunk (see
                    # _prefetch_summaries); anything it couldn't settle is
                    # looked up the one-artist way
                    prefetched = {}
                    wanted = [a.name for a in chunk if _needs_lookup(a, overwrite)]
                    if wanted:
                        if progress:
                            progress(done, total, artist.name)
                        try:
                            prefetched = _prefetch_summaries(http, wanted)
                        except Exception:  # fall back to one artist at a time
                            log.exception("batched Wikipedia lookup failed")
                        if rate_limit_s:
                            time.sleep(rate_limit_s)
                if progress:
                    progress(done, total, artist.name)
                kwargs = {"overwrite": overwrite}
                if artist.name in prefetched:
                    kwargs["found"] = prefetched[artist.name]
                outcome = download_bio_for_artist(http, db, artist, **kwargs)
                result.outcomes.append(outcome)
                db.commit()
                done += 1

                if outcome.status == "downloaded":
                    result.downloaded += 1
                elif outcome.status == "already_exists":
                    result.already_exists += 1
                elif outcome.status == "not_found":
                    result.not_found += 1
                elif outcome.status == "error":
                    result.errors.append(f"{artist.name}: {outcome.detail}")

    if progress:
        progress(total, total, "")
    return result


def artists_missing_bio(limit: Optional[int] = None) -> list[int]:
    """Every artist id with no biography saved yet - what Settings' bulk
    "Download artist profiles…" button targets by default (overwrite=False
    would just skip these same rows one network round trip at a time
    otherwise, so filtering up front here saves the wasted lookups).

    2026-10-13: plus every artist whose fetched biography isn't about
    music (`is_doubtful` - the city for Boston), so one run puts those
    right."""
    with session_scope() as db:
        stmt = select(Artist.id).where(
            (Artist.profile.is_(None)) | (Artist.profile == "")
        )
        ids = [row[0] for row in db.execute(stmt).all()]
        doubtful = db.scalars(select(Artist).where(
            Artist.profile.is_not(None), Artist.profile != "",
            Artist.urls.like("%wikipedia.org/wiki/%"),
        ))
        ids.extend(a.id for a in doubtful if is_doubtful(a))
        return ids[:limit] if limit else ids
