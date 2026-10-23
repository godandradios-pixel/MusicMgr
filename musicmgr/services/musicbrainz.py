"""Look an album up on MusicBrainz (the tag editor's "look up this album").

MusicBrainz is the open music database MusicBrainz Picard tags
from: no account or key needed. Another of the app's deliberate network
exceptions, like LRCLIB (lyrics), Wikipedia (bios), Last.fm and Discogs.

The flow (ui/widgets/tag_editor.py drives it):

1. `search_releases(artist, album, track_count)` - candidate releases,
   best first. MusicBrainz lists every pressing separately (the 1980 US
   LP, the 2003 remaster CD, ...), so the picker shows year, country,
   format, label and track count to tell them apart.
2. `get_release(id)` - one release with its whole track list.
3. `match_tracks(...)` - pairs the album's songs with the release's
   tracks (by title, then position and length), so an album missing a
   song or holding a bonus track still lines up.
4. `values_for(...)` - the tags for one song from its matched track.
   Nothing is written here: the editor fills its boxes with these, the user
   looks them over, and Save writes them (services/tag_editor.py).
5. `fetch_front_cover(id)` - the release's front cover from the Cover Art
   Archive, if it has one.

MusicBrainz asks for at most one request a second and a User-Agent naming
the app and a contact; `_get` keeps to both, and backs off on a 503.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional, Sequence

import requests

from .matching import normalize
from .tag_writer import TagValues

log = logging.getLogger(__name__)

API = "https://musicbrainz.org/ws/2"
COVER_API = "https://coverartarchive.org/release"
TIMEOUT_S = 20
MIN_INTERVAL_S = 1.1
VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"
RELEASE_URL = "https://musicbrainz.org/release/"

_lock = threading.Lock()
_last_call = 0.0


class MusicBrainzError(Exception):
    """A lookup failed. `str()` is a sentence fit to show the user."""


def _user_agent() -> str:
    try:
        from .. import __version__ as version
    except Exception:  # pragma: no cover
        version = "dev"
    return f"MusicMgr/{version} ( https://github.com/godandradios-pixel/MusicMgr )"


def _get(url: str, params: Optional[dict] = None, http=None, *, raw: bool = False):
    """GET with MusicBrainz's pacing and a polite retry on 503."""
    global _last_call
    http = http or requests
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    for attempt in range(3):
        with _lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.monotonic()
        try:
            resp = http.get(url, params=params, headers=headers, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            if attempt == 2:
                raise MusicBrainzError("Couldn't reach MusicBrainz - check the internet "
                                   "connection and try again.") from exc
            time.sleep(1.5 * (attempt + 1))
            continue
        status = getattr(resp, "status_code", 200)
        if status in (429, 503) and attempt < 2:
            time.sleep(2.0 * (attempt + 1))
            continue
        if status == 404:
            return None
        if status >= 400:
            raise MusicBrainzError(f"MusicBrainz answered with an error ({status}). Try again in a minute.")
        if raw:
            return resp.content
        try:
            return resp.json()
        except ValueError as exc:
            raise MusicBrainzError("MusicBrainz sent back something MusicMgr couldn't read.") from exc
    raise MusicBrainzError("MusicBrainz is busy right now. Try again in a minute.")


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass
class ReleaseSummary:
    """One search result - enough to tell pressings apart."""

    id: str
    title: str
    artist: str
    date: str = ""
    country: str = ""
    formats: str = ""
    track_count: int = 0
    label: str = ""
    catalog_number: str = ""
    barcode: str = ""
    status: str = ""
    kind: str = ""
    disambiguation: str = ""
    score: int = 0

    @property
    def year(self) -> str:
        return self.date[:4] if self.date else ""

    def describe(self) -> str:
        """'1980 · US · 12" Vinyl · Atlantic SD 16018 · 10 tracks'"""
        bits = [self.year, self.country, self.formats,
                " ".join(x for x in (self.label, self.catalog_number) if x),
                f"{self.track_count} track{'s' if self.track_count != 1 else ''}"
                if self.track_count else ""]
        return " · ".join(b for b in bits if b)


@dataclass
class MBTrack:
    disc: int
    #: position on its disc, 1-based
    position: int
    #: what's printed: "3", or "A1" / "B" on a record
    number: str
    title: str
    artist: str
    artist_id: str = ""
    length_ms: Optional[int] = None
    recording_id: str = ""
    release_track_id: str = ""
    isrc: str = ""


@dataclass
class MBRelease(ReleaseSummary):
    album_artist_id: str = ""
    release_group_id: str = ""
    genres: list[str] = field(default_factory=list)
    tracks: list[MBTrack] = field(default_factory=list)
    #: tracks per disc
    disc_sizes: dict[int, int] = field(default_factory=dict)
    has_front_cover: bool = False

    @property
    def disc_count(self) -> int:
        return len(self.disc_sizes) or 1

    @property
    def is_compilation(self) -> bool:
        return self.album_artist_id == VARIOUS_ARTISTS_ID


def _credit(credits) -> tuple[str, str]:
    """An artist-credit list -> ('Simon & Garfunkel', first artist id)."""
    if not credits:
        return "", ""
    text = "".join((c.get("name") or (c.get("artist") or {}).get("name") or "")
                   + (c.get("joinphrase") or "") for c in credits)
    first = (credits[0].get("artist") or {}).get("id") or ""
    return text.strip(), first


def _formats(media) -> str:
    counts: dict[str, int] = {}
    for m in media or []:
        name = m.get("format") or "Digital Media"
        counts[name] = counts.get(name, 0) + 1
    return " + ".join(f"{n}×{name}" if n > 1 else name for name, n in counts.items())


def _summary_from(data: dict) -> dict:
    artist, artist_id = _credit(data.get("artist-credit"))
    labels = data.get("label-info") or []
    label = next(((li.get("label") or {}).get("name") for li in labels
                  if (li.get("label") or {}).get("name")), "") or ""
    catalog = next((li.get("catalog-number") for li in labels if li.get("catalog-number")), "") or ""
    group = data.get("release-group") or {}
    media = data.get("media") or []
    count = data.get("track-count")
    if count is None:
        count = sum(int(m.get("track-count") or len(m.get("tracks") or [])) for m in media)
    return dict(
        id=data.get("id", ""),
        title=data.get("title", ""),
        artist=artist,
        date=data.get("date") or "",
        country=data.get("country") or "",
        formats=_formats(media),
        track_count=int(count or 0),
        label=label if label != "[no label]" else "",
        catalog_number=catalog if catalog != "[none]" else "",
        barcode=data.get("barcode") or "",
        status=data.get("status") or "",
        kind=group.get("primary-type") or "",
        disambiguation=data.get("disambiguation") or "",
        score=int(data.get("score") or 0),
    ), artist_id


# --------------------------------------------------------------------------
# search and lookup
# --------------------------------------------------------------------------

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _quote(text: str) -> str:
    return '"' + _LUCENE_SPECIAL.sub(r"\\\1", text.strip()) + '"'


def _fuzzy(text: str) -> str:
    """'Back in Blak' -> '(back~ in blak~)': each longer word may be one or
    two letters off, so a misspelt title still finds the album."""
    words = [w for w in re.split(r"[^\w']+", text.lower()) if w]
    words = [w.replace("'", "") for w in words]
    return "(" + " ".join(f"{w}~" if len(w) >= 4 and not w.isdigit() else w for w in words if w) + ")"


def search_releases(artist: str, album: str, track_count: Optional[int] = None,
                    limit: int = 15, http=None) -> list[ReleaseSummary]:
    """Releases matching an album title and artist, best first. Same-
    scoring results with the right number of tracks come first, then
    official releases, then the earliest.

    Tries the exact title and artist first; if MusicBrainz has nothing,
    allows small misspellings ("Back in Blak"), then the title alone (the
    artist may be the misspelt part, "ACDC" for AC/DC)."""
    album = (album or "").strip()
    artist = (artist or "").strip()
    if not album and not artist:
        return []
    use_artist = bool(artist) and normalize(artist) not in ("various artists", "various")
    queries = []
    exact = []
    if album:
        exact.append(f"release:{_quote(album)}")
    if use_artist:
        exact.append(f"artist:{_quote(artist)}")
    queries.append(" AND ".join(exact))
    if album and _fuzzy(album) != "()":
        fuzzy_album = f"release:{_fuzzy(album)}"
        if use_artist and _fuzzy(artist) != "()":
            queries.append(f"{fuzzy_album} AND artist:{_fuzzy(artist)}")
        queries.append(fuzzy_album)
    releases = []
    for query in dict.fromkeys(queries):
        data = _get(f"{API}/release/", {"query": query, "fmt": "json", "limit": limit}, http)
        releases = (data or {}).get("releases") or []
        if releases:
            break
    out = [ReleaseSummary(**_summary_from(r)[0]) for r in releases]

    def rank(r: ReleaseSummary):
        return (-r.score,
                0 if track_count and r.track_count == track_count else 1,
                0 if r.status == "Official" else 1,
                r.date or "9999")
    out.sort(key=rank)
    return out


def get_release(release_id: str, http=None) -> MBRelease:
    """One release with its full track list."""
    data = _get(f"{API}/release/{release_id}",
                {"inc": "recordings+artist-credits+labels+isrcs+release-groups+genres",
                 "fmt": "json"}, http)
    if not data:
        raise MusicBrainzError("MusicBrainz doesn't have that release any more.")
    summary, artist_id = _summary_from(data)
    rel = MBRelease(**summary)
    rel.album_artist_id = artist_id
    group = data.get("release-group") or {}
    rel.release_group_id = group.get("id") or ""
    genres = data.get("genres") or group.get("genres") or []
    genres = sorted(genres, key=lambda g: -int(g.get("count") or 0))
    rel.genres = [g["name"].title() for g in genres if g.get("name")][:3]
    rel.has_front_cover = bool((data.get("cover-art-archive") or {}).get("front"))
    for medium in data.get("media") or []:
        disc = int(medium.get("position") or 1)
        tracks = medium.get("tracks") or []
        rel.disc_sizes[disc] = int(medium.get("track-count") or len(tracks))
        for i, t in enumerate(tracks, start=1):
            rec = t.get("recording") or {}
            credit, credit_id = _credit(t.get("artist-credit") or rec.get("artist-credit"))
            isrcs = rec.get("isrcs") or []
            rel.tracks.append(MBTrack(
                disc=disc,
                position=int(t.get("position") or i),
                number=str(t.get("number") or t.get("position") or i),
                title=t.get("title") or rec.get("title") or "",
                artist=credit or rel.artist,
                artist_id=credit_id or artist_id,
                length_ms=t.get("length") or rec.get("length"),
                recording_id=rec.get("id") or "",
                release_track_id=t.get("id") or "",
                isrc=isrcs[0] if isrcs else "",
            ))
    rel.track_count = len(rel.tracks) or rel.track_count
    return rel


def fetch_front_cover(release_id: str, http=None) -> Optional[bytes]:
    """The release's front cover (500px) from the Cover Art Archive."""
    try:
        data = _get(f"{COVER_API}/{release_id}/front-500", None, http, raw=True)
    except MusicBrainzError:
        return None
    return data or None


# --------------------------------------------------------------------------
# lining the album's songs up with the release
# --------------------------------------------------------------------------


@dataclass
class LocalTrack:
    """What the editor knows about one of the album's songs."""

    title: str
    disc: Optional[int] = None
    track: Optional[int] = None
    length_ms: Optional[int] = None


def _similar(a: str, b: str) -> float:
    a, b = normalize(a or ""), normalize(b or "")
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def match_tracks(local: Sequence[LocalTrack], release: MBRelease) -> list[Optional[int]]:
    """For each local song, the index into `release.tracks` it matches (or
    None). Each release track is used at most once; pairs are taken best
    first, scoring title likeness most, then the same disc and track
    number, then a similar length."""
    scores = []
    for i, loc in enumerate(local):
        for j, mb in enumerate(release.tracks):
            title = _similar(loc.title, mb.title)
            same_pos = 1.0 if (loc.track == mb.position and (loc.disc or 1) == mb.disc) else 0.0
            length = 0.0
            if loc.length_ms and mb.length_ms:
                diff = abs(loc.length_ms - mb.length_ms) / 1000
                length = 1.0 if diff <= 3 else (0.5 if diff <= 10 else 0.0)
            score = title * 0.6 + same_pos * 0.3 + length * 0.1
            # a title alone has to be close; a weaker likeness needs the
            # same place on the album as well
            if title >= 0.8 or (same_pos and (length >= 0.5 or title >= 0.45)) \
                    or (length >= 1.0 and title >= 0.6):
                scores.append((score, i, j))
    scores.sort(key=lambda s: (-s[0], s[1], s[2]))
    out: list[Optional[int]] = [None] * len(local)
    used: set[int] = set()
    for score, i, j in scores:
        if out[i] is None and j not in used and score >= 0.35:
            out[i] = j
            used.add(j)
    return out


def values_for(release: MBRelease, track: MBTrack, *, keep_side: bool = False,
               include_genre: bool = False) -> TagValues:
    """The tags for one song from its matched release track. `keep_side`
    writes the printed number ("A", "B2") instead of the position - for a
    78 whose track numbers are already record sides."""
    number = track.number if keep_side and not track.number.isdigit() else str(track.position)
    disc_count = release.disc_count
    return TagValues(
        title=track.title,
        artist=track.artist,
        album=release.title,
        album_artist="Various Artists" if release.is_compilation else release.artist,
        track=number,
        track_total=str(release.disc_sizes.get(track.disc, "") or ""),
        # a one-disc release leaves the disc tags as they are
        disc=str(track.disc) if disc_count > 1 else None,
        disc_total=str(disc_count) if disc_count > 1 else None,
        date=release.date or None,
        genre=", ".join(release.genres) if include_genre and release.genres else None,
        label=release.label or None,
        catalog_number=release.catalog_number or None,
        barcode=release.barcode or None,
        country=release.country or None,
        isrc=track.isrc or None,
        compilation="1" if release.is_compilation else "",
        mb_album_id=release.id,
        mb_album_artist_id=release.album_artist_id or None,
        mb_artist_id=track.artist_id or None,
        mb_release_group_id=release.release_group_id or None,
        mb_release_track_id=track.release_track_id or None,
        mb_recording_id=track.recording_id or None,
    )
