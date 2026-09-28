"""Lyric editor model: build and save a synced .lrc sidecar by hand.

2026-09-28 - James: "I would like a lyric editor in MusicMgr. One where it
can determine the timestamps and place in the LRC file." Settled with him
up front (AskUserQuestion): timestamps come from **tap-to-sync** (the song
plays and he taps "Stamp" as each line starts - no speech-recognition
model, no new dependency), the editor opens from **Now Playing's Lyrics
pane**, and the starting text comes from either the track's **existing
.lrc** or **LRCLIB's lyrics** (see `lyrics_downloader.fetch_lyrics_text`).

This module is the Qt-free half: the line list the editor works on, the
tap/nudge/shift operations, and turning that back into a file at the exact
sidecar path `services/lyrics.py:find_lyrics_path` reads from - so a saved
edit shows up in the Lyrics pane with nothing else to change. The dialog
itself lives in `ui/widgets/lyrics_editor.py`.

Saving keeps one backup of whatever .lrc was there before
(`<track>.lrc.bak`, overwritten each save) - a re-timing session that goes
badly can always be undone by hand.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from .lyrics import _META_TAG, _TIME_TAG, _WORD_TAG, LyricLine, _read_text, parse_lrc

#: smallest nudge step the editor offers, and the rounding unit of the
#: written file (LRC's ".xx" is centiseconds)
CENTISECOND_MS = 10


@dataclass
class EditLine:
    """One editable lyric line. `time_ms` is None until it's been stamped."""

    text: str
    time_ms: Optional[int] = None


# -- building the starting line list -------------------------------------


def lines_from_plain_text(text: str) -> list[EditLine]:
    """Plain lyrics (pasted, or LRCLIB's `plainLyrics`) -> unstamped lines.

    Blank lines (stanza breaks in plain lyrics) are dropped - in an LRC
    file a gap is expressed by timing, not by an empty row, and an empty
    row would be one more thing to stamp for no visible benefit. Any stray
    `[mm:ss.xx]` tags are stripped rather than trusted, so feeding this
    synced text gives a clean slate to re-time."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _META_TAG.match(line):
            continue
        line = _WORD_TAG.sub("", _TIME_TAG.sub("", line)).strip()
        if line:
            lines.append(EditLine(text=line))
    return lines


def lines_from_lyrics(lines: Sequence[LyricLine]) -> list[EditLine]:
    """An existing parsed .lrc (`services/lyrics.py:LyricsResult.lines`) ->
    editable lines, keeping whatever timestamps it already has. A repeated
    chorus written as `[00:12.00][00:45.00]text` arrives here already
    expanded into one line per timestamp (parse_lrc does that), which is
    exactly what an editor wants - each occurrence is timed on its own.
    Blank timed lines are kept: in an existing file they're deliberate
    "music break" markers."""
    return [EditLine(text=line.text, time_ms=line.time_ms) for line in lines]


def load_lines_for_editing(audio_path: Union[str, Path]) -> Optional[list[EditLine]]:
    """The track's existing sidecar as editable lines, or None if there
    isn't one (or it can't be read)."""
    path = Path(audio_path).with_suffix(".lrc")
    if not path.is_file():
        return None
    try:
        text = _read_text(path)
    except OSError:
        return None
    return lines_from_lyrics(parse_lrc(text).lines)


def retext(lines: Sequence[EditLine], new_text: str) -> list[EditLine]:
    """Replace the line text with `new_text` (one line per row, blanks
    dropped) while keeping every timestamp that still has a home.

    Lines whose text is unchanged keep their time wherever they moved to;
    a same-sized block of changed lines (fixing typos, rewording) keeps its
    times row for row; genuinely inserted lines arrive unstamped. So fixing
    a word in an already-synced file never throws away the sync."""
    old_texts = [line.text for line in lines]
    new_texts = [t.strip() for t in new_text.splitlines() if t.strip()]
    result = [EditLine(text=t) for t in new_texts]
    matcher = difflib.SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and a1 - a0 == b1 - b0):
            for offset in range(b1 - b0):
                result[b0 + offset].time_ms = lines[a0 + offset].time_ms
    return result


# -- editing operations --------------------------------------------------


def clamp_ms(ms: int, duration_ms: int = 0) -> int:
    ms = max(0, int(ms))
    if duration_ms:
        ms = min(ms, int(duration_ms))
    return ms


def stamp(lines: list[EditLine], index: int, position_ms: int, lead_ms: int = 0) -> int:
    """Stamp line `index` with the current playback position (minus
    `lead_ms`, to compensate for tap reaction time) and return the index of
    the next line to stamp - `index + 1`, or `index` itself on the last
    line so repeated taps there just re-stamp it."""
    if not (0 <= index < len(lines)):
        return index
    lines[index].time_ms = clamp_ms(position_ms - lead_ms)
    return index + 1 if index + 1 < len(lines) else index


def nudge(lines: list[EditLine], index: int, delta_ms: int, duration_ms: int = 0) -> None:
    """Move one already-stamped line earlier/later. An unstamped line is
    left alone - there's nothing to nudge from."""
    if 0 <= index < len(lines) and lines[index].time_ms is not None:
        lines[index].time_ms = clamp_ms(lines[index].time_ms + delta_ms, duration_ms)


