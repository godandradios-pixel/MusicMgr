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

Saving overwrites the .lrc in place with no backup - a `.bak` beside every
edited track would only clutter the USB sync, and a mangled file is easy to
replace by fetching the lyrics from LRCLIB again.
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


def to_timed_text(lines: Sequence[EditLine]) -> str:
    """The "Edit text…" box's contents: one row per line, each stamped line
    led by its `[mm:ss.xx]` so the times can be edited as text too
    (2026-09-30 - James: "if you edit lyrics with timestamps it removes the
    timestamps when you save, I would like the edit lyrics to allow edit
    of timestamps"). A timed empty row is a music break and is kept."""
    rows = []
    for line in lines:
        if line.time_ms is None:
            rows.append(line.text)
        else:
            rows.append(f"[{format_timestamp(line.time_ms)}]{line.text}")
    return "\n".join(rows)


def _tag_ms(match) -> int:
    minutes, seconds, frac = match.group(1), match.group(2), match.group(3) or "0"
    frac_ms = int(frac.ljust(3, "0")[:3])
    return (int(minutes) * 60 + int(seconds)) * 1000 + frac_ms


def from_timed_text(old_lines: Sequence[EditLine], text: str) -> list[EditLine]:
    """Read the "Edit text…" box back.

    - A row starting with `[mm:ss.xx]` gets exactly that time - that's how a
      time is edited. Several leading tags (`[00:12.00][00:45.00]chorus`)
      make one line per time, like any .lrc.
    - A row without a time keeps the time its words had before, where
      `retext`'s matching can find one (so text pasted without times, or a
      reworded line, doesn't lose its sync); otherwise it's unstamped.
    - A row that's only a time (`[01:02.30]`) is a music break; an empty row
      with no time is dropped. `[ti:...]`-style header rows are ignored.
    - Word-level `<mm:ss.xx>` tags inside a line are dropped, same as when
      a file is loaded."""
    rows: list[tuple[str, list[int]]] = []
    for raw in text.splitlines():
        row = raw.strip()
        if _META_TAG.match(row) and not _TIME_TAG.match(row):
            continue
        times = []
        while True:
            m = _TIME_TAG.match(row)
            if not m:
                break
            times.append(_tag_ms(m))
            row = row[m.end():].lstrip()
        row = _WORD_TAG.sub("", row).strip()
        if not row and not times:
            continue
        rows.append((row, times))

    # times for the untimed rows, from the words they had before
    untimed = [i for i, (row, times) in enumerate(rows) if not times and row]
    inherited = retext(old_lines, "\n".join(rows[i][0] for i in untimed))
    inherited_ms = {i: line.time_ms for i, line in zip(untimed, inherited)}

    result: list[EditLine] = []
    for i, (row, times) in enumerate(rows):
        if times:
            result.extend(EditLine(text=row, time_ms=t) for t in times)
        else:
            result.append(EditLine(text=row, time_ms=inherited_ms.get(i)))
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
    """Write the sidecar next to the audio file, overwriting any existing
    one (no backup is kept). Returns the written path. Raises OSError
    if the folder isn't writable - the dialog reports it."""
    lrc_path = Path(audio_path).with_suffix(".lrc")
    text = to_lrc(lines, title=title, artist=artist, album=album, duration_ms=duration_ms)
    lrc_path.write_text(text, encoding="utf-8")
    return lrc_path
