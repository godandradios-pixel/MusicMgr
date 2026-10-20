"""Songs heard on an internet station (2026-10-19 - James chose "Heard on
this station" for Now Playing's right-hand pane while a station plays).

A station sends the song it's playing as stream text - usually
"Artist - Title", sometimes just a title, an ad or the station's slogan.
`HeardLog` keeps what each station sent since it was tuned in (in memory -
it's "since you tuned in", not a history). `find_track` looks a heard song
up in the library with the same scorer chart matching uses, and
`add_to_heard_playlist` puts it on the "Heard on the Radio" playlist.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Playlist, PlaylistItem
from .matching import best_match, normalize

PLAYLIST_NAME = "Heard on the Radio"
#: per station, the newest this many songs are kept
KEEP = 200


@dataclass
class Heard:
    when: dt.datetime
    text: str
    artist: str = ""
    title: str = ""


def parse_stream_title(text: str) -> tuple[str, str]:
    """(artist, title) from a station's stream text: "Bob Seger - Night
    Moves" -> ("Bob Seger", "Night Moves"). Without " - " it's all title.
    Some stations send "Title by Artist" - also understood."""
    text = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip(" -")
    if " - " in text:
        artist, _, title = text.partition(" - ")
        return artist.strip(), title.strip()
    m = re.match(r"^(.+?)\s+by\s+(.+)$", text, re.IGNORECASE)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", text


@dataclass
class HeardLog:
    """What each station sent, newest first."""

    by_station: dict[int, list[Heard]] = field(default_factory=dict)

    def add(self, station_id: int, text: str, when: Optional[dt.datetime] = None) -> bool:
        """Note `text` for `station_id`. False (nothing added) when it's
        empty or the same as the last thing that station sent - stations
        resend the title every few seconds."""
        text = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
        if not text:
            return False
        items = self.by_station.setdefault(station_id, [])
        if items and items[0].text == text:
            return False
        artist, title = parse_stream_title(text)
        items.insert(0, Heard(when or dt.datetime.now(), text, artist, title))
        del items[KEEP:]
        return True

    def for_station(self, station_id: Optional[int]) -> list[Heard]:
        return list(self.by_station.get(station_id, [])) if station_id is not None else []


def find_track(session: Session, heard: Heard) -> Optional[int]:
    """The library track a heard song is, or None. Needs an artist - a bare
    title ("Traffic and weather") is never matched."""
    if not heard.artist or not heard.title:
        return None
    from .charts import _candidate_tracks
    from .duplicates import album_copy_for

    hit = best_match(heard.title, heard.artist, _candidate_tracks(session, normalize(heard.title)))
    if hit is None:
        return None
    return album_copy_for(session, hit[0]) or hit[0]


def add_to_heard_playlist(session: Session, track_id: int) -> bool:
    """Add a track to "Heard on the Radio" (made the first time). False
    when it's already there."""
    from . import playlists as pl

    playlist = session.scalar(select(Playlist).where(
        Playlist.name == PLAYLIST_NAME, Playlist.kind == Playlist.KIND_MANUAL).limit(1))
    if playlist is None:
        playlist = pl.create_playlist(session, PLAYLIST_NAME,
                                      description="Songs you heard on a radio station")
    if session.scalar(select(PlaylistItem.id).where(
            PlaylistItem.playlist_id == playlist.id, PlaylistItem.track_id == track_id).limit(1)):
        return False
    pl.add_tracks(session, playlist.id, [track_id])
    return True
