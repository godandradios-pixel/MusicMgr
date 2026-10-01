"""Old-time radio shows (2026-10-01) - the tuner's Old-Time band.

James keeps his old-time radio programs in one folder (D:\\Radio), one
sub-folder per show: The Shadow, The Whistler, Lone Ranger, Commercials,
WWII News and Sounds, ... about 4,900 MP3s. Each top-level folder becomes a
`RadioShow`; every audio file under it (sub-folders included) a
`RadioEpisode`. They are kept apart from the music library on purpose -
episodes never become Tracks.

**Air dates** come from file names, which carry them in several styles:
`1944-01-09-CBS-...`, `Whistler 51-03-11 (458) ...`, `LoneRanger_380722_...`,
`430613  608 The White Ticket`, `Abbott_costello490428266...`. The tag year
is the fallback. Episodes play in air-date order.

**Titles**: the tag title, unless the file name says more (the WWII tags
drop the speaker - "in Monte Cassino" vs "BBC Godfrey Talbot in Monte
Cassino") or the tag is just the file name again ("430905  620 Axford
Rises to Shine").

**Resume** (James's choice): each show remembers its current episode and
the position in it; tuning to a show picks up there, and a finished episode
moves the show on to the next broadcast.

Two copies of the same broadcast (same air date and title - the WWII folder
has several) count once; the extra copy is marked `duplicate_of`.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import RadioEpisode, RadioShow, Setting

log = logging.getLogger(__name__)

ROOT_KEY = "otr_root"
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma", ".aac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
#: an episode heard this far through counts as played
PLAYED_FRACTION = 0.95
#: positions closer to the start than this aren't worth resuming
MIN_RESUME_MS = 15_000

ProgressFn = Callable[[int, int, str], None]


# --------------------------------------------------------------------------
# folder setting
# --------------------------------------------------------------------------


def get_root(session: Session) -> Optional[str]:
    row = session.get(Setting, ROOT_KEY)
    return row.value if row and row.value else None


def set_root(session: Session, folder: str) -> None:
    row = session.get(Setting, ROOT_KEY)
    if row is None:
        session.add(Setting(key=ROOT_KEY, value=folder))
    else:
        row.value = folder


# --------------------------------------------------------------------------
# parsing file names
# --------------------------------------------------------------------------

_FULL_DATE = re.compile(r"(?<!\d)(19\d\d|20\d\d)[-_. ]?(\d\d|xx)[-_. ]?(\d\d|xx)(?!\d)", re.I)
_SHORT_DATE = re.compile(r"(?<!\d)(\d\d)[-_. ](\d\d|xx)[-_. ](\d\d|xx)(?!\d)", re.I)
_PACKED_DATE = re.compile(r"(?<!\d)(\d\d)(\d\d)(\d\d)(\d{0,4})(?!\d)")
_PAREN_NO = re.compile(r"\((\d{1,5})\)")


@dataclass
class AirDate:
    iso: Optional[str]
    year: Optional[int]


def _valid(month: str, day: str) -> bool:
    try:
        m = int(month) if month.lower() != "xx" else 1
        d = int(day) if day.lower() != "xx" else 1
    except ValueError:
        return False
    return 1 <= m <= 12 and 1 <= d <= 31


def _iso(year: int, month: str, day: str) -> str:
    parts = [f"{year:04d}"]
    if month.lower() != "xx":
        parts.append(f"{int(month):02d}")
        if day.lower() != "xx":
            parts.append(f"{int(day):02d}")
    return "-".join(parts)


def _century(yy: int, tag_year: Optional[int]) -> int:
    if tag_year and tag_year >= 2000 and yy <= tag_year % 100 + 1:
        return 2000 + yy
    return 1900 + yy


def parse_air_date(name: str, tag_year: Optional[int] = None) -> AirDate:
    """Air date from a file name (no extension), falling back to the tag year."""
    m = _FULL_DATE.search(name)
    if m and _valid(m.group(2), m.group(3)):
        return AirDate(_iso(int(m.group(1)), m.group(2), m.group(3)), int(m.group(1)))
    m = _SHORT_DATE.search(name)
    if m and _valid(m.group(2), m.group(3)):
        year = _century(int(m.group(1)), tag_year)
        return AirDate(_iso(year, m.group(2), m.group(3)), year)
    for m in _PACKED_DATE.finditer(name):
        yy, mm, dd = m.group(1), m.group(2), m.group(3)
        if not _valid(mm, dd) or mm == "00" or dd == "00":
            continue
        year = _century(int(yy), tag_year)
        if tag_year and abs(year - tag_year) > 1:
            continue
        return AirDate(_iso(year, mm, dd), year)
    m = _PAREN_YEAR.search(name)
    if m:
        return AirDate(m.group(1), int(m.group(1)))
    return AirDate(str(tag_year) if tag_year else None, tag_year)


def parse_episode_no(name: str, tag_track: Optional[str]) -> Optional[int]:
    m = _PAREN_NO.search(name)
    if m and not _PAREN_YEAR.search(m.group(0)):
        return int(m.group(1))
    if tag_track:
        digits = re.match(r"\s*(\d+)", str(tag_track))
        if digits:
            return int(digits.group(1))
    return None


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def common_prefix(stems: Sequence[str], share: float = 0.6) -> list[str]:
    """Words most of a show's file names start with ("Whistler",
    "LoneRanger", "Stories Of Sherlock Holmes SA", "FDR Fireside Chat") -
    stripped from titles. Dates and numbers never count."""
    def words(stem: str) -> list[str]:
        t = _FULL_DATE.sub(" ", stem)
        t = _SHORT_DATE.sub(" ", t)
        return [w for w in _words(t).split() if not w.isdigit()]

    lists = [words(s) for s in stems if s]
    if len(lists) < 3:
        return []
    prefix: list[str] = []
    for i in range(6):
        counts: dict[str, int] = {}
        for ws in lists:
            if len(ws) > i and ws[: i] == prefix:
                counts[ws[i]] = counts.get(ws[i], 0) + 1
        if not counts:
            break
        word, n = max(counts.items(), key=lambda kv: kv[1])
        if n < share * len(lists):
            break
        prefix.append(word)
    return prefix


def _strip_prefix(text: str, prefix: Sequence[str]) -> str:
    if not prefix:
        return text
    sep = r"[^a-z0-9]*(?:\d+[^a-z0-9]+)?"
    pattern = r"(?i)^[^a-z0-9]*" + sep.join(re.escape(w) for w in prefix) + r"(?![a-z])"
    stripped = re.sub(pattern, " ", text, count=1)
    return stripped if _words(stripped) else text


_PAREN_YEAR = re.compile(r"\((1[89]\d\d|20\d\d)\)")


def clean_title(raw: str, show: str, prefix: Sequence[str] = ()) -> str:
    """Strip dates, episode numbers, the show's own name and file-name
    punctuation from a title."""
    t = raw.replace("_", " ")
    t = _strip_prefix(t, prefix)
    t = re.sub(r"(?i)^\s*" + re.escape(show) + r"\b", " ", t)
    t = _FULL_DATE.sub(" ", t)
    t = _SHORT_DATE.sub(" ", t)
    t = re.sub(r"(?i)\bxx[-_. ]xx[-_. ]xx\b", " ", t)
    t = re.sub(r"(?<!\d)\d{6}(?:[-_ ]?\d{1,4})?(?!\d)", " ", t)  # 380722, 490428266
    t = _PAREN_NO.sub(" ", t)
    t = _PAREN_YEAR.sub(" ", t)
    t = re.sub(r"\((?:x|xx)?\s*\)", " ", t, flags=re.I)
    t = re.sub(r"^\s*(?:SA|CHI-?\w*)\b", " ", t)
    t = re.sub(r"^[\s\-–—:.,]*(?:\d{1,4}\b[\s\-–—:.]*)?", "", t)  # leading "608 "
    # file-name hyphens between words ("Reports-From-Link-Up")
    if t.count("-") >= 3 and " " not in t.strip():
        t = t.replace("-", " ")
    t = re.sub(r"\s*-{2,}\s*", " - ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—:.,")
    return t


def episode_title(tag_title: Optional[str], stem: str, show: str,
                  prefix: Sequence[str] = ()) -> str:
    from_name = clean_title(stem, show, prefix)
    from_tag = clean_title(tag_title, show, prefix) if tag_title else ""
    if not from_tag:
        return from_name or stem
    # the tag is only the tail of a fuller file name ("in Monte Cassino")
    if (from_name and len(_words(from_name)) > len(_words(from_tag))
            and _words(from_name).endswith(_words(from_tag))):
        return from_name
    return from_tag


def dial_label(name: str, limit: int = 18) -> str:
    """A show's name as printed on the dial: "The Shadow" -> "Shadow",
    "Vincent Price - The Vintage Radio Shows" -> "Vincent Price",
    "Abbott And Costello" -> "Abbott & Costello"."""
    t = name.split(" - ")[0].strip()
    t = re.sub(r"(?i)^the\s+", "", t)
    t = re.sub(r"(?i)\s+and\s+", " & ", t)
    words = t.split()
    while len(words) > 1 and len(" ".join(words)) > limit:
        words.pop()
    while len(words) > 1 and words[-1].lower() in ("&", "of", "and", "the", "a"):
        words.pop()
    return " ".join(words)


_POLICE_WORDS = (
    "shadow", "whistler", "hornet", "sherlock", "holmes", "hickok", "detective",
    "mystery", "suspense", "dragnet", "police", "crime", "inner sanctum", "sam spade",
    "marlowe", "gang busters", "gangbusters", "boston blackie", "nick carter", "lights out",
    "johnny dollar", "dick tracy", "fbi", "mr. district attorney",
)
_HISTORY_WORDS = (
    "history", "fireside", "fdr", "president", "voices", "speech", "harvey",
    "rest of the story", "inaugural", "address",
)


def guess_band(show_name: str, kind: str) -> str:
    """Which band of the dial a show goes on, by type of program (James's
    choice): AM drama and comedy, POLICE crime and mystery, SW1 war and
    world news, SW2 history and speeches, LW commercials (and On the Air)."""
    low = show_name.lower()
    if kind == RadioShow.KIND_COMMERCIAL:
        return "lw"
    if kind == RadioShow.KIND_NEWS or re.search(r"\bwwii\b|\bwar\b|\bnews\b", low):
        return "sw1"
    if any(w in low for w in _HISTORY_WORDS):
        return "sw2"
    if any(w in low for w in _POLICE_WORDS):
        return "police"
    return "am"


def set_band(session: Session, show_id: int, band: str) -> None:
    show = session.get(RadioShow, show_id)
    if show is not None:
        show.band = band


def guess_kind(show_name: str) -> str:
    low = show_name.lower()
    if "commercial" in low:
        return RadioShow.KIND_COMMERCIAL
    if "news" in low:
        return RadioShow.KIND_NEWS
    return RadioShow.KIND_SHOW


def sort_key(air: AirDate, episode_no: Optional[int], rel_path: str) -> str:
    date = (air.iso or "9999").ljust(10, "0")
    return f"{date}|{episode_no or 0:06d}|{rel_path.lower()}"


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


@dataclass
class ScanResult:
    shows: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    cancelled: bool = False

    def summary(self) -> str:
        parts = [f"{self.shows} shows"]
        if self.added:
            parts.append(f"{self.added:,} episodes added")
        if self.updated:
            parts.append(f"{self.updated:,} updated")
        if self.missing:
            parts.append(f"{self.missing:,} missing")
        if not (self.added or self.updated or self.missing):
            parts.append("nothing new")
        text = " · ".join(parts)
        return text + (" (stopped early)" if self.cancelled else "")


def _read_tags(path: Path) -> dict:
    try:
        import mutagen

        audio = mutagen.File(str(path), easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}
    out = {"length": getattr(audio.info, "length", 0) or 0}
    for key in ("title", "date", "tracknumber"):
        value = audio.get(key) if hasattr(audio, "get") else None
        if value:
            out[key] = str(value[0])
    return out


def _cover_for(folder: Path) -> Optional[str]:
    images = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda p: (0 if re.search(r"cover|folder|front", p.stem, re.I) else 1, p.name.lower()),
    ) if folder.is_dir() else []
    return str(images[0]) if images else None


def show_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))),
        key=lambda p: p.name.lower(),
    )


def scan(
    session_factory,
    root: str,
    progress: Optional[ProgressFn] = None,
    cancelled: Callable[[], bool] = lambda: False,
    read_tags: Callable[[Path], dict] = _read_tags,
) -> ScanResult:
    """Bring the shows/episodes tables in line with the folder. Only new or
    changed files have their tags read; commits per show."""
    result = ScanResult()
    root_path = Path(root)
    folders = show_folders(root_path)
    files_by_show: list[tuple[Path, list[Path]]] = []
    for folder in folders:
        audio = sorted(
            p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
            and not any(part.startswith(("_", ".")) for part in p.relative_to(folder).parts)
        )
        files_by_show.append((folder, audio))
    total = sum(len(files) for _, files in files_by_show)
    done = 0
    seen_paths: set[str] = set()
    seen_folders: set[str] = set()
    for folder, files in files_by_show:
        if cancelled():
            result.cancelled = True
            break
        seen_folders.add(str(folder))
        prefix = common_prefix([p.stem for p in files])
        with session_factory() as session:
            show = session.scalar(select(RadioShow).where(RadioShow.folder == str(folder)))
            if show is None:
                kind = guess_kind(folder.name)
                show = RadioShow(name=folder.name, folder=str(folder), kind=kind,
                                 band=guess_band(folder.name, kind))
                session.add(show)
                session.flush()
            show.is_missing = False
            show.cover_path = _cover_for(folder) or show.cover_path
            existing = {
                e.path: e for e in session.scalars(
                    select(RadioEpisode).where(RadioEpisode.show_id == show.id)
                )
            }
            for path in files:
                if cancelled():
                    result.cancelled = True
                    break
                done += 1
                if progress and (done % 25 == 0 or done == total):
                    progress(done, total, folder.name)
                key = str(path)
                seen_paths.add(key)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                ep = existing.get(key)
                if ep is not None and ep.mtime == stat.st_mtime and ep.size_bytes == stat.st_size:
                    if ep.is_missing:
                        ep.is_missing = False
                    result.unchanged += 1
                    continue
                tags = read_tags(path)
                tag_year = None
                if tags.get("date"):
                    m = re.match(r"\s*(\d{4})", tags["date"])
                    tag_year = int(m.group(1)) if m else None
                rel = str(path.relative_to(folder))
                air = parse_air_date(path.stem, tag_year)
                number = parse_episode_no(path.stem, tags.get("tracknumber"))
                if ep is None:
                    ep = RadioEpisode(show_id=show.id, path=key, title="")
                    session.add(ep)
                    result.added += 1
                else:
                    result.updated += 1
                ep.title = episode_title(tags.get("title"), path.stem, folder.name, prefix)[:500]
                ep.air_date = air.iso
                ep.year = air.year
                ep.episode_no = number
                ep.duration_ms = int(tags.get("length", 0) * 1000) or ep.duration_ms
                ep.size_bytes = stat.st_size
                ep.mtime = stat.st_mtime
                ep.sort_key = sort_key(air, number, rel)
                ep.is_missing = False
            session.flush()
            _mark_duplicates(session, show.id)
            session.commit()
        result.shows += 1
    if not result.cancelled:
        with session_factory() as session:
            for ep in session.scalars(select(RadioEpisode).where(RadioEpisode.is_missing.is_(False))):
                if ep.path not in seen_paths and ep.path.startswith(str(root_path)):
                    ep.is_missing = True
                    result.missing += 1
            for show in session.scalars(select(RadioShow)):
                if show.folder not in seen_folders and show.folder.startswith(str(root_path)):
                    show.is_missing = True
            session.commit()
    if progress:
        progress(total, total, "")
    return result


def _mark_duplicates(session: Session, show_id: int) -> None:
    first: dict[tuple[str, str], int] = {}
    episodes = session.scalars(
        select(RadioEpisode)
        .where(RadioEpisode.show_id == show_id, RadioEpisode.is_missing.is_(False))
        .order_by(RadioEpisode.size_bytes.desc(), RadioEpisode.id)
    )
    for ep in episodes:
        if not ep.air_date or len(ep.air_date) < 10:
            ep.duplicate_of = None
            continue
        key = (ep.air_date, _words(ep.title))
        if key in first and first[key] != ep.id:
            ep.duplicate_of = first[key]
        else:
            first[key] = ep.id
            ep.duplicate_of = None


# --------------------------------------------------------------------------
# browsing and resume
# --------------------------------------------------------------------------


def list_shows(session: Session, kinds: Optional[Sequence[str]] = None) -> list[RadioShow]:
    stmt = select(RadioShow).where(RadioShow.is_missing.is_(False)).order_by(RadioShow.name)
    if kinds:
        stmt = stmt.where(RadioShow.kind.in_(list(kinds)))
    return [s for s in session.scalars(stmt) if _has_episodes(session, s.id)]


def _has_episodes(session: Session, show_id: int) -> bool:
    return session.scalar(
        select(RadioEpisode.id).where(
            RadioEpisode.show_id == show_id, RadioEpisode.is_missing.is_(False)
        ).limit(1)
    ) is not None


def episodes(session: Session, show_id: int) -> list[RadioEpisode]:
    """Playable episodes of a show, in air-date order (duplicates hidden)."""
    return list(session.scalars(
        select(RadioEpisode)
        .where(
            RadioEpisode.show_id == show_id,
            RadioEpisode.is_missing.is_(False),
            RadioEpisode.duplicate_of.is_(None),
        )
        .order_by(RadioEpisode.sort_key)
    ))


def current_episode(session: Session, show: RadioShow) -> Optional[RadioEpisode]:
    """Where tuning to `show` picks up: its current episode if not finished,
    else the next unplayed one after it (wrapping round), else the first."""
    eps = episodes(session, show.id)
    if not eps:
        return None
    idx = next((i for i, e in enumerate(eps) if e.id == show.current_episode_id), None)
    if idx is not None and not eps[idx].played:
        return eps[idx]
    start = (idx + 1) if idx is not None else 0
    for e in eps[start:] + eps[:start]:
        if not e.played:
            return e
    return eps[start % len(eps)]


def progress_counts(session: Session, show_id: int) -> tuple[int, int]:
    eps = episodes(session, show_id)
    return sum(1 for e in eps if e.played), len(eps)


def save_position(session: Session, episode_id: int, position_ms: int, duration_ms: int = 0) -> bool:
    """Remember where an episode got to. Returns True if that finished it
    (then the show moves on to the next broadcast)."""
    ep = session.get(RadioEpisode, episode_id)
    if ep is None:
        return False
    length = duration_ms or ep.duration_ms or 0
    ep.last_played_at = dt.datetime.now(dt.timezone.utc)
    show = session.get(RadioShow, ep.show_id)
    if length and position_ms >= length * PLAYED_FRACTION:
        mark_played(session, ep)
        return True
    ep.position_ms = position_ms if position_ms >= MIN_RESUME_MS else 0
    if show is not None and show.kind == RadioShow.KIND_SHOW:
        show.current_episode_id = ep.id
    return False


def mark_played(session: Session, ep: RadioEpisode, played: bool = True) -> None:
    ep.played = played
    ep.position_ms = 0
    show = session.get(RadioShow, ep.show_id)
    if show is None:
        return
    if played:
        eps = episodes(session, show.id)
        idx = next((i for i, e in enumerate(eps) if e.id == ep.id), None)
        nxt = eps[idx + 1] if idx is not None and idx + 1 < len(eps) else None
        show.current_episode_id = nxt.id if nxt else ep.id
    else:
        show.current_episode_id = ep.id


def set_current(session: Session, ep: RadioEpisode) -> None:
    show = session.get(RadioShow, ep.show_id)
    if show is not None:
        show.current_episode_id = ep.id


def format_air_date(iso: Optional[str]) -> str:
    """'1951-03-11' -> 'Sunday, March 11, 1951'; partial dates degrade."""
    if not iso:
        return ""
    try:
        if len(iso) == 10:
            d = dt.date.fromisoformat(iso)
            return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")
        if len(iso) == 7:
            d = dt.date.fromisoformat(iso + "-01")
            return d.strftime("%B %Y")
    except ValueError:
        pass
    return iso[:4]


# --------------------------------------------------------------------------
# queue items
# --------------------------------------------------------------------------


def queue_item(ep: RadioEpisode, show: RadioShow, resume: bool = True):
    from .player import QueueItem

    return QueueItem(
        track_id=0,
        title=ep.title,
        artist=show.name,
        album=format_air_date(ep.air_date),
        path=ep.path,
        duration_ms=ep.duration_ms or 0,
        cover_path=show.cover_path,
        kind="episode",
        episode_id=ep.id,
        rg_track_gain=ep.rg_gain,
        start_ms=ep.position_ms if resume and ep.position_ms and not ep.played else 0,
        hydrated=True,
    )


def queue_for_show(session: Session, show_id: int, start_episode_id: Optional[int] = None,
                   limit: int = 25):
    """The current episode (or `start_episode_id`) and the broadcasts after
    it, as queue items."""
    show = session.get(RadioShow, show_id)
    if show is None:
        return []
    eps = episodes(session, show_id)
    if not eps:
        return []
    start = None
    if start_episode_id is not None:
        start = next((i for i, e in enumerate(eps) if e.id == start_episode_id), None)
    if start is None:
        cur = current_episode(session, show)
        start = next((i for i, e in enumerate(eps) if cur and e.id == cur.id), 0)
    picked = eps[start : start + limit]
    items = [queue_item(e, show, resume=(i == 0)) for i, e in enumerate(picked)]
    return [i for i in items if os.path.exists(i.path)]


# --------------------------------------------------------------------------
# on-air: a broadcast night
# --------------------------------------------------------------------------


@dataclass
class OnAirState:
    #: the year this evening's broadcast is "from"
    year: Optional[int]
    played_ids: set
    last_show_id: Optional[int] = None


#: programs shorter than this are segments, not programs
MIN_PROGRAM_MS = 10 * 60_000
#: news bulletins longer than this are skipped between programs
MAX_BULLETIN_MS = 8 * 60_000
#: chance of a news bulletin between programs (when one fits the year)
BULLETIN_CHANCE = 0.4


def pick_evening_year(session: Session, rng: random.Random) -> Optional[int]:
    """A year for tonight's broadcast, weighted by how many programs aired then."""
    rows = session.execute(
        select(RadioEpisode.year)
        .join(RadioShow, RadioShow.id == RadioEpisode.show_id)
        .where(RadioShow.kind == RadioShow.KIND_SHOW, RadioEpisode.year.is_not(None),
               RadioEpisode.is_missing.is_(False), RadioEpisode.duplicate_of.is_(None),
               RadioEpisode.year.between(1930, 1962))
    ).all()
    years = [y for (y,) in rows]
    return rng.choice(years) if years else None


