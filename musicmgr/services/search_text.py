"""Accent- and case-blind text for search boxes.

Searching "expose" should find Exposé, "beyonce" Beyoncé, "motley crue"
Mötley Crüe, and "dont" or "don't" a title tagged with a curly apostrophe
(Don’t). Text compared as typed misses all of these: SQLite's ILIKE only
folds A-Z.

`fold` is the one rule every search uses - accents dropped, case folded
(ß -> ss too), letters NFKD can't split (ø æ œ ł đ þ) spelled out, curly
quotes and long dashes made plain. It's applied to *both* sides, so typing
the accent still works ("Exposé" finds a file tagged "Expose" as well).

- In Python (filter boxes): ``fold(needle) in fold(text)``. Memoised - the
  same titles are folded on every keystroke.
- In SQL: db/session.py registers it on every connection as ``mm_fold()``;
  `fold_ilike(col, pattern)` is the drop-in for ``col.ilike(pattern)``.

Only search uses this. Matching chart rows to songs has its own, stricter
key (matching.normalize).
"""

from __future__ import annotations

import functools
import unicodedata
from typing import Optional

from sqlalchemy import func

#: letters NFKD leaves whole, and typography people don't type
_SPELL_OUT = str.maketrans({
    "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe",
    "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d",
    "þ": "th", "Þ": "th", "ı": "i",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    " ": " ",
})


@functools.lru_cache(maxsize=262_144)
def fold(text: Optional[str]) -> str:
    """"Mötley Crüe" -> "motley crue", "Exposé" -> "expose",
    "Don’t" -> "don't"."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", str(text).translate(_SPELL_OUT))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold().translate(_SPELL_OUT)


def matches(needle: str, *texts: Optional[str]) -> bool:
    """`needle` (already folded) found in any of `texts`."""
    return any(needle in fold(t) for t in texts if t)


def fold_ilike(column, pattern: str):
    """Accent-blind ``column.ilike(pattern)`` - % and _ keep their meaning."""
    return func.mm_fold(column).like(fold(pattern))


def register(dbapi_conn) -> None:
    """Make ``mm_fold()`` callable in SQL on this sqlite3 connection."""
    dbapi_conn.create_function("mm_fold", 1, fold, deterministic=True)
