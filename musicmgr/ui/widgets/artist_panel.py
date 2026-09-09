"""Artist page: header with counts and play actions, then their releases."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import func, select

from ...db.models import Artist, Release, Track
from ...services import library as lib
from ...services import videos as vid_svc
from ...services.library import format_duration
from .common import Breadcrumb, CoverArt, TouchButton, TouchList, dim_label
from .cover_grid import ALBUM_SORTS, CoverGrid, GridTile


class ArtistDetailPanel(QWidget):
    """One artist: their discography as a grid, plus play-everything actions.

    Also lists any music videos filed under this artist (Video.artist_id -
    see services/video_scanner.py) underneath the release grid, when there
    are any. Tapping one routes to the Videos view via
    ctx.playVideoRequested rather than playing inline here - a video plays
    in its own embedded pane there, not inside the Library view.
    """

    releaseActivated = Signal(int)
    videoActivated = Signal(int)

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._artist_id: Optional[int] = None
        self._artist_name: str = ""
        self._tracks: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # replaces a lone "‹ Artists" pill - see release_panel.py's
        # Breadcrumb for why (LibraryView wires the one click target this
        # page needs: back to the Artists grid).
        self.breadcrumb = Breadcrumb()
        layout.addWidget(self.breadcrumb)

        header = QFrame()
        header.setObjectName("Card")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(18)

        self.portrait = CoverArt(120, circular=True)
        hl.addWidget(self.portrait, 0, Qt.AlignVCenter)

        meta = QVBoxLayout()
        meta.setSpacing(4)

        self.name = QLabel("")
        self.name.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.name.setWordWrap(True)
        # a wrapped QLabel inside this many nested layouts (VBox in HBox in a
        # Card frame) can occasionally negotiate a wildly inflated height on
        # first layout, reserving far more vertical space than the actual
        # text needs and leaving a tall blank gap above it - two lines of a
        # 26px name is already a very long artist name, so cap it there as a
        # hard ceiling regardless of what the layout negotiation computes
        self.name.setMaximumHeight(72)
        meta.addWidget(self.name)

        self.stats = dim_label("")
        meta.addWidget(self.stats)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        play = TouchButton("Play everything", primary=True)
        play.clicked.connect(lambda: self.play(shuffle=False))
        shuffle = TouchButton("Shuffle")
        shuffle.clicked.connect(lambda: self.play(shuffle=True))
        queue = TouchButton("Add to queue")
        queue.clicked.connect(lambda: self.ctx.enqueue_tracks(self._tracks))
        for b in (play, shuffle, queue):
            actions.addWidget(b)
        actions.addStretch(1)
        meta.addLayout(actions)
        # meta (and therefore the card) hugs its own content width rather
        # than stretching to the full page width - a stretch factor here
        # used to let the card's background/border balloon out to the right
        # of the actual name/stats/buttons with nothing in it
        hl.addLayout(meta, 0)
        layout.addWidget(header, 0, Qt.AlignLeft)

        # a single left-to-right scrolling row, not a page-filling grid - an
        # artist can have anywhere from one release to several hundred, and
        # this is one section on a page that also has videos below it, not
        # the whole screen the way the Albums library grid is. Smaller tiles
        # than the full-page grid uses, so several fit across the row
        # cleanly instead of one full tile plus a sliver of the next.
        # No sort picker here either (2026-09-05) - pinned to Title, same as
        # the top-level Library grids (see cover_grid.py's "Sort chips
        # removed" note); James's follow-up request extended that removal to
        # this row too.
        self.releases = CoverGrid(
            sorts=ALBUM_SORTS,
            noun="release",
            horizontal=True,
            art_size=130,
            default_sort="title",
            show_sort_picker=False,
        )
        self.releases.tileActivated.connect(self.releaseActivated.emit)
        layout.addWidget(self.releases, 0)

        self.video_section = QWidget()
        vs = QVBoxLayout(self.video_section)
        vs.setContentsMargins(0, 0, 0, 0)
        vs.setSpacing(6)
        video_head = QLabel("Music videos")
        video_head.setObjectName("Crumb")
        vs.addWidget(video_head)
        self.video_list = TouchList(row_height=64)
        self.video_list.itemActivatedPayload.connect(
            lambda p: self.videoActivated.emit(p["key"]) if p else None
        )
        vs.addWidget(self.video_list, 1)
        # stretch=1 so this section fills whatever's left below the header
        # and release row, the same way ReleaseDetailPanel anchors its
        # track_list to the bottom of that page (see its
        # `layout.addWidget(self.track_list, 1)`) - one list, one
        # predictably-placed scrollbar flush with the page's true bottom
        # edge. The old fixed setMaximumHeight(220) plus a trailing
        # addStretch(1) left this list stranded mid-page with its own
        # scrollbar floating above a block of dead space below it (James's
        # "scroll bar in the middle" report, 2026-09-06) instead of reaching
        # the bottom of the window.
        layout.addWidget(self.video_section, 1)
        self.video_section.setVisible(False)

    # -- api ------------------------------------------------------------------

    @property
    def artist_id(self) -> Optional[int]:
        return self._artist_id

    @property
    def tracks(self) -> list:
        return self._tracks

    def set_artist(self, artist_id: Optional[int]) -> None:
        self._artist_id = artist_id
        self._artist_name = ""
        self._tracks = []
        if artist_id is None:
            self.name.setText("")
            self.stats.setText("")
            self.releases.set_tiles([])
            self.video_list.set_rows([])
            self.video_section.setVisible(False)
            self.breadcrumb.set_path([])
            return

        tiles: list[GridTile] = []
        with self.ctx.session() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                return
            self.name.setText(artist.name)
            self._artist_name = artist.name
            self.breadcrumb.set_path(["Artists", artist.name])

            # every release this artist is the *album* artist for - a guest
            # verse or a production credit elsewhere does not belong on their
            # page, only in search (see lib.list_artists)
            stmt = (
                select(Release, func.count(func.distinct(Track.id)))
                .outerjoin(Track, Track.release_id == Release.id)
                .where(
                    Release.album_artist_id == artist_id,
                    Release.is_compilation.is_(False),
                )
                .group_by(Release.id)
                .order_by(func.coalesce(Release.year, 9999), Release.title)
            )
            total_ms = 0
            for release, track_count in session.execute(stmt):
                tiles.append(
                    GridTile(
                        key=release.id,
                        title=release.title,
                        subtitle=release.artist_display or "",
                        year=release.year,
                        cover_path=release.cover_path,
                        count=track_count or 0,
                        sort_key=release.title_key or release.title.lower(),
                    )
                )

            self._tracks = list(lib.list_tracks_for_album_artist(session, artist_id))
            total_ms = sum(t.duration_ms or 0 for t in self._tracks)
            self.portrait.set_source(
                artist.image_path or (tiles[0].cover_path if tiles else None),
                artist.name,
            )

            video_rows = [
                {
                    "primary": video.title,
                    "secondary": format_duration(video.duration_ms),
                    "key": video.id,
                }
                for video in vid_svc.list_videos(session, artist_id=artist_id)
            ]
        self.video_list.set_rows(video_rows)
        self.video_section.setVisible(bool(video_rows))

        releases = len(tiles)
        tracks = len(self._tracks)
        self.stats.setText(
            f"{releases} release{'s' if releases != 1 else ''} · "
            f"{tracks} track{'s' if tracks != 1 else ''} · {format_duration(total_ms)}"
        )
        self.releases.set_tiles(tiles)
        # same reasoning as ReleaseDetailPanel.set_release - lets the
        # bottom PlayerBar's play button start this artist's tracks even
        # before "Play everything"/a release/a track's been tapped.
        self.ctx.set_viewing(self._tracks, source=f"artist:{artist_id}")

    def play(self, shuffle: bool = False) -> None:
        if not self._tracks:
            self.ctx.notify("Nothing playable for that artist")
            return
        self.ctx.player.set_shuffle(shuffle)
        self.ctx.play_tracks(self._tracks, start=0, source=f"artist:{self._artist_id}")