def shift_all(lines: Iterable[EditLine], delta_ms: int, duration_ms: int = 0) -> None:
    """Move every stamped line by the same amount - the fix for a whole
    file that's consistently a little early or late."""
    for line in lines:
        if line.time_ms is not None:
            line.time_ms = clamp_ms(line.time_ms + delta_ms, duration_ms)


def out_of_order(lines: Sequence[EditLine]) -> list[int]:
    """Indices of stamped lines timed *earlier* than a stamped line above
    them - almost always a mis-tap. The editor flags these rather than
    silently re-sorting, since the text order is what James wrote."""
    bad = []
    latest: Optional[int] = None
    for i, line in enumerate(lines):
        if line.time_ms is None:
            continue
        if latest is not None and line.time_ms < latest:
            bad.append(i)
        else:
            latest = line.time_ms
    return bad


def active_line_index(lines: Sequence[EditLine], position_ms: int) -> int:
    """The line that would be highlighted at `position_ms` - the last
    stamped line at or before it (-1 if none). Unlike `services/lyrics.py:
    current_line_index`, this doesn't assume the list is sorted or stop at
    the first later line, since a half-stamped list isn't."""
    best, best_time = -1, -1
    for i, line in enumerate(lines):
        if line.time_ms is not None and best_time <= line.time_ms <= position_ms:
            best, best_time = i, line.time_ms
    return best


# -- writing -------------------------------------------------------------


def format_timestamp(ms: int) -> str:
    """`[mm:ss.xx]`-style body, rounded to the nearest centisecond. Minutes
    aren't capped at 99 - parse_lrc reads up to three digits."""
    cs = (max(0, int(ms)) + CENTISECOND_MS // 2) // CENTISECOND_MS
    minutes, rem = divmod(cs, 6000)
    seconds, hundredths = divmod(rem, 100)
    return f"{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def to_lrc(
    lines: Sequence[EditLine],
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration_ms: int = 0,
) -> str:
    """The .lrc text for `lines`. Stamped lines are written in time order
    (what every player expects); an unstamped line has no position in time
    to be written at, so it's left out - `unstamped_count` lets the dialog
    warn about that before saving. Standard `[ti:]/[ar:]/[al:]/[length:]`
    header tags are included when known, plus `[by:MusicMgr]`."""
    header = []
    if title:
        header.append(f"[ti:{title}]")
    if artist:
        header.append(f"[ar:{artist}]")
    if album:
        header.append(f"[al:{album}]")
    if duration_ms:
        total_s = int(round(duration_ms / 1000))
        header.append(f"[length:{total_s // 60:02d}:{total_s % 60:02d}]")
    header.append("[by:MusicMgr]")

    stamped = sorted(
        (line for line in lines if line.time_ms is not None), key=lambda l: l.time_ms
    )
    body = [f"[{format_timestamp(line.time_ms)}]{line.text}" for line in stamped]
    return "\n".join(header + [""] + body) + "\n"


def unstamped_count(lines: Sequence[EditLine]) -> int:
    return sum(1 for line in lines if line.time_ms is None)


def save_lrc(
    audio_path: Union[str, Path],
    lines: Sequence[EditLine],
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration_ms: int = 0,
) -> Path:
    """Write the sidecar next to the audio file, backing up any existing
    one to `<name>.lrc.bak` first. Returns the written path. Raises OSError
    if the folder isn't writable - the dialog reports it."""
    lrc_path = Path(audio_path).with_suffix(".lrc")
    if lrc_path.is_file():
        backup = lrc_path.with_name(lrc_path.name + ".bak")
        backup.write_bytes(lrc_path.read_bytes())
    text = to_lrc(lines, title=title, artist=artist, album=album, duration_ms=duration_ms)
    lrc_path.write_text(text, encoding="utf-8")
    return lrc_path
