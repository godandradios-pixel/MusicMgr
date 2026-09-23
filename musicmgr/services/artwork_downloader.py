"""Search Discogs for an album cover and save it as `Release.cover_path`.

2026-09-18 - James: "Ability to search for album artwork" - the same
deliberate, requested exception to the rest of the app's "read from local
files, no network calls" stance that lyrics_downloader.py (LRCLIB),
artist_bio_downloader.py (Wikipedia) and lastfm_popularity.py (Last.fm)
already carved out, for the same reason: there is no local source for
cover art beyond whatever a file's own embedded tag already had (see
services/scanner.py's `_save_cover`, which this module's own `_save_cover`
below deliberately mirrors byte-for-byte in naming scheme).

Source: the Discogs database search API (`/database/search`,
`type=release`), chosen over the iTunes Search API and MusicBrainz/Cover
Art Archive for the same reason the Discogs data model already backs this
whole app (`Master` -> `Release` -> `Track`, see architecture.md): far
better coverage of vinyl, obscure, and out-of-print releases than iTunes,
and no separate MusicBrainz-ID resolution step first. Needs a free
personal access token from discogs.com/settings/developers - see
`DiscogsCredentialsDialog` in ui/views/settings.py, the same "Setting
key/value table" pattern `lastfm_popularity.py` already used for its own
API key (`API_KEY_KEY`/`get_api_key`/`set_api_key` there, mirrored here as
`TOKEN_KEY`/`get_api_token`/`set_api_token`).

Two ways in, both ending up here:

- Per-release, on demand: the release page's "Search artwork" button
  (`ArtworkSearchThread` in ui/widgets/common.py) fetches candidates *and*
  their thumbnails, then `ArtworkPickerDialog` lets James pick one before
  anything is saved - deliberately not auto-applied, given how much a
  wrong cover would stand out on a release he's looking at right now.
- Bulk, from Settings ("Search for missing album artwork…"): every
  release still on the placeholder gets the single best (first, since
  Discogs' own search already ranks by relevance) matching result applied
  automatically, no per-release review - the same auto-apply-and-report
  shape `download_bios_for_artists`/`update_popularity_for_artists`
  already use for their own bulk actions, chosen for the same reason: a
  review step for potentially thousands of releases isn't actually a bulk
  action any more. `releases_missing_cover` is the equivalent of
  `artist_bio_downloader.artists_missing_bio` - only ever targets releases
  with no cover yet, never a "redo everything" mode.

A Discogs search result's `cover_image` occasionally points at Discogs'
own "no image" placeholder (a `spacer.gif`) rather than a real photo -
`search_artwork_candidates` filters those out before they ever reach a
picker or a bulk apply.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import requests
from sqlalchemy import select

from .. import config
from ..db.models import Release, Setting
from ..db.session import session_scope

log = logging.getLogger(__name__)

DISCOGS_SEARCH_URL = "https://api.discogs.com/database/search"

#: Setting.key row this module owns - see lastfm_popularity.py's
#: API_KEY_KEY for the same pattern (and services/jukebox.py for the
#: original user of the generic Setting table).
TOKEN_KEY = "discogs_api_token"

#: light, polite pacing between releases in a bulk run - same default
#: lastfm_popularity.py uses; Discogs documents 60 req/min for an
#: authenticated token, so this stays well under that.
DEFAULT_RATE_LIMIT_S = 0.3

#: candidates offered per search - enough to give a real choice on a
#: touchscreen picker without turning it into a second scrollable list
DEFAULT_LIMIT = 8

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5

_HEADERS = {
    "User-Agent": "MusicMgr-Artwork-Downloader/1.0 (local desktop music library app)",
    "Accept": "application/json",
}

#: Discogs' own "no image available for this release" placeholder -
#: returned as a normal-looking cover_image/thumb URL, so it has to be
#: filtered out by name rather than by a missing field.
_PLACEHOLDER_MARKER = "spacer.gif"


@dataclass
class ArtworkCandidate:
    discogs_id: int
    title: str  # Discogs' own "Artist - Title" string
    year: Optional[str] = None
    format: str = ""
    country: Optional[str] = None
    thumb_url: str = ""
    #: the higher-resolution image actually saved as the cover
    image_url: str = ""


@dataclass
class ArtworkSearchOutcome:
    """What a single-release candidate search (picker flow) came back
    with - `thumbnails` is `{candidate index: image bytes}`, pre-fetched
    off the UI thread so ArtworkPickerDialog needs no network code of its
    own (see ArtworkSearchThread, ui/widgets/common.py)."""

    candidates: list[ArtworkCandidate] = field(default_factory=list)
    thumbnails: dict[int, bytes] = field(default_factory=dict)
    #: one of: None (success), not_configured, not_found, error
    error: Optional[str] = None


@dataclass
class ArtworkOutcome:
    release_id: int
    release_title: str
    #: one of: applied, already_has_cover, not_found, not_configured, error
    status: str
    detail: str = ""


@dataclass
class ArtworkSearchResult:
    applied: int = 0
    already_has_cover: int = 0
    not_found: int = 0
    not_configured: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: list[ArtworkOutcome] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.applied:
            parts.append(f"{self.applied} applied")
        if self.already_has_cover:
            parts.append(f"{self.already_has_cover} already had a cover")
        if self.not_found:
            parts.append(f"{self.not_found} not found on Discogs")
        if self.not_configured:
            parts.append("Discogs API token not set up yet")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " · ".join(parts) if parts else "Nothing to do"


# -- credentials (Setting table) -------------------------------------------


def get_api_token(db) -> Optional[str]:
    row = db.get(Setting, TOKEN_KEY)
    return row.value if row else None


def set_api_token(db, token: str) -> None:
    row = db.get(Setting, TOKEN_KEY)
    if row is None:
        row = Setting(key=TOKEN_KEY, value=token)
        db.add(row)
    else:
        row.value = token


def has_api_token() -> bool:
    with session_scope() as db:
        return bool(get_api_token(db))


# -- Discogs request/retry, the same shape as the app's other network
# integrations (lyrics_downloader._lrclib_request, artist_bio_downloader.
# _wiki_request, lastfm_popularity._lastfm_request) -----------------------


def _discogs_request(http: requests.Session, url: str, params: dict, token: str) -> Optional[dict]:
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Discogs token={token}"
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = http.get(url, params=params, headers=headers, timeout=20)

            if response.status_code in _RETRY_STATUSES:
                if attempt >= _MAX_RETRIES:
                    log.warning(
                        "Discogs: HTTP %s after %d attempts", response.status_code, _MAX_RETRIES
                    )
                    return None
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    wait_seconds = 2**attempt
                time.sleep(wait_seconds)
                continue

            if response.status_code == 401:
                log.warning("Discogs: rejected the API token")
                return None

            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout:
            if attempt >= _MAX_RETRIES:
                log.warning("Discogs: request timed out after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.exceptions.ConnectionError:
            if attempt >= _MAX_RETRIES:
                log.warning("Discogs: connection failed after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.RequestException as exc:
            log.warning("Discogs: %s", exc)
            return None

    return None


def _download_image_bytes(http: requests.Session, url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        response = http.get(url, headers=_HEADERS, timeout=20)
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        log.warning("Discogs: image download failed: %s", exc)
        return None


def _format_str(raw) -> str:
    """Discogs search results give `format` as a flat list of strings in
    practice ("Vinyl", "LP", "Album") but this API has a history of loose
    typing - accept a bare string too rather than assuming the list shape."""
    if isinstance(raw, list):
        return ", ".join(str(x) for x in raw if x)
    return str(raw) if raw else ""


def search_artwork_candidates(
    http: requests.Session,
    token: str,
    artist: str,
    album: str,
    limit: int = DEFAULT_LIMIT,
) -> list[ArtworkCandidate]:
    """Search Discogs for `artist`/`album` and return up to `limit`
    candidates with a real cover image - Discogs' own placeholder
    "no image" results (see _PLACEHOLDER_MARKER) are dropped rather than
    offered as a choice."""
    params = {
        "type": "release",
        "artist": artist,
        "release_title": album,
        "per_page": limit,
        "page": 1,
    }
    data = _discogs_request(http, DISCOGS_SEARCH_URL, params, token)
    if not data:
        return []

    candidates: list[ArtworkCandidate] = []
    for r in (data.get("results") or [])[:limit]:
        image_url = r.get("cover_image") or ""
        if not image_url or _PLACEHOLDER_MARKER in image_url:
            continue
        candidates.append(
            ArtworkCandidate(
                discogs_id=r.get("id"),
                title=r.get("title") or "",
                year=str(r.get("year")) if r.get("year") else None,
                format=_format_str(r.get("format")),
                country=r.get("country"),
                thumb_url=r.get("thumb") or image_url,
                image_url=image_url,
            )
        )
    return candidates


def _save_cover(data: bytes, release_id: int, ext_hint: Optional[str] = None) -> Optional[str]:
    """Save cover bytes under config.ART_DIR, deliberately the same
    digest-named scheme scanner.py's own `_save_cover` uses for a
    tag-embedded cover - so a moved/portable data\\ folder still resolves
    a saved cover by filename the same way (see ui/widgets/common.py's
    `_resolve_art_path`).

    `ext_hint` (added for `apply_local_image_to_release` - a file James
    picked from his own disk) preserves the source file's own real
    extension when it's a recognised image type, rather than forcing
    everything through the jpg/png sniff below - that sniff still covers
    the Discogs-download path (always jpg or png in practice) and is the
    fallback if the hint isn't usable."""
    if not data:
        return None
    try:
        config.ensure_dirs()
        digest = hashlib.sha1(data).hexdigest()[:16]
        hint = (ext_hint or "").lower()
        if hint in config.IMAGE_EXTENSIONS:
            ext = hint
        else:
            ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        out = config.ART_DIR / f"release_{release_id}_{digest}{ext}"
        if not out.exists():
            out.write_bytes(data)
        return str(out)
    except Exception as exc:  # pragma: no cover
        log.warning("cover save failed: %s", exc)
        return None


