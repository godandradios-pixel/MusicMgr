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

#: matches the leading article plus ALL the whitespace after it, so a stray
#: double space ("The  Romantics") can't leave a leading space in the sort
#: key. See sort_name() below.
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

    Strips *all* the whitespace after the article, so "The  Romantics"
    doesn't get a leading space that sorts it ahead of everything else.
    """
    match = _ARTICLE_RE.match(name)
    if not match:
        return name
    rest = name[match.end():].strip()
    return f"{rest}, {match.group(1)}"


#: acts whose own name has a comma or "&"/"and"/"+" in it - never split
PROTECTED_NAMES = (
    "Blood, Sweat & Tears", "Earth, Wind & Fire", "Emerson, Lake & Palmer",
    "Emerson, Lake & Powell", "Crosby, Stills, Nash & Young", "Crosby, Stills & Nash",
    "Peter, Paul & Mary", "Ray, Goodman & Brown", "Phantom, Rocker & Slick",
    "Phillips, Craig & Dean", "Dino, Desi & Billy", "Lambert, Hendricks & Ross",
    "Dave Dee, Dozy, Beaky, Mick & Tich", "Tyler, The Creator", "Lipps, Inc.",
    "Earth, Wind and Fire", "10,000 Maniacs",
    "for KING & COUNTRY", "for KING + COUNTRY",
)
_PROTECTED_RE = [
    re.compile(
        r"(?<!\w)" + r"\s*".join(
            r"(?:&|and)" if w == "&" else re.escape(w) for w in re.findall(r"&|[^\s&]+", name)
        ) + r"(?!\w)",
        re.IGNORECASE,
    )
    for name in sorted(PROTECTED_NAMES, key=len, reverse=True)
]
#: ", Jr." / ", Sr." / ", Inc." / ", III" belong to the name before them
_SUFFIX_RE = re.compile(r",\s*(?=(?:jr|sr|inc|ii|iii|iv)\b\.?)", re.IGNORECASE)
#: "Tommy Dorsey and His Orchestra", "Kay Kyser & Her ..." - one act
_HIS_RE = re.compile(r"\s+(?:and|&)\s+(?=(?:his|her|their)\b)", re.IGNORECASE)
#: "Nelly Furtado ft.Timbaland" - no space after the dot
_GLUED_FEAT_RE = re.compile(r"\s+(feat|ft)\.(?=\S)", re.IGNORECASE)
_LEADING_FEAT_RE = re.compile(r"^\s*(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
#: ", vocal refrain by Frank Sinatra" / ", vocal by ..."
_VOCAL_BY_RE = re.compile(r",?\s*\bvocal(?:\s+refrain)?\s+by\s+", re.IGNORECASE)
#: words inside a big-band credit's brackets that describe a role, not a name
_ROLE_WORDS = re.compile(
    r"\b(?:vocal\s+refrain\s+by|vocal\s+by|vocals?|refrain|featuring|feat\.?|ft\.?|with|"
    r"chorus|trio|quartet|quintet|band|group|singers?|ensemble|duet|male|female|"
    r"incl|v|voc|vcl)\b\.?",
    re.IGNORECASE,
)
_ROLE_HINT = re.compile(r"\b(?:vocal|vocals|refrain|feat|ft|featuring|with|chorus|trio|quartet|"
                        r"v|voc|vcl|incl)\b",
                        re.IGNORECASE)
#: a bracket that's a recording note, not a person: "(as 'Lloyd Keating')",
#: "[aka Barry Wood]", "(Decca version)", "(dir. Joe Moss)", "(composer)" -
#: dropped from the credit, so Ben Selvin's pseudonym records file under Ben
#: Selvin
_NOTE_BRACKET = re.compile(
    r"^\s*(?:as|aka|a\.k\.a\.)\s|\bversion\b|\bdir\b\.?|\bdirected\b|\bcomposer\b|"
    r"\btheme\b|^\s*et\s*$|\bstudio\b|\btake\s*\d|\binstrumental\b",
    re.IGNORECASE,
)
#: leftover pieces of free-text credits that aren't anyone
_NOT_A_NAME = re.compile(
    r"^(?:the|and|by|unidentified|unknown|brief|dir|voal|vocal|vocals|refrain|trumpet|piano|"
    r"organ|pipe organ|solo|version \d+|including .*|incl .*)$",
    re.IGNORECASE,
)
_KEEP = "\x00"   # stands in for a comma that's part of a name
_JOIN = "\x01"   # stands in for a space-and-space that's part of a name


def _split_list(text: str) -> list[str]:
    """Split one bracket-free stretch on feat/&/and/with/x and commas."""
    out: list[str] = []
    for part in _FEAT_SPLIT.split(text):
        part = part.strip(" -/")
        if "," not in part and ";" not in part:
            out.append(part)
            continue
        pieces = (re.sub(r"^(?:and|&)\s+", "", p.strip(" -/"), flags=re.I) for p in re.split(r"[,;]", part))
        out.extend(p for p in pieces if p)  # "A, B, and C": the last one is "C"
    return out


def _split_bracket(inner: str) -> list[str]:
    """The singers inside "Glenn Miller (Tex Beneke & chorus, vocal)"."""
    cleaned = _ROLE_WORDS.sub(" ", inner)
    cleaned = re.sub(r"^\s*(?:and|&|by)\s+|\s+(?:and|&)\s*$", " ", cleaned.strip(), flags=re.I)
    return [p for p in _split_list(re.sub(r"\s+", " ", cleaned).strip())
            if p.strip().lower() not in ("and", "&", "the", "by")]


def split_artists(text: Optional[str]) -> list[str]:
    """Break a credit string into individual artist names.

    'Daft Punk feat. Pharrell Williams & Nile Rodgers'
        -> ['Daft Punk', 'Pharrell Williams', 'Nile Rodgers']

    Names that aren't lists stay whole:
    bands with a comma or "&" in their name (PROTECTED_NAMES), "..., Jr.",
    "Leader and His Orchestra", and "Fisher, Eddie". Lists that are lists
    come apart: "Shawn Mendes, Camila Cabello", "Nelly Furtado ft.Timbaland",
    a credit left as just "ft. Enya", "Huntrix: Ejae, Audrey Nuna and Rei
    Ami", and big-band credits with the singers in brackets - "Glenn Miller
    (Tex Beneke & chorus, vocal)" is Glenn Miller featuring Tex Beneke, not
    "Glenn Miller (Tex Beneke" and "chorus, vocal)".
    """
    if not text:
        return []
    raw = re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()
    if not raw:
        return []
    # a credit whose first "name" is only punctuation is one name, not two
    # ("? and the Mysterians")
    first = _FEAT_SPLIT.split(raw)[0]
    if not normalize(first):
        return [raw]

    if re.fullmatch(r"[^\s,]+,\s*[^\s,]+", raw):
        return [raw]  # "Fisher, Eddie": one person, surname first
    s = _LEADING_FEAT_RE.sub("", raw)
    s = _GLUED_FEAT_RE.sub(lambda m: f" {m.group(1)}. ", s)
    for pattern in _PROTECTED_RE:
        s = pattern.sub(lambda m: m.group(0).replace(",", _KEEP)
                        .replace(" & ", _JOIN + "&" + _JOIN).replace(" and ", _JOIN + "and" + _JOIN)
                        .replace(" + ", _JOIN + "+" + _JOIN), s)
    s = _SUFFIX_RE.sub(lambda m: m.group(0).replace(",", _KEEP), s)
    s = _HIS_RE.sub(lambda m: m.group(0).replace(" ", _JOIN), s)
    s = _VOCAL_BY_RE.sub(" feat. ", s)

    names: list[str] = []
    singers: list[str] = []

    def take(m: "re.Match[str]") -> str:
        inner = m.group(1)
        if _NOTE_BRACKET.search(inner):
            return " "
        if _ROLE_HINT.search(inner) or "," in inner or ";" in inner:
            singers.extend(_split_bracket(inner))
            return " "
        return m.group(0)  # a bracket that's part of a name - keep it

    s = re.sub(r"\[([^\[\]]*)\]", take, s)
    s = re.sub(r"\(([^()]*)\)", take, s)
    s = re.sub(r"\(([^()]*)$", take, s)  # "Johnny Mercer (feat. Paul Weston" - never closed
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    group = re.match(r"^([^:,()]+):\s*(.+)$", s)
    if group and ("," in group.group(2) or " and " in group.group(2)):
        names.append(group.group(1))
        names.extend(_split_list(group.group(2)))
    else:
        names.extend(_split_list(s))
    names.extend(singers)

    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        name = name.replace(_KEEP, ",").replace(_JOIN, " ").strip(" -/,")
        key = normalize(name)
        if not key or key in seen or _NOT_A_NAME.match(re.sub(r"[^\w\s]", " ", name).strip()):
            continue  # a piece with no letters, or the same singer twice
        seen.add(key)
        out.append(name)
    return out


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

    The artist always has to be reasonably close (MIN_ARTIST_SIMILARITY),
    even for an exact title: otherwise a chart entry links to someone
    else's song of the same name, e.g. a hit that charted for two artists
    at once collapsing onto one record.
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
