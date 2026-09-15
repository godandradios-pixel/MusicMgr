"""Videos browser: a sortable, groupable table (MusicBee-style), tap a row to
open the embedded player pane with an in-app full-screen toggle.

The table itself lives in `ui/widgets/video_table.py` (columns, sorting,
group-by - see its docstring for why Title/Artist/Year/Time rather than
MusicBee's Album/Genre/Rating). This is the third shape this view has taken
in one day (2026-08-30): a cover grid with meaningless title-initials tiles
first, then a plain single-column list once the tiles were dropped, now a
proper table once James asked for MusicBee-style columns/sort/group-by -
see architecture notes for the full history.

Deliberately just one presentation (no Artists/Genres chips like the Library
view) - a video is a flat row, not a Master/Release/Track hierarchy to browse
several ways (see Video's docstring in db/models.py). A Library search match
(Albums/Artists/Tracks/Title Details) that's a video routes here too, via
ctx.playVideoRequested/focusVideoArtistRequested - see
LibraryView._activate_video/_activate_video_group.

Watched folders are managed from Settings ("Watched video folders"), the same
split Library uses between browsing (here) and folder bookkeeping (Settings).
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import QStackedWidget

from ...db.models import Video
from ...services import videos as vid_svc
from ...services.library import format_duration
from ..context import AppContext
from ..widgets.common import dim_label
from ..widgets.video_panel import VideoPlayerPanel
from ..widgets.video_table import VideoRow, VideoTable
from .base import BaseView

PANE_TABLE = 0
PANE_PLAYER = 1


class VideosView(BaseView):
    title_text = "Videos"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._loaded = False

        self.stats = dim_label("")
        self.header.insertWidget(1, self.stats)

        hint = dim_label("Add folders to scan from Settings → Watched video folders")
        self.body().addWidget(hint)

        self.panes = QStackedWidget()
        self.panes.addWidget(self._build_table())   # 0
        self.panes.addWidget(self._build_player())  # 1
        self.body().addWidget(self.panes, 1)

        ctx.videosChanged.connect(self._on_videos_changed)
        ctx.playVideoRequested.connect(self._open_video)
        ctx.focusVideoArtistRequested.connect(self._focus_artist)

    # -- construction -----------------------------------------------------

    def _build_table(self):
        self.table = VideoTable()
        self.table.rowActivated.connect(self._open_video)
        return self.table

    def _build_player(self):
        self.player_panel = VideoPlayerPanel(self.ctx)
        # "‹ Back" drops onto this view's own table (2026-09-15, once
        # Videos got a real sidebar entry - see NAV_ITEMS in ui/app.py).
        # Doesn't stop playback, same as leaving Now Playing doesn't stop
        # music - the persistent PlayerBar keeps transport for it either way
        # (see video_panel.py's docstring).
        self.player_panel.backRequested.connect(lambda: self.panes.setCurrentIndex(PANE_TABLE))
        return self.player_panel

    # -- data loading -------------------------------------------------------

    def refresh(self) -> None:
        if not self._loaded:
            self._load_videos()

    def _on_videos_changed(self) -> None:
        self._loaded = False
        self.refresh()

    def _load_videos(self) -> None:
        rows: list[VideoRow] = []
        total_ms = 0
        with self.ctx.session() as session:
            for video in vid_svc.list_videos(session):
                total_ms += video.duration_ms or 0
                rows.append(VideoRow(
                    key=video.id,
                    title=video.title,
                    artist=video.artist_display or "",
                    year=video.year,
                    duration_ms=video.duration_ms,
                    sort_key=video.title_key or video.title.lower(),
                ))
        self.table.set_rows(rows)
        plural = "" if len(rows) == 1 else "s"
        self.stats.setText(f"{len(rows)} video{plural} · {format_duration(total_ms)}")
        self._loaded = True

    # -- navigation -----------------------------------------------------------

    def _open_video(self, video_id: int) -> None:
        with self.ctx.session() as session:
            video = session.get(Video, video_id)
            if video is None:
                return
            missing, path = video.is_missing, video.path
            title, artist, vid = video.title, video.artist_display, video.id

        if missing or not os.path.exists(path):
            self.ctx.notify("That video file is missing")
            return
        self.player_panel.play(vid, path, title, artist)
        self.panes.setCurrentIndex(PANE_PLAYER)

    def _focus_artist(self, artist: str) -> None:
        """A collapsed "N videos" tile from a Library search (2026-09-06) -
        no single video to play, so this lands on the table itself, grouped
        by Artist and scrolled to that artist's section, rather than the
        player pane `_open_video` uses."""
        self.panes.setCurrentIndex(PANE_TABLE)
        self.table.focus_artist(artist)
