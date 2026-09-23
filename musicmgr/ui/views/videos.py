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

        # a "Add folders to scan from Settings…" hint used to sit here,
        # always shown regardless of whether any videos existed - removed
        # 2026-09-15 (James: reclaim that row of height "to allow the video
        # to be larger" - the embedded player's actual height is whatever's
        # left after everything else in body(), so one less always-on row
        # is real vertical room back for the picture).
        # same "Search this view…" + Space/⌫/✕ shape as LibraryView's own
        # search row (2026-09-15, James: "the search bar for videos to
        # include artists and track titles") - videos had no search at all
        # before this, unlike every Library presentation. Matches by title
        # OR artist (see VideoTable.set_filter_text) rather than needing a
        # separate "search by" picker the way Library's four presentations
        # each search their own field.
        # wrapped in a widget of its own (2026-09-23) rather than added to
        # body() as a bare layout, purely so the whole row can be hidden in
        # one call while the player pane is up - Qt reclaims a hidden
        # widget's space, but a layout full of individually-hidden widgets
        # still leaves its own spacing behind (the same reasoning
        # video_panel.py's `self.controls.setVisible(False)` already
        # follows).
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

        # 2026-09-15 same-day follow-up - James: "It's missing the
        # alphabet, how do I touch type letters to search without it" -
        # the search row above added Space/⌫/✕ but not this, and on a
        # touch panel with no physical keyboard this bar is the *only* way
        # to actually type a letter (tapping one spells it into the search
        # box) - see JumpBar's own docstring and LibraryView's module
        # docstring for the same reasoning there.
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
        # its own docstring), rather than dropping onto this view's own
        # table the way it briefly did while Videos had a real sidebar
        # entry (2026-09-15 to 2026-09-16): reached by a single-video match
        # (_open_video, below) that never visited the table at all, "back"
        # to it would be a step that was never taken - the same "actively
        # misleading" problem this button's very first design already
        # ran into once (see video_panel.py's own docstring). Doesn't stop
        # playback, same as leaving Now Playing doesn't stop music - the
        # persistent PlayerBar keeps transport for it either way (see
        # video_panel.py's docstring).
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

        2026-09-23, James: "allow the video window to be larger". All four
        of these exist to browse the *table*; while the player pane is up
        the table isn't on screen at all, so they were costing the picture
        roughly a third of the window for controls that had nothing to act
        on. Hidden widgets don't reserve space in Qt, so this is real
        height handed to `video_widget`'s stretch factor - the same trick
        the 2026-09-15 "Add folders to scan from Settings…" hint removal
        and video_panel.py's own hidden `controls` row already used, just
        applied to the whole browse chrome at once instead of one row.
        Everything comes straight back on the way out (`_focus_artist`, or
        the next visit to the table).
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
        """A collapsed "N videos" tile from a Library search (2026-09-06) -
        no single video to play, so this lands on the table itself, grouped
        by Artist and scrolled to that artist's section, rather than the
        player pane `_open_video` uses."""
        self.panes.setCurrentIndex(PANE_TABLE)
        self._set_browse_chrome_visible(True)
        self.table.focus_artist(artist)