def _pool(session: Session, kind: str, max_ms: Optional[int] = None,
          min_ms: Optional[int] = None) -> list[tuple[RadioEpisode, RadioShow]]:
    stmt = (
        select(RadioEpisode, RadioShow)
        .join(RadioShow, RadioShow.id == RadioEpisode.show_id)
        .where(RadioShow.kind == kind, RadioShow.is_missing.is_(False),
               RadioEpisode.is_missing.is_(False), RadioEpisode.duplicate_of.is_(None))
    )
    rows = session.execute(stmt).all()
    out = []
    for ep, show in rows:
        length = ep.duration_ms or 0
        if max_ms is not None and length and length > max_ms:
            continue
        if min_ms is not None and length and length < min_ms:
            continue
        out.append((ep, show))
    return out


def _era_weight(year: Optional[int], target: Optional[int]) -> float:
    if not target or not year:
        return 0.3
    gap = abs(year - target)
    return 1.0 if gap == 0 else 0.6 if gap <= 2 else 0.2 if gap <= 5 else 0.03


def build_on_air_block(session: Session, state: OnAirState, rng: random.Random):
    """One program and what airs before it: 1-2 commercials and sometimes a
    news bulletin. Returns queue items."""
    items = []
    programs = [
        (e, s) for e, s in _pool(session, RadioShow.KIND_SHOW, min_ms=MIN_PROGRAM_MS)
        if e.id not in state.played_ids
    ]
    if not programs:
        return []
    # don't air the same show twice running
    others = [(e, s) for e, s in programs if s.id != state.last_show_id] or programs
    weights = [_era_weight(e.year, state.year) for e, _ in others]
    # spread across shows: a 500-episode show shouldn't drown out a 20-episode one
    per_show: dict[int, int] = {}
    for _, s in others:
        per_show[s.id] = per_show.get(s.id, 0) + 1
    weights = [w / per_show[s.id] ** 0.7 for w, (_, s) in zip(weights, others)]
    ep, show = rng.choices(others, weights=weights, k=1)[0]

    commercials = [(e, s) for e, s in _pool(session, RadioShow.KIND_COMMERCIAL)
                   if e.id not in state.played_ids]
    for e, s in rng.sample(commercials, k=min(len(commercials), rng.choice((1, 2)))):
        items.append(queue_item(e, s, resume=False))
        state.played_ids.add(e.id)
    if rng.random() < BULLETIN_CHANCE:
        news = [(e, s) for e, s in _pool(session, RadioShow.KIND_NEWS, max_ms=MAX_BULLETIN_MS)
                if e.id not in state.played_ids and state.year and e.year
                and abs(e.year - state.year) <= 1]
        if news:
            e, s = rng.choice(news)
            items.append(queue_item(e, s, resume=False))
            state.played_ids.add(e.id)
    items.append(queue_item(ep, show, resume=False))
    state.played_ids.add(ep.id)
    state.last_show_id = show.id
    return [i for i in items if os.path.exists(i.path)]
