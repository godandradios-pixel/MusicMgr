"""Playback engine: a queue on top of QtMultimedia, plus play-history writing.

The queue holds plain dataclasses rather than ORM objects so nothing detached
is passed between the UI and the database session.

**2026-10-01 - gapless hand-off, crossfade, and volume levelling.** James
picked these as the first two items off the "what could MusicMgr add"
list. Playback now runs on two "decks" (each a QMediaPlayer with its own
QAudioOutput) instead of one player:

- While a track plays, the next one in play order is already loaded on the
  idle deck (`_refresh_preload`).
- **No crossfade (the default):** when the playing deck reaches the end, the
  idle deck - already opened and probed - starts straight away, rather than
  the old open-the-next-file-then-play. Not sample-perfect gapless (that
  would need one continuous audio stream, which QMediaPlayer can't do), but
  the gap shrinks to the audio device starting up.
- **Crossfade (1-12 s, Settings > Playback):** the idle deck starts that many
  seconds before the end and the two are mixed with equal-power volume
  curves. Consecutive tracks from the same album hand off gaplessly instead
  unless "Crossfade within albums" is on, so live albums and segued records
  aren't smeared. Repeat-one never crossfades into itself.
- **Volume levelling:** each deck's volume is the volume slider times the
  track's ReplayGain factor (`services/replaygain.py`). "Auto" uses album
  gain while an album plays in order, track gain otherwise. A track with no
  level yet plays at `FALLBACK_GAIN_DB` while it's measured in the
  background, then eases to its real level. QAudioOutput can't go above
  full volume, so levelling only ever turns tracks down - it can't clip.

The play-history rules are unchanged: a crossfade or gapless hand-off
counts the outgoing track as completed, exactly like reaching its end did.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import random
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from PySide6.QtCore import QElapsedTimer, QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from sqlalchemy import select

from ..db.models import MediaFile, PlayEvent, Track
from ..db.session import session_scope
from . import playback_prefs, replaygain

log = logging.getLogger(__name__)

REPEAT_OFF, REPEAT_ALL, REPEAT_ONE = "off", "all", "one"

#: a play only counts toward charts once this much has been heard
SCROBBLE_MIN_MS = 30_000
SCROBBLE_MIN_FRACTION = 0.5

#: gain assumed for a track whose level hasn't been read or measured yet -
#: roughly where a typical modern master lands, so an unmeasured loud track
#: doesn't jump out while it's being measured
FALLBACK_GAIN_DB = -6.0
#: volume ramp tick (crossfades and level changes)
TICK_MS = 30
#: how long a track takes to ease to a level that arrives mid-track
GAIN_RAMP_MS = 1200


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
    # 2026-10-01 levelling/crossfade data, filled in by `from_track` or
    # lazily by `PlayerController._hydrate` (looked up by path) for items
    # built elsewhere (track tables, track panel)
    media_file_id: Optional[int] = None
    release_id: Optional[int] = None
    rg_track_gain: Optional[float] = None
    rg_album_gain: Optional[float] = None
    hydrated: bool = False

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
            media_file_id=mf.id,
            release_id=track.release_id,
            rg_track_gain=mf.rg_track_gain,
            rg_album_gain=mf.rg_album_gain,
            hydrated=True,
        )


def queue_items_from_tracks(tracks: Iterable[Track]) -> list[QueueItem]:
    items = []
    for t in tracks:
        item = QueueItem.from_track(t)
        if item is not None and os.path.exists(item.path):
            items.append(item)
    return items


class LoudnessThread(QThread):
    """Reads or measures one file's level off the GUI thread for a track the
    player is about to play (or is playing) - see `_ensure_gain`. Same
    decode-on-its-own-thread shape as services/spectrum.py:SpectrumThread."""

    done = Signal(int)  # media_file_id

    def __init__(self, media_file_id: int, path: str, parent=None) -> None:
        super().__init__(parent)
        self.media_file_id = media_file_id
        self.path = path

    def run(self) -> None:  # pragma: no cover - exercised via the app
        try:
            release_id = replaygain.analyze_paths_one(self.media_file_id, self.path)
            if release_id is not None:
                with session_scope() as session:
                    replaygain.fill_album_gains(session, [release_id])
        except replaygain.MeasureInterrupted:
            return
        except Exception as exc:
            log.warning("loudness measurement failed for %s: %s", self.path, exc)
        self.done.emit(self.media_file_id)


#: running LoudnessThreads, kept referenced until they finish so a
#: PlayerController going away mid-measurement can't destroy a running thread
_RUNNING_THREADS: set = set()


def stop_background_measuring(wait_ms: int = 3000) -> None:
    """Called at shutdown (MainWindow.closeEvent): asks any in-flight
    measurement to stop and waits for it, so Qt never destroys a running
    thread. An interrupted file stores nothing and is measured next time."""
    for thread in list(_RUNNING_THREADS):
        thread.requestInterruption()
    for thread in list(_RUNNING_THREADS):
        thread.wait(wait_ms)


class _Deck:
    """One QMediaPlayer + QAudioOutput, and what it's loaded with."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.item: Optional[QueueItem] = None
        #: the play-order cursor `item` was loaded for
        self.cursor: int = -1
        #: crossfade multiplier, 0..1
        self.fade = 1.0
        #: levelling multiplier, and where it's easing to
        self.gain = 1.0
        self.gain_target = 1.0

    def load(self, item: QueueItem, cursor: int) -> None:
        self.item = item
        self.cursor = cursor
        self.player.setSource(QUrl.fromLocalFile(item.path))

    def unload(self) -> None:
        self.player.stop()
        if self.item is not None:
            # releases the file handle (USB sync may want to move it)
            self.player.setSource(QUrl())
        self.item = None
        self.cursor = -1
        self.fade = 1.0

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState


