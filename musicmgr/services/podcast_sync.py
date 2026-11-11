"""Podcasts through the USB drive, so every PC has the same podcasts on its
PODCAST band and picks up each episode where another PC left it.

Rides on the Settings › USB sync "Podcasts" choice, inside the drive's
library state (services/library_state.py), as a flat dict merged three-way
key by key like old-time radio progress (otr_sync.py):

    feed:<feed link>            {"title", "author", "image_url", "dial_order"}
                                - subscribed; a key gone on one PC is an
                                unsubscribe there
    current:<feed link>         the episode (its guid) tuning in resumes at
    ep:<feed link>\\t<guid>       [heard, position in ms] - only for episodes
                                heard or started

A podcast arriving from the drive goes on the dial straight away; its
episodes are read from the feed the next time the Radio page opens, and
progress for them lands with the sync after that (until then it's carried,
never read as removed). The audio files themselves travel as the Podcast
folder pair (services/usb_sync.py), when episodes have been downloaded.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import PodcastEpisode, PodcastFeed
from . import podcasts

KEY = "podcasts"
_FEED_FIELDS = ("title", "author", "image_url", "dial_order")


def _ep_key(url: str, guid: str) -> str:
    return f"{url}\t{guid}"


class PodcastIndex:
    """This PC's podcasts and episodes by their sync keys."""

    def __init__(self, db: Session) -> None:
        self.feeds: dict[str, PodcastFeed] = {}
        self.episodes: dict[str, PodcastEpisode] = {}
        self.guid_of: dict[int, str] = {}
        for feed in db.scalars(select(PodcastFeed)):
            self.feeds[feed.feed_url] = feed
        urls = {f.id: url for url, f in self.feeds.items()}
        for ep in db.scalars(select(PodcastEpisode)):
            url = urls.get(ep.feed_id)
            if url is not None:
                self.episodes[_ep_key(url, ep.guid)] = ep
                self.guid_of[ep.id] = ep.guid

    def placeable(self, key: str) -> bool:
        kind, _, rest = key.partition(":")
        if kind == "feed":
            return True             # can always be subscribed to here
        if kind == "current":
            return rest in self.feeds
        if kind == "ep":
            return rest in self.episodes
        return False

    def prefer_usb(self, key: str, ours, theirs) -> bool:
        """Both sides changed `key`: take the drive's when it's further
        along - heard beats not heard, then the later position."""
        kind, _, _rest = key.partition(":")
        if kind == "ep":
            o = list(ours or [False, 0])[:2]
            u = list(theirs or [False, 0])[:2]
            if bool(u[0]) != bool(o[0]):
                return bool(u[0])
            return int(u[1] or 0) > int(o[1] or 0)
        return False


def read_podcasts(db: Session, idx: PodcastIndex | None = None) -> dict:
    idx = idx or PodcastIndex(db)
    out: dict[str, object] = {}
    for url, feed in idx.feeds.items():
        out[f"feed:{url}"] = {f: getattr(feed, f) for f in _FEED_FIELDS}
        if feed.current_episode_id is not None:
            guid = idx.guid_of.get(feed.current_episode_id)
            if guid:
                out[f"current:{url}"] = guid
    for key, ep in idx.episodes.items():
        if ep.played or ep.position_ms:
            out[f"ep:{key}"] = [bool(ep.played), int(ep.position_ms or 0)]
    return out


def carry_unplaced(local: dict, base: dict, idx: PodcastIndex) -> None:
    """Keys this PC can't place yet (an episode it hasn't read from the feed)
    look exactly as they did last time, so they're never taken as removed."""
    for key, value in base.items():
        if key not in local and not idx.placeable(key):
            local[key] = value


def apply_podcasts(db: Session, idx: PodcastIndex, merged: dict, local: dict) -> int:
    """Make this PC's podcasts match `merged`. Returns how many changes landed."""
    changed = 0
    # subscriptions first, so progress for a podcast that's still here applies
    for key in sorted(set(merged) | set(local)):
        kind, _, url = key.partition(":")
        if kind != "feed":
            continue
        new, old = merged.get(key), local.get(key)
        if new == old:
            continue
        feed = idx.feeds.get(url)
        if new is None:
            if feed is not None:
                podcasts.remove_feed(db, feed.id)
                del idx.feeds[url]
                for k in [k for k in idx.episodes if k.startswith(url + "\t")]:
                    del idx.episodes[k]
                changed += 1
            continue
        if feed is None:
            feed = podcasts.add_feed(db, url, new.get("title") or "", new.get("author") or "",
                                     new.get("image_url") or "")
            feed.last_checked = None        # read its episodes when the page opens
            idx.feeds[url] = feed
        for f in _FEED_FIELDS:
            value = new.get(f)
            if value is not None:
                setattr(feed, f, value)
        changed += 1
    db.flush()
    for key in set(merged) | set(local):
        new = merged.get(key)
        if new == local.get(key) or not idx.placeable(key):
            continue
        kind, _, rest = key.partition(":")
        if kind == "current":
            feed = idx.feeds.get(rest)
            if feed is None:
                continue
            ep = idx.episodes.get(_ep_key(rest, new)) if new else None
            feed.current_episode_id = ep.id if ep is not None else None
            changed += 1
        elif kind == "ep":
            ep = idx.episodes[rest]
            played, position = (new or [False, 0])[:2]
            ep.played, ep.position_ms = bool(played), int(position or 0)
            changed += 1
    db.flush()
    return changed
