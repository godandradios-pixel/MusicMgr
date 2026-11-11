"""Podcasts - the Radio page's PODCAST band.

A podcast is a show whose episodes come from an RSS feed instead of a
folder. It sits on the PODCAST band of the dial (ui/widgets/radio_dial.py),
between LW and SXM, and behaves much like an old-time radio show
(services/otr.py): tuning in resumes the episode in progress, or plays the
newest one not heard yet; positions are saved as it plays and an episode
heard past 95% counts as heard.

Episodes stream straight from the feed. "Download" keeps a copy in the
Podcast folder (D:\\Podcast\\<podcast title>\\, next to the Radio folder;
~/Podcast on Linux), which is played instead of the stream when it's there
and can travel on the USB drive (Settings › USB sync › Podcasts). Nothing
downloads by itself.

Finding podcasts: Apple's free podcast directory (the iTunes Search API -
no account or key), or paste a feed's RSS link, or an Apple Podcasts page
link (looked up to its feed).

New episodes are looked for when the Radio page opens (in the background,
at most every CHECK_EVERY) and with "Check for new episodes" - never at app
start-up. A feed that hasn't changed answers 304 (ETag / Last-Modified), so
a check costs little.

The feed is read with the standard library's XML parser plus the itunes:
namespace (duration, image, episode artwork) - no extra packages, so the
.exe doesn't grow.

Settings › Appearance › Radio can take the band off the dial
(SHOW_KEY); subscriptions and progress are kept.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import PodcastEpisode, PodcastFeed, Setting

log = logging.getLogger(__name__)

#: the band's key on the dial (ui/widgets/radio_dial.py:BANDS)
BAND = "pod"
#: Settings › Appearance › Radio: show the PODCAST band. Off hides it;
#: the subscriptions stay
SHOW_KEY = "tuner_show_podcast"
#: where downloaded episodes go (D:\\Podcast); see default_root
ROOT_KEY = "podcast_root"
FOLDER_NAME = "Podcast"
#: the speed episodes play at (the player bar's speed button)
SPEED_KEY = "podcast_speed"
SPEEDS = (1.0, 1.25, 1.5)

TIMEOUT_S = 15
#: the Radio page looks for new episodes when it opens, at most this often
CHECK_EVERY = dt.timedelta(hours=3)
#: keep at most this many episodes of a feed (the newest); downloaded,
#: started or in-progress ones are always kept
KEEP_EPISODES = 300
#: an episode heard past this share of its length counts as heard
HEARD_SHARE = 0.95
#: a position closer to the start than this isn't worth resuming at
MIN_RESUME_MS = 15_000
#: what tuning in queues: the episode and the unheard ones after it
QUEUE_LIMIT = 10
#: an episode counts as "new" for this long after it first turned up
NEW_FOR = dt.timedelta(days=14)
MAX_FEED_BYTES = 30_000_000

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
ATOM = "{http://www.w3.org/2005/Atom}"


def _utcnow() -> dt.datetime:
    """Now, as naive UTC (how the episode dates are stored)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class FeedError(Exception):
    """A feed couldn't be fetched or read (the message is for the user)."""


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def _get(session: Session, key: str) -> Optional[str]:
    row = session.get(Setting, key)
    return row.value if row is not None else None


def _put(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def shown(session: Session) -> bool:
    """Whether the PODCAST band is on the dial (on unless turned off)."""
    return _get(session, SHOW_KEY) != "0"


def set_shown(session: Session, on: bool) -> None:
    """Turn the PODCAST band on or off (the caller commits)."""
    _put(session, SHOW_KEY, "1" if on else "0")


def speed(session: Session) -> float:
    try:
        value = float(_get(session, SPEED_KEY) or 1.0)
    except ValueError:
        value = 1.0
    return value if value in SPEEDS else 1.0


def set_speed(session: Session, value: float) -> None:
    _put(session, SPEED_KEY, f"{value:g}" if value in SPEEDS else "1")


def next_speed(value: float) -> float:
    """The speed button's next step: 1× → 1.25× → 1.5× → 1×."""
    try:
        i = SPEEDS.index(value)
    except ValueError:
        return SPEEDS[0]
    return SPEEDS[(i + 1) % len(SPEEDS)]


def speed_label(value: float) -> str:
    return f"{value:g}×"


def default_root(session: Session) -> str:
    """D:\\Podcast: beside the Radio folder (D:\\Radio -> D:\\Podcast,
    ~/Radio -> ~/Podcast), else ~/Podcast."""
    from .otr import get_root

    try:
        radio = get_root(session)
    except Exception:  # pragma: no cover - no radio tables
        radio = None
    if radio:
        return str(Path(radio).parent / FOLDER_NAME)
    return str(Path.home() / FOLDER_NAME)


def get_root(session: Session) -> str:
    return _get(session, ROOT_KEY) or default_root(session)


def root_is_set(session: Session) -> bool:
    return bool(_get(session, ROOT_KEY))


def set_root(session: Session, folder: str) -> None:
    _put(session, ROOT_KEY, str(folder))


# --------------------------------------------------------------------------
# reading a feed
# --------------------------------------------------------------------------


@dataclass
class EpisodeInfo:
    guid: str
    title: str
    audio_url: str
    published: Optional[dt.datetime] = None
    duration_ms: Optional[int] = None
    description: str = ""


@dataclass
class FeedInfo:
    title: str
    author: str = ""
    description: str = ""
    image_url: str = ""
    episodes: list[EpisodeInfo] = field(default_factory=list)


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h\d)\s*/?\s*>")


