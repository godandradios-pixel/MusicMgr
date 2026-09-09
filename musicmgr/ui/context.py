"""Shared application context handed to every view."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from PySide6.QtCore import QObject, Signal
from sqlalchemy.orm import Session

from ..db.session import session_scope
from ..services.player import PlayerController, QueueItem, queue_items_from_tracks


class AppContext(QObject):
    #: emitted after a scan or import so open views can refresh
    libraryChanged = Signal()
    #: emitted after a video scan so the Videos view (and Settings' stats
    #: card) can refresh
    videosChanged = Signal()
    #: emitted when playlists are created/edited
    playlistsChanged = Signal()
    #: transient message for the status area
    notified = Signal(str)
    #: request the main window switch to a view by key
    navigateRequested = Signal(str)
    #: ask the Videos view to open and play a specific video - used by the
    #: artist page's "Music videos" list, which lives in the Library view
    playVideoRequested = Signal(int)
    #: ask the Videos view to group by Artist and scroll to one artist's
    #: section, rather than playing a specific video - used when a Library
    #: search match is a *collapsed* "N videos" tile (an Albums/Artists grid
    #: match on the artist's name rather than any one video's own title, see
    #: CoverGrid._rebuild()'s "video_group" tiles, 2026-09-06) which has no
    #: single video id to hand to playVideoRequested.
    focusVideoArtistRequested = Signal(str)
    #: a video started/stopped playing - lets the persistent PlayerBar take
    #: over (or hand back) transport for it, so there's one set of controls
    #: for video the same way there's one for music. See
    #: ui/widgets/player_bar.py:PlayerBar.enter_video_mode/exit_video_mode
    #: and ui/widgets/video_panel.py, which emits these.
    videoPlaybackStarted = Signal(object, str, str)  # VideoController, title, artist
    videoPlaybackEnded = Signal()
    #: Now Playing's "‹ Back" button asking to return to whatever section was
    #: active before Now Playing was opened. NowPlayingView doesn't hold a
    #: MainWindow reference, so it asks generically rather than knowing the
    #: target itself - MainWindow.navigate() is what actually tracks which
    #: section that was (see MainWindow._nowplaying_back_key).
    nowPlayingBackRequested = Signal()
    #: the video player's "‹ Back" button asking to return to whatever
    #: section was active before Videos was opened - the exact same pattern
    #: as nowPlayingBackRequested, for the exact same reason: Videos has no
    #: sidebar entry of its own any more (see "Now Playing and Videos
    #: removed from the left sidebar" in architecture.md), so it's never a
    #: place someone navigates *to* on purpose - only ever a mid-transit
    #: waypoint on the way to playing a specific video (from a search match
    #: or an artist page's own video list), and landing back on its table
    #: after watching would be a dead end, the same gap Now Playing had
    #: before its own back button existed (2026-09-06).
    videoBackRequested = Signal()
    #: ask the Library view to open a specific artist's page - used by Now
    #: Playing's tappable artist name, so someone can jump straight from
    #: what's playing to that artist's page without hunting through Library
    #: (2026-09-06). Library switches itself to the Library section and
    #: selects Artist mode before opening the page; the emitter doesn't need
    #: to navigate first, mirroring playVideoRequested's contract.
    openArtistRequested = Signal(int)
    #: ask the Library view to open a specific release's page - the same
    #: pattern as openArtistRequested, for Now Playing's tappable album
    #: art/title (2026-09-06). A release opened this way isn't part of the
    #: Albums or Artists browsing hierarchy, so Library gives its breadcrumb
    #: a standalone "Now Playing" ancestor rather than one of those grids.
    openReleaseRequested = Signal(int)

    def __init__(self, player: PlayerController, parent=None) -> None:
        super().__init__(parent)
        self.player = player
        #: the tracks behind whatever release/artist page is currently on
        #: screen, kept in sync by ReleaseDetailPanel.set_release and
        #: ArtistDetailPanel.set_artist via set_viewing() - never queued or
        #: played on its own, only consulted by play_or_resume() so the
        #: persistent PlayerBar's one play button can start something even
        #: before any specific track has been tapped (see "Redundant Play
        #: button removed from the release page" / its follow-up in
        #: architecture.md for why this exists).
        self._viewing_tracks: list = []
        self._viewing_source: str = "library"

    @contextmanager
    def session(self) -> Iterator[Session]:
        with session_scope() as s:
            yield s

    def notify(self, message: str) -> None:
        self.notified.emit(message)

    def set_viewing(self, tracks, source: str = "library") -> None:
        """Record what a release/artist page is currently displaying, so
        play_or_resume() has something to fall back to."""
        self._viewing_tracks = list(tracks)
        self._viewing_source = source

    def play_or_resume(self) -> None:
        """The persistent PlayerBar's one play button. If a queue already
        exists (paused mid-track, or loaded but not yet started), just
        resumes/starts it - the ordinary case. If nothing has been queued
        at all, falls back to whatever release/artist page is currently on
        screen (see set_viewing) rather than doing nothing, since there's
        no other "play" affordance left on those pages."""
        if self.player.current is not None or self.player.queue:
            self.player.toggle()
            return
        if self._viewing_tracks:
            self.play_tracks(self._viewing_tracks, start=0, source=self._viewing_source)
        else:
            self.notify("Open an album or artist to play")

    def play_tracks(self, tracks, start: int = 0, source: str = "library") -> int:
        """Convert ORM tracks to queue items and start playback. Returns count.

        Every real "hit play" action in the app - a track tap, Play
        everything, Shuffle, Play owned, a playlist/chart's Play, and the
        PlayerBar's own empty-queue fallback (play_or_resume) - routes
        through this one method, so it's also the single place to send the
        person to Now Playing when that happens (see "Auto-navigate to Now
        Playing when playback starts" in architecture.md). Advancing to the
        next queued track never calls this again, so nothing pulls the
        person back here mid-playback - they stay on Now Playing (or
        wherever they've navigated since) until they hit play somewhere new.
        Deliberately not in play_or_resume()'s *resume* branch or in
        enqueue_tracks() - resuming an already-loaded, merely-paused track,
        or just queueing something for later, isn't "hitting play" on
        something new and shouldn't yank the person away from whatever
        they're doing.
        """
        items = queue_items_from_tracks(tracks)
        if not items:
            self.notify("No playable files for that selection")
            return 0
        start = min(max(start, 0), len(items) - 1)
        self.player.play_tracks(items, start=start, source=source)
        self.navigateRequested.emit("nowplaying")
        return len(items)

    def enqueue_tracks(self, tracks, play_next: bool = False) -> int:
        items = queue_items_from_tracks(tracks)
        if items:
            self.player.enqueue(items, play_next=play_next)
            self.notify(
                f"{len(items)} track{'s' if len(items) != 1 else ''} "
                f"{'queued next' if play_next else 'added to queue'}"
            )
        return len(items)
