"""Repair chart text whose accented letters were mangled on the way in.

2026-10-30 (James): the Charts page showed "Expos�" for Exposé. Two
kinds of damage reach the database from chart CSVs:

- **U+FFFD** ("�", drawn as a diamond with a question mark) - the
  accented letter is gone; something decoded the file with
  ``errors="replace"`` before MusicMgr ever saw it, or an older MusicMgr did.
  The letter can't be worked out from the text alone, so it is looked up:
  every � stands for one or two *non-ASCII* characters (decoding never
  replaces a plain ASCII byte), so "Expos�" is matched against the names
  already known - library artists and titles, and chart rows that came in
  clean - as ``Expos[^\\x00-\\x7f]{1,2}``. Only one distinct answer counts;
  two different candidates leave the text alone rather than guess.
- **Mojibake** - UTF-8 read as Windows-1252, so "Exposé" arrives as
  "ExposÃ©". That one is reversible from the text itself.

`decode_csv_bytes` is the reading side: UTF-8, with any byte that isn't
valid UTF-8 read as Windows-1252 *on its own* - one stray cp1252 byte in an
otherwise UTF-8 file no longer turns every other accent in the file into
mojibake (the old whole-file fallback did).

`ChartTextRepairer` fixes single strings; `repair_chart_entries` fixes rows
already in the library (Settings › Library health's "Chart songs with
garbled letters").
"""

from __future__ import annotations

import codecs
import re
from collections import Counter
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

REPLACEMENT = "�"

# -- reading -------------------------------------------------------------------


def _cp1252_fallback(err: UnicodeDecodeError):
    bad = err.object[err.start:err.end]
    return bad.decode("cp1252", errors="replace"), err.end


codecs.register_error("musicmgr_cp1252", _cp1252_fallback)


def decode_csv_bytes(raw: bytes) -> str:
    """UTF-8 (BOM dropped); bytes that aren't valid UTF-8 are read one at a
    time as Windows-1252. The five bytes cp1252 leaves undefined still
    become � - and the repairer can usually work those out."""
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    return raw.decode("utf-8", errors="musicmgr_cp1252")


# -- mojibake ------------------------------------------------------------------

#: what UTF-8 accents look like read as cp1252: Ã© Ã¶ Ã± â€™ Â° ...
_MOJIBAKE = re.compile("[ÂÃÅÎÏÐÑ][\u0080-¿ŒœŠšŸŽžƒˆ˜–—‘-„†-•…‰‹›€™]|â€")


def fix_mojibake(text: str) -> str:
    """"ExposÃ©" -> "Exposé". Returns `text` unchanged unless the round trip
    works cleanly and leaves no mojibake behind."""
    if not text or not _MOJIBAKE.search(text):
        return text
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not _MOJIBAKE.search(fixed):
            return fixed
    return text


def is_damaged(text: Optional[str]) -> bool:
    return bool(text) and (REPLACEMENT in text or bool(_MOJIBAKE.search(text)))


# -- U+FFFD --------------------------------------------------------------------
#
# 2026-10-30, second pass (James's library: 1,031 rows, none fixed by the
# first pass). In practice a \ufffd in a chart row is usually NOT a lost
# letter - the year-end lists came from Wikipedia, which puts non-breaking
# spaces around "featuring"/"and"/"&", and each one became \ufffd:
#   "Coolio\ufffdfeaturing\ufffdL.V."       -> a space
#   "Beyonc\ufffdfeaturing\ufffdSean Paul"  -> é *and* the space (one \ufffd
#                                         swallowed both bytes)
#   "M\ufffda", "Thal\ufffda", "Eric Ben\ufffdt" -> a letter inside a word
#   "Diddy \ufffd Dirty Money"           -> a dash between two spaces
# So each \ufffd is decided on its own, from the words either side of it:
# a letter when a known word fits (library + chart names + the short list
# below), otherwise a space when it sits between two non-space characters,
# a dash when it sits between two spaces, else it's left alone.