def plain_text(value: str) -> str:
    """Show notes as plain text: tags dropped, paragraphs kept."""
    if not value:
        return ""
    text = _BLOCK_RE.sub("\n", value)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def parse_duration(value: Optional[str]) -> Optional[int]:
    """itunes:duration - "1:02:03", "62:03" or seconds ("3723") - in ms."""
    if not value:
        return None
    value = value.strip()
    try:
        if ":" in value:
            secs = 0.0
            for part in value.split(":"):
                secs = secs * 60 + float(part or 0)
        else:
            secs = float(value)
    except ValueError:
        return None
    return int(secs * 1000) if secs > 0 else None


def parse_date(value: Optional[str]) -> Optional[dt.datetime]:
    """An RSS pubDate (RFC 822) or an Atom/ISO date, as naive UTC."""
    if not value:
        return None
    value = value.strip()
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def _text(el, path: str) -> str:
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(data: bytes) -> FeedInfo:
    """A podcast's RSS feed: its title, art and audio episodes (newest
    first). Items without an audio enclosure (articles, videos only) are
    skipped. Raises FeedError if it isn't a podcast feed."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"That isn't a podcast feed (the page couldn't be read: {exc})") from None
    channel = root.find("channel")
    if channel is None:
        raise FeedError("That isn't a podcast feed - no RSS channel in it")
    image = ""
    it_img = channel.find(f"{ITUNES}image")
    if it_img is not None:
        image = (it_img.get("href") or "").strip()
    if not image:
        image = _text(channel, "image/url")
    info = FeedInfo(
        title=_text(channel, "title") or "Untitled podcast",
        author=_text(channel, f"{ITUNES}author") or _text(channel, "managingEditor"),
        description=plain_text(_text(channel, f"{ITUNES}summary") or _text(channel, "description")),
        image_url=image,
    )
    seen: set[str] = set()
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        url = (enc.get("url") or "").strip() if enc is not None else ""
        kind = (enc.get("type") or "").lower() if enc is not None else ""
        if not url or (kind and not kind.startswith("audio") and "mpeg" not in kind
                       and "octet" not in kind):
            continue
        guid = _text(item, "guid") or url
        if guid in seen:
            continue
        seen.add(guid)
        desc = (_text(item, f"{CONTENT}encoded") or _text(item, "description")
                or _text(item, f"{ITUNES}summary"))
        info.episodes.append(EpisodeInfo(
            guid=guid,
            title=_text(item, "title") or Path(urlsplit(url).path).stem or "Episode",
            audio_url=url,
            published=parse_date(_text(item, "pubDate")),
            duration_ms=parse_duration(_text(item, f"{ITUNES}duration")),
            description=plain_text(desc),
        ))
    info.episodes.sort(key=lambda e: e.published or dt.datetime.min, reverse=True)
    return info


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------


def _user_agent() -> str:
    from .stations import _user_agent as ua

    return ua()


def _http(http=None):
    if http is not None:
        return http
    import requests

    return requests.Session()


@dataclass
class Fetched:
    #: None when the feed answered "not modified"
    data: Optional[bytes]
    etag: Optional[str] = None
    last_modified: Optional[str] = None


def fetch_feed(url: str, etag: Optional[str] = None, last_modified: Optional[str] = None,
               http=None) -> Fetched:
    headers = {"User-Agent": _user_agent(), "Accept": "application/rss+xml, application/xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        r = _http(http).get(url, timeout=TIMEOUT_S, headers=headers)
    except Exception as exc:
        raise FeedError(f"Couldn't reach the podcast's feed ({exc})") from None
    status = getattr(r, "status_code", 200)
    if status == 304:
        return Fetched(None, etag, last_modified)
    if status >= 400:
        raise FeedError(f"The podcast's feed answered HTTP {status}")
    data = r.content or b""
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("The podcast's feed is too large to read")
    hdr = getattr(r, "headers", {}) or {}
    return Fetched(data, hdr.get("ETag"), hdr.get("Last-Modified"))


@dataclass
class Found:
    """A podcast from a directory search, not subscribed to (yet)."""

    title: str
    author: str
    feed_url: str
    image_url: str = ""
    genre: str = ""
    episodes: int = 0

    @property
    def detail(self) -> str:
        bits = [self.author, self.genre, f"{self.episodes:,} episodes" if self.episodes else ""]
        return " · ".join(b for b in bits if b)


ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
_APPLE_ID_RE = re.compile(r"podcasts\.apple\.com/.*?/id(\d+)", re.I)


def _found(raw: dict) -> Optional[Found]:
    url = (raw.get("feedUrl") or "").strip()
    if not url:
        return None
    return Found(
        title=(raw.get("collectionName") or raw.get("trackName") or "").strip() or url,
        author=(raw.get("artistName") or "").strip(),
        feed_url=url,
        image_url=raw.get("artworkUrl600") or raw.get("artworkUrl100") or "",
        genre=raw.get("primaryGenreName") or "",
        episodes=int(raw.get("trackCount") or 0),
    )


def search(term: str, limit: int = 40, http=None) -> list[Found]:
    """Apple's podcast directory, by name or subject."""
    term = term.strip()
    if not term:
        return []
    try:
        r = _http(http).get(ITUNES_SEARCH, timeout=TIMEOUT_S,
                            params={"media": "podcast", "term": term, "limit": limit},
                            headers={"User-Agent": _user_agent()})
        data = r.json()
    except Exception as exc:
        raise FeedError(f"Couldn't reach the podcast directory ({exc})") from None
    out, seen = [], set()
    for raw in data.get("results") or []:
        f = _found(raw)
        if f is not None and f.feed_url not in seen:
            seen.add(f.feed_url)
            out.append(f)
    return out


