"""Subtitles for videos: a `.srt` file beside the video, shown as a caption
bar under the picture (ui/widgets/video_panel.py).

`find_subtitles` looks for `Song.srt` beside `Song.mp4` (any capitalization
of the extension), then a language-tagged one (`Song.en.srt`, English
first). `load_captions` reads it into `Captions`, which answers "what's on
screen at this moment" (`text_at`).

The parser takes .srt files as they come from YouTube downloads, which
vary: auto-generated lowercase lines with "[Music]", "♪ ... ♪" lyrics
split over two lines, "(crowd cheering)" sound cues, and cues that share
their start time and belong on screen together (both are shown, one above
the other). UTF-8 with or without a BOM, falling back to Windows-1252;
CRLF or LF; formatting tags (<i>, <font ...>, {\\an8}) are dropped. A
block that can't be read is skipped, never an error - a broken .srt just
means fewer captions.
"""

from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: a bigger file isn't a subtitle file - don't read it
MAX_SRT_BYTES = 2_000_000

_TIME = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")
_TAG = re.compile(r"<[^>]*>|\{\\[^}]*\}")
_BLOCK_SPLIT = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


def _ms(h: str, m: str, s: str, frac: str) -> int:
    # "5" after the comma is 500 ms, "05" is 50 ms - pad to three digits
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues: list[Cue] = []
    for block in _BLOCK_SPLIT.split(text.strip()):
        lines = block.split("\n")
        at = next((i for i, line in enumerate(lines) if _TIME.search(line)), None)
        if at is None:
            continue
        g = _TIME.search(lines[at]).groups()
        start, end = _ms(*g[:4]), _ms(*g[4:])
        body = [_TAG.sub("", line).strip() for line in lines[at + 1:]]
        body = [line for line in body if line]
        if not body or end <= start:
            continue
        cues.append(Cue(start, end, "\n".join(body)))
    cues.sort(key=lambda c: (c.start_ms, c.end_ms))
    return cues


class Captions:
    """The cues of one video, looked up by playback position."""

    def __init__(self, cues: list[Cue]) -> None:
        self.cues = cues
        self._starts = [c.start_ms for c in cues]
        #: no cue lasts longer than this, which bounds how far back
        #: text_at has to look for one still on screen
        self._longest = max((c.end_ms - c.start_ms for c in cues), default=0)

    def __len__(self) -> int:
        return len(self.cues)

    def text_at(self, ms: int) -> str:
        """Everything on screen at `ms` - usually one cue; several that
        overlap are stacked in order. "" between cues."""
        hi = bisect.bisect_right(self._starts, ms)
        lo = bisect.bisect_left(self._starts, ms - self._longest)
        showing = [c.text for c in self.cues[lo:hi] if c.end_ms > ms]
        # the same words twice in a row (a repeated cue) only once
        out: list[str] = []
        for text in showing:
            if not out or out[-1] != text:
                out.append(text)
        return "\n".join(out)


def find_subtitles(video_path: str | Path) -> Optional[Path]:
    """`Song.srt` beside `Song.mp4`, else `Song.en.srt` (or another
    language-tagged one). None if there's none."""
    path = Path(video_path)
    folder, stem = path.parent, path.stem
    try:
        names = [p for p in folder.iterdir() if p.suffix.lower() == ".srt" and p.is_file()]
    except OSError:
        return None
    exact = [p for p in names if p.stem == stem]
    if not exact:
        exact = [p for p in names if p.stem.casefold() == stem.casefold()]
    if exact:
        return exact[0]
    tagged = [p for p in names if p.stem.casefold().startswith(stem.casefold() + ".")]
    if not tagged:
        return None
    english = [p for p in tagged if p.stem.casefold().split(".")[-1] in ("en", "eng", "english")]
    return (english or sorted(tagged))[0]


def read_srt(path: Path) -> str:
    raw = path.read_bytes()[:MAX_SRT_BYTES]
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def load_captions(video_path: str | Path) -> Optional[Captions]:
    """The captions for this video, or None when it has no readable .srt."""
    srt = find_subtitles(video_path)
    if srt is None:
        return None
    try:
        cues = parse_srt(read_srt(srt))
    except OSError:
        log.warning("couldn't read subtitles %s", srt)
        return None
    return Captions(cues) if cues else None