#: accented names that turn up on the charts, for when the library doesn't
#: have the artist itself
KNOWN_WORDS = (
    "Beyoncé", "Rosé", "Mýa", "Thalía", "MØ", "Chanté", "Johntá", "Jhené", "Benét",
    "Zhané", "Solé", "André", "Exposé", "Río", "Céline", "Sinéad", "Björk", "Mötley",
    "Crüe", "Ramírez", "José", "Señorita", "Café", "Fiancé", "Déjà", "Rós", "Gómez",
    "Hernández", "Martínez", "Rodríguez", "Pérez", "Peña", "Muñoz", "Ibáñez", "Niño",
    "Tiësto", "Motörhead", "Blue Öyster", "Röyksopp", "Mónica", "Bebé", "Corazón",
    "Canción", "Amor", "Sueño", "Mañana", "Pequeño", "Él", "Así", "Más",
)

_WORD = re.compile(r"[^\W\d_]+")
_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]{1,2}")


def _pattern(damaged: str, gap: str) -> re.Pattern:
    parts = damaged.split(REPLACEMENT)
    return re.compile(gap.join(re.escape(p) for p in parts), re.IGNORECASE)


#: a lost letter was one or two non-ASCII characters...
_NON_ASCII = "[^\\x00-\\x7f]{1,2}"
#: ...unless the library only has it spelled without the accent ("Expose")
_ANY_LETTER = "[^\\W\\d_]"


class ChartTextRepairer:
    """Fixes chart titles and artists against a set of known-good names."""

    def __init__(self, names: Iterable[str], known_words: Iterable[str] = KNOWN_WORDS) -> None:
        self._names: list[str] = []
        #: (letters before, letters after) the accented run, lowercased ->
        #: the run as spelled: "Beyoncé" files ("beyonc", "") -> "é"
        self._fills: dict[tuple[str, str], Counter] = {}
        #: how often each whole word turns up, lowercased: "Man" (Method Man,
        #: Beenie Man...) is far more common than "Maná", so "Method
        #: Man\ufffdfeaturing" gets a space; "Ros" is no more common than "Rosé",
        #: so "Ros\ufffdand Bruno Mars" gets the é
        self._plain: Counter = Counter()
        seen: set[str] = set()
        for name in names:
            if not name or is_damaged(name) or name in seen:
                continue
            seen.add(name)
            self._names.append(name)
            self._learn(name)
        for word in known_words:
            self._learn(word)
        self._cache: dict[str, str] = {}

    def _learn(self, text: str) -> None:
        for word in _WORD.findall(text):
            self._plain[word.lower()] += 1
            for m in _NON_ASCII_RUN.finditer(word):
                key = (word[:m.start()].lower(), word[m.end():].lower())
                if key != ("", ""):
                    self._fills.setdefault(key, Counter())[m.group()] += 1

    @classmethod
    def from_library(cls, session: Session) -> "ChartTextRepairer":
        from ..db.models import Artist, ChartEntry, Track

        def col(c):
            return session.scalars(select(c).distinct()).all()

        return cls([*col(Artist.name), *col(Track.artist_display), *col(Track.title),
                    *col(ChartEntry.artist_name), *col(ChartEntry.title)])

    @staticmethod
    def _one(candidates: Iterable[str], counts: Optional[Counter] = None) -> Optional[str]:
        """The single answer, case aside (most-used spelling wins), or None
        when the candidates disagree."""
        by_fold: dict[str, Counter] = {}
        for c in candidates:
            by_fold.setdefault(c.casefold(), Counter())[c] += (counts or {}).get(c, 1)
        if len(by_fold) != 1:
            return None
        return next(iter(by_fold.values())).most_common(1)[0][0]

    def _lookup(self, damaged: str) -> Optional[str]:
        for gap in (_NON_ASCII, _ANY_LETTER):
            pat = _pattern(damaged, gap)
            hit = self._one(n for n in self._names if pat.fullmatch(n))
            if hit:
                return hit
        return None

    def _letter(self, before: str, after: str) -> Optional[str]:
        found = self._fills.get((before.lower(), after.lower()))
        return self._one(found, found) if found else None

    def _decide(self, left: str, right: str) -> Optional[str]:
        """What one \ufffd between `left` and `right` stood for, or None."""
        lc, rc = left[-1:], right[:1]
        m = re.search(r"[^\W\d_]+$", left)
        wl = m.group() if m else ""
        m = re.match(r"[^\W\d_]+", right)
        wr = m.group() if m else ""
        if wl and wr:
            x = self._letter(wl, wr)                  # "M\ufffda" -> "Mýa"
            if x:
                return x
        joined = bool(rc) and not rc.isspace()      # a space could fit here
        if wl and (len(wl) >= 2 or not joined):
            x = self._letter(wl, "")                  # "Beyonc\ufffd..." -> "Beyoncé"
            if x and not (joined and self._plain[wl.lower()] > self._plain[(wl + x).lower()]):
                return x + (" " if joined and rc not in ",.;:!?)" else "")
        if wr and not wl:
            x = self._letter("", wr)
            if x:
                return x
        if lc and rc and not lc.isspace() and not rc.isspace():
            return " "                                # a non-breaking space
        if lc == " " and rc == " ":
            return "\u2013"                           # "Diddy \ufffd Dirty Money"
        return None

    def _fill(self, text: str) -> str:
        whole = self._lookup(text)
        if whole:
            return whole
        parts = text.split(REPLACEMENT)
        out = [parts[0]]
        for i in range(1, len(parts)):
            # what's been decided so far, so "Ben\ufffdt\ufffdfeaturing" sees
            # "Benét" before its second \ufffd, not just "t"
            choice = self._decide("".join(out), parts[i])
            out.append(REPLACEMENT if choice is None else choice)
            out.append(parts[i])
        return re.sub(r"  +", " ", "".join(out)).strip()

    def repair(self, text: Optional[str]) -> Optional[str]:
        if not text or not is_damaged(text):
            return text
        if text not in self._cache:
            fixed = fix_mojibake(text)
            if REPLACEMENT in fixed:
                fixed = self._fill(fixed)
            self._cache[text] = fixed
        return self._cache[text]


