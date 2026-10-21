"""Internet radio stations (2026-10-01) - the tuner's FM and AM bands.

Stations come from the free Radio Browser directory (radio-browser.info: a
community list of 30,000+ stations, no account or API key) - searched from
the tuner's "Find stations..." dialog and added to the dial - or a stream
URL James pastes in himself. Nothing is fetched at startup (James: "I
should never be checking for updates or anything that relies on an
internet connection at startup"); the network is only touched when he
searches, adds a station's logo, or tunes in.

Radio Browser asks clients to send a descriptive User-Agent and to spread
load over its mirrors; `_get` tries them in turn.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import RadioStation

log = logging.getLogger(__name__)

MIRRORS = (
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
)
TIMEOUT_S = 12

#: the "Find stations" dialog's category chips: label -> search parameters
CATEGORIES: list[tuple[str, dict]] = [
    ("Old-time radio", {"tag": "old time radio"}),
    ("Big band & swing", {"tagList": "big band"}),
    ("1940s & 50s", {"tag": "40s"}),
    ("Oldies", {"tag": "oldies"}),
    ("Classic country", {"tag": "classic country"}),
    ("Christian", {"tag": "christian"}),
    ("Pittsburgh", {"name": "pittsburgh"}),
]
#: the numbers printed along the FM and AM scales of the dial
#: (ui/widgets/radio_dial.py draws them; stations are placed against them)
FM_MARKS = (88, 90, 92, 94, 96, 98, 100, 102, 104, 106, 108)
AM_MARKS = (540, 600, 700, 800, 900, 1000, 1200, 1400, 1600)
SCALE_MARKS = {"fm": FM_MARKS, "am": AM_MARKS}
#: where the first and last printed marks sit along the 0..1 dial
SCALE_LO, SCALE_HI = 0.08, 0.92
#: nothing is placed closer to the ends of the glass than this
DIAL_MIN, DIAL_MAX = 0.02, 0.98
#: stations closer than this on the dial are nudged apart
MIN_GAP = 0.018

_FM_RE = re.compile(r"(?<![\d.])(\d{2,3}\.\d)(?![\d.])")
_AM_RE = re.compile(r"(?<![\d.])(\d{3,4})(?![\d.]|\s*'?s\b)")

#: "Add some to get started" on an empty FM or AM band
STARTER_SEARCH = {"tag": "old time radio"}
STARTER_COUNT = 6


def frequency(name: str, band: str) -> Optional[float]:
    """The frequency in a station's name, if it has one for this band:
    "102.5 WDVE" -> 102.5 on FM, "KDKA 1020" -> 1020 on AM (2026-10-03,
    James: put stations "at their real frequencies"). Internet-only names
    ("20s Gold", "WQED Pittsburgh") and an FM number on the AM band have
    none."""
    name = name or ""
    if band == "fm":
        for m in _FM_RE.finditer(name):
            f = float(m.group(1))
            if 87.5 <= f <= 108.0:
                return f
    elif band == "am":
        for m in _AM_RE.finditer(name):
            f = float(m.group(1))
            if 530 <= f <= 1710:
                return f
    return None


def scale_position(band: str, freq: float) -> float:
    """Where `freq` sits along the 0..1 dial, against the printed scale.
    The AM scale is crowded at the top like a real set's (540, 600, 700 ...
    1000, 1200, 1400, 1600 are evenly spaced), so this interpolates between
    neighbouring marks rather than assuming a straight line."""
    marks = SCALE_MARKS[band]
    n = len(marks)
    ts = [SCALE_LO + (SCALE_HI - SCALE_LO) * i / (n - 1) for i in range(n)]
    if freq <= marks[0]:
        i = 0
    elif freq >= marks[-1]:
        i = n - 2
    else:
        i = next(k for k in range(n - 1) if marks[k] <= freq <= marks[k + 1])
    t = ts[i] + (ts[i + 1] - ts[i]) * (freq - marks[i]) / (marks[i + 1] - marks[i])
    return max(DIAL_MIN, min(DIAL_MAX, t))


def even_positions(n: int) -> list[float]:
    """n names spread evenly along the dial (the program bands, or a
    station band where nothing has a frequency)."""
    if n <= 0:
        return []
    if n == 1:
        return [0.5]
    return [SCALE_LO + (SCALE_HI - SCALE_LO) * i / (n - 1) for i in range(n)]


def dial_positions(band: str, names: Sequence[str]) -> list[float]:
    """Where each of a band's names sits along the 0..1 dial, in the order
    given. On FM and AM a station with a frequency in its name sits at that
    frequency; the rest (internet-only) fill the gaps between them, in
    their dial order, a bigger gap taking more of them."""
    n = len(names)
    if band not in SCALE_MARKS:
        return even_positions(n)
    fixed = {i: scale_position(band, f) for i, name in enumerate(names)
             if (f := frequency(name, band)) is not None}
    if not fixed:
        return even_positions(n)
    # two stations on (nearly) the same spot: nudge them apart so both can be tuned
    spots = sorted(fixed.items(), key=lambda kv: (kv[1], kv[0]))
    for k in range(1, len(spots)):
        i, t = spots[k]
        prev = spots[k - 1][1]
        if t - prev < MIN_GAP:
            spots[k] = (i, min(DIAL_MAX, prev + MIN_GAP))
    fixed = dict(spots)
    loose = [i for i in range(n) if i not in fixed]
    out = [0.0] * n
    for i, t in fixed.items():
        out[i] = t
    if loose:
        edges = [DIAL_MIN] + sorted(fixed.values()) + [DIAL_MAX]
        gaps = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
        # each internet-only station goes where it leaves the most room
        # around it, so they gather in the empty stretches of the dial
        # rather than squeezing between two stations a few notches apart.
        # An end gap has a station on one side only, so its spacing is
        # counted as if the glass edge were half a station away.
        last = len(gaps) - 1

        def spacing(k: int, m: int) -> float:
            a, b = gaps[k]
            return (b - a) / (m + (0.5 if k in (0, last) else 1.0))

        counts = [0] * len(gaps)
        for _ in loose:
            k = max(range(len(gaps)), key=lambda k: (spacing(k, counts[k] + 1), -k))
            counts[k] += 1
        slots: list[float] = []
        for k, (a, b) in enumerate(gaps):
            m = counts[k]
            if not m:
                continue
            step = spacing(k, m)
            if k == 0:          # left end: back from the first station
                slots += [b - step * j for j in range(m, 0, -1)]
            elif k == last:     # right end: on from the last station
                slots += [a + step * j for j in range(1, m + 1)]
            else:
                slots += [a + step * j for j in range(1, m + 1)]
        for i, t in zip(loose, sorted(slots)):
            out[i] = t
    return out


def band_stations(session: Session, band: str) -> list[RadioStation]:
    """A band's stations in dial order (stations without a band count as FM)."""
    return [st for st in dial(session) if (st.band if st.band in SCALE_MARKS else "fm") == band]


