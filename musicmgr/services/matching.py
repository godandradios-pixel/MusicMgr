"""Text normalisation and fuzzy matching.

Used in two places:
  * de-duplicating artists/releases while scanning ("The Beatles" == "Beatles, The")
  * linking imported chart rows to tracks you actually own
"""

from __future__ import annotations

import difflib
import functools
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


#: normalize/_title_core/primary_artist/_artist_core are pure and called
#: for the same library titles over and over by chart matching (each chart
#: song is scored against up to 800 library tracks), so they're memoised
_MEMO = functools.lru_cache(maxsize=262_144)


@_MEMO
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


@_MEMO
def primary_artist(text: Optional[str]) -> str:
    parts = split_artists(text)
    return parts[0] if parts else (text or "")


def _contains_words(a: str, b: str) -> bool:
    """True when the shorter string appears in the longer as whole words."""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return bool(short) and f" {short} " in f" {long_} "


def _seq_ratio(x: str, y: str, need: float) -> float:
    """difflib's ratio of x and y - or, when its cheap upper bounds
    (real_quick_ratio, quick_ratio) show it must be below `need`, just 0.0
    without working it out. Callers pass as `need` the most the ratio could
    matter: the best score they already have, or the cut-off below which
    they throw the result away - so what they end up with is exactly the
    same, only faster (chart matching compares each chart song with up to
    800 library titles, and most can't come near; 2026-10-29)."""
    sm = difflib.SequenceMatcher(None, x, y)
    if need > 0.0 and (sm.real_quick_ratio() < need or sm.quick_ratio() < need):
        return 0.0
    return sm.ratio()


def similarity(a: str, b: str, floor: float = 0.0) -> float:
    """0.0-1.0 similarity of two already-normalised strings.

    With a `floor`, an answer below it may come back as some other value
    below it (see _seq_ratio) - for callers that discard those anyway."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = 0.0
    # containment bonus: "gladys knight" vs "gladys knight and the pips".
    # Whole words only - as plain substrings "men" was "in" "instrumental",
    # "sing" in "missing" and "m" in "fair to midland", which scored 0.9
    if _contains_words(a, b):
        ratio = 0.9
    # token overlap catches word reordering
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        ratio = max(ratio, jaccard * 0.95)
    # difflib last, and only if it could still change the answer
    return max(ratio, _seq_ratio(a, b, max(ratio, floor)))


#: below this artist similarity a chart entry never matches, however exact
#: the title (see score_pair). Was 0.45 until 2026-10-29: two unrelated short
#: names score about 0.5 on spelling alone, and PC1's charts had ~1,000
#: songs matched to another act's record of the same title ("Rod Wave" /
#: "Matroda", "Post Malone" / "The Police", "Natalie Cole" / "Nat King
#: Cole"). Spelling variants of the same act now score high instead (see
#: _artist_core), so the bar can sit where different acts fall below it.
MIN_ARTIST_SIMILARITY = 0.8
#: ...and below this title similarity it never matches, however exact the
#: artist: a one-hit wonder ("Hurricane" by MS MR) mustn't land on another
#: song by the same act ("Beautiful Hurricane"), and title and artist
#: weighted together let a perfect artist carry a title only 57% alike
MIN_TITLE_SIMILARITY = 0.8

#: words a library title may carry beyond the chart's without being a
#: different song: "Billie Jean (Single Version)", "Crush Original Version",
#: "Billionaire ft. Bruno Mars"
_VERSION_WORDS = frozenset(
    "single version album radio edit mix remix original mono stereo remaster "
    "remastered extended long short live demo take recording recorded digital "
    "re inch lp ep vocal orchestra soundtrack from the "
    "official video audio bonus track explicit clean freestyle part pt i ii".split()
)
_FEAT_WORD = re.compile(r"^(?:feat|ft|featuring|with|prod)$")
#: a title's version note: anything in brackets, or after " - "
_TITLE_NOTE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]|\s+-\s+.*$")


@_MEMO
def _title_core(title: str) -> str:
    """The title without its bracketed or " - " version notes - for
    "Kiss Me (Official Paris Version)" just "kiss me"."""
    return normalize(_TITLE_NOTE.sub("", title or "")) or normalize(title)


def _extra_is_note(short: str, long_: str) -> bool:
    """Whether `long_` is `short` plus only version-note words (or a
    featured credit) before or after it."""
    if not _contains_words(short, long_):
        return False
    i = f" {long_} ".index(f" {short} ")
    extra = (long_[:i] + " " + long_[i + len(short):]).split()
    if not extra:
        return True
    if any(_FEAT_WORD.match(w) for w in extra):
        # everything from "ft"/"with" on is a credit, not part of the title
        head = []
        for w in extra:
            if _FEAT_WORD.match(w):
                break
            head.append(w)
        extra = head
    return all(w in _VERSION_WORDS or re.fullmatch(r"(?:19|20)\d\d", w) for w in extra)


def title_similarity(title_a: str, title_b: str, floor: float = 0.0) -> float:
    """0.0-1.0 similarity of two song titles (raw, not normalised).

    Stricter than `similarity`: one title found inside another only counts
    when what's left over is a version note, so "Billie Jean" matches
    "Billie Jean (Single Version)" but "Hurricane" doesn't match "Beautiful
    Hurricane" and "Men" doesn't match "Midnight Train to Georgia
    (instrumental)"."""
    # a double A-side charts as "Moody Blue/She Thinks I Still Care" -
    # either side is the record
    if "/" in (title_a or "") or "/" in (title_b or ""):
        parts_a = _sides(title_a)
        parts_b = _sides(title_b)
        if len(parts_a) > 1 or len(parts_b) > 1:
            return max(_title_similarity(title_a, title_b, floor),
                       *(_title_similarity(x, y, floor) for x in parts_a for y in parts_b))
    return _title_similarity(title_a, title_b, floor)


