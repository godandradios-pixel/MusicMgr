"""Videos page: music videos and movies, on the sidebar between Title
Details and Playlists.

Two tabs split the collection by `Video.media_type` - **Music Videos** and
**Movies** (anything under a "Movies" folder, see
services/video_scanner.py:media_type_for_path) - each showing its count.
Within a tab, two layouts:

* **Grid** (the default) - 16:9 thumbnail tiles with the running time on
  each, sortable by Title / Artist / Year / Recently added
  (ui/widgets/video_grid.py).
* **List** - the sortable, groupable table (ui/widgets/video_table.py).

Both get the same "Search this view…" + Space/⌫/✕ row (matching title OR
artist) and the A-Z jump bar, as LibraryView has. Tap a video and it plays
in the embedded player pane, with the browse chrome hidden so the picture
gets the height.

"‹ Back" from the player returns to the grid/list when the video was
opened here; when it was opened from somewhere else (a Library search
match, routed via ctx.playVideoRequested) it goes back to that section
instead - see ctx.videosBackRequested and MainWindow._go_back_from_videos.
A collapsed "N videos" Library search tile (ctx.focusVideoArtistRequested)
lands on the List layout grouped by artist, scrolled to that artist.

Pictures come from the background filler (ui/video_details_worker.py),
started when this page opens; while it works the stats line reads
"finding pictures N of M" and new pictures appear every 25 videos
(ctx.videoDetailsProgress) without losing the scroll position.

Watched folders are managed from Settings ("Watched video folders"), the same
split Library uses between browsing (here) and folder bookkeeping (Settings).
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLineEdit, QStackedWidget, QWidget

from ...db.models import Video
from ...services import videos as vid_svc
from ...services.library import format_duration
from ..context import AppContext
from ..widgets.common import ChipButton, TouchButton, dim_label
from ..widgets.cover_grid import JumpBar
from ..widgets.video_grid import VideoGrid
from ..widgets.video_panel import VideoPlayerPanel
from ..widgets.video_table import VideoRow, VideoTable
from .base import BaseView

#: self.panes - browsing (grid or list, see LAYOUT_*) or the player
PANE_BROWSE = 0
PANE_PLAYER = 1

#: self.layouts, inside PANE_BROWSE
LAYOUT_GRID = 0
LAYOUT_LIST = 1

TAB_MUSIC_VIDEOS = Video.MEDIA_MUSIC_VIDEO
TAB_MOVIES = Video.MEDIA_MOVIE

#: tab -> (chip label, singular noun, plural noun)
TAB_NAMES = {
    TAB_MUSIC_VIDEOS: ("Music Videos", "music video", "music videos"),
    TAB_MOVIES: ("Movies", "movie", "movies"),
}


class VideosView(BaseView):
    title_text = "Videos"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._loaded = False
        self._all_rows: list[VideoRow] = []
        self._tab = TAB_MUSIC_VIDEOS
        #: True while the player pane shows a video opened from this page's
        #: own grid/list - "‹ Back" then returns here rather than to
        #: whichever section a search match came from
        self._opened_here = False
        #: "finding pictures N of M" while the background filler runs
        self._details_status = ""

        self.stats = dim_label("")
        self.header.insertWidget(1, self.stats)

        # Music Videos / Movies tabs on the left, Grid / List on the right
        self.tab_row = QWidget()
        tab_row = QHBoxLayout(self.tab_row)
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(8)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self.tab_chips: dict[str, ChipButton] = {}
        for tab in (TAB_MUSIC_VIDEOS, TAB_MOVIES):
            chip = ChipButton(TAB_NAMES[tab][0])
            chip.setProperty("tab", tab)
            chip.setChecked(tab == self._tab)
            self._tab_group.addButton(chip)
            self.tab_chips[tab] = chip
            tab_row.addWidget(chip)
        self._tab_group.buttonClicked.connect(lambda b: self.set_tab(b.property("tab")))
        tab_row.addStretch(1)
        tab_row.addWidget(dim_label("View"))
        self._layout_group = QButtonGroup(self)
        self._layout_group.setExclusive(True)
        self.grid_chip = ChipButton("Grid")
        self.grid_chip.setChecked(True)
        self.list_chip = ChipButton("List")
        for idx, chip in ((LAYOUT_GRID, self.grid_chip), (LAYOUT_LIST, self.list_chip)):
            chip.setProperty("layout", idx)
            self._layout_group.addButton(chip)
            tab_row.addWidget(chip)
        self._layout_group.buttonClicked.connect(
            lambda b: self.set_layout(b.property("layout")))
        self.body().addWidget(self.tab_row)

        # same "Search this view…" + Space/⌫/✕ shape as LibraryView's own
        # search row. Matches by title OR artist rather than a "search by"
        # picker. In a widget of its own so the whole row can be hidden in
        # one call while the player pane is up.
        self.search_row = QWidget()
        search_row = QHBoxLayout(self.search_row)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search this view…")
        self.search_box.setFixedWidth(640)
        self.search_box.setToolTip("Searches by video title or artist")
        self.search_box.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self.search_box)

        space_btn = TouchButton("Space")
        space_btn.setFixedWidth(96)
        space_btn.setToolTip("Insert a space")
        space_btn.clicked.connect(lambda: self.search_box.insert(" "))
        search_row.addWidget(space_btn)

        backspace_btn = TouchButton("⌫")
        backspace_btn.setFixedWidth(72)
        backspace_btn.setToolTip("Backspace")
        backspace_btn.clicked.connect(self.search_box.backspace)
        search_row.addWidget(backspace_btn)

        clear_btn = TouchButton("✕")
        clear_btn.setFixedWidth(72)
        clear_btn.setToolTip("Clear the search box")
        clear_btn.clicked.connect(self.search_box.clear)
        search_row.addWidget(clear_btn)

        search_row.addStretch(1)
        self.body().addWidget(self.search_row)

        # on a touch panel with no physical keyboard, this bar is the only
        # way to type a letter (tapping one spells it into the search box) -
        # see JumpBar's docstring and LibraryView's module docstring.
        self.jump_bar = JumpBar()
        self.jump_bar.letterPicked.connect(self._on_letter_key)
        self.jump_bar.letterTyped.connect(self._on_letter_typed)
        self.body().addWidget(self.jump_bar)

        self.layouts = QStackedWidget()
        self.layouts.addWidget(self._build_grid())   # LAYOUT_GRID
        self.layouts.addWidget(self._build_table())  # LAYOUT_LIST
        self.panes = QStackedWidget()
        self.panes.addWidget(self.layouts)            # PANE_BROWSE
        self.panes.addWidget(self._build_player())    # PANE_PLAYER
        self.body().addWidget(self.panes, 1)

        ctx.videosChanged.connect(self._on_videos_changed)
        ctx.videoDetailsProgress.connect(self._on_details_progress)
        ctx.playVideoRequested.connect(self._open_video_from_elsewhere)
        ctx.focusVideoArtistRequested.connect(self._focus_artist)

    # -- construction -----------------------------------------------------

    def _build_grid(self):
        self.grid = VideoGrid()
        self.grid.rowActivated.connect(self._open_video)
        self.grid.updated.connect(self._refresh_jump_bar)
        return self.grid

    def _build_table(self):
        self.table = VideoTable()
        self.table.rowActivated.connect(self._open_video)
        self.table.updated.connect(self._refresh_jump_bar)
        return self.table

    def _build_player(self):
        self.player_panel = VideoPlayerPanel(self.ctx)
        # "‹ Back" doesn't stop playback, same as leaving Now Playing
        # doesn't stop music - the persistent PlayerBar keeps transport
        # (see video_panel.py's docstring).
        self.player_panel.backRequested.connect(self._on_player_back)
        return self.player_panel

    # -- data loading -------------------------------------------------------

    def refresh(self) -> None:
        if not self._loaded:
            self._load_videos()

    def _on_videos_changed(self) -> None:
        self._loaded = False
        self.refresh()

    def _on_details_progress(self, done: int, total: int) -> None:
        """ctx.videoDetailsProgress - pick up the pictures found so far
        (only if this page has been loaded; otherwise its first visit
        reads them anyway). (0, 0) = finished."""
        self._details_status = (
            f"finding pictures {done:,} of {total:,}" if total and done < total else "")
        if self._loaded:
            self._load_videos(keep_scroll=True)

    def _load_videos(self, keep_scroll: bool = False) -> None:
        rows: list[VideoRow] = []
        with self.ctx.session() as session:
            for v in vid_svc.list_videos_for_search(session):
                rows.append(VideoRow(
                    key=v["video_id"],
                    title=v["title"],
                    artist=v["artist_name"] or "",
                    year=v["year"],
                    duration_ms=v["duration_ms"],
                    sort_key=v["sort_key"],
                    thumb=v["thumbnail_path"],
                    fallback_art=v["artist_image_path"],
                    media_type=v["media_type"],
                    added_rank=v["added_rank"],
                ))
        self._all_rows = rows
        self._loaded = True
        self._show_tab(keep_scroll)

    def _tab_rows(self, tab: str) -> list[VideoRow]:
        return [r for r in self._all_rows if r.media_type == tab]

    def _show_tab(self, keep_scroll: bool = False) -> None:
        """Feed the active tab's videos to both layouts and update the
        counts - the tab labels carry each tab's size, the stats line the
        active tab's count and total running time."""
        for tab, chip in self.tab_chips.items():
            chip.setText(f"{TAB_NAMES[tab][0]} ({len(self._tab_rows(tab)):,})")
        rows = self._tab_rows(self._tab)
        self.grid.set_rows(rows, keep_scroll=keep_scroll)
        self.table.set_rows(rows, keep_scroll=keep_scroll)
        total_ms = sum(r.duration_ms or 0 for r in rows)
        _label, one, many = TAB_NAMES[self._tab]
        text = f"{len(rows):,} {one if len(rows) == 1 else many} · {format_duration(total_ms)}"
        if self._details_status:
            text += f" · {self._details_status}"
        self.stats.setText(text)
        self._refresh_jump_bar()

    def set_tab(self, tab: str) -> None:
        """Music Videos / Movies. The search text carries over."""
        self._tab = tab
        for t, chip in self.tab_chips.items():
            chip.setChecked(t == tab)
        self._show_tab()

    @property
    def tab(self) -> str:
        return self._tab

    def set_layout(self, layout: int) -> None:
        """Grid / List."""
        self.layouts.setCurrentIndex(layout)
        self.grid_chip.setChecked(layout == LAYOUT_GRID)
        self.list_chip.setChecked(layout == LAYOUT_LIST)
        self._refresh_jump_bar()

    def _browser(self):
        """Whichever of the grid / list is showing."""
        return self.grid if self.layouts.currentIndex() == LAYOUT_GRID else self.table

    def _on_search_changed(self, text: str) -> None:
        self.grid.set_filter_text(text)
        self.table.set_filter_text(text)

    def _on_letter_key(self, letter: str) -> None:
        """JumpBar.letterPicked - scrolls the grid/list straight to this
        letter."""
        self._browser().scroll_to_letter(letter)

    def _on_letter_typed(self, letter: str) -> None:
        """JumpBar.letterTyped - the literal letter tapped, spelled into
        the search box the same way LibraryView._on_letter_typed does,
        since there's no physical keyboard to type one with here either.
        '#' isn't a real character, so it's never typed, only used for
        its usual jump."""
        if letter != "#":
            self.search_box.insert(letter)

    def _refresh_jump_bar(self) -> None:
        if self.panes.currentIndex() == PANE_PLAYER:
            return  # stays hidden with the rest of the browse chrome
        letters = self._browser().jump_letters()
        self.jump_bar.setVisible(bool(letters))
        if letters:
            self.jump_bar.set_available(letters)

    def _set_browse_chrome_visible(self, visible: bool) -> None:
        """Show/hide everything above the panes - the page title, the
        stats, the tab row, the search row and the A-Z jump bar.

        All of it exists to browse, which isn't on screen while the player
        pane is up, so hiding it hands that height to the video (hidden
        widgets don't reserve space in Qt). Everything comes back on the
        way out (`_show_browse`).
        """
        self.title.setVisible(visible)
        self.stats.setVisible(visible)
        self.tab_row.setVisible(visible)
        self.search_row.setVisible(visible)
        if visible:
            # not a bare setVisible(True) - the jump bar has its own reason
            # to stay hidden (too few letters to be worth a bar, see
            # _refresh_jump_bar), which this must not override.
            self._refresh_jump_bar()
        else:
            self.jump_bar.setVisible(False)

    def _show_browse(self) -> None:
        self._opened_here = False
        self.panes.setCurrentIndex(PANE_BROWSE)
        self._set_browse_chrome_visible(True)

    # -- navigation -----------------------------------------------------------

    def _open_video(self, video_id: int) -> None:
        """A tile/row tapped on this page."""
        if self._play(video_id):
            self._opened_here = True

    def _open_video_from_elsewhere(self, video_id: int) -> None:
        """ctx.playVideoRequested - a Library search match. "‹ Back" goes
        back there, not to this page's grid."""
        if self._play(video_id):
            self._opened_here = False

    def _play(self, video_id: int) -> bool:
        with self.ctx.session() as session:
            video = session.get(Video, video_id)
            if video is None:
                return False
            missing, path = video.is_missing, video.path
            title, artist, vid = video.title, video.artist_display, video.id

        if missing or not os.path.exists(path):
            self.ctx.notify("That video file is missing")
            return False
        self.player_panel.play(vid, path, title, artist)
        self.panes.setCurrentIndex(PANE_PLAYER)
        self._set_browse_chrome_visible(False)
        return True

    def _on_player_back(self) -> None:
        if self._opened_here:
            self._show_browse()
        else:
            # leave the page ready to browse for its next visit
            self._show_browse()
            self.ctx.videosBackRequested.emit()

    def _focus_artist(self, artist: str) -> None:
        """A collapsed "N videos" tile from a Library search - no single
        video to play, so this lands on the List layout, grouped by Artist
        and scrolled to that artist's section, on whichever tab has their
        videos (Music Videos first)."""
        self.refresh()
        kinds = {r.media_type for r in self._all_rows if r.artist == artist}
        if kinds and self._tab not in kinds:
            self.set_tab(TAB_MUSIC_VIDEOS if TAB_MUSIC_VIDEOS in kinds else TAB_MOVIES)
        self.set_layout(LAYOUT_LIST)
        self._show_browse()
        self.table.focus_artist(artist)