def apply_artwork_candidate(http: requests.Session, db, release: Release, candidate: ArtworkCandidate) -> ArtworkOutcome:
    """Download `candidate`'s full-size image and set it as `release`'s
    cover - `db` is an open session the caller commits, same convention
    artist_bio_downloader.download_bio_for_artist uses."""
    data = _download_image_bytes(http, candidate.image_url)
    if not data:
        return ArtworkOutcome(release.id, release.title, "error", "couldn't download the selected image")
    path = _save_cover(data, release.id)
    if not path:
        return ArtworkOutcome(release.id, release.title, "error", "couldn't save the image")
    release.cover_path = path
    return ArtworkOutcome(release.id, release.title, "applied")


# -- per-release picker flow -----------------------------------------------


def search_and_fetch_thumbnails(release_id: int, limit: int = DEFAULT_LIMIT) -> ArtworkSearchOutcome:
    """Search Discogs for one release and pre-download every candidate's
    thumbnail, all off the UI thread (see ArtworkSearchThread in
    ui/widgets/common.py) - ArtworkPickerDialog only ever renders bytes
    it's handed, never makes a request of its own."""
    with session_scope() as db:
        token = get_api_token(db)
        release = db.get(Release, release_id)
        if release is None:
            return ArtworkSearchOutcome(error="error")
        artist = release.artist_display or ""
        title = release.title

    if not token:
        return ArtworkSearchOutcome(error="not_configured")

    http = requests.Session()
    try:
        candidates = search_artwork_candidates(http, token, artist, title, limit=limit)
    except Exception:  # a bad response must not crash the UI thread's caller
        log.exception("artwork search failed for release %s", release_id)
        return ArtworkSearchOutcome(error="error")

    if not candidates:
        return ArtworkSearchOutcome(error="not_found")

    thumbnails: dict[int, bytes] = {}
    for i, candidate in enumerate(candidates):
        thumb = _download_image_bytes(http, candidate.thumb_url)
        if thumb:
            thumbnails[i] = thumb

    return ArtworkSearchOutcome(candidates=candidates, thumbnails=thumbnails)