# -- rows already in the library ------------------------------------------------


def _damaged_filter(model):
    conds = []
    for col in (model.title, model.artist_name):
        conds.append(col.contains(REPLACEMENT))
        for marker in ("Ã", "Â", "â€"):
            conds.append(col.contains(marker))
    return or_(*conds)


def damaged_chart_rows(session: Session) -> list:
    """(id, title, artist) of chart rows that look garbled."""
    from ..db.models import ChartEntry

    rows = session.execute(
        select(ChartEntry.id, ChartEntry.title, ChartEntry.artist_name)
        .where(_damaged_filter(ChartEntry))
    ).all()
    # the SQL markers are broad ("Â" alone): keep only real damage
    return [r for r in rows if is_damaged(r[1]) or is_damaged(r[2])]


def repair_chart_entries(session: Session, ids: Optional[Iterable[int]] = None,
                         threshold: float = 0.72) -> tuple[int, int]:
    """Repair garbled chart rows (all of them, or just `ids`), then match
    the fixed rows that aren't linked to a song yet. Charts touched are
    marked edited so the fix wins the next USB sync. Returns (rows fixed,
    rows still garbled)."""
    from ..db.models import ChartEntry, ChartIssue
    from .charts import match_entry
    from .chart_sync import mark_edited
    from .matching import normalize

    if ids is None:
        ids = [r[0] for r in damaged_chart_rows(session)]
    ids = list(ids)
    if not ids:
        return 0, 0
    repairer = ChartTextRepairer.from_library(session)
    fixed = left = 0
    issue_ids: set[int] = set()
    for start in range(0, len(ids), 500):
        for e in session.scalars(select(ChartEntry).where(ChartEntry.id.in_(ids[start:start + 500]))):
            title, artist = repairer.repair(e.title), repairer.repair(e.artist_name)
            if (title, artist) != (e.title, e.artist_name):
                e.title, e.artist_name = title, artist
                e.title_key, e.artist_key = normalize(title), normalize(artist)
                fixed += 1
                issue_ids.add(e.issue_id)
                if e.track_id is None and not e.match_locked:
                    session.flush()
                    match_entry(session, e, threshold)
            if is_damaged(e.title) or is_damaged(e.artist_name):
                left += 1
    session.flush()
    if issue_ids:
        for cid in set(session.scalars(
                select(ChartIssue.chart_id).where(ChartIssue.id.in_(issue_ids)))):
            mark_edited(session, cid)
    return fixed, left
