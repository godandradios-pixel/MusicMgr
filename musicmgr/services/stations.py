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

import logging
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
#: "Add some to get started" on an empty FM or AM band
STARTER_SEARCH = {"tag": "old time radio"}
STARTER_COUNT = 6


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


def move(session: Session, station_id: int, delta: int) -> None:
    """Shift a station left/right along the dial."""
    stations = dial(session)
    idx = next((i for i, s in enumerate(stations) if s.id == station_id), None)
    if idx is None:
        return
    j = max(0, min(len(stations) - 1, idx + delta))
    stations.insert(j, stations.pop(idx))
    for order, st in enumerate(stations, start=1):
        st.dial_order = order
    session.flush()


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