class PlayerController(QObject):
    """Owns the queue and the two playback decks. One instance per application."""

    trackChanged = Signal(object)      # QueueItem | None
    queueChanged = Signal()
    positionChanged = Signal(int)      # ms
    durationChanged = Signal(int)      # ms
    playbackStateChanged = Signal(str)  # playing | paused | stopped
    errorOccurred = Signal(str)

    #: background measuring of unlevelled tracks; tests switch it off
    auto_measure = True

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

        self._volume = 0.8
        self._prefs = playback_prefs.PlaybackPrefs()
        self._decks = (_Deck("A"), _Deck("B"))
        for deck in self._decks:
            p = deck.player
            p.positionChanged.connect(lambda ms, d=deck: self._on_position(d, ms))
            p.durationChanged.connect(lambda ms, d=deck: self._on_duration(d, ms))
            p.mediaStatusChanged.connect(lambda st, d=deck: self._on_media_status(d, st))
            p.playbackStateChanged.connect(lambda st, d=deck: self._on_state(d, st))
            p.errorOccurred.connect(lambda e, msg, d=deck: self._on_error(d, e, msg))
        self._active = self._decks[0]
        #: the outgoing deck while a crossfade runs
        self._fade_from: Optional[_Deck] = None
        self._fade_ms = 0
        self._fade_clock = QElapsedTimer()
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        #: media_file_ids being measured, and ones already tried this session
        self._measuring: set[int] = set()
        self._measure_tried: set[int] = set()
        self._apply_volume()
        self.reload_prefs()

    # -- preferences ------------------------------------------------------

    def reload_prefs(self) -> None:
        """Re-read Settings > Playback. Safe to call any time; a changed
        levelling mode applies to the playing track straight away."""
        try:
            with session_scope() as session:
                self._prefs = playback_prefs.load(session)
        except Exception as exc:  # pragma: no cover - no database yet
            log.debug("playback prefs not loaded: %s", exc)
        self._regain_all(ramp=True)
        self._refresh_preload()

    @property
    def prefs(self) -> playback_prefs.PlaybackPrefs:
        return self._prefs

    # -- decks ------------------------------------------------------------

    @property
    def _standby(self) -> _Deck:
        return self._decks[1] if self._active is self._decks[0] else self._decks[0]

    def _item_at(self, cursor: Optional[int]) -> Optional[QueueItem]:
        if cursor is None or not (0 <= cursor < len(self._order)):
            return None
        idx = self._order[cursor]
        return self._queue[idx] if 0 <= idx < len(self._queue) else None

    def _peek_next_cursor(self) -> Optional[int]:
        """Where `next()` would go when the current track ends naturally."""
        if not self._order or self._cursor < 0:
            return None
        if self._repeat == REPEAT_ONE:
            return self._cursor
        if self._cursor + 1 < len(self._order):
            return self._cursor + 1
        if self._repeat == REPEAT_ALL:
            return 0
        return None

    def _apply_volume(self, deck: Optional[_Deck] = None) -> None:
        for d in (deck,) if deck is not None else self._decks:
            d.audio.setVolume(max(0.0, min(1.0, self._volume * d.gain * d.fade)))

    def _refresh_preload(self) -> None:
        """Make sure the idle deck holds whatever plays next. Skipped while a
        crossfade is running - the outgoing deck is still in use then, and
        `_finish_fade` calls this once it's free."""
        if self._fade_from is not None:
            return
        standby = self._standby
        if self.current is None:
            if standby.item is not None:
                standby.unload()
            return
        cursor = self._peek_next_cursor()
        item = self._item_at(cursor)
        if item is None or not os.path.exists(item.path):
            if standby.item is not None:
                standby.unload()
            return
        if standby.item is item:
            standby.cursor = cursor
            return
        standby.unload()
        self._hydrate(item)
        standby.load(item, cursor)
        standby.fade = 1.0
        standby.gain = standby.gain_target = self._gain_factor(item, cursor)
        self._apply_volume(standby)
        self._ensure_gain(item)

    def _standby_ready(self) -> bool:
        standby = self._standby
        cursor = self._peek_next_cursor()
        if standby.item is None or cursor is None or standby.cursor != cursor:
            return False
        if standby.item is not self._item_at(cursor):
            return False
        return standby.player.mediaStatus() not in (
            QMediaPlayer.MediaStatus.InvalidMedia,
            QMediaPlayer.MediaStatus.NoMedia,
        )

    def _crossfade_ms_for_next(self) -> int:
        seconds = self._prefs.crossfade_s
        if seconds <= 0 or self._repeat == REPEAT_ONE:
            return 0
        current = self.current
        upcoming = self._item_at(self._peek_next_cursor())
        if current is None or upcoming is None:
            return 0
        if (
            not self._prefs.crossfade_same_album
            and current.release_id is not None
            and current.release_id == upcoming.release_id
        ):
            return 0
        fade = seconds * 1000
        current_ms = self._active.player.duration() or current.duration_ms
        for length in (current_ms, upcoming.duration_ms):
            if length:
                fade = min(fade, length // 2)
        return max(0, int(fade))

    def _advance_to_standby(self, fade_ms: int) -> None:
        """Hand playback to the preloaded deck - instantly (gapless) or
        mixed over `fade_ms`. The outgoing track counts as completed."""
        self._flush_play_event(completed=True)
        old, new = self._active, self._standby
        self._cursor = new.cursor
        self._active = new
        self._reset_play_tracking()
        if fade_ms > 0:
            new.fade = 0.0
            self._apply_volume(new)
            new.player.play()
            self._fade_from = old
            self._fade_ms = fade_ms
            self._fade_clock.start()
            self._tick.start()
        else:
            new.fade = 1.0
            self._apply_volume(new)
            new.player.play()
            old.unload()
        self.trackChanged.emit(new.item)
        self.durationChanged.emit(new.player.duration() or new.item.duration_ms)
        if fade_ms <= 0:
            self._refresh_preload()

    def _finish_fade(self) -> None:
        old = self._fade_from
        if old is None:
            return
        self._fade_from = None
        old.unload()
        self._active.fade = 1.0
        self._apply_volume(self._active)
        self._stop_tick_if_idle()
        self._refresh_preload()

    def _stop_tick_if_idle(self) -> None:
        if self._fade_from is None and all(
            abs(d.gain - d.gain_target) < 1e-4 for d in self._decks
        ):
            self._tick.stop()

    def _on_tick(self) -> None:
        if self._fade_from is not None:
            t = min(1.0, self._fade_clock.elapsed() / max(1, self._fade_ms))
            # equal-power curves keep the combined loudness steady mid-fade
            self._active.fade = math.sin(t * math.pi / 2)
            self._fade_from.fade = math.cos(t * math.pi / 2)
            self._apply_volume()
            if t >= 1.0:
                self._finish_fade()
        step = TICK_MS / GAIN_RAMP_MS
        for d in self._decks:
            if abs(d.gain - d.gain_target) >= 1e-4:
                # ease in dB so a level change sounds even
                cur = 20 * math.log10(max(d.gain, 1e-6))
                tgt = 20 * math.log10(max(d.gain_target, 1e-6))
                delta = tgt - cur
                cur += delta if abs(delta) < 0.05 else delta * min(1.0, step * 4)
                d.gain = d.gain_target if abs(tgt - cur) < 0.05 else 10 ** (cur / 20)
                self._apply_volume(d)
        self._stop_tick_if_idle()

    def _abort_fade(self) -> None:
        """A transport action mid-crossfade drops the outgoing track at once."""
        if self._fade_from is not None:
            self._finish_fade()

    # -- levelling --------------------------------------------------------

    def _hydrate(self, item: QueueItem) -> None:
        """Fill an item's file id, release id and gains from the database
        (looked up by path) if whoever built it didn't."""
        if item.hydrated:
            return
        item.hydrated = True
        if not item.path:
            return
        try:
            with session_scope() as session:
                row = session.execute(
                    select(MediaFile, Track.release_id)
                    .join(Track, Track.id == MediaFile.track_id)
                    .where(MediaFile.path == item.path)
                ).first()
                if row is None:
                    return
                mf, release_id = row
                item.media_file_id = mf.id
                item.release_id = release_id
                item.rg_track_gain = mf.rg_track_gain
                item.rg_album_gain = mf.rg_album_gain
        except Exception as exc:  # pragma: no cover - db unavailable
            log.debug("could not look up %s: %s", item.path, exc)

    def _album_context(self, cursor: int) -> bool:
        """True when this track is part of an album playing in order - its
        neighbour in play order is from the same release."""
        if self._shuffle:
            return False
        item = self._item_at(cursor)
        if item is None or item.release_id is None:
            return False
        for neighbour in (self._item_at(cursor - 1), self._item_at(cursor + 1)):
            if neighbour is not None:
                self._hydrate(neighbour)
                if neighbour.release_id == item.release_id:
                    return True
        return False

    def _gain_factor(self, item: QueueItem, cursor: int) -> float:
        mode = self._prefs.levelling
        if mode == playback_prefs.LEVELLING_OFF:
            return 1.0
        self._hydrate(item)
        use_album = mode == playback_prefs.LEVELLING_ALBUM or (
            mode == playback_prefs.LEVELLING_AUTO and self._album_context(cursor)
        )
        db = replaygain.gain_db_for(
            item.rg_track_gain,
            item.rg_album_gain,
            use_album,
            self._prefs.preamp_db,
            FALLBACK_GAIN_DB,
        )
        return replaygain.db_to_factor(db)

    def gain_db_for_current(self) -> Optional[float]:
        """The levelling adjustment on the playing track, in dB (None = off)."""
        if self._prefs.levelling == playback_prefs.LEVELLING_OFF or self.current is None:
            return None
        return 20 * math.log10(max(self._active.gain_target, 1e-6))

    def _regain_all(self, ramp: bool) -> None:
        for d in self._decks:
            if d.item is None:
                continue
            target = self._gain_factor(d.item, d.cursor)
            d.gain_target = target
            # the audible deck(s) ease; an idle preloaded one just jumps
            if not ramp or (d is not self._active and d is not self._fade_from):
                d.gain = target
        self._apply_volume()
        if any(abs(d.gain - d.gain_target) >= 1e-4 for d in self._decks):
            self._tick.start()

    def _ensure_gain(self, item: QueueItem) -> None:
        if (
            not self.auto_measure
            or self._prefs.levelling == playback_prefs.LEVELLING_OFF
            or item.rg_track_gain is not None
            or item.media_file_id is None
        ):
            return
        mf_id = item.media_file_id
        if mf_id in self._measuring or mf_id in self._measure_tried:
            return
        self._measuring.add(mf_id)
        self._measure_tried.add(mf_id)
        thread = LoudnessThread(mf_id, item.path)
        _RUNNING_THREADS.add(thread)
        thread.done.connect(self._on_gain_measured)
        thread.finished.connect(lambda t=thread: _RUNNING_THREADS.discard(t))
        thread.start()

    @Slot(int)
    def _on_gain_measured(self, media_file_id: int) -> None:
        self._measuring.discard(media_file_id)
        try:
            with session_scope() as session:
                mf = session.get(MediaFile, media_file_id)
                if mf is None:
                    return
                track = session.get(Track, mf.track_id)
                release_id = track.release_id if track else None
                gains = {media_file_id: (mf.rg_track_gain, mf.rg_album_gain)}
                if release_id is not None:
                    rows = session.execute(
                        select(MediaFile.id, MediaFile.rg_track_gain, MediaFile.rg_album_gain)
                        .join(Track, Track.id == MediaFile.track_id)
                        .where(Track.release_id == release_id)
                    )
                    for fid, tg, ag in rows:
                        gains[fid] = (tg, ag)
        except Exception as exc:  # pragma: no cover
            log.debug("could not read measured gain: %s", exc)
            return
        for item in self._queue:
            if item.media_file_id in gains:
                item.rg_track_gain, item.rg_album_gain = gains[item.media_file_id]
        self._regain_all(ramp=True)

    # -- queue ------------------------------------------------------------

    @property
    def queue(self) -> list[QueueItem]:
        return list(self._queue)

    @property
    def current(self) -> Optional[QueueItem]:
        return self._item_at(self._cursor)

    @property
    def source(self) -> str:
        """What the current queue was started from ("library", "artist:12",
        "radio", ...) - the radio uses it to notice something else took over."""
        return self._source

    def remaining_count(self) -> int:
        """Tracks still to come after the current one, in play order."""
        if not self._order:
            return 0
        return max(0, len(self._order) - max(self._cursor, 0) - 1)

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
                self._active.cursor = 0
                return
        if keep_current and current_idx >= 0 and current_idx in self._order:
            self._cursor = self._order.index(current_idx)
        else:
            self._cursor = -1 if not self._order else min(max(self._cursor, 0), len(self._order) - 1)
        self._active.cursor = self._cursor

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
        else:
            self._refresh_preload()

    def clear_queue(self) -> None:
        self.stop()
        self._queue.clear()
        self._order.clear()
        self._cursor = -1
        for d in self._decks:
            d.unload()
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
            self._refresh_preload()
        self.queueChanged.emit()

    # -- transport --------------------------------------------------------

    def play_tracks(
        self, items: Sequence[QueueItem], start: int = 0, source: str = "library"
    ) -> None:
        self.set_queue(items, start=start, source=source)

    def _reset_play_tracking(self) -> None:
        self._played_ms = 0
        self._current_scrobbled = False
        self._started_at = dt.datetime.now(dt.timezone.utc)

    def _load_current(self) -> None:
        self._abort_fade()
        item = self.current
        if item is None:
            for d in self._decks:
                d.unload()
            self.trackChanged.emit(None)
            return
        if not os.path.exists(item.path):
            self.errorOccurred.emit(f"File missing: {item.path}")
            self.next()
            return
        standby = self._standby
        reused = standby.item is item and standby.cursor == self._cursor
        if reused:
            # the user jumped to exactly what was preloaded (usually Next)
            self._active.unload()
            self._active = standby
            deck = standby
        else:
            deck = self._active
            self._hydrate(item)
            deck.load(item, self._cursor)
        deck.fade = 1.0
        deck.gain = deck.gain_target = self._gain_factor(item, self._cursor)
        self._apply_volume(deck)
        self._reset_play_tracking()
        deck.player.play()
        self.trackChanged.emit(item)
        if reused:
            self.durationChanged.emit(deck.player.duration() or item.duration_ms)
        self._ensure_gain(item)
        self._refresh_preload()

    def play(self) -> None:
        if self.current is None and self._queue:
            self.next()
        else:
            self._active.player.play()

    def pause(self) -> None:
        self._abort_fade()
        self._active.player.pause()

    def toggle(self) -> None:
        if self._active.is_playing():
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._flush_play_event()
        self._abort_fade()
        self._active.player.stop()

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
            self._abort_fade()
            self._active.player.stop()
            self._refresh_preload()
            self.trackChanged.emit(None)
            return
        self._load_current()

    def previous(self) -> None:
        self._abort_fade()
        # restart the track if more than 4 seconds in, like every other player
        if self._active.player.position() > 4000:
            self._active.player.setPosition(0)
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
        self._abort_fade()
        self._active.player.setPosition(int(ms))

    def seek_relative(self, delta_ms: int) -> None:
        self._abort_fade()
        self._active.player.setPosition(max(0, self._active.player.position() + delta_ms))

    # -- modes ------------------------------------------------------------

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    def set_shuffle(self, enabled: bool) -> None:
        self._shuffle = bool(enabled)
        self._rebuild_order()
        self.queueChanged.emit()
        self._refresh_preload()

    @property
    def repeat(self) -> str:
        return self._repeat

    def cycle_repeat(self) -> str:
        self._repeat = {
            REPEAT_OFF: REPEAT_ALL, REPEAT_ALL: REPEAT_ONE, REPEAT_ONE: REPEAT_OFF
        }[self._repeat]
        self._refresh_preload()
        return self._repeat

    @property
    def volume(self) -> float:
        """The volume slider's value - levelling and fades are applied on
        top of it, per deck."""
        return self._volume

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, float(value)))
        self._apply_volume()

    def is_playing(self) -> bool:
        return self._active.is_playing()

    def position(self) -> int:
        return self._active.player.position()

    def duration(self) -> int:
        return self._active.player.duration() or (self.current.duration_ms if self.current else 0)

    # -- Qt callbacks -----------------------------------------------------

    def _on_position(self, deck: _Deck, ms: int) -> None:
        if deck is not self._active:
            return
        self._played_ms = max(self._played_ms, ms)
        self.positionChanged.emit(ms)
        if self._fade_from is None and deck.is_playing():
            fade_ms = self._crossfade_ms_for_next()
            length = deck.player.duration()
            if fade_ms > 0 and length > 0 and length - ms <= fade_ms and self._standby_ready():
                self._advance_to_standby(fade_ms)

    def _on_duration(self, deck: _Deck, ms: int) -> None:
        if deck is self._active:
            self.durationChanged.emit(ms)

    def _on_media_status(self, deck: _Deck, status) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if deck is self._fade_from:
            self._finish_fade()
            return
        if deck is not self._active:
            return
        if self._standby_ready():
            self._advance_to_standby(0)
            return
        self._flush_play_event(completed=True)
        if self._repeat == REPEAT_ONE:
            self._load_current()
        else:
            self.next()

    def _on_state(self, deck: _Deck, state) -> None:
        if deck is not self._active:
            return
        mapping = {
            QMediaPlayer.PlaybackState.PlayingState: "playing",
            QMediaPlayer.PlaybackState.PausedState: "paused",
            QMediaPlayer.PlaybackState.StoppedState: "stopped",
        }
        self.playbackStateChanged.emit(mapping.get(state, "stopped"))

    def _on_error(self, deck: _Deck, error, message: str) -> None:  # pragma: no cover
        if error == QMediaPlayer.Error.NoError:
            return
        if deck is not self._active:
            # a preloaded file that won't open: drop it, and let the normal
            # path report it when (if) it's reached
            log.info("preload failed: %s", message)
            if deck is not self._fade_from:
                deck.unload()
            return
        log.warning("player error: %s", message)
        self.errorOccurred.emit(message or "Playback error")

    # -- history ----------------------------------------------------------

    def _flush_play_event(self, completed: bool = False, skipped: bool = False) -> None:
        """Write a PlayEvent for the track that just finished/was left."""
        item = self.current
        if item is None or self._current_scrobbled or self._started_at is None:
            return
        played = int(self._played_ms)
        duration = item.duration_ms or self._active.player.duration() or 0
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