def apply_artwork_to_release(release_id: int, candidate: ArtworkCandidate) -> ArtworkOutcome:
    """Save whichever candidate James picked in ArtworkPickerDialog -
    opens its own short-lived session/commit, the same "just this one
    write" shape lyrics_downloader-adjacent single-item calls use."""
    http = requests.Session()
    with session_scope() as db:
        release = db.get(Release, release_id)
        if release is None:
            return ArtworkOutcome(release_id, "", "error", "release no longer exists")
        outcome = apply_artwork_candidate(http, db, release, candidate)
        db.commit()
        return outcome


# -- manual "choose from file" flow (release page's "Choose from file…") ---


def apply_local_image_to_release(release_id: int, file_path: str) -> ArtworkOutcome:
    """Save an image James picked from his own filesystem as this
    release's cover - the manual counterpart to apply_artwork_to_release,
    for a release Discogs has nothing for (or one he'd rather point at his
    own scan/photo than accept an online match for). Same "open a short-
    lived session, save, commit" shape as the Discogs picker's own apply
    step; the only real difference is the bytes come from disk instead of
    a download, and `_save_cover` gets the file's own extension as a hint
    so a webp/bmp/gif James picks isn't silently forced into a .jpg."""
    src = Path(file_path)
    try:
        data = src.read_bytes()
    except OSError as exc:
        return ArtworkOutcome(release_id, "", "error", f"couldn't read that file: {exc}")
    if not data:
        return ArtworkOutcome(release_id, "", "error", "that file is empty")

    with session_scope() as db:
        release = db.get(Release, release_id)
        if release is None:
            return ArtworkOutcome(release_id, "", "error", "release no longer exists")
        path = _save_cover(data, release.id, ext_hint=src.suffix)
        if not path:
            return ArtworkOutcome(release.id, release.title, "error", "couldn't save the image")
        release.cover_path = path
        db.commit()
        return ArtworkOutcome(release.id, release.title, "applied")


