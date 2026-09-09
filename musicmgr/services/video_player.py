"""Playback engine for a single music video.

Deliberately separate from services/player.py:PlayerController rather than
reusing it: a video isn't part of a shuffled/queued listening session (Videos
has no playlists yet - see architecture.md's extension points), and giving it
its own QMediaPlayer means starting a video never disturbs whatever queue is
sitting paused in the persistent PlayerBar. `ui/widgets/video_panel.py`
attaches this to a QVideoWidget for the actual picture.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..db.session import session_scope
from .videos import record_watch

log = logging.getLogger(__name__)

#: a watch only counts toward play_count once this much has been seen -
#: same thresholds as services/player.py's SCROBBLE_MIN_*
WATCH_MIN_MS = 30_000
WATCH_MIN_FRACTION = 0.5


class VideoController(QObject):
    positionChanged = Signal(int)     # ms
    durationChanged = Signal(int)     # ms
    playbackStateChanged = Signal(str)  # playing | paused | stopped
    errorOccurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._video_id: Optional[int] = None
        self._played_ms = 0
        self._recorded = False

        self._audio = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self.durationChanged.emit)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)

    def set_video_output(self, widget) -> None:
        self._player.setVideoOutput(widget)

    # -- transport ----------------------------------------------------------

    def play_path(self, path: str, video_id: Optional[int] = None) -> None:
        self._record_if_due()
        self._video_id = video_id
        self._played_ms = 0
        self._recorded = False
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._record_if_due()
        self._player.stop()

    def seek(self, ms: int) -> None:
        self._player.setPosition(int(ms))

    def seek_relative(self, delta_ms: int) -> None:
        self._player.setPosition(max(0, self._player.position() + delta_ms))

    def duration(self) -> int:
        return self._player.duration()

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def volume(self) -> float:
        return self._audio.volume()

    def set_volume(self, value: float) -> None:
        self._audio.setVolume(max(0.0, min(1.0, float(value))))

    # -- Qt callbacks ---------------------------------------------------------

    @Slot(int)
    def _on_position(self, ms: int) -> None:
        self._played_ms = max(self._played_ms, ms)
        self.positionChanged.emit(ms)

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._record_if_due(completed=True)

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
            log.warning("video player error: %s", message)
            self.errorOccurred.emit(message or "Playback error")

    # -- watch history ----------------------------------------------------

    def _record_if_due(self, completed: bool = False) -> None:
        if self._video_id is None or self._recorded:
            return
        duration = self._player.duration() or 0
        played = self._played_ms
        enough = completed or played >= WATCH_MIN_MS or (
            duration and played >= duration * WATCH_MIN_FRACTION
        )
        if not enough:
            return
        self._recorded = True
        try:
            with session_scope() as session:
                record_watch(session, self._video_id)
        except Exception as exc:  # pragma: no cover
            log.warning("could not record video watch: %s", exc)