def apple_id(link: str) -> Optional[str]:
    m = _APPLE_ID_RE.search(link or "")
    return m.group(1) if m else None


def resolve_link(link: str, http=None) -> str:
    """A pasted link as a feed address: an Apple Podcasts page is looked up
    to its feed; anything else is taken as the feed itself."""
    link = link.strip()
    ident = apple_id(link)
    if not ident:
        return link
    try:
        r = _http(http).get(ITUNES_LOOKUP, timeout=TIMEOUT_S, params={"id": ident},
                            headers={"User-Agent": _user_agent()})
        results = r.json().get("results") or []
    except Exception as exc:
        raise FeedError(f"Couldn't look up that Apple Podcasts link ({exc})") from None
    for raw in results:
        f = _found(raw)
        if f is not None:
            return f.feed_url
    raise FeedError("Apple Podcasts has no public feed for that podcast")


def looks_like_link(text: str) -> bool:
    return bool(re.match(r"(?i)^(https?|feed)://\S+$", (text or "").strip()))


def image_dir() -> Path:
    return config.DATA_DIR / "podcasts"


def fetch_image(feed_id: int, url: str, http=None) -> Optional[str]:
    """Save a podcast's cover art next to the library (best effort)."""
    if not url:
        return None
    try:
        r = _http(http).get(url, timeout=TIMEOUT_S, headers={"User-Agent": _user_agent()})
        data = r.content
        if getattr(r, "status_code", 200) != 200 or not data or len(data) > 8_000_000:
            return None
    except Exception:
        return None
    ext = ".jpg"
    low = urlsplit(url).path.lower()
    for e in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if low.endswith(e):
            ext = e
    folder = image_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{feed_id}{ext}"
    path.write_bytes(data)
    return str(path)


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------


def feeds(session: Session) -> list[PodcastFeed]:
    """On the dial, in dial order."""
    return list(session.scalars(
        select(PodcastFeed).order_by(PodcastFeed.dial_order, PodcastFeed.id)))


def by_url(session: Session, url: str) -> Optional[PodcastFeed]:
    return session.scalar(select(PodcastFeed).where(PodcastFeed.feed_url == url.strip()))


