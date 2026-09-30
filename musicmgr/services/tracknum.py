"""Track numbers that aren't numbers (2026-09-30).

James's 78 rpm rips are tagged with the record side as the track number -
"A" and "B" - because that's what matters for a physical 78: "The track
number is important in the playlist because I indicate Side A and Side
B." Vinyl rips use the same idea with "A1", "A2", "B1".

`int("A")` has no answer, so until now those files came in with no track
number at all, and a record's two sides sorted by title. Now:

- `Track.position` keeps the side as written ("A", "B", "A1"), and is what
  every track list shows;
- `Track.track_no` gets a number that sorts the sides in record order:
  A = 1, B = 2, ... and, with a number after the letter, side * 100 + n
  (A1 = 101, A2 = 102, B1 = 201). Plain numeric tags are untouched.
"""

from __future__ import annotations

import re
from typing import Optional

_SIDE = re.compile(r"^\s*(?:side\s*)?([A-Za-z])\s*[-.]?\s*(\d{1,2})?\s*$", re.IGNORECASE)


def parse_side(text: Optional[str]) -> Optional[tuple[int, str]]:
    """'A' -> (1, 'A'), 'b' -> (2, 'B'), 'A2' -> (102, 'A2'), 'Side B' ->
    (2, 'B'); anything else (including plain numbers) -> None."""
    if not text:
        return None
    m = _SIDE.match(str(text).split("/")[0])
    if not m:
        return None
    side = m.group(1).upper()
    order = ord(side) - ord("A") + 1
    if m.group(2):
        n = int(m.group(2))
        return order * 100 + n, f"{side}{n}"
    return order, side


def is_side(position: Optional[str]) -> bool:
    return bool(position) and parse_side(position) is not None


def label(track_no: Optional[int], position: Optional[str]) -> str:
    """What a track list shows in its # column: the side ("A", "B2") when
    the track has one, else the plain track number."""
    if is_side(position):
        return position  # type: ignore[return-value]
    return str(track_no) if track_no else ""
