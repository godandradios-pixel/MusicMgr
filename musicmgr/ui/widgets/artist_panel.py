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
from ...services.library import format_duration
from .common import Breadcrumb, CoverArt, TouchButton, dim_label
from .cover_grid import GridTile
from .gatefold_coverflow import GatefoldCoverflow


class ArtistDetailPanel(QWidget):
    """One artist: their discography as a folding cover row, plus
    play-everything actions.

    Used to also list the artist's music videos underneath the release row
    (Video.artist_id - see services/video_scanner.py); removed 2026-09-15
    (James: "Remove the video list...introduce a Video sidebar menu
    option") in favor of a real "Videos" section of its own (see
    NAV_ITEMS in ui/app.py) rather than duplicating a per-artist listing
    here.
    """

    releaseActivated = Signal(int)

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
        # the release row's own "N releases ‹ ›" counter/pager shares this
        # row rather than getting one of its own below the header card
        # (2026-09-15, James: "move the 41 releases up a row") - built by
        # GatefoldCoverflow (it owns the wiring to the row itself) but laid
        # out here, since *where* they sit is this page's call, not that
        # widget's - see GatefoldCoverflow's own docstring.
        crumb_row = QHBoxLayout()
        crumb_row.setSpacing(8)
        crumb_row.addWidget(self.breadcrumb)
        crumb_row.addStretch(1)
        layout.addLayout(crumb_row)

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

        # a scroll/drag-driven "gatefold" row rather than a page-filling
        # grid - an artist can have anywhere from one release to several
        # hundred, and this is one section on a page. See
        # gatefold_coverflow.py for the fold itself; sorted by title, same
        # as the top-level Library grids.
        self.releases = GatefoldCoverflow(noun="release", panel_size=190)
        self.releases.tileActivated.connect(self.releaseActivated.emit)
        layout.addWidget(self.releases, 0)
        crumb_row.addWidget(self.releases.count_label)
        crumb_row.addWidget(self.releases.prev_btn)
        crumb_row.addWidget(self.releases.next_btn)

        layout.addStretch(1)

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
