"""Playback engine: a queue on top of QtMultimedia, plus play-history writing.

The queue holds plain dataclasses rather than ORM objects so nothing detached
is passed between the UI and the database session.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from sqlalchemy import select

from ..db.models import PlayEvent, Track
from ..db.session import session_scope

log = logging.getLogger(__name__)

REPEAT_OFF, REPEAT_ALL, REPEAT_ONE = "off", "all", "one"

#: a play only counts toward charts once this much has been heard
SCROBBLE_MIN_MS = 30_000
SCROBBLE_MIN_FRACTION = 0.5


@dataclass
class QueueItem:
    track_id: int
    title: str
    artist: str
    album: str
    path: str
    duration_ms: int = 0
    cover_path: Optional[str] = None
    position: Optional[str] = None

    @classmethod
    def from_track(cls, track: Track) -> Optional["QueueItem"]:
        mf = track.primary_file
        if mf is None:
            return None
        release = track.release
        return cls(
            track_id=track.id,
            title=track.title,
            artist=track.artist_display or (release.artist_display if release else ""),
            album=release.title if release else "",
            path=mf.path,
            duration_ms=track.duration_ms or mf.duration_ms or 0,
            cover_path=release.cover_path if release else None,
            position=track.position,
        )


def queue_items_from_tracks(tracks: Iterable[Track]) -> list[QueueItem]:
    items = []
    for t in tracks:
        item = QueueItem.from_track(t)
        if item is not None and os.path.exists(item.path):
            items.append(item)
    return items


class PlayerController(QObject):
    """Owns the queue and the QMediaPlayer. One instance per application."""

    trackChanged = Signal(object)      # QueueItem | None
    queueChanged = Signal()
    positionChanged = Signal(int)      # ms
    durationChanged = Signal(int)      # ms
    playbackStateChanged = Signal(str)  # playing | paused | stopped
    errorOccurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._queue: list[QueueItem] = []
        self._order: list[int] = []      # indices into _queue, shuffled or not
        self._cursor: int = -1           # position within _order
        self._shuffle = False
        self._repeat = REPEAT_OFF
        self._source = "library"
        self._played_ms = 0
        self._started_at: Optional[dt.datetime] = None
        self._current_scrobbled = False

        self._audio = QAudioOutput()
        self._audio.setVolume(0.8)
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self.durationChanged.emit)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)

    # -- queue ------------------------------------------------------------

    @property
    def queue(self) -> list[QueueItem]:
        return list(self._queue)

    @property
    def current(self) -> Optional[QueueItem]:
        if 0 <= self._cursor < len(self._order):
            idx = self._order[self._cursor]
            if 0 <= idx < len(self._queue):
                return self._queue[idx]
        return None

    @property
    def current_index(self) -> int:
        return self._order[self._cursor] if 0 <= self._cursor < len(self._order) else -1

    def _rebuild_order(self, keep_current: bool = True) -> None:
        current_idx = self.current_index
        self._order = list(range(len(self._queue)))
        if self._shuffle:
            random.shuffle(self._order)
            if keep_current and current_idx >= 0:
                self._order.remove(current_idx)
                self._order.insert(0, current_idx)
                self._cursor = 0
                return
        if keep_current and current_idx >= 0 and current_idx in self._order:
            self._cursor = self._order.index(current_idx)
        else:
            self._cursor = -1 if not self._order else min(max(self._cursor, 0), len(self._order) - 1)

    def set_queue(
        self, items: Sequence[QueueItem], start: int = 0, source: str = "library"
    ) -> None:
        self._flush_play_event()
        self._queue = list(items)
        self._source = source
        self._order = list(range(len(self._queue)))
        if self._shuffle:
            random.shuffle(self._order)
            if 0 <= start < len(self._queue):
                self._order.remove(start)
                self._order.insert(0, start)
                start = 0
        else:
            start = start if 0 <= start < len(self._queue) else 0
        self._cursor = start - 1 if self._queue else -1
        self.queueChanged.emit()
        if self._queue:
            self.next()

    def enqueue(self, items: Sequence[QueueItem], play_next: bool = False) -> None:
        if not items:
            return
        if play_next and self.current is not None:
            insert_at = self.current_index + 1
            self._queue[insert_at:insert_at] = list(items)
        else:
            self._queue.extend(items)
        self._rebuild_order()
        self.queueChanged.emit()
        if self.current is None:
            self.next()

    def clear_queue(self) -> None:
        self.stop()
        self._queue.clear()
        self._order.clear()
        self._cursor = -1
        self.queueChanged.emit()
        self.trackChanged.emit(None)

    def remove_at(self, index: int) -> None:
        if not (0 <= index < len(self._queue)):
            return
        playing = self.current_index
        del self._queue[index]
        if index == playing:
            self._rebuild_order(keep_current=False)
            self.next()
        else:
            self._rebuild_order()
        self.queueChanged.emit()

    # -- transport --------------------------------------------------------

    def play_tracks(
        self, items: Sequence[QueueItem], start: int = 0, source: str = "library"
    ) -> None:
        self.set_queue(items, start=start, source=source)

    def _load_current(self) -> None:
        item = self.current
        if item is None:
            self._player.stop()
            self.trackChanged.emit(None)
            return
        if not os.path.exists(item.path):
            self.errorOccurred.emit(f"File missing: {item.path}")
            self.next()
            return
        self._played_ms = 0
        self._current_scrobbled = False
        self._started_at = dt.datetime.now(dt.timezone.utc)
        self._player.setSource(QUrl.fromLocalFile(item.path))
        self._player.play()
        self.trackChanged.emit(item)

    def play(self) -> None:
        if self.current is None and self._queue:
            self.next()
        else:
            self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._flush_play_event()
        self._player.stop()

    def next(self, user_initiated: bool = False) -> None:
        self._flush_play_event(skipped=user_initiated)
        if not self._order:
            self.trackChanged.emit(None)
            return
        if self._repeat == REPEAT_ONE and self._cursor >= 0 and not user_initiated:
            self._load_current()
            return
        if self._cursor + 1 < len(self._order):
            self._cursor += 1
        elif self._repeat == REPEAT_ALL:
            self._cursor = 0
        else:
            self._cursor = -1
            self._player.stop()
            self.trackChanged.emit(None)
            return
        self._load_current()

    def previous(self) -> None:
        # restart the track if more than 4 seconds in, like every other player
        if self._player.position() > 4000:
            self._player.setPosition(0)
            return
        self._flush_play_event(skipped=True)
        if self._cursor > 0:
            self._cursor -= 1
        elif self._repeat == REPEAT_ALL and self._order:
            self._cursor = len(self._order) - 1
        self._load_current()

    def jump_to(self, queue_index: int) -> None:
        if not (0 <= queue_index < len(self._queue)):
            return
        self._flush_play_event(skipped=True)
        if queue_index in self._order:
            self._cursor = self._order.index(queue_index)
            self._load_current()

    def seek(self, ms: int) -> None:
        self._player.setPosition(int(ms))

    def seek_relative(self, delta_ms: int) -> None:
        self._player.setPosition(max(0, self._player.position() + delta_ms))

    # -- modes ------------------------------------------------------------

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    def set_shuffle(self, enabled: bool) -> None:
        self._shuffle = bool(enabled)
        self._rebuild_order()
        self.queueChanged.emit()

    @property
    def repeat(self) -> str:
        return self._repeat

    def cycle_repeat(self) -> str:
        self._repeat = {
            REPEAT_OFF: REPEAT_ALL, REPEAT_ALL: REPEAT_ONE, REPEAT_ONE: REPEAT_OFF
        }[self._repeat]
        return self._repeat

    @property
    def volume(self) -> float:
        return self._audio.volume()

    def set_volume(self, value: float) -> None:
        self._audio.setVolume(max(0.0, min(1.0, float(value))))

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def position(self) -> int:
        return self._player.position()

    def duration(self) -> int:
        return self._player.duration() or (self.current.duration_ms if self.current else 0)

    # -- Qt callbacks -----------------------------------------------------

    @Slot(int)
    def _on_position(self, ms: int) -> None:
        self._played_ms = max(self._played_ms, ms)
        self.positionChanged.emit(ms)

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._flush_play_event(completed=True)
            if self._repeat == REPEAT_ONE:
                self._load_current()
            else:
                self.next()

    @Slot(QMediaPlayer.PlaybackState)
    def _on_state(self, state) -> None:
        mapping = {
            QMediaPlayer.PlaybackState.PlayingState: "playing",
            QMediaPlayer.PlaybackState.PausedState: "paused",
            QMediaPlayer.PlaybackState.StoppedState: "stopped",
        }
        self.playbackStateChanged.emit(mapping.get(state, "stopped"))

    @Slot(QMediaPlayer.Error, str)
    def _on_error(self, error, message: str) -> None:  # pragma: no cover
        if error != QMediaPlayer.Error.NoError:
            log.warning("player error: %s", message)
            self.errorOccurred.emit(message or "Playback error")

    # -- history ----------------------------------------------------------

    def _flush_play_event(self, completed: bool = False, skipped: bool = False) -> None:
        """Write a PlayEvent for the track that just finished/was left."""
        item = self.current
        if item is None or self._current_scrobbled or self._started_at is None:
            return
        played = int(self._played_ms)
        duration = item.duration_ms or self._player.duration() or 0
        enough = played >= SCROBBLE_MIN_MS or (
            duration and played >= duration * SCROBBLE_MIN_FRACTION
        )
        if played <= 0:
            return
        self._current_scrobbled = True
        try:
            with session_scope() as session:
                session.add(
                    PlayEvent(
                        track_id=item.track_id,
                        started_at=self._started_at,
                        ms_played=played,
                        completed=bool(completed),
                        skipped=bool(skipped and not enough),
                        source=self._source,
                    )
                )
                track = session.get(Track, item.track_id)
                if track is not None:
                    if enough:
                        track.play_count = (track.play_count or 0) + 1
                        track.last_played_at = dt.datetime.now(dt.timezone.utc)
                    elif skipped:
                        track.skip_count = (track.skip_count or 0) + 1
        except Exception as exc:  # pragma: no cover
            log.warning("could not record play event: %s", exc)
