"""Text normalisation and fuzzy matching.

Used in two places:
  * de-duplicating artists/releases while scanning ("The Beatles" == "Beatles, The")
  * linking imported chart rows to tracks you actually own
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable, Optional, Sequence, Tuple

_ARTICLES = ("the ", "a ", "an ")

#: matches the leading article plus ALL the whitespace after it, however much
#: there is - a fixed "the " (4 chars) slice missed a stray double space in a
#: real tag ("The  Romantics"), leaving a leading space in the sort key that
#: then sorted before every other artist (a space is "less than" any letter
#: or digit). See sort_name() below.
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)

#: parenthetical noise that should not affect matching
_NOISE_PATTERNS = [
    r"\((?:feat|ft|featuring|with)\.?[^)]*\)",
    r"\[(?:feat|ft|featuring|with)\.?[^\]]*\]",
    r"\((?:remaster(?:ed)?|mono|stereo|live|bonus track|deluxe|explicit|"
    r"radio edit|single version|album version|\d{4} (?:remaster|mix|version))[^)]*\)",
    r"\[(?:remaster(?:ed)?|mono|stereo|live|bonus track|deluxe|explicit)[^\]]*\]",
    r"\s-\s(?:\d{4}\s)?remaster(?:ed)?(?:\s\d{4})?$",
]

_FEAT_SPLIT = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring|with|&|\+|,|and|x)\s+", re.IGNORECASE
)


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize(text: Optional[str]) -> str:
    """Aggressive key used for equality comparison and DB dedup columns."""
    if not text:
        return ""
    s = strip_accents(str(text)).lower().strip()
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for art in _ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    return s


def sort_name(name: str) -> str:
    """'The Beatles' -> 'Beatles, The' for alphabetical browsing.

    Strips *all* the whitespace between the article and the rest of the
    name, not just a fixed one-space-wide slice - a tag with a stray extra
    space ("The  Romantics") would otherwise leave that space as the first
    character of the sort key, sorting the artist ahead of everything else.
    """
    match = _ARTICLE_RE.match(name)
    if not match:
        return name
    rest = name[match.end():].strip()
    return f"{rest}, {match.group(1)}"


def split_artists(text: Optional[str]) -> list[str]:
    """Break a credit string into individual artist names.

    'Daft Punk feat. Pharrell Williams & Nile Rodgers'
        -> ['Daft Punk', 'Pharrell Williams', 'Nile Rodgers']
    """
    if not text:
        return []
    parts = [p.strip(" -/") for p in _FEAT_SPLIT.split(str(text))]
    return [p for p in parts if p]


def primary_artist(text: Optional[str]) -> str:
    parts = split_artists(text)
    return parts[0] if parts else (text or "")


def similarity(a: str, b: str) -> float:
    """0.0-1.0 similarity of two already-normalised strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # containment bonus: "billie jean" vs "billie jean single version"
    if a in b or b in a:
        ratio = max(ratio, 0.9)
    # token overlap catches word reordering
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        ratio = max(ratio, jaccard * 0.95)
    return ratio


#: below this artist similarity a chart entry never matches, however exact
#: the title (see score_pair)
MIN_ARTIST_SIMILARITY = 0.45
#: comparing individual names out of "A featuring B" / "A & B" credits must
#: be this close to count - two different people sharing a surname
#: ("Chuck Jackson" / "Jermaine Jackson") shouldn't
NAME_PAIR_SIMILARITY = 0.75


def artist_similarity(artist_a: str, artist_b: str) -> float:
    """How alike two artist credits are: the whole credits, or - for
    "Missy Elliott Featuring Ludacris" vs "Missy Elliott ft. Ciara" - the
    two lead artists. Only the lead names count: "Future Featuring Drake"
    and "DJ Khaled ft. Drake" share Drake but aren't the same record."""
    full = similarity(normalize(artist_a), normalize(artist_b))
    lead = similarity(normalize(primary_artist(artist_a)), normalize(primary_artist(artist_b)))
    return max(full, lead if lead >= NAME_PAIR_SIMILARITY else 0.0)


def score_pair(
    title_a: str, artist_a: str, title_b: str, artist_b: str
) -> float:
    """Combined title+artist confidence, title weighted more heavily.

    2026-10-01 - the artist now always has to be reasonably close. This
    used to let an exact title through with almost any artist ("if a < 0.45
    and t < 0.95"), which on James's library had linked about 29,600 of
    285,600 chart entries to someone else's song of the same name - Phil
    Collins to Tom Jones, and two 1949 hits that charted for two artists at
    once (Cruising Down the River, Forever and Ever) both to Russ Morgan's
    record, so a 30-song chart saved as a 28-song playlist. Found via
    exactly that: James's Billboard 1949 Year End playlist coming up short.
    """
    t = similarity(normalize(title_a), normalize(title_b))
    a = artist_similarity(artist_a, artist_b)
    if a < MIN_ARTIST_SIMILARITY:
        return 0.0
    return round(t * 0.65 + a * 0.35, 4)


def best_match(
    title: str,
    artist: str,
    candidates: Sequence[Tuple[int, str, str]],
    threshold: float = 0.72,
) -> Optional[Tuple[int, float]]:
    """Pick the best (id, score) from `candidates` of (id, title, artist)."""
    best: Optional[Tuple[int, float]] = None
    for cid, ctitle, cartist in candidates:
        score = score_pair(title, artist, ctitle or "", cartist or "")
        if score >= threshold and (best is None or score > best[1]):
            best = (cid, score)
    return best