def neighbour(session: Session, station_id: int, delta: int, wrap: bool = True) -> Optional[int]:
    """The station `delta` places left/right of this one along its band, as
    the dial shows them (the player bar's previous/next while a station is
    on). Wraps round from one end of the band to the other."""
    st = session.get(RadioStation, station_id)
    if st is None:
        return None
    band = st.band if st.band in SCALE_MARKS else "fm"
    sts = band_stations(session, band)
    if len(sts) < 2:
        return None
    pos = dial_positions(band, [s.name for s in sts])
    order = [sts[i].id for i in sorted(range(len(sts)), key=lambda i: (pos[i], i))]
    k = order.index(station_id) + delta
    if wrap:
        k %= len(order)
    elif not 0 <= k < len(order):
        return None
    return order[k]


def _user_agent() -> str:
    try:
        from ..version import APP_VERSION

        return f"MusicMgr/{APP_VERSION}"
    except Exception:  # pragma: no cover
        return "MusicMgr/1"


@dataclass
class Found:
    """A station from a directory search, not (yet) on the dial."""

    uuid: str
    name: str
    url: str
    homepage: str = ""
    favicon: str = ""
    country: str = ""
    tags: str = ""
    codec: str = ""
    bitrate: int = 0
    votes: int = 0

    @property
    def detail(self) -> str:
        bits = [self.country, self.codec.upper() if self.codec else "",
                f"{self.bitrate} kbps" if self.bitrate else ""]
        return " · ".join(b for b in bits if b)


class DirectoryError(Exception):
    pass


def _get(path: str, params: dict, http=None) -> list:
    import requests

    http = http or requests.Session()
    last: Optional[Exception] = None
    for base in MIRRORS:
        try:
            r = http.get(base + path, params=params, timeout=TIMEOUT_S,
                         headers={"User-Agent": _user_agent()})
            if getattr(r, "status_code", 200) >= 500:
                last = DirectoryError(f"HTTP {r.status_code}")
                continue
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as exc:  # try the next mirror
            last = exc
    raise DirectoryError(str(last) if last else "station directory unreachable")


