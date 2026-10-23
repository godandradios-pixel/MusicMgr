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
    #: ask the Videos view to open and play a specific video - used when a
    #: Library search match (Albums/Artists/Tracks/Title Details) is a
    #: single video, routed via LibraryView._activate_video
    playVideoRequested = Signal(int)
    #: ask the Videos view to group by Artist and scroll to one artist's
    #: section - used when a Library search match is a *collapsed* "N
    #: videos" tile (see CoverGrid._rebuild()'s "video_group" tiles), which
    #: has no single video id to hand to playVideoRequested.
    focusVideoArtistRequested = Signal(str)
    #: a video started/stopped playing - lets the persistent PlayerBar take
    #: over (or hand back) transport for it, so there's one set of controls
    #: for video the same way there's one for music. See
    #: ui/widgets/player_bar.py:PlayerBar.enter_video_mode/exit_video_mode
    #: and ui/widgets/video_panel.py, which emits these.
    videoPlaybackStarted = Signal(object, str, str)  # VideoController, title, artist
    videoPlaybackEnded = Signal()
    #: the embedded video player's "‹ Back" button asking to return to
    #: whatever section was active before Videos was reached - the same
    #: "ask, don't know the target yourself" contract as
    #: nowPlayingBackRequested below, since Videos has no sidebar entry of
    #: its own to fall back to (see NAV_ITEMS in ui/app.py,
    #: VideosView._build_player and MainWindow._go_back_from_videos).
    videosBackRequested = Signal()
    #: Now Playing's "‹ Back" button asking to return to whatever section was
    #: active before Now Playing was opened. NowPlayingView doesn't hold a
    #: MainWindow reference, so it asks generically rather than knowing the
    #: target itself - MainWindow.navigate() is what actually tracks which
    #: section that was (see MainWindow._nowplaying_back_key).
    nowPlayingBackRequested = Signal()
    #: ask the Library view to open a specific artist's page - used by Now
    #: Playing's tappable artist name. Library switches itself to the
    #: Library section and selects Artist mode before opening the page; the
    #: emitter doesn't need to navigate first, mirroring
    #: playVideoRequested's contract.
    openArtistRequested = Signal(int)
    #: ask the Library view to open a specific release's page - the same
    #: pattern as openArtistRequested, for Now Playing's tappable album
    #: art/title. A release opened this way isn't part of the Albums or
    #: Artists browsing hierarchy, so Library gives its breadcrumb a
    #: standalone "Now Playing" ancestor rather than one of those grids.
    openReleaseRequested = Signal(int)

    #: same as openArtistRequested, for Settings' "Missing metadata"
    #: dashboard - a separate signal because the artist page's breadcrumb
    #: needs to say "Missing metadata" and lead back there, not to the
    #: plain Artists grid (see LibraryView._open_artist_from_missing_metadata).
    openArtistFromMissingMetadataRequested = Signal(int)
    #: same idea as openArtistFromMissingMetadataRequested, for a release
    #: row (Album artwork / Lyrics tabs) - the release breadcrumb's "Now
    #: Playing" ancestor (openReleaseRequested, above) becomes "Missing
    #: metadata" here instead (LibraryView._open_release_from_missing_metadata).
    openReleaseFromMissingMetadataRequested = Signal(int)
    #: the "Missing metadata" breadcrumb's own back action, both of the
    #: above - SettingsView reopens its dashboard dialog in response
    #: (reusing its cached scan - see SettingsView._last_metadata_scan -
    #: so this is instant, not another full rescan).
    missingMetadataBackRequested = Signal()

    def __init__(self, player: PlayerController, parent=None) -> None:
        super().__init__(parent)
        self.player = player
        #: "radio from this song" - services/radio.py
        from ..services.radio import RadioController

        self.radio = RadioController(player, self)
        self.radio.notified.connect(self.notify)
        #: radio tuner page - services/tuner.py
        from ..services.tuner import TunerController

        self.tuner = TunerController(player, self)
        self.tuner.notified.connect(self.notify)
        #: phone remote - services/web_remote.py. Created here (not
        #: started): MainWindow starts it at launch when Settings › Phone
        #: remote is on, and Settings turns it on and off.
        from ..services.web_remote import RemoteServer

        self.web_remote = RemoteServer(self, self)
        #: the tracks behind whatever release/artist page is currently on
        #: screen, kept in sync by ReleaseDetailPanel.set_release and
        #: ArtistDetailPanel.set_artist via set_viewing() - never queued or
        #: played on its own, only consulted by play_or_resume() so the
        #: PlayerBar's play button can start something before any track has
        #: been tapped (see "Redundant Play button removed from the release
        #: page" in architecture.md).
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

    def play_tracks(
        self, tracks, start: int = 0, source: str = "library", navigate: bool = True
    ) -> int:
        """Convert ORM tracks to queue items and start playback. Returns count.

        Every real "hit play" action in the app - a track tap, Play
        everything, Shuffle, Play owned, a playlist/chart's Play, and the
        PlayerBar's empty-queue fallback (play_or_resume) - routes through
        this one method, so it's also the single place to send the person
        to Now Playing (see "Auto-navigate to Now Playing when playback
        starts" in architecture.md). Advancing to the next queued track
        never calls this, so nothing pulls the person back mid-playback.
        Deliberately not used for play_or_resume()'s *resume* branch or
        enqueue_tracks() - resuming or queueing isn't "hitting play" on
        something new.
        """
        items = queue_items_from_tracks(tracks)
        if not items:
            self.notify("No playable files for that selection")
            return 0
        start = min(max(start, 0), len(items) - 1)
        self.player.play_tracks(items, start=start, source=source)
        # navigate=False: the Jukebox page's Play all/Shuffle
        # stay put so its cards can follow the music
        if navigate:
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
