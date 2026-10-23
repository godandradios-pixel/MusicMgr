"""Look up a track's lyrics on YouTube Music.

A second lyrics source alongside LRCLIB (`lyrics_downloader.py`). It must
keep working without a YouTube Music subscription, so everything here runs
**without signing in**: ytmusicapi's search, watch-playlist and lyrics
calls all work for an anonymous visitor, and there is no account, cookie
or OAuth file anywhere in the app.

ytmusicapi is an unofficial library - it talks to the same endpoints the
music.youtube.com web player uses. If Google changes those, lookups here
start failing until the library is updated (bump the pin in
requirements.txt). Every failure is caught and reported as an `error`
fetch, so a broken YouTube Music lookup can never stop an album's LRCLIB
downloads or crash the lyric editor.

How a lookup works:

1. Search YouTube Music's *songs* for "artist title".
2. Keep only results whose title matches ours (same normalizing as the
   LRCLIB matcher - case, punctuation and "(Remastered)"-style suffixes
   ignored) and whose artist overlaps ours, and, when both durations are
   known, within 10 seconds of ours - the same "obviously a different
   version" cut LRCLIB uses. Closest duration wins.
3. Ask that song's watch playlist for its lyrics id, then fetch the lyrics
   with timestamps. YouTube Music has timed lyrics for many songs (from
   its mobile client); when it does, they come back as LRC text in
   `synced`, otherwise the plain words in `plain`.
"""

from __future__ import annotations

import logging
from typing import Optional

from .lyrics_downloader import LyricsTextFetch, _normalize

log = logging.getLogger(__name__)

#: what the UI calls this source
SOURCE_NAME = "YouTube Music"

#: how far a result's length may differ from ours and still count as the
#: same recording (seconds) - matches lyrics_downloader._choose_best_result
MAX_DURATION_DIFF_S = 10

#: how many song results to consider per search
SEARCH_LIMIT = 10

#: seconds before a single YouTube Music request gives up
REQUEST_TIMEOUT_S = 15


def _make_client():
    """A signed-out ytmusicapi client. Imported here, not at module load,
    so a missing or broken ytmusicapi only disables this source instead of
    stopping the app from starting."""
    import functools

    import requests
    from ytmusicapi import YTMusic

    session = requests.Session()
    session.request = functools.partial(session.request, timeout=REQUEST_TIMEOUT_S)
    return YTMusic(requests_session=session)


def _artists_match(wanted: str, result: dict) -> bool:
    wanted = _normalize(wanted)
    if not wanted:
        return False
    names = [_normalize(str(a.get("name", ""))) for a in (result.get("artists") or [])]
    names = [n for n in names if n]
    joined = " ".join(names)
    for name in names + ([joined] if joined else []):
        if name == wanted or name in wanted or wanted in name:
            return True
    return False


def _result_seconds(result: dict) -> int:
    seconds = result.get("duration_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        return int(seconds)
    text = str(result.get("duration") or "")
    try:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    except ValueError:
        return 0


def choose_best_song(results: list, *, title: str, artist: str, duration_ms: int = 0) -> Optional[dict]:
    """The search result that's the same song as ours, or None. Public so
    the matching rules can be tested without the network."""
    wanted_title = _normalize(title)
    wanted_seconds = round(duration_ms / 1000) if duration_ms else 0
    best = None
    best_difference = None
    for result in results or []:
        if not isinstance(result, dict) or not result.get("videoId"):
            continue
        if _normalize(str(result.get("title", ""))) != wanted_title:
            continue
        if not _artists_match(artist, result):
            continue
        seconds = _result_seconds(result)
        if wanted_seconds and seconds:
            difference = abs(wanted_seconds - seconds)
            if difference > MAX_DURATION_DIFF_S:
                continue
        else:
            difference = MAX_DURATION_DIFF_S  # unknown length ranks below a close one
        if best_difference is None or difference < best_difference:
            best, best_difference = result, difference
    return best


def _format_time(ms: int) -> str:
    ms = max(int(ms), 0)
    minutes, rest = divmod(ms, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{millis // 10:02d}]"


def timed_lines_to_lrc(lines) -> str:
    """YouTube Music's timed lines (ytmusicapi `LyricLine`s, or dicts with
    the same fields) as LRC text. A line that's only a music note or blank
    is kept as an empty timed line - an instrumental break, the same way
    an .lrc marks one."""
    out = []
    for line in lines or []:
        if isinstance(line, dict):
            text, start = line.get("text", ""), line.get("start_time", 0)
        else:
            text, start = getattr(line, "text", ""), getattr(line, "start_time", 0)
        text = (text or "").strip()
        if text == "♪":
            text = ""
        out.append(f"{_format_time(start)}{text}")
    return "\n".join(out)


def fetch_lyrics_text(
    *,
    title: str,
    artist: str,
    album: str = "",
    duration_ms: int = 0,
    client=None,
) -> LyricsTextFetch:
    """Look a track up on YouTube Music and hand back its lyric text - the
    same result shape as `lyrics_downloader.fetch_lyrics_text` (LRCLIB), so
    callers treat the two sources alike. Never writes anything. `album` is
    accepted for that symmetry but not used: YouTube Music's song search
    doesn't take one, and the title/artist/length match is enough.

    `client` is for tests - a stand-in for ytmusicapi's `YTMusic`."""
    if not artist:
        return LyricsTextFetch("no_artist")
    if not title:
        return LyricsTextFetch("not_found")
    try:
        yt = client if client is not None else _make_client()
        results = yt.search(f"{artist} {title}", filter="songs", limit=SEARCH_LIMIT)
        song = choose_best_song(results, title=title, artist=artist, duration_ms=duration_ms)
        if song is None:
            return LyricsTextFetch("not_found")
        watch = yt.get_watch_playlist(videoId=song["videoId"], limit=1)
        browse_id = (watch or {}).get("lyrics")
        if not browse_id:
            return LyricsTextFetch("not_found")
        found = yt.get_lyrics(browse_id, timestamps=True)
    except Exception as exc:  # unofficial API: anything can change under us
        log.warning("YouTube Music lyrics lookup failed for %s - %s: %s", artist, title, exc)
        return LyricsTextFetch("error", detail=str(exc))

    if not found or not found.get("lyrics"):
        return LyricsTextFetch("not_found")
    if found.get("hasTimestamps"):
        synced = timed_lines_to_lrc(found["lyrics"])
        plain = "\n".join(
            line for line in (l.split("]", 1)[1] for l in synced.splitlines()) if line
        )
        return LyricsTextFetch("found", plain=plain, synced=synced)
    plain = str(found["lyrics"]).strip()
    if not plain:
        return LyricsTextFetch("not_found")
    return LyricsTextFetch("found", plain=plain)