def _sides(title: str) -> list[str]:
    """"A/B" as its two sides - only when both are words, so "24/7" and
    "AC/DC" stay whole."""
    # a "/" inside a version note - "(Live At ..., NY / January 27, 2017)"
    # - doesn't make two sides
    parts = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", title or "").split("/")
    if len(parts) > 1 and all(re.search(r"[^\W\d_]{2}", p) for p in parts):
        return parts
    return [title or ""]


def _title_similarity(title_a: str, title_b: str, floor: float = 0.0) -> float:
    a, b = normalize(title_a), normalize(title_b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ca, cb = _title_core(title_a), _title_core(title_b)
    if ca and ca == cb:
        return 0.95
    best = 0.0
    for x, y in ((a, b), (ca, cb)):
        ratio = 0.0
        tx, ty = set(x.split()), set(y.split())
        if tx and ty:
            ratio = len(tx & ty) / len(tx | ty) * 0.95
        short, long_ = (x, y) if len(x) <= len(y) else (y, x)
        if _extra_is_note(short, long_):
            ratio = max(ratio, 0.9)
        # difflib last, and only if it could still change the answer
        ratio = max(ratio, _seq_ratio(x, y, max(ratio, best, floor)))
        best = max(best, ratio)
    return best
#: comparing individual names out of "A featuring B" / "A & B" credits must
#: be this close to count - two different people sharing a surname
#: ("Chuck Jackson" / "Jermaine Jackson") shouldn't
NAME_PAIR_SIMILARITY = 0.75


#: words that say what kind of act it is, not who - "Tommy Dorsey And His
#: Orchestra", "Jack Teter Trio", "(Little Jack Little, vocal)". Two
#: different 1930s-40s bands shared enough of these that "Riley-Farley
#: Orchestra" scored 0.58 against "Tommy Dorsey And His Orchestra" and
#: "Jack Teter Trio" 0.48 against "Nat King Cole Trio" - both over the old
#: artist minimum, so a song recorded by many acts in the same year matched
#: the wrong record (2026-10-29). artist_similarity compares the names with
#: these left out.
_ACT_WORDS = frozenset(
    "orchestra orch orchestre his her their and with band trio quartet quintet "
    "sextet septet octet ensemble singers chorus choir vocal vocals vocalist "
    "refrain featuring feat ft accompanied acc duet introducing the".split()
)
#: the same name spelt two ways: "Bros." / "Brothers", "Matchbox 20" /
#: "Matchbox Twenty", "5th Dimension" / "Fifth Dimension"
_ARTIST_WORD_FORMS = {
    "bros": "brothers", "co": "company", "mr": "mister", "st": "saint",
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
    "7": "seven", "8": "eight", "9": "nine", "10": "ten", "20": "twenty",
    "1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth",
    "5th": "fifth", "6th": "sixth",
}
#: a nickname in quotes: Missy "Misdemeanor" Elliott, Clarence "Frog Man" Henry
_QUOTED_NICKNAME = re.compile(r"\s[\"\u201c\u201d][^\"\u201c\u201d]{1,30}[\"\u201c\u201d]?(?=\s)|\s'[^'\s][^']{0,28}'(?=\s)")
#: anything in brackets
_BRACKETED = re.compile(r"[\(\[][^)\]]*[\)\]]")
#: a $ or ! standing in for a letter: Ca$h Out, P!nk, Ke$ha
_SYMBOL_LETTERS = (re.compile(r"(?<=\w)\$(?=\w)"), re.compile(r"(?<=\w)!(?=\w)"))


@_MEMO
def _artist_core(text: Optional[str], keep_nickname: bool = False) -> str:
    """An artist credit, normalised for comparing who it is: nickname in
    quotes dropped, "$"/"!" read as letters, initials joined ("t.A.T.u.",
    "S.O.S. Band", "N*E*R*D" -> "tatu", "sos", "nerd"), numbers and "Bros."
    spelt out, and the _ACT_WORDS left out - or the whole normalised credit
    if that's all it is ("The Band", "Orchestra")."""
    raw = f" {text or ''} "
    if not keep_nickname:
        raw = _QUOTED_NICKNAME.sub(" ", raw)
    # a nickname or note in brackets: "Arthur (Guitar Boogie) Smith",
    # "Slim (Of 112)", "(Little Jack Little, vocal)" - unless that's all
    # there is
    unbracketed = _BRACKETED.sub(" ", raw)
    if normalize(unbracketed):
        raw = unbracketed
    raw = _SYMBOL_LETTERS[0].sub("s", raw)
    raw = _SYMBOL_LETTERS[1].sub("i", raw)
    full = normalize(raw)
    words: list[tuple[str, bool]] = []   # (word, made of initials)
    letters = ""
    for w in full.split():
        if len(w) == 1 and w.isalpha():
            letters += w          # an initial: keep joining
            continue
        if letters:
            words.append((letters, True))
            letters = ""
        words.append((_ARTIST_WORD_FORMS.get(w, w), False))
    if letters:
        words.append((letters, True))
    # initials are a name even when they spell a filler word ("H.E.R.")
    core = " ".join(w for w, initials in words if initials or w not in _ACT_WORDS)
    return core or " ".join(w for w, _ in words) or full


def artist_similarity(artist_a: str, artist_b: str, floor: float = 0.0) -> float:
    """How alike two artist credits are: the whole credits, or - for
    "Missy Elliott Featuring Ludacris" vs "Missy Elliott ft. Ciara" - the
    two lead artists. Only the lead names count: "Future Featuring Drake"
    and "DJ Khaled ft. Drake" share Drake but aren't the same record.

    Both are compared without words like "Orchestra", "and His" or "Trio"
    (_artist_core), so "Victor Young & His Orchestra" still is "Victor
    Young" but two bands no longer look alike for being bands."""
    best = 0.0
    # a quoted part is sometimes a nickname to ignore (Missy "Misdemeanor"
    # Elliott / Missy Elliott) and sometimes part of the name (Nat "King"
    # Cole / Nat King Cole), so both readings are tried
    for core_a in _core_variants(artist_a):
        for core_b in _core_variants(artist_b):
            best = max(best, _name_similarity(core_a, core_b, max(floor, best)))
    lead_floor = max(floor, NAME_PAIR_SIMILARITY)
    lead_a, lead_b = primary_artist(artist_a), primary_artist(artist_b)
    for core_a in _core_variants(lead_a):
        for core_b in _core_variants(lead_b):
            lead = _name_similarity(core_a, core_b, max(lead_floor, best))
            if lead >= NAME_PAIR_SIMILARITY:
                best = max(best, lead)
    # the same people listed differently: "Dolly Parton, Emmylou Harris &
    # Linda Ronstadt" / "Linda, Dolly Parton and Emmylou Harris"
    if best < 0.9 and _most_names_shared(artist_a, artist_b):
        best = 0.9
    return best


def _core_variants(text: Optional[str]) -> tuple[str, ...]:
    plain = _artist_core(text)
    kept = _artist_core(text, True)
    return (plain,) if kept == plain else (plain, kept)


@_MEMO
def _most_names_shared(artist_a: Optional[str], artist_b: Optional[str]) -> bool:
    """Whether two credits of two or more names each share at least two
    thirds of their names (each name matching at NAME_PAIR_SIMILARITY).
    One shared guest isn't enough: "Future Featuring Drake" and "DJ Khaled
    ft. Drake" aren't the same record."""
    names_a = [_artist_core(n) for n in split_artists(artist_a)]
    names_b = [_artist_core(n) for n in split_artists(artist_b)]
    if len(names_a) < 2 and len(names_b) < 2:
        return False
    shared = sum(1 for a in names_a
                 if any(_name_similarity(a, b, NAME_PAIR_SIMILARITY) >= NAME_PAIR_SIMILARITY
                        for b in names_b))
    return shared >= 2 and shared / max(len(names_a), len(names_b)) >= 2 / 3


#: names this short (letters, spaces aside) only count when they're the
#: same: one letter apart is already another act - "Her" (H.E.R.) / "Cher",
#: "Lio" / "iio", "Amber" / "America"
_SHORT_NAME = 5


def _name_similarity(a: str, b: str, floor: float = 0.0) -> float:
    if a and b and a.replace(" ", "") == b.replace(" ", ""):
        return 1.0   # "soulDecision" / "Soul Decision"
    if a and b and a != b and min(len(a.replace(" ", "")), len(b.replace(" ", ""))) <= _SHORT_NAME:
        # a short name can still be part of a longer credit ("Joji" in
        # "Joji & Diplo") - similarity's whole-word containment covers it
        return 0.9 if _contains_words(a, b) else 0.0
    return similarity(a, b, floor)


def score_pair(
    title_a: str, artist_a: str, title_b: str, artist_b: str
) -> float:
    """Combined title+artist confidence, title weighted more heavily.

    The artist always has to be reasonably close (MIN_ARTIST_SIMILARITY),
    even for an exact title: otherwise a chart entry links to someone
    else's song of the same name, e.g. a hit that charted for two artists
    at once collapsing onto one record. Likewise the title on its own has
    to be close (MIN_TITLE_SIMILARITY, `title_similarity`), however exact
    the artist: otherwise it links to another song by the same act.
    """
    # the floors let difflib skip pairs that can't pass (see _seq_ratio)
    t = title_similarity(title_a, title_b, MIN_TITLE_SIMILARITY)
    if t < MIN_TITLE_SIMILARITY:
        return 0.0
    a = artist_similarity(artist_a, artist_b, MIN_ARTIST_SIMILARITY)
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