def add_feed(session: Session, url: str, title: str = "", author: str = "",
             image_url: str = "") -> PodcastFeed:
    """Put a podcast on the dial (episodes come with apply_feed / check).
    Already subscribed: that one."""
    url = url.strip()
    found = by_url(session, url)
    if found is not None:
        return found
    order = session.scalar(select(func.max(PodcastFeed.dial_order))) or 0
    feed = PodcastFeed(feed_url=url, title=title.strip() or url, author=author or None,
                       image_url=image_url or None, dial_order=order + 1)
    session.add(feed)
    session.flush()
    return feed


def apply_feed(session: Session, feed: PodcastFeed, info: FeedInfo,
               now: Optional[dt.datetime] = None) -> int:
    """Bring a podcast's episodes up to date with its feed. Returns how many
    episodes are new. Episodes that left the feed are dropped unless
    they're downloaded or started; the newest KEEP_EPISODES are kept."""
    now = now or _utcnow()
    first_time = not feed.episodes
    feed.title = info.title or feed.title
    feed.author = info.author or feed.author
    feed.description = info.description or feed.description
    if info.image_url and info.image_url != feed.image_url:
        feed.image_url = info.image_url
        feed.image_path = None          # fetched again
    have = {ep.guid: ep for ep in feed.episodes}
    added = 0
    for item in info.episodes[:KEEP_EPISODES]:
        ep = have.pop(item.guid, None)
        if ep is None:
            ep = PodcastEpisode(feed=feed, guid=item.guid, first_seen=now)
            session.add(ep)
            added += 1
        ep.title = item.title[:500]
        ep.audio_url = item.audio_url
        ep.published = item.published
        ep.duration_ms = item.duration_ms or ep.duration_ms
        ep.description = item.description or None
    for ep in have.values():
        if not (ep.local_path or ep.position_ms or ep.played):
            session.delete(ep)
    feed.last_checked = now
    feed.last_error = None
    session.flush()
    if first_time:
        # what was already in the feed when subscribing isn't "new"
        for ep in feed.episodes:
            ep.first_seen = now - NEW_FOR
        return 0
    return added


def remove_feed(session: Session, feed_id: int) -> None:
    """Unsubscribe. Downloaded episodes stay in the Podcast folder."""
    feed = session.get(PodcastFeed, feed_id)
    if feed is None:
        return
    if feed.image_path:
        try:
            os.remove(feed.image_path)
        except OSError:
            pass
    session.delete(feed)
    session.flush()


def move(session: Session, feed_id: int, delta: int) -> bool:
    """Move a podcast along the band (Move ◀ / ▶)."""
    order = feeds(session)
    ids = [f.id for f in order]
    if feed_id not in ids:
        return False
    i = ids.index(feed_id)
    j = i + delta
    if not 0 <= j < len(order):
        return False
    order[i], order[j] = order[j], order[i]
    for n, f in enumerate(order, start=1):
        f.dial_order = n
    session.flush()
    return True


def due(feed: PodcastFeed, now: Optional[dt.datetime] = None) -> bool:
    now = now or _utcnow()
    return feed.last_checked is None or now - feed.last_checked >= CHECK_EVERY


@dataclass
class CheckResult:
    checked: int = 0
    new: int = 0
    failed: list[str] = field(default_factory=list)
    #: feed id -> how many new episodes it got
    per_feed: dict[int, int] = field(default_factory=dict)

    def summary(self) -> str:
        if not self.checked:
            return "No podcasts to check"
        bits = [f"{self.new} new episode{'s' if self.new != 1 else ''}" if self.new
                else "No new episodes"]
        if self.failed:
            bits.append(f"couldn't reach {', '.join(self.failed)}")
        return " · ".join(bits)


