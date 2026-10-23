"""An artist's top songs: one row per song, and the user's own picks.

Two things, both for the artist page's top-songs list
(ui/widgets/artist_panel.py):

- **One row per song.** Last.fm's chart lists versions of a song as
  separate entries - "Hells Bells", "Hells Bells - Remastered", "Tom
  Sawyer (Live)" - so one song could take several of the five places.
  `song_key` treats every version of a song as the same song, and
  `distinct_chart` folds the chart down to one row per song, with the
  plain title and the versions' plays added together.
- **Picks.** The user can choose the five himself (`ArtistTopPick`). Picks
  survive a new Last.fm fetch, and "Use Last.fm's list" puts the chart
  back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import delete, select

from ..db.models import ArtistTopPick
from .matching import strip_accents

#: words that make a title a version of a song rather than a different one
_VERSION_WORDS = (
    r"remaster(?:ed)?|live|version|edit|mix|remix|mono|stereo|demo|acoustic|"
    r"single|radio|edition|deluxe|anniversary|take|session|bonus|explicit|"
    r"clean|instrumental|unplugged|re-?recorded|rerecorded|extended|original|"
    r"alternate|alt|outtake|reprise|lp|45|digital|from|recorded|in concert"
)
_BRACKETED = re.compile(r"[\(\[][^\)\]]*\b(?:" + _VERSION_WORDS + r")\b[^\)\]]*[\)\]]",
                        re.IGNORECASE)
_DASHED = re.compile(r"\s[-–—]\s.*\b(?:" + _VERSION_WORDS + r")\b.*$", re.IGNORECASE)
_FEAT = re.compile(r"[\(\[]\s*(?:feat|ft|featuring|with)\.?\s[^\)\]]*[\)\]]", re.IGNORECASE)


def clean_title(title: str) -> str:
    """'Hells Bells - Remastered 2003' -> 'Hells Bells';
    'Tom Sawyer (Live in Cleveland)' -> 'Tom Sawyer'."""
    text = _FEAT.sub(" ", title or "")
    text = _BRACKETED.sub(" ", text)
    text = _DASHED.sub(" ", text)
    text = " ".join(text.split()).strip(" -–—")
    return text or (title or "").strip()


def song_key(title: str) -> str:
    """The same key for every version of one song."""
    text = strip_accents(clean_title(title)).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = " ".join(text.split())
    return re.sub(r"^(?:the|a|an) ", "", text)  # "The Spirit of Radio" = "Spirit of Radio"


@dataclass
class ChartSong:
    title: str
    key: str
    #: Last.fm plays for every version of the song together
    playcount: Optional[int]


def distinct_chart(entries: Sequence) -> list[ChartSong]:
    """Fold a chart (anything with .title and .playcount) to one row per
    song, best first. The song's title is its plainest version's."""
    songs: dict[str, ChartSong] = {}
    order: list[str] = []
    for entry in entries:
        key = song_key(entry.title)
        if not key:
            continue
        plays = entry.playcount
        song = songs.get(key)
        if song is None:
            songs[key] = ChartSong(clean_title(entry.title), key, plays)
            order.append(key)
            continue
        if plays is not None:
            song.playcount = (song.playcount or 0) + plays
        cleaned = clean_title(entry.title)
        if len(cleaned) < len(song.title):
            song.title = cleaned
    out = [songs[k] for k in order]
    first_seen = {k: i for i, k in enumerate(order)}
    out.sort(key=lambda s: (-(s.playcount if s.playcount is not None else -1),
                            first_seen[s.key]))
    return out


# -- picks ---------------------------------------------------------------------


def get_picks(db, artist_id: int) -> list[str]:
    return list(db.scalars(select(ArtistTopPick.title)
                           .where(ArtistTopPick.artist_id == artist_id)
                           .order_by(ArtistTopPick.position)))


def set_picks(db, artist_id: int, titles: Sequence[str]) -> list[str]:
    """Save the artist's top songs in order - blanks and repeats (two
    versions of one song) dropped. An empty list means "use Last.fm's"."""
    kept: list[str] = []
    seen: set[str] = set()
    for title in titles:
        title = (title or "").strip()
        key = song_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(title)
    db.execute(delete(ArtistTopPick).where(ArtistTopPick.artist_id == artist_id))
    for i, title in enumerate(kept, start=1):
        db.add(ArtistTopPick(artist_id=artist_id, position=i, title=title))
    db.flush()
    return kept


def clear_picks(db, artist_id: int) -> None:
    set_picks(db, artist_id, [])