# -- bulk flow (Settings' "Search for missing album artwork…") -------------


def search_artwork_for_releases(
    release_ids: Sequence[int],
    *,
    overwrite: bool = False,
    rate_limit_s: float = DEFAULT_RATE_LIMIT_S,
    progress: Optional[Callable[[int, int, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ArtworkSearchResult:
    """Bulk cover search for a list of release ids - auto-applies the top
    (first, already relevance-ranked by Discogs) candidate for each one
    rather than prompting per release, the same auto-apply-and-report
    shape download_bios_for_artists/update_popularity_for_artists use for
    their own bulk actions (see module docstring for why a per-release
    review step doesn't scale to a real library).

    2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings". `should_stop`, checked once per release
    before that release's own search starts - same safe-to-interrupt shape
    the other two bulk downloaders above just adopted."""
    result = ArtworkSearchResult()
    total = len(release_ids)

    with session_scope() as db:
        token = get_api_token(db)
    if not token:
        result.not_configured = total
        result.outcomes = [ArtworkOutcome(rid, "", "not_configured") for rid in release_ids]
        if progress:
            progress(total, total, "")
        return result

    http = requests.Session()

    with session_scope() as db:
        for i, release_id in enumerate(release_ids, start=1):
            if should_stop and should_stop():
                break
            release = db.get(Release, release_id)
            if release is None:
                continue

            if progress:
                progress(i - 1, total, release.title)

            if release.cover_path and not overwrite:
                outcome = ArtworkOutcome(release.id, release.title, "already_has_cover")
            else:
                try:
                    candidates = search_artwork_candidates(
                        http, token, release.artist_display or "", release.title
                    )
                    if not candidates:
                        outcome = ArtworkOutcome(release.id, release.title, "not_found")
                    else:
                        outcome = apply_artwork_candidate(http, db, release, candidates[0])
                except Exception as exc:  # one bad release must not stop a bulk run
                    log.exception("artwork search failed for %s", release.title)
                    outcome = ArtworkOutcome(release.id, release.title, "error", str(exc))

            result.outcomes.append(outcome)
            db.commit()

            if outcome.status == "applied":
                result.applied += 1
            elif outcome.status == "already_has_cover":
                result.already_has_cover += 1
            elif outcome.status == "not_found":
                result.not_found += 1
            elif outcome.status == "error":
                result.errors.append(f"{release.title}: {outcome.detail}")

            # only pace ourselves when a request actually went out
            if outcome.status not in ("already_has_cover",) and i < total:
                time.sleep(rate_limit_s)

    if progress:
        progress(total, total, "")
    return result


def releases_missing_cover(limit: Optional[int] = None) -> list[int]:
    """Every release id with no cover saved yet - what Settings' bulk
    "Search for missing album artwork…" button targets by default, the
    same shape artist_bio_downloader.artists_missing_bio uses for bios."""
    with session_scope() as db:
        stmt = select(Release.id).where(
            (Release.cover_path.is_(None)) | (Release.cover_path == "")
        )
        if limit:
            stmt = stmt.limit(limit)
        return [row[0] for row in db.execute(stmt).all()]
