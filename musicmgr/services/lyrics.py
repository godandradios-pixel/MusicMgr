"""Lyrics: read a sibling .lrc file for a track's audio file.

Deliberately not a database feature - no `Track.lyrics` column, no scanner
changes, no rescan needed. `find_lyrics_path()` just checks for a file next
to the audio file with the same name and a `.lrc` extension (the
near-universal sidecar convention: foobar2000, MusicBee, and most
rippers/taggers that produce lyric files all use this shape), and
`load_lyrics()` reads and parses it at playback time. Drop a matching .lrc
next to a track and it shows up the next time that track plays - nothing
else to do. This matches the rest of the app's "read from local files, no
network calls" stance (see architecture notes) - fetching lyrics from an
online service is a different, bigger decision and isn't what this does.

LRC format: `[mm:ss.xx]lyric text`, one line per tag, optionally more than
one time tag on one line for a repeated chorus (`[00:12.34][00:45.67]text`),
plus metadata tags like `[ar:...]`/`[ti:...]`/`[by:...]` that aren't lyric
lines at all. A file with no time tags anywhere is still shown, just
unsynced (plain scrolling text, no highlight-as-you-go).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union


@dataclass
class LyricLine:
    #: None on an untimed line - either the whole file has no timestamps at
    #: all, or (rare) one line lacks its own tag.
    time_ms: Optional[int]
    text: str


@dataclass
class LyricsResult:
    lines: list[LyricLine] = field(default_factory=list)
    #: True once at least one line carries a real timestamp - drives
    #: whether LyricsPanel highlights/auto-scrolls or just shows plain text.
    synced: bool = False
    source_path: Optional[Path] = None


_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_META_TAG = re.compile(r"^\[([a-zA-Z]+):(.*)\]$")
#: word-level ("enhanced") LRC timing inside a line, e.g. "<00:12.34>Some" -
#: stripped for display since this only does line-level sync, not karaoke
#: word-by-word.
_WORD_TAG = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")


def find_lyrics_path(audio_path: Union[str, Path, None]) -> Optional[Path]:
    if not audio_path:
        return None
    candidate = Path(audio_path).with_suffix(".lrc")
    return candidate if candidate.is_file() else None


def _read_text(path: Path) -> str:
    """Same UTF-8-first, cp1252-fallback approach as chart CSV import
    (`services/charts.py:_read_csv_text`) - lyric files circulating online
    are just as likely to carry stray Windows-1252 bytes as chart CSVs are."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_lrc(text: str) -> LyricsResult:
    offset_ms = 0
    timed_lines: list[LyricLine] = []
    untimed_lines: list[str] = []
    any_timed = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        tags = list(_TIME_TAG.finditer(line))
        if not tags:
            meta = _META_TAG.match(line)
            if meta:
                key, value = meta.group(1).lower(), meta.group(2).strip()
                if key == "offset":
                    # [offset:+/-ms] - shifts every timestamp by this many ms.
                    # Convention used here: a positive offset means the tagged
                    # times run late, so it's subtracted to bring them
                    # forward. Uncommon tag in practice; flip the sign below
                    # if a real file ever disagrees.
                    try:
                        offset_ms = int(value)
                    except ValueError:
                        pass
                continue  # ar/ti/al/by/re/ve/length/... - not a lyric line
            untimed_lines.append(line)
            continue

        any_timed = True
        body = _TIME_TAG.sub("", line)
        body = _WORD_TAG.sub("", body).strip()
        for m in tags:
            minutes, seconds = int(m.group(1)), int(m.group(2))
            frac = m.group(3) or "0"
            # ".xx" is centiseconds, ".xxx" is milliseconds - pad/trim to 3
            # digits either way so this always lands in milliseconds.
            frac_ms = int((frac + "000")[:3])
            ms = minutes * 60_000 + seconds * 1000 + frac_ms - offset_ms
            timed_lines.append(LyricLine(time_ms=max(0, ms), text=body))

    if any_timed:
        timed_lines.sort(key=lambda l: l.time_ms)
        return LyricsResult(lines=timed_lines, synced=True)

    plain = [LyricLine(time_ms=None, text=t) for t in untimed_lines]
    return LyricsResult(lines=plain, synced=False)


def load_lyrics(audio_path: Union[str, Path, None]) -> Optional[LyricsResult]:
    """None means no .lrc file exists for this track - distinct from an
    empty LyricsResult, which means one exists but is blank/unparseable."""
    path = find_lyrics_path(audio_path)
    if path is None:
        return None
    try:
        text = _read_text(path)
    except OSError:
        return None
    result = parse_lrc(text)
    result.source_path = path
    return result


def current_line_index(lines: Sequence[LyricLine], position_ms: int) -> int:
    """Index of the last timed line whose time_ms <= position_ms, or -1
    before the first line (or if nothing in `lines` is timed)."""
    idx = -1
    for i, line in enumerate(lines):
        if line.time_ms is None:
            continue
        if line.time_ms <= position_ms:
            idx = i
        else:
            break
    return idx
