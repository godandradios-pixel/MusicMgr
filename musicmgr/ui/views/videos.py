"""Videos browser: a sortable, groupable table (MusicBee-style), tap a row to
open the embedded player pane with an in-app full-screen toggle.

The table itself lives in `ui/widgets/video_table.py` (columns, sorting,
group-by - see its docstring for why Title/Artist/Year/Time rather than
MusicBee's Album/Genre/Rating).

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

from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QStackedWidget, QWidget

from ...db.models import Video
from ...services import videos as vid_svc
from ...services.library import format_duration
from ..context import AppContext
from ..widgets.common import TouchButton, dim_label
from ..widgets.cover_grid import JumpBar
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

        # no always-on hint row: the embedded player's height is whatever
        # body() leaves, so every row here costs the picture.
        # same "Search this view…" + Space/⌫/✕ shape as LibraryView's own
        # search row. Matches by title OR artist (see
        # VideoTable.set_filter_text) rather than a "search by" picker.
        # In a widget of its own so the whole row can be hidden in one call
        # while the player pane is up - Qt reclaims a hidden widget's space,
        # but a layout of individually-hidden widgets leaves its spacing.
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

        self.panes = QStackedWidget()
        self.panes.addWidget(self._build_table())   # 0
        self.panes.addWidget(self._build_player())  # 1
        self.body().addWidget(self.panes, 1)
        self.table.updated.connect(self._refresh_jump_bar)

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
        # "‹ Back" asks MainWindow to return to whatever section was active
        # before a search match routed here (ctx.videosBackRequested - see
        # its docstring), not to this view's table: a single-video match
        # (_open_video) never visited the table. Doesn't stop playback, same
        # as leaving Now Playing doesn't stop music - the persistent
        # PlayerBar keeps transport (see video_panel.py's docstring).
        self.player_panel.backRequested.connect(self.ctx.videosBackRequested.emit)
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
                    thumb=video.thumbnail_path or None,
                ))
        self.table.set_rows(rows)
        plural = "" if len(rows) == 1 else "s"
        self.stats.setText(f"{len(rows)} video{plural} · {format_duration(total_ms)}")
        self._loaded = True

    def _on_search_changed(self, text: str) -> None:
        self.table.set_filter_text(text)

    def _on_letter_key(self, letter: str) -> None:
        """JumpBar.letterPicked - scrolls the table straight to this
        letter (see VideoTable.scroll_to_letter)."""
        self.table.scroll_to_letter(letter)

    def _on_letter_typed(self, letter: str) -> None:
        """JumpBar.letterTyped - the literal letter tapped, spelled into
        the search box the same way LibraryView._on_letter_typed does,
        since there's no physical keyboard to type one with here either.
        '#' isn't a real character, so it's never typed, only used for
        its usual jump."""
        if letter != "#":
            self.search_box.insert(letter)

    def _refresh_jump_bar(self) -> None:
        letters = self.table.jump_letters()
        self.jump_bar.setVisible(bool(letters))
        if letters:
            self.jump_bar.set_available(letters)

    def _set_browse_chrome_visible(self, visible: bool) -> None:
        """Show/hide everything above the panes - the page title, the
        "N videos · H:MM:SS" stats, the search row and the A-Z jump bar.

        All four exist to browse the *table*, which isn't on screen while
        the player pane is up, so hiding them hands that height to
        `video_widget` (hidden widgets don't reserve space in Qt).
        Everything comes back on the way out (`_focus_artist`, or the next
        visit to the table).
        """
        self.title.setVisible(visible)
        self.stats.setVisible(visible)
        self.search_row.setVisible(visible)
        if visible:
            # not a bare setVisible(True) - the jump bar has its own reason
            # to stay hidden (too few letters to be worth a bar, see
            # _refresh_jump_bar), which this must not override.
            self._refresh_jump_bar()
        else:
            self.jump_bar.setVisible(False)

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
        self._set_browse_chrome_visible(False)

    def _focus_artist(self, artist: str) -> None:
        """A collapsed "N videos" tile from a Library search -
        no single video to play, so this lands on the table itself, grouped
        by Artist and scrolled to that artist's section, rather than the
        player pane `_open_video` uses."""
        self.panes.setCurrentIndex(PANE_TABLE)
        self._set_browse_chrome_visible(True)
        self.table.focus_artist(artist)
