"""Download synced .lrc lyrics from LRCLIB (https://lrclib.net) and write
them to the same sidecar path `services/lyrics.py` already reads from.

This is a deliberate, requested exception to the rest of the app's "read
from local files, no network calls" stance (see `services/lyrics.py`'s
module docstring, and `architecture.md`) - James asked for it explicitly,
after uploading a standalone script (`lrc_downloader.py`) that did the same
job against a bare folder tree with no database behind it. The lookup,
retry and matching logic below is ported from that script essentially
unchanged; what differs is where the track metadata comes from and how a
track's own existing-lyrics check works:

- The original script reads title/artist/album/duration from the audio
  file's own tags via mutagen, since it has nothing else to go on. MusicMgr
  already carries the same metadata in its database (and, for the currently
  playing track, right on the in-memory `QueueItem`) - including any manual
  corrections made in Title Details - so callers here just pass that in
  directly (`LyricsTrackInput`) instead of this module re-reading tags off
  disk.
- The original script's default behavior skips an entire album folder if it
  already contains *any* .lrc file. James was asked about this directly and
  chose to check every track individually instead (AskUserQuestion,
  2026-09-07) - so `download_lyrics_for_album` below has no folder-level
  shortcut at all: each track's own sidecar path is checked on its own
  merits, every time.

`download_lyrics_for_track` is the single-track worker (used for the
Now Playing panel's per-track button); `download_lyrics_for_album` just
loops it over a list of tracks and aggregates the outcomes into one
`AlbumLyricsResult` (used for both the per-album button and, as a
"batch of one", the per-track button too - one result shape instead of
two parallel ones).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import requests

log = logging.getLogger(__name__)

LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"

#: seconds between LRCLIB requests - the same self-imposed pace as the
#: original script, to stay polite to a free public API
DEFAULT_RATE_LIMIT_S = 0.75

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5

_HEADERS = {
    "User-Agent": "MusicMgr-LRC-Downloader/1.3",
    "Lrclib-Client": "MusicMgr-LRC-Downloader/1.3",
    "Accept": "application/json",
}


@dataclass
class LyricsTrackInput:
    """What a caller hands in per track - already-known metadata, no tag
    reading or DB access done in here."""

    audio_path: Path
    title: str
    artist: str
    album: str = ""
    duration_ms: int = 0


@dataclass
class TrackLyricsOutcome:
    audio_path: Path
    #: one of: downloaded, already_exists, no_artist, instrumental,
    #: not_found, error
    status: str
    detail: str = ""


@dataclass
class AlbumLyricsResult:
    downloaded: int = 0
    already_exists: int = 0
    instrumental: int = 0
    not_found: int = 0
    no_artist: int = 0
    errors: list[str] = field(default_factory=list)
    outcomes: list[TrackLyricsOutcome] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.downloaded:
            parts.append(f"{self.downloaded} downloaded")
        if self.already_exists:
            parts.append(f"{self.already_exists} already had lyrics")
        if self.instrumental:
            parts.append(f"{self.instrumental} instrumental")
        if self.not_found:
            parts.append(f"{self.not_found} not found")
        if self.no_artist:
            parts.append(f"{self.no_artist} missing artist tag")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " · ".join(parts) if parts else "Nothing to do"


# -- LRCLIB request/retry, ported from lrc_downloader.py --------------------


def _lrclib_request(session: requests.Session, url: str, params: dict) -> Optional[object]:
    """Call LRCLIB with retry handling: 429/5xx and timeouts/connection
    errors are retried with backoff (honoring a `Retry-After` header when
    the server sends one); 204/404 mean "no such record", not an error."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=20)

            if response.status_code in (204, 404):
                return None

            if response.status_code in _RETRY_STATUSES:
                if attempt >= _MAX_RETRIES:
                    log.warning(
                        "LRCLIB: HTTP %s after %d attempts", response.status_code, _MAX_RETRIES
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
                log.warning("LRCLIB: request timed out after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.exceptions.ConnectionError:
            if attempt >= _MAX_RETRIES:
                log.warning("LRCLIB: connection failed after %d attempts", _MAX_RETRIES)
                return None
            time.sleep(2**attempt)
        except requests.RequestException as exc:
            log.warning("LRCLIB: %s", exc)
            return None

    return None


def _exact_lookup(session: requests.Session, track: dict) -> Optional[dict]:
    params = {"track_name": track["title"], "artist_name": track["artist"]}
    if track["album"]:
        params["album_name"] = track["album"]
    if track["duration"]:
        params["duration"] = track["duration"]
    return _lrclib_request(session, LRCLIB_GET, params)


def _search_lookup(session: requests.Session, track: dict) -> list:
    params = {"track_name": track["title"], "artist_name": track["artist"]}
    result = _lrclib_request(session, LRCLIB_SEARCH, params)
    return result if isinstance(result, list) else []


def _normalize(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\([^)]*(remaster|remastered|edition|version)[^)]*\)", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _choose_best_result(track: dict, results: list) -> Optional[dict]:
    wanted_title = _normalize(track["title"])
    wanted_artist = _normalize(track["artist"])

    best = None
    best_difference = 999999

    for result in results:
        result_title = _normalize(str(result.get("trackName", "")))
        result_artist = _normalize(str(result.get("artistName", "")))

        if result_title != wanted_title:
            continue
        if (
            result_artist != wanted_artist
            and wanted_artist not in result_artist
            and result_artist not in wanted_artist
        ):
            continue

        try:
            result_duration = int(round(float(result.get("duration", 0))))
        except (TypeError, ValueError):
            result_duration = 0

        if track["duration"] and result_duration:
            difference = abs(track["duration"] - result_duration)
            if difference > 10:  # avoid obviously different versions
                continue
        else:
            difference = 0

        if difference < best_difference:
            best = result
            best_difference = difference

    return best


def _find_lyrics(session: requests.Session, track: dict) -> tuple[Optional[dict], Optional[str]]:
    result = _exact_lookup(session, track)
    if result:
        return result, "Exact"

    results = _search_lookup(session, track)
    result = _choose_best_result(track, results)
    if result:
        return result, "Search"

    return None, None


# -- public API ---------------------------------------------------------------


def download_lyrics_for_track(
    session: requests.Session,
    audio_path: Union[str, Path],
    *,
    title: str,
    artist: str,
    album: str = "",
    duration_ms: int = 0,
    overwrite: bool = False,
    save_plain: bool = True,
) -> TrackLyricsOutcome:
    """Look up and save lyrics for one track. Writes to the same
    `Path(audio_path).with_suffix(".lrc")` sidecar `services/lyrics.py`
    reads from - that's the entire integration point with the rest of the
    lyrics feature, nothing else needs to change to pick this up.

    `save_plain=True` saves unsynced lyrics when no synced version exists,
    unlike the original script's opt-in `--plain` flag - LyricsPanel already
    displays unsynced lyrics just fine (plain scrolling text, no highlight),
    so there's no reason to leave a found-but-unsynced lyric on the table.
    """
    audio_path = Path(audio_path)
    lrc_path = audio_path.with_suffix(".lrc")

    if lrc_path.is_file() and not overwrite:
        return TrackLyricsOutcome(audio_path, "already_exists")

    if not artist:
        return TrackLyricsOutcome(audio_path, "no_artist")

    track = {
        "title": title or audio_path.stem,
        "artist": artist,
        "album": album,
        "duration": round(duration_ms / 1000) if duration_ms else 0,
    }

    try:
        result, method = _find_lyrics(session, track)
    except Exception as exc:  # one bad track must not stop an album run
        log.exception("lyrics lookup failed for %s", audio_path)
        return TrackLyricsOutcome(audio_path, "error", str(exc))

    if not result:
        return TrackLyricsOutcome(audio_path, "not_found")

    if result.get("instrumental") is True:
        return TrackLyricsOutcome(audio_path, "instrumental")

    text = result.get("syncedLyrics") or (result.get("plainLyrics") if save_plain else None)
    if not text:
        return TrackLyricsOutcome(audio_path, "not_found")

    try:
        lrc_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        return TrackLyricsOutcome(audio_path, "error", str(exc))

    return TrackLyricsOutcome(audio_path, "downloaded", method)


def download_lyrics_for_album(
    tracks: Sequence[LyricsTrackInput],
    *,
    overwrite: bool = False,
    save_plain: bool = True,
    rate_limit_s: float = DEFAULT_RATE_LIMIT_S,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> AlbumLyricsResult:
    """Download lyrics for every track given, one LRCLIB lookup per track -
    deliberately no "skip the whole folder if it already has an .lrc"
    shortcut (the original script's default): each track's own sidecar file
    is checked on its own, every time. `progress(done, total, title)` is
    called before each track and once more at the end with done==total."""
    result = AlbumLyricsResult()
    session = requests.Session()
    session.headers.update(_HEADERS)

    total = len(tracks)
    for i, track in enumerate(tracks, start=1):
        if progress:
            progress(i - 1, total, track.title)

        outcome = download_lyrics_for_track(
            session,
            track.audio_path,
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration_ms=track.duration_ms,
            overwrite=overwrite,
            save_plain=save_plain,
        )
        result.outcomes.append(outcome)

        if outcome.status == "downloaded":
            result.downloaded += 1
        elif outcome.status == "already_exists":
            result.already_exists += 1
        elif outcome.status == "instrumental":
            result.instrumental += 1
        elif outcome.status == "not_found":
            result.not_found += 1
        elif outcome.status == "no_artist":
            result.no_artist += 1
        elif outcome.status == "error":
            result.errors.append(f"{track.audio_path.name}: {outcome.detail}")

        # only pace ourselves when a request actually went out
        hit_network = outcome.status not in ("already_exists", "no_artist")
        if hit_network and i < total:
            time.sleep(rate_limit_s)

    if progress:
        progress(total, total, "")
    return result