def check(session_factory, feed_ids: Optional[list[int]] = None, only_due: bool = False,
          http=None, cancelled: Callable[[], bool] = lambda: False) -> CheckResult:
    """Look for new episodes (run on a background thread). `session_factory`
    is db.session.new_session; each feed is committed as it's done."""
    result = CheckResult()
    http = _http(http)
    with session_factory() as session:
        rows = [(f.id, f.feed_url, f.etag, f.last_modified, f.title)
                for f in feeds(session)
                if (feed_ids is None or f.id in feed_ids) and (not only_due or due(f))]
    for feed_id, url, etag, modified, title in rows:
        if cancelled():
            break
        result.checked += 1
        try:
            got = fetch_feed(url, etag, modified, http=http)
            info = parse_feed(got.data) if got.data is not None else None
        except FeedError as exc:
            result.failed.append(title)
            with session_factory() as session:
                feed = session.get(PodcastFeed, feed_id)
                if feed is not None:
                    feed.last_error = str(exc)
                    feed.last_checked = _utcnow()
                    session.commit()
            continue
        with session_factory() as session:
            feed = session.get(PodcastFeed, feed_id)
            if feed is None:
                continue
            if info is None:
                feed.last_checked = _utcnow()
                feed.last_error = None
                added = 0
            else:
                added = apply_feed(session, feed, info)
                feed.etag, feed.last_modified = got.etag, got.last_modified
            need_image = feed.image_url and not (feed.image_path and os.path.exists(feed.image_path))
            image_url = feed.image_url
            session.commit()
        if need_image:
            path = fetch_image(feed_id, image_url, http=http)
            if path:
                with session_factory() as session:
                    feed = session.get(PodcastFeed, feed_id)
                    if feed is not None:
                        feed.image_path = path
                        session.commit()
        result.new += added
        if added:
            result.per_feed[feed_id] = added
    return result


def subscribe(session_factory, link: str, title: str = "", author: str = "",
              image_url: str = "", http=None) -> int:
    """Subscribe to a podcast by its feed (or Apple Podcasts) link: read the
    feed first, so a link that isn't a podcast is refused (FeedError) rather
    than leaving an empty name on the dial. Returns the feed's id."""
    http = _http(http)
    url = resolve_link(link, http=http)
    if url.lower().startswith("feed://"):
        url = "https://" + url[7:]
    got = fetch_feed(url, http=http)
    info = parse_feed(got.data or b"")
    if not info.episodes:
        raise FeedError(f"{info.title} has no audio episodes in its feed")
    with session_factory() as session:
        feed = add_feed(session, url, info.title or title, info.author or author,
                        info.image_url or image_url)
        apply_feed(session, feed, info)
        feed.etag, feed.last_modified = got.etag, got.last_modified
        feed_id = feed.id
        image = feed.image_url
        session.commit()
    path = fetch_image(feed_id, image, http=http) if image else None
    if path:
        with session_factory() as session:
            feed = session.get(PodcastFeed, feed_id)
            if feed is not None:
                feed.image_path = path
                session.commit()
    return feed_id


# --------------------------------------------------------------------------
# episodes and playing
# --------------------------------------------------------------------------


def episodes(session: Session, feed_id: int) -> list[PodcastEpisode]:
    """Newest first."""
    return list(session.scalars(
        select(PodcastEpisode).where(PodcastEpisode.feed_id == feed_id)
        .order_by(PodcastEpisode.published.desc().nulls_last(), PodcastEpisode.id.desc())))


def is_new(ep: PodcastEpisode, now: Optional[dt.datetime] = None) -> bool:
    """Arrived since subscribing, within NEW_FOR, and not started."""
    now = now or _utcnow()
    return (not ep.played and not ep.position_ms and ep.first_seen is not None
            and now - ep.first_seen < NEW_FOR)


def new_count(session: Session, feed_id: int) -> int:
    now = _utcnow()
    return sum(1 for ep in episodes(session, feed_id) if is_new(ep, now))


def current_episode(session: Session, feed: PodcastFeed) -> Optional[PodcastEpisode]:
    """What tuning in plays: the episode last played if it isn't finished,
    else the newest one not heard yet, else the newest."""
    if feed.current_episode_id is not None:
        ep = session.get(PodcastEpisode, feed.current_episode_id)
        if ep is not None and ep.feed_id == feed.id and not ep.played:
            return ep
    eps = episodes(session, feed.id)
    started = [e for e in eps if e.position_ms and not e.played]
    if started:
        return started[0]
    unheard = [e for e in eps if not e.played]
    if unheard:
        return unheard[0]
    return eps[0] if eps else None


def playable_path(ep: PodcastEpisode) -> str:
    """The downloaded copy if it's still there, else the stream."""
    if ep.local_path and os.path.exists(ep.local_path):
        return ep.local_path
    return ep.audio_url


def queue_item(ep: PodcastEpisode, feed: PodcastFeed, resume: bool = True):
    from .player import QueueItem

    published = ep.published.strftime("%b %-d, %Y") if ep.published and os.name != "nt" \
        else (ep.published.strftime("%b %#d, %Y") if ep.published else "")
    return QueueItem(
        track_id=0,
        title=ep.title,
        artist=feed.title,
        album=published,
        path=playable_path(ep),
        duration_ms=ep.duration_ms or 0,
        cover_path=feed.image_path,
        kind="podcast",
        episode_id=ep.id,
        start_ms=ep.position_ms if resume and ep.position_ms and not ep.played else 0,
        hydrated=True,
    )