def _found(raw: dict) -> Optional[Found]:
    url = (raw.get("url_resolved") or raw.get("url") or "").strip()
    name = (raw.get("name") or "").strip()
    if not url or not name or str(raw.get("lastcheckok", 1)) == "0":
        return None
    try:
        bitrate = int(raw.get("bitrate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    try:
        votes = int(raw.get("votes") or 0)
    except (TypeError, ValueError):
        votes = 0
    return Found(
        uuid=raw.get("stationuuid") or "",
        name=name,
        url=url,
        homepage=raw.get("homepage") or "",
        favicon=raw.get("favicon") or "",
        country=raw.get("countrycode") or raw.get("country") or "",
        tags=raw.get("tags") or "",
        codec=raw.get("codec") or "",
        bitrate=bitrate,
        votes=votes,
    )


def search(text: str = "", params: Optional[dict] = None, limit: int = 60, http=None) -> list[Found]:
    """Search the directory: free text matches station names (and a tag of
    the same words); `params` comes from CATEGORIES. Most-liked first."""
    query = {"limit": limit, "hidebroken": "true", "order": "votes", "reverse": "true"}
    query.update(params or {})
    results: list[Found] = []
    seen: set[str] = set()
    searches = [query]
    if text.strip():
        searches = [{**query, "name": text.strip()}, {**query, "tag": text.strip().lower()}]
    for q in searches:
        for raw in _get("/json/stations/search", q, http=http):
            f = _found(raw)
            if f is None or (f.uuid or f.url) in seen:
                continue
            seen.add(f.uuid or f.url)
            results.append(f)
    results.sort(key=lambda f: -f.votes)
    return results[:limit]


def register_click(uuid: str, http=None) -> None:
    """Radio Browser's popularity count - one per station per day. Best effort."""
    if not uuid:
        return
    try:
        _get(f"/json/url/{uuid}", {}, http=http)
    except Exception:
        pass


# --------------------------------------------------------------------------
# the dial
# --------------------------------------------------------------------------


def dial(session: Session) -> list[RadioStation]:
    return list(session.scalars(select(RadioStation).order_by(RadioStation.dial_order, RadioStation.id)))


def on_dial(session: Session, found: Found) -> Optional[RadioStation]:
    if found.uuid:
        st = session.scalar(select(RadioStation).where(RadioStation.rb_uuid == found.uuid))
        if st is not None:
            return st
    return session.scalar(select(RadioStation).where(RadioStation.stream_url == found.url))


def add(session: Session, found: Found, band: str = "fm") -> RadioStation:
    existing = on_dial(session, found)
    if existing is not None:
        return existing
    last = session.scalar(select(func.max(RadioStation.dial_order))) or 0
    st = RadioStation(
        name=found.name[:300], stream_url=found.url, homepage=found.homepage or None,
        favicon_url=found.favicon or None, country=found.country or None,
        tags=found.tags or None, codec=found.codec or None, bitrate=found.bitrate or None,
        rb_uuid=found.uuid or None, dial_order=last + 1, band=band,
    )
    session.add(st)
    session.flush()
    return st


def add_manual(session: Session, name: str, url: str, band: str = "fm") -> RadioStation:
    return add(session, Found(uuid="", name=name.strip() or url, url=url.strip()), band=band)


def set_band(session: Session, station_id: int, band: str) -> None:
    st = session.get(RadioStation, station_id)
    if st is not None:
        st.band = band


def rename(session: Session, station_id: int, name: str) -> Optional[RadioStation]:
    """The name printed on the dial (James, 2026-10-01: "I would like to be
    able to rename the radio station text. For example the 100.7 WMMS -
    CLEVELAND, OHIO is way too long"). Blank names are ignored."""
    st = session.get(RadioStation, station_id)
    name = (name or "").strip()
    if st is not None and name:
        st.name = name[:300]
    return st


def remove(session: Session, station_id: int) -> None:
    st = session.get(RadioStation, station_id)
    if st is not None:
        if st.favicon_path:
            try:
                Path(st.favicon_path).unlink()
            except OSError:
                pass
        session.delete(st)
        session.flush()


def move(session: Session, station_id: int, delta: int,
         among: Optional[Sequence[int]] = None) -> None:
    """Shift a station left/right along the dial. `among` limits the move
    to those stations (the Radio page passes the band's internet-only ones,
    which are the only ones whose place depends on the order - 2026-10-03):
    the station swaps places with the next of them along."""
    stations = dial(session)
    idx = next((i for i, s in enumerate(stations) if s.id == station_id), None)
    if idx is None:
        return
    if among is not None:
        allowed = set(among) | {station_id}
        picks = [i for i, s in enumerate(stations) if s.id in allowed]
        k = picks.index(idx) + delta
        if not 0 <= k < len(picks):
            return
        j = picks[k]
        stations[idx], stations[j] = stations[j], stations[idx]
    else:
        j = max(0, min(len(stations) - 1, idx + delta))
        stations.insert(j, stations.pop(idx))
    for order, st in enumerate(stations, start=1):
        st.dial_order = order
    session.flush()


# --------------------------------------------------------------------------
# off the air (2026-10-03)
# --------------------------------------------------------------------------


def set_off_air(session: Session, station_id: int, off: bool) -> bool:
    """Mark a station whose stream stopped answering (or clear the mark
    once it plays again). True if anything changed."""
    st = session.get(RadioStation, station_id)
    if st is None or bool(st.off_air_since) == off:
        return False
    st.off_air_since = dt.datetime.now() if off else None
    session.flush()
    return True


_CALL_RE = re.compile(r"\b([KW][A-Z]{2,3})\b")


def replacement_queries(name: str) -> list[str]:
    """What to search the directory for when a station's link is dead: its
    call letters ("WXDX") if the name has them, and the name without the
    frequency ("The X")."""
    out = []
    call = _CALL_RE.search(name or "")
    if call:
        out.append(call.group(1))
    plain = _AM_RE.sub(" ", _FM_RE.sub(" ", name or ""))
    plain = re.sub(r"\b(AM|FM)\b", " ", plain)
    if call:
        plain = plain.replace(call.group(1), " ")
    plain = " ".join(re.sub(r"[-–—,|/()]+", " ", plain).split())
    if len(plain) >= 3 and plain.lower() not in (q.lower() for q in out):
        out.append(plain)
    return out or [(name or "").strip()]


def find_replacements(name: str, rb_uuid: Optional[str], current_url: str,
                      http=None, limit: int = 30) -> list[Found]:
    """Other stream links for a station that's off the air: first the same
    directory entry again (stations do move their streams, and the
    directory follows), then entries found by call letters and name. The
    dead link itself is left out."""
    results: list[Found] = []
    seen = {(current_url or "").strip()}
    if rb_uuid:
        try:
            for raw in _get("/json/stations/byuuid", {"uuids": rb_uuid}, http=http):
                f = _found(raw)
                if f is not None and f.url not in seen:
                    seen.add(f.url)
                    results.append(f)
        except DirectoryError:
            pass
    errors = []
    for q in replacement_queries(name):
        try:
            found = search(q, limit=limit, http=http)
        except DirectoryError as exc:
            errors.append(exc)
            continue
        for f in found:
            if f.url not in seen:
                seen.add(f.url)
                results.append(f)
    if not results and errors:
        raise errors[0]
    return results[:limit]


def replace_link(session: Session, station_id: int, found: Found) -> Optional[RadioStation]:
    """Point a station at a new stream, keeping its name, band, place on
    the dial and logo."""
    st = session.get(RadioStation, station_id)
    if st is None:
        return None
    st.stream_url = found.url
    st.rb_uuid = found.uuid or st.rb_uuid
    st.codec = found.codec or st.codec
    st.bitrate = found.bitrate or st.bitrate
    st.country = st.country or found.country or None
    st.homepage = st.homepage or found.homepage or None
    st.favicon_url = st.favicon_url or found.favicon or None
    st.off_air_since = None
    session.flush()
    return st


def logo_dir() -> Path:
    return config.DATA_DIR / "stations"


def fetch_logo(station_id: int, url: str, http=None) -> Optional[str]:
    """Download a station's logo next to the library (best effort)."""
    import requests

    if not url:
        return None
    http = http or requests.Session()
    try:
        r = http.get(url, timeout=TIMEOUT_S, headers={"User-Agent": _user_agent()})
        data = r.content
        if getattr(r, "status_code", 200) != 200 or not data or len(data) > 2_000_000:
            return None
    except Exception:
        return None
    ext = ".png"
    low = url.lower().split("?")[0]
    for e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".ico", ".svg"):
        if low.endswith(e):
            ext = e
    if ext in (".svg",):
        return None
    folder = logo_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{station_id}{ext}"
    path.write_bytes(data)
    return str(path)


def queue_item(st: RadioStation):
    from .player import QueueItem

    detail = " · ".join(x for x in (st.country, (st.codec or "").upper(),
                                     f"{st.bitrate} kbps" if st.bitrate else "") if x)
    return QueueItem(
        track_id=0,
        title=st.name,
        artist="Internet radio",
        album=detail,
        path=st.stream_url,
        cover_path=st.favicon_path,
        kind="stream",
        station_id=st.id,
        hydrated=True,
    )


def starter_stations(http=None) -> list[Found]:
    return search(params=STARTER_SEARCH, limit=STARTER_COUNT, http=http)


def names(stations: Sequence[RadioStation]) -> list[str]:
    return [s.name for s in stations]
