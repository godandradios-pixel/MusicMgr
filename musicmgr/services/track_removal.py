"""Remove songs that have no file left - "Remove from library" on a right-click.

2026-10-30 - James couldn't get rid of the artist "2000-09". An untagged
file in D:\\Music\\Hot Country\\2000-09\\2008 came in with its folder names
as artist and album. The file was renamed later and came back as a new,
correct song, but the old one had two plays, so the missing-file cleanup
(scanner.purge_orphaned_tracks) keeps it on purpose and nothing in the app
could remove it.

This is the by-hand version of that cleanup, for songs James picks: a song
goes even with plays, playlist places, chart matches or a jukebox side.

- Only a song with no present file can be removed. A song with a file is
  skipped (a rescan would only bring it back).
- When the song plainly lives on as another, playable song (scanner's
  `_live_twin`: same title and performer, same album or album artist), its
  history moves there first (scanner.carry_history) - same as a scan does.
- Otherwise its plays and playlist places go with it, chart entries it
  matched become unmatched (a Re-match can link a real copy), and its
  jukebox sides are emptied - a card left with neither side is removed.
- Playlists that lose a song get updated_at bumped so USB sync carries it.
- Albums left empty go (scanner.purge_empty_releases), then any artist the
  song or its album credited that nothing else uses.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..db.models import Credit, JukeboxSlot, MediaFile, Playlist, PlaylistItem, Release, Track
from . import scanner


@dataclass
class RemovalResult:
    removed: int = 0       # songs removed
    moved: int = 0         # of those, ones whose history went to a live copy
    skipped: int = 0       # songs that still have a file (left alone)
    albums: int = 0        # empty albums removed
    artists: int = 0       # unused artists removed

    def summary(self) -> str:
        if not self.removed:
            return ("Nothing removed - those songs still have a file"
                    if self.skipped else "Nothing removed")
        parts = [f"Removed {self.removed} song{'s' if self.removed != 1 else ''}"]
        if self.moved:
            parts.append(f"{self.moved} handed its plays to the copy you have"
                         if self.moved == 1 else
                         f"{self.moved} handed their plays to the copies you have")
        if self.albums:
            parts.append(f"{self.albums} empty album{'s' if self.albums != 1 else ''}")
        if self.artists:
            parts.append(f"{self.artists} artist{'s' if self.artists != 1 else ''}")
        if self.skipped:
            parts.append(f"{self.skipped} still with a file left alone")
        return " · ".join(parts)


def has_present_file(session: Session, track_id: int) -> bool:
    return session.scalar(
        select(MediaFile.id).where(MediaFile.track_id == track_id,
                                   MediaFile.is_missing.is_(False)).limit(1)
    ) is not None


def is_removable(session: Session, track_id: int) -> bool:
    """True for a song that exists and has no present file."""
    return session.get(Track, track_id) is not None and not has_present_file(session, track_id)


def fileless_track_ids(session: Session) -> list[int]:
    """Every song with no present file - after a purge, the ones it kept
    for their history (Settings › Purge offers to remove those too)."""
    playable = select(MediaFile.track_id).where(MediaFile.is_missing.is_(False))
    return list(session.scalars(select(Track.id).where(Track.id.not_in(playable))
                                .order_by(Track.id)))


def remove_fileless_tracks(session: Session, track_ids: Iterable[int]) -> RemovalResult:
    """Remove each of `track_ids` that has no present file - see the
    module docstring. Flushes; the caller's session commits."""
    from .video_scanner import _remove_unused_artists

    result = RemovalResult()
    playable = select(MediaFile.track_id).where(MediaFile.is_missing.is_(False))
    release_ids: set[int] = set()
    artist_ids: set[int] = set()
    touched_playlists: set[int] = set()
    now = dt.datetime.now(dt.timezone.utc)

    for track_id in dict.fromkeys(track_ids):
        track = session.get(Track, track_id)
        if track is None:
            continue
        if has_present_file(session, track_id):
            result.skipped += 1
            continue
        release_ids.add(track.release_id)
        artist_ids.update(session.scalars(select(Credit.artist_id).where(
            or_(Credit.track_id == track_id, Credit.release_id == track.release_id))))
        album_artist = session.scalar(select(Release.album_artist_id)
                                      .where(Release.id == track.release_id))
        if album_artist:
            artist_ids.add(album_artist)

        twin = scanner._live_twin(session, track, playable)
        if twin is not None and scanner.carry_history(session, {track_id: twin.id}):
            result.moved += 1
            result.removed += 1
            continue

        touched_playlists.update(session.scalars(
            select(PlaylistItem.playlist_id).where(PlaylistItem.track_id == track_id)))
        for slot in session.scalars(select(JukeboxSlot).where(or_(
                JukeboxSlot.side_a_track_id == track_id,
                JukeboxSlot.side_b_track_id == track_id))).all():
            if slot.side_a_track_id == track_id:
                slot.side_a_track_id = None
            if slot.side_b_track_id == track_id:
                slot.side_b_track_id = None
            if slot.side_a_track_id is None and slot.side_b_track_id is None:
                session.delete(slot)
        session.flush()
        session.delete(track)   # files, credits, plays and places cascade
        session.flush()
        result.removed += 1

    for playlist in session.scalars(select(Playlist).where(Playlist.id.in_(touched_playlists))):
        playlist.updated_at = now
    session.flush()
    if result.removed:
        result.albums = scanner.purge_empty_releases(session, release_ids)
        result.artists = _remove_unused_artists(session, artist_ids)
    session.expire_all()
    return result