def queue_for_feed(session: Session, feed_id: int, start_episode_id: Optional[int] = None,
                   limit: int = QUEUE_LIMIT):
    """The episode to play (`start_episode_id`, or current_episode) and the
    unheard ones after it in the list (older), as queue items."""
    feed = session.get(PodcastFeed, feed_id)
    if feed is None:
        return []
    eps = episodes(session, feed_id)
    if not eps:
        return []
    start = session.get(PodcastEpisode, start_episode_id) if start_episode_id else None
    if start is None or start.feed_id != feed_id:
        start = current_episode(session, feed)
    if start is None:
        return []
    i = next((k for k, e in enumerate(eps) if e.id == start.id), 0)
    picked = [start] + [e for e in eps[i + 1:] if not e.played][: limit - 1]
    return [queue_item(e, feed, resume=(n == 0)) for n, e in enumerate(picked)]


def set_current(session: Session, ep: PodcastEpisode) -> None:
    feed = session.get(PodcastFeed, ep.feed_id)
    if feed is not None:
        feed.current_episode_id = ep.id


def mark_played(session: Session, ep: PodcastEpisode, played: bool = True) -> None:
    ep.played = played
    ep.position_ms = 0
    if played:
        ep.last_played_at = _utcnow()


def save_position(session: Session, episode_id: int, position_ms: int, duration_ms: int) -> None:
    """Where an episode was left; heard past HEARD_SHARE counts as heard."""
    ep = session.get(PodcastEpisode, episode_id)
    if ep is None:
        return
    length = duration_ms or ep.duration_ms or 0
    if length and position_ms >= length * HEARD_SHARE:
        mark_played(session, ep, True)
        return
    if ep.played:
        return
    ep.position_ms = position_ms if position_ms >= MIN_RESUME_MS else 0
    ep.last_played_at = _utcnow()


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, limit: int = 120) -> str:
    """A file or folder name that's fine on Windows and Linux."""
    name = _BAD_CHARS.sub(" ", text or "").strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    return (name[:limit].rstrip(". ") or "Podcast")


def download_path(root: str, feed: PodcastFeed, ep: PodcastEpisode) -> Path:
    ext = Path(urlsplit(ep.audio_url).path).suffix.lower()
    if ext not in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"):
        ext = ".mp3"
    date = ep.published.strftime("%Y-%m-%d ") if ep.published else ""
    return Path(root) / safe_name(feed.title) / f"{date}{safe_name(ep.title, 100)}{ext}"


def download(url: str, dest: Path, http=None,
             progress: Optional[Callable[[int, int], None]] = None,
             cancelled: Callable[[], bool] = lambda: False) -> bool:
    """Save an episode to `dest` (via a .part file). False if cancelled."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with _http(http).get(url, timeout=TIMEOUT_S, stream=True,
                             headers={"User-Agent": _user_agent()}) as r:
            status = getattr(r, "status_code", 200)
            if status >= 400:
                raise FeedError(f"The episode's download answered HTTP {status}")
            total = int((getattr(r, "headers", {}) or {}).get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancelled():
                        break
                    if chunk:
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        if cancelled():
            part.unlink(missing_ok=True)
            return False
        os.replace(part, dest)
        return True
    except FeedError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise FeedError(f"The download failed ({exc})") from None


def delete_download(session: Session, ep: PodcastEpisode) -> None:
    if ep.local_path:
        try:
            os.remove(ep.local_path)
        except OSError:
            pass
        folder = Path(ep.local_path).parent
        ep.local_path = None
        try:
            folder.rmdir()      # only if that left it empty
        except OSError:
            pass


def link_downloads(session: Session) -> int:
    """Episodes whose file is already in the Podcast folder (it came on the
    USB drive from another PC) get it as their copy; copies that are gone
    are forgotten. Returns how many changed."""
    root = get_root(session)
    changed = 0
    for feed in feeds(session):
        for ep in feed.episodes:
            path = download_path(root, feed, ep)
            if ep.local_path and not os.path.exists(ep.local_path):
                ep.local_path = None
                changed += 1
            if not ep.local_path and path.is_file():
                ep.local_path = str(path)
                changed += 1
    session.flush()
    return changed
