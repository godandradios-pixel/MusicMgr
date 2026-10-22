"""Fills in video thumbnails and years in the background (2026-10-22) -
see services/video_details.py for where each comes from.

`start(ctx)` runs `VideoDetailsThread` unless one is already running. The
main window calls it a couple of minutes after launch (only when a cheap
count says some video still needs it, so an ordinary start does no work)
and Settings calls it after every library scan.

Frames are grabbed with ffmpeg when it's on the PATH; otherwise with Qt's
own player (`QtFrameGrabber`), created inside this thread with no audio
output, so nothing is heard and the music player is untouched. Each grab
has a time limit; Qt's player has frozen on rare occasions inside its own
FFmpeg code (conftest.py's watchdog note), and if a grab never comes back
this thread simply stays parked there - it holds no lock the rest of the
app needs - and on close `stop()` gives it three seconds, then ends it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEventLoop, QThread, QTimer, QUrl, Signal

from ..db.session import session_scope
from ..services import video_details

log = logging.getLogger(__name__)

GRAB_TIMEOUT_MS = 15_000


class QtFrameGrabber:
    """One QMediaPlayer, reused for every grab; must be created and used in
    the same thread (VideoDetailsThread.run)."""

    def __init__(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

        self._status = QMediaPlayer.MediaStatus
        self.player = QMediaPlayer()
        self.sink = QVideoSink()
        self.player.setVideoSink(self.sink)  # no audio output: silent

    def __call__(self, path: Path, out: Path, duration_ms: Optional[int]) -> Optional[bool]:
        player, sink, status = self.player, self.sink, self._status
        at_ms = int(min(30_000, duration_ms / 3)) if duration_ms else 10_000
        want_us = max(0, at_ms - 1500) * 1000
        loop = QEventLoop()
        result: dict = {"image": None, "failed": False, "timed_out": False}

        def on_frame(frame) -> None:
            if result["image"] is not None or not frame.isValid():
                return
            if 0 <= frame.startTime() < want_us:
                return
            image = frame.toImage()
            if image.isNull():
                return
            result["image"] = image
            loop.quit()

        def on_status(s) -> None:
            if s == status.LoadedMedia:
                player.setPosition(at_ms)
                player.play()
            elif s == status.InvalidMedia:
                result["failed"] = True
                loop.quit()

        def on_timeout() -> None:
            result["timed_out"] = True
            loop.quit()

        c_frame = sink.videoFrameChanged.connect(on_frame)
        c_status = player.mediaStatusChanged.connect(on_status)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(on_timeout)
        timer.start(GRAB_TIMEOUT_MS)
        try:
            player.setSource(QUrl.fromLocalFile(str(path)))
            loop.exec()
        finally:
            timer.stop()
            sink.videoFrameChanged.disconnect(c_frame)
            player.mediaStatusChanged.disconnect(c_status)
            player.stop()
            player.setSource(QUrl())
        image = result["image"]
        if image is None:
            # a time-out on one file isn't "stuck" - the next file still runs
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        if image.width() > video_details.GRAB_WIDTH:
            image = image.scaledToWidth(video_details.GRAB_WIDTH)
        return bool(image.save(str(out), "JPG", 85))


class VideoDetailsThread(QThread):
    done = Signal(int, int)  # years found, thumbnails found

    def run(self) -> None:  # pragma: no cover - exercised interactively
        try:
            grab = video_details.ffmpeg_grabber()
            if grab is None:
                try:
                    grab = QtFrameGrabber()
                except Exception:
                    log.exception("no video frame grabber")
                    grab = None
            with session_scope() as session:
                years, thumbs = video_details.fill_video_details(
                    session, grab=grab, should_stop=self.isInterruptionRequested)
            if years or thumbs:
                log.info("videos: %d years and %d thumbnails filled in", years, thumbs)
            self.done.emit(years, thumbs)
        except Exception:
            log.exception("filling in video details failed")
            self.done.emit(0, 0)


def stop(ctx, wait_ms: int = 3000) -> None:
    """On close: ask the filler to stop after the current video. One parked
    in a frozen grab is left to the process exit."""
    running: Optional[VideoDetailsThread] = getattr(ctx, "_video_details_thread", None)
    if running is not None and running.isRunning():
        running.requestInterruption()
        if not running.wait(wait_ms):
            log.warning("video details still running at close")
            running.terminate()
            running.wait(1000)


def start(ctx, *, only_if_pending: bool = False) -> Optional[VideoDetailsThread]:
    """Run the filler unless it's already running. Kept on `ctx`, so the
    window and Settings share one."""
    running: Optional[VideoDetailsThread] = getattr(ctx, "_video_details_thread", None)
    if running is not None and running.isRunning():
        return running
    if only_if_pending:
        try:
            with ctx.session() as session:
                if not video_details.pending_count(session):
                    return None
        except Exception:
            log.exception("video details check failed")
            return None
    thread = VideoDetailsThread()

    def finished(years: int, thumbs: int) -> None:
        if years or thumbs:
            ctx.videosChanged.emit()

    thread.done.connect(finished)
    ctx._video_details_thread = thread
    thread.start(QThread.LowestPriority)
    return thread
