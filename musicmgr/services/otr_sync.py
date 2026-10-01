"""Old-time radio progress through the USB drive (2026-10-01).

James, after the Radio page's internet stations were added to USB sync:
"Please add moves for shows ... the Mint PC to pick up where you left off".
Rides on the Settings › USB sync "Radio" choice, inside the drive's
library state (services/library_state.py), as a flat dict merged three-way
key by key like the rest:

    band:<show>               the band a show is on (Band… moves)
    current:<show>            the episode tuning to the show resumes at
    ep:<show>/<episode path>  [heard, position in ms] - only for episodes
                              heard or started; one that's absent is unheard

A show is keyed by its folder's name ("The Shadow") and an episode by its
path inside that folder with "/" separators, so D:\\Radio\\The Shadow\\… on
Windows and ~/Radio/The Shadow/… on Mint are the same key. The audio files
themselves aren't copied by this: each PC reads its own radio folder.
Progress for a show or episode this PC doesn't have is kept on the drive
for the PCs that do, never read as "removed".
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import RadioEpisode, RadioShow

KEY = "otr"


class OtrIndex:
    """This PC's shows and episodes by their sync keys."""

    def __init__(self, db: Session) -> None:
        self.shows: dict[str, RadioShow] = {}
        self.episodes: dict[str, RadioEpisode] = {}
        self.key_of_episode: dict[int, str] = {}
        for show in db.scalars(select(RadioShow)):
            name = Path(show.folder).name
            self.shows[name] = show
        by_id = {s.id: (n, s) for n, s in self.shows.items()}
        for ep in db.scalars(select(RadioEpisode)):
            owner = by_id.get(ep.show_id)
            if owner is None:
                continue
            name, show = owner
            try:
                rel = Path(ep.path).relative_to(Path(show.folder)).as_posix()
            except ValueError:
                rel = Path(ep.path).name
            key = f"{name}/{rel}"
            self.episodes[key] = ep
            self.key_of_episode[ep.id] = key

    def placeable(self, key: str) -> bool:
        kind, _, rest = key.partition(":")
        if kind == "band":
            return rest in self.shows
        if kind == "current":
            return rest in self.shows
        if kind == "ep":
            return rest in self.episodes
        return False


    def prefer_usb(self, key: str, ours, theirs) -> bool:
        """Both sides changed `key` (or, on a first sync, both simply have
        it): take the drive's when it's further along - a show moved by
        hand off the band it would be guessed onto, an episode heard or
        listened further, a resume point later in the show."""
        kind, _, rest = key.partition(":")
        if kind == "band":
            show = self.shows.get(rest)
            if show is None:
                return False
            from .otr import guess_band

            default = guess_band(show.name, show.kind)
            return ours == default and theirs != default
        if kind == "current":
            a, b = self.episodes.get(ours or ""), self.episodes.get(theirs or "")
            return bool(a is not None and b is not None and b.sort_key > a.sort_key)
        if kind == "ep":
            o = list(ours or [False, 0])[:2]
            u = list(theirs or [False, 0])[:2]
            if bool(u[0]) != bool(o[0]):
                return bool(u[0])
            return int(u[1] or 0) > int(o[1] or 0)
        return False


def read_otr(db: Session, idx: Optional[OtrIndex] = None) -> dict:
    idx = idx or OtrIndex(db)
    out: dict[str, object] = {}
    for name, show in idx.shows.items():
        out[f"band:{name}"] = show.band
        if show.current_episode_id is not None:
            key = idx.key_of_episode.get(show.current_episode_id)
            if key:
                out[f"current:{name}"] = key
    for key, ep in idx.episodes.items():
        if ep.played or ep.position_ms:
            out[f"ep:{key}"] = [bool(ep.played), int(ep.position_ms or 0)]
    return out


def carry_unplaced(local: dict, base: dict, idx: OtrIndex) -> None:
    """Keys this PC can't place (a show or episode it doesn't have) look,
    from here, exactly as they did last time - so they're never taken as
    removed and stay on the drive for the PCs that have them."""
    for key, value in base.items():
        if key not in local and not idx.placeable(key):
            local[key] = value


def apply_otr(db: Session, idx: OtrIndex, merged: dict, local: dict) -> int:
    """Make this PC's shows match `merged`. Returns how many changes landed."""
    changed = 0
    for key in set(merged) | set(local):
        new = merged.get(key)
        if new == local.get(key) or not idx.placeable(key):
            continue
        kind, _, rest = key.partition(":")
        if kind == "band":
            if new:
                idx.shows[rest].band = new
                changed += 1
        elif kind == "current":
            ep = idx.episodes.get(new) if new else None
            idx.shows[rest].current_episode_id = ep.id if ep is not None else None
            changed += 1
        elif kind == "ep":
            ep = idx.episodes[rest]
            played, position = (new or [False, 0])[:2]
            ep.played, ep.position_ms = bool(played), int(position or 0)
            changed += 1
    db.flush()
    return changed
