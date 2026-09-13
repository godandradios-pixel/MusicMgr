"""Full-screen Now Playing: big artwork, track metadata/chart history, synced
lyrics, and the live queue. Transport (play/pause, prev/next, skip) and the
seek bar both live solely on the persistent PlayerBar at the bottom of the
window now - see ui/widgets/player_bar.py. The decorative pulse visualizer
that used to sit under the artwork here moved there too (2026-09-06, James:
"I don't like that visualizer anymore. I was thinking more of a full one
across the bottom" - see ui/widgets/visualizer.py's module docstring), so
this view no longer builds or drives one of its own.

2026-09-06 follow-up (James: Title Details double-click-to-play going
"Not Responding"/"basically unusable"): `_refresh_queue()` used to rebuild
this view's queue `TouchList` - a real widget item per queued track - on
every single `PlayerController.trackChanged`/`queueChanged` signal, with
no regard for whether Now Playing was even the page on screen. Title
Details' "queue what's on screen" rule (LibraryView._play_from_details)
can hand the player a queue of the *entire* library - tens of thousands of
tracks - so that rebuild ran and froze the whole app even while sitting on
a completely different screen. Profiling a 98,044-item queue found this
rebuild alone cost ~2.5 seconds of synchronous UI-thread work. Fixed with
`_is_current_page()`, which checks `QStackedWidget.currentWidget()` rather
than `QWidget.isVisible()` (isVisible() needs the whole top-level window
shown, which this project's headless tests never do) and gates
`_refresh_queue()`'s expensive body behind it; `refresh()` (called by
MainWindow.navigate() the moment the person actually arrives here) still
rebuilds it correctly on arrival.

2026-09-07 follow-up (James: "I also want to be able to click to add a
jukebox entry from the track detail page"): a `JukeboxToggle` sits right
next to the rating stars now (mirroring Title Details' own Jukebox column,
which existed from 2026-09-07 to 2026-09-13 - see track_details_table.py's
module docstring for that column's whole arc, including its removal) so
the track that's actually playing can be loaded onto or pulled off the
board without leaving this page - see `_on_jukebox_toggled` and
`JukeboxToggle`'s own docstring in widgets/common.py. This toggle is
unaffected by that column's removal; Now Playing is now one of only two
remaining places to add a track to the jukebox board (the other being the
Jukebox page's own "+ Add to jukebox" picker)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from ...db.models import Chart, ChartEntry, ChartIssue, Track
from ...services import jukebox as jukebox_svc
from ...services import library as lib
from ...services.library import format_duration
from ...services.player import QueueItem
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import (
    ChipButton,
    CoverArt,
    JukeboxToggle,
    StarRating,
    TouchButton,
    TouchList,
    dim_label,
)
from ..widgets.lyrics_panel import LyricsPanel
from .base import BaseView
from .jukebox import JukeboxPickerDialog


class NowPlayingView(BaseView):
    title_text = "Now Playing"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)

        # the currently-playing track's id and the artist/release ids the
        # tappable artist name/album art jump to - QueueItem itself carries
        # none of these (see services/player.py:QueueItem), so they're
        # looked up once per track change (_load_track_meta) and kept here
        # for the mousePressEvent handlers and the rating widget to read at
        # tap time. None whenever nothing is playing.
        self._current_track_id: Optional[int] = None
        self._current_artist_id: Optional[int] = None
        self._current_release_id: Optional[int] = None

        # Now Playing is a top-level nav destination like Library/Videos/
        # Charts/Playlists, but unlike those it's usually reached mid-browse
        # (tapping the persistent bar's "Now Playing"/Queue button) rather
        # than as somewhere you meant to go - so, unlike its siblings, it
        # needs a way back to whatever you were actually doing.
        # ctx.nowPlayingBackRequested is a generic request rather than this
        # view choosing a target itself: it doesn't know what was on screen
        # before it, only MainWindow does (see MainWindow.navigate/
        # _go_back_from_nowplaying).
        back = TouchButton("‹ Back")
        back.clicked.connect(ctx.nowPlayingBackRequested.emit)
        self.header.insertWidget(0, back)

        clear = TouchButton("Clear queue")
        clear.clicked.connect(ctx.player.clear_queue)
        self.header.addWidget(clear)

        row = QHBoxLayout()
        row.setSpacing(28)

        # ---- left: artwork and metadata ----
        left = QVBoxLayout()
        left.setSpacing(14)
        left.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        # tappable, jumping to the album's page - the same mousePressEvent-
        # override trick player_bar.py's own cover thumbnail already uses to
        # reach Now Playing (James: "tapping 'Arrival' could jump to the
        # album - quick way to browse from what's playing", 2026-09-06)
        self.cover = CoverArt(340)
        self.cover.setCursor(Qt.PointingHandCursor)
        self.cover.mousePressEvent = lambda _e: self._open_current_release()
        left.addWidget(self.cover, 0, Qt.AlignHCenter)

        self.track_title = QLabel("Nothing playing")
        self.track_title.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.track_title.setAlignment(Qt.AlignCenter)
        self.track_title.setWordWrap(True)
        # tappable - jumps to the artist's page (James: "tapping the artist
        # name could jump to their page", 2026-09-06). Same trick as
        # self.cover above.
        self.track_artist = QLabel("")
        self.track_artist.setStyleSheet(f"font-size: 17px; color: {COLORS['text_dim']};")
        self.track_artist.setAlignment(Qt.AlignCenter)
        self.track_artist.setCursor(Qt.PointingHandCursor)
        self.track_artist.mousePressEvent = lambda _e: self._open_current_artist()
        # tappable - jumps to the album's page, same as self.cover (both
        # exist as separate tap targets: the art is bigger, the label is
        # readable when there's no art)
        self.track_album = QLabel("")
        self.track_album.setObjectName("Subtitle")
        self.track_album.setAlignment(Qt.AlignCenter)
        self.track_album.setCursor(Qt.PointingHandCursor)
        self.track_album.mousePressEvent = lambda _e: self._open_current_release()
        for w in (self.track_title, self.track_artist, self.track_album):
            left.addWidget(w)

        # rating stars for the track that's actually playing - Title Details
        # already has a tappable Rating column (track_details_table.py's
        # RatingDelegate); James asked for "the same stars under the track
        # title on Now Playing" so a track can be rated in the moment
        # without going to find it in the library (2026-09-06).
        self.rating_stars = StarRating(star_size=26)
        self.rating_stars.ratingChanged.connect(self._on_rating_changed)
        # jukebox toggle alongside the stars (2026-09-07 follow-up, James:
        # "I also want to be able to click to add a jukebox entry from the
        # track detail page") - Title Details' own Jukebox column's
        # counterpart here, so the track that's actually playing can be
        # loaded onto (or pulled off) the board without leaving this page.
        self.jukebox_toggle = JukeboxToggle()
        self.jukebox_toggle.toggled.connect(self._on_jukebox_toggled)
        # a plain QWidget container (not addLayout directly) so the whole
        # row can be centered as one unit the same way `self.rating_stars`
        # alone used to be (`addWidget(..., 0, Qt.AlignHCenter)`) - both
        # StarRating and JukeboxToggle size to their own content, and
        # centering the container keeps them hugging each other in the
        # middle rather than spreading across left's full column width.
        rating_container = QWidget()
        rating_row = QHBoxLayout(rating_container)
        rating_row.setContentsMargins(0, 0, 0, 0)
        rating_row.setSpacing(20)
        rating_row.addWidget(self.rating_stars)
        rating_row.addWidget(self.jukebox_toggle)
        left.addWidget(rating_container, 0, Qt.AlignHCenter)

        self.chart_note = QLabel("")
        self.chart_note.setObjectName("Subtitle")
        self.chart_note.setAlignment(Qt.AlignCenter)
        self.chart_note.setWordWrap(True)
        left.addWidget(self.chart_note)

        # Transport (play/pause, prev/next, -15s/+30s) and the seek bar both
        # live only on the persistent PlayerBar at the bottom of the window
        # now - having a second copy of either here just duplicated it on
        # screen.
        # the artwork column doesn't need as much width as the queue/lyrics
        # column - its content (a 340px cover, centered text) stays the same
        # size regardless of how wide its column is, so giving it the bigger
        # share just left a lot of dead margin on either side of it while the
        # lyrics/queue list was squeezed narrower than it needed
        row.addLayout(left, 2)

        # ---- right: queue / lyrics ----
        right = QVBoxLayout()
        right.setSpacing(8)
        qhead = QHBoxLayout()
        qhead.setSpacing(8)
        self._right_chips = QButtonGroup(self)
        self._right_chips.setExclusive(True)
        queue_chip = ChipButton("Up next")
        queue_chip.setChecked(True)
        lyrics_chip = ChipButton("Lyrics")
        for chip in (queue_chip, lyrics_chip):
            self._right_chips.addButton(chip)
            qhead.addWidget(chip)
        qhead.addStretch(1)
        self.queue_meta = dim_label("")
        qhead.addWidget(self.queue_meta)
        right.addLayout(qhead)

        self.right_panes = QStackedWidget()

        queue_pane = QWidget()
        queue_layout = QVBoxLayout(queue_pane)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(8)
        self.queue_list = TouchList()
        self.queue_list.itemActivatedPayload.connect(self._on_queue_tapped)
        queue_layout.addWidget(self.queue_list, 1)
        remove = TouchButton("Remove selected")
        remove.clicked.connect(self._remove_selected)
        queue_layout.addWidget(remove)
        self.right_panes.addWidget(queue_pane)  # 0

        self.lyrics_panel = LyricsPanel()
        self.right_panes.addWidget(self.lyrics_panel)  # 1

        right.addWidget(self.right_panes, 1)
        self._right_chips.buttonClicked.connect(
            lambda btn: self.right_panes.setCurrentIndex(
                0 if btn is queue_chip else 1
            )
        )
        row.addLayout(right, 3)

        self.body().addLayout(row, 1)

        ctx.player.trackChanged.connect(self._on_track_changed)
        ctx.player.queueChanged.connect(self._refresh_queue)
        ctx.player.positionChanged.connect(self._on_position)

    # -- slots ---------------------------------------------------------------

    def _is_current_page(self) -> bool:
        """True while this is the page `MainWindow.navigate()` last
        switched the shared `QStackedWidget` to - see `_refresh_queue`.
        Deliberately not `self.isVisible()`: that also needs the whole
        top-level window to be shown, which headless tests (and, briefly,
        a window that hasn't been shown yet) never satisfy, whereas
        `QStackedWidget.addWidget(view)` in app.py makes `self.parentWidget
        ()` that stack directly, so `currentWidget()` reflects which page
        is logically active regardless of real on-screen visibility."""
        parent = self.parentWidget()
        return parent is not None and parent.currentWidget() is self

    def refresh(self) -> None:
        self._on_track_changed(self.ctx.player.current)
        self._refresh_queue()

    def _on_track_changed(self, item: Optional[QueueItem]) -> None:
        if item is None:
            self._current_track_id = None
            self._current_artist_id = None
            self._current_release_id = None
            self.track_title.setText("Nothing playing")
            self.track_artist.setText("")
            self.track_album.setText("")
            self.chart_note.setText("")
            self.cover.set_source(None, "")
            self.rating_stars.set_rating(0)
            self.jukebox_toggle.set_on(False)
            self.lyrics_panel.load_for_path(None)
            self._refresh_queue()
            return
        self._current_track_id = item.track_id
        self.track_title.setText(item.title)
        self.track_artist.setText(item.artist)
        self.track_album.setText(item.album)
        self.cover.set_source(item.cover_path, item.album or item.artist)
        note, rating, on_jukebox, artist_id, release_id = self._load_track_meta(item.track_id)
        self.chart_note.setText(note)
        self.rating_stars.set_rating(rating)
        self.jukebox_toggle.set_on(on_jukebox)
        self._current_artist_id = artist_id
        self._current_release_id = release_id
        self.lyrics_panel.load_for_path(
            item.path,
            title=item.title,
            artist=item.artist,
            album=item.album,
            duration_ms=item.duration_ms,
        )
        self._refresh_queue()

    def _load_track_meta(
        self, track_id: int
    ) -> tuple[str, int, bool, Optional[int], Optional[int]]:
        """One query for everything `_on_track_changed` needs beyond what
        `QueueItem` already carries: the chart-history note (formerly
        `_chart_history`), the track's rating (mirroring Title Details'
        Rating column - see `StarRating`), whether it's currently on the
        jukebox board (mirroring Title Details' Jukebox column - see
        `JukeboxToggle`, 2026-09-07 follow-up), and the artist/release ids
        the tappable artist name/album art jump to (2026-09-06). Returns
        `(chart_note, rating, on_jukebox, artist_id, release_id)`; rating is
        0, on_jukebox is False, and the ids are None wherever there's
        nothing to show/jump to."""
        notes = []
        rating = 0
        on_jukebox = False
        artist_id: Optional[int] = None
        release_id: Optional[int] = None
        with self.ctx.session() as session:
            stmt = (
                select(Chart.name, ChartEntry.rank, ChartEntry.peak_pos)
                .join(ChartIssue, ChartIssue.chart_id == Chart.id)
                .join(ChartEntry, ChartEntry.issue_id == ChartIssue.id)
                .where(ChartEntry.track_id == track_id)
            )
            best: dict[str, int] = {}
            weeks: dict[str, int] = {}
            for name, rank, peak in session.execute(stmt):
                value = min(x for x in (rank, peak) if x)
                best[name] = min(best.get(name, 999), value)
                weeks[name] = weeks.get(name, 0) + 1
            for name, peak in best.items():
                notes.append(f"{name}: peaked at #{peak} · {weeks[name]} week(s) charted")
            track = session.get(Track, track_id)
            if track is not None:
                if track.play_count:
                    notes.append(f"Played {track.play_count} time(s) from this library")
                rating = track.rating or 0
                release_id = track.release_id
                if track.release is not None:
                    artist_id = track.release.album_artist_id
            on_jukebox = jukebox_svc.find_code_for_track(session, track_id) is not None
        return "\n".join(notes), rating, on_jukebox, artist_id, release_id

    def _open_current_artist(self) -> None:
        """The tappable artist name/portrait - jumps straight to that
        artist's Library page (James, 2026-09-06). A no-op when nothing's
        playing or the release has no artist on file (album_artist_id can be
        NULL - see db/models.py:Release.album_artist_id)."""
        if self._current_artist_id is None:
            return
        self.ctx.navigateRequested.emit("library")
        self.ctx.openArtistRequested.emit(self._current_artist_id)

    def _open_current_release(self) -> None:
        """The tappable album art/title - jumps straight to that release's
        Library page (James, 2026-09-06). A no-op when nothing's playing."""
        if self._current_release_id is None:
            return
        self.ctx.navigateRequested.emit("library")
        self.ctx.openReleaseRequested.emit(self._current_release_id)

    def _on_rating_changed(self, rating: int) -> None:
        """A star tapped on `self.rating_stars` - persists exactly like
        Title Details' own Rating column does (services/library.py's
        `set_track_rating`, the same function `LibraryView._on_rating_changed`
        calls), so a track rated from here or there always agrees."""
        if self._current_track_id is None:
            return
        with self.ctx.session() as session:
            lib.set_track_rating(session, self._current_track_id, rating)

    def _on_jukebox_toggled(self) -> None:
        """`self.jukebox_toggle` tapped - mirrors Title Details' own
        Jukebox column (`LibraryView._on_jukebox_toggle_requested`) so a
        track toggled from here or there always agrees. Unlike
        `_on_rating_changed`, this can't be fire-and-forget.

        2026-09-13 follow-up (see `LibraryView._on_jukebox_toggle_requested`'s
        own docstring for the full story): turning OFF is still an
        immediate flip, but turning ON now opens the standard
        `JukeboxPickerDialog` pre-filled and pre-checked for the current
        track, instead of silently filing it under the default genre via
        `services/library.py:toggle_jukebox_membership`."""
        if self._current_track_id is None:
            return
        track_id = self._current_track_id
        with self.ctx.session() as session:
            track = session.get(Track, track_id)
            if track is None:
                return
            if jukebox_svc.find_code_for_track(session, track_id) is not None:
                jukebox_svc.remove_track(session, track_id)
                self.jukebox_toggle.set_on(False)
                return
            artist_id = lib.album_artist_id_for_track(session, track)
            title = track.title
            release = track.release
            artist_name = (
                release.album_artist.name
                if artist_id is not None and release is not None and release.album_artist is not None
                else ""
            )
        if artist_id is None:
            self.ctx.notify("This track has no album artist to file a jukebox slot under")
            return
        dialog = JukeboxPickerDialog(
            self,
            self._search_addable_tracks,
            genres=jukebox_svc.JUKEBOX_GENRES,
            default_genre=jukebox_svc.DEFAULT_JUKEBOX_GENRE,
            initial_artist_query=artist_name,
            initial_track_query=title,
        )
        dialog.check_track(track_id, artist_id)
        if dialog.exec() != QDialog.Accepted:
            return
        picks = dialog.selected_picks()
        genre = dialog.selected_genre() or jukebox_svc.DEFAULT_JUKEBOX_GENRE
        if not picks:
            return
        with self.ctx.session() as session:
            for pick_track_id, pick_artist_id in picks:
                jukebox_svc.place_track(session, pick_artist_id, pick_track_id, genre=genre)
        # only the currently-playing track has a glyph on this page to
        # update - an extra pick made from this same dialog (MAX_PICKS
        # allows a second song) has no on-screen representation here the
        # way it would in Title Details' own table
        if any(pt == track_id for pt, _ in picks):
            self.jukebox_toggle.set_on(True)

    def _search_addable_tracks(self, artist_query: str, track_query: str) -> list[dict]:
        """`JukeboxPickerDialog`'s `search_tracks` callback - see
        `ui/views/jukebox.py:JukeboxView._search_addable_tracks`, the same
        pattern for the same dialog's other call sites."""
        with self.ctx.session() as session:
            return jukebox_svc.search_addable_tracks(session, artist_query, track_query)

    def _on_position(self, ms: int) -> None:
        self.lyrics_panel.update_position(ms)

    def _refresh_queue(self) -> None:
        if not self._is_current_page():
            # 2026-09-06 follow-up (James: Title Details going "Not
            # Responding"/"basically unusable" double-clicking a track):
            # this ran unconditionally on every trackChanged/queueChanged,
            # rebuilding a real QListWidgetItem per queued track - fine at
            # a few dozen, but Title Details' own "queue what's on screen"
            # rule (see LibraryView._play_from_details) can hand the
            # player a queue of the *entire* library, tens of thousands of
            # items, and this view doesn't even need to be the one on
            # screen for that rebuild to run and freeze the whole app.
            # refresh() (called by MainWindow.navigate() the moment the
            # person actually arrives here) rebuilds it properly then.
            return
        player = self.ctx.player
        current_index = player.current_index
        rows = []
        total_ms = 0
        for idx, item in enumerate(player.queue):
            total_ms += item.duration_ms or 0
            playing = idx == current_index
            rows.append({
                "lead": "♪" if playing else str(idx + 1),
                # 2026-09-13 follow-up (see ui/theme.py's #Primary comment
                # for this whole cleanup) - the currently-playing row's
                # highlight, walnut brown now instead of red-orange.
                "lead_color": COLORS["jukebox_key_hi"] if playing else COLORS["text_dim"],
                "primary": item.title,
                "secondary": item.artist,
                "trail": format_duration(item.duration_ms),
                "bold": playing,
                "color": COLORS["jukebox_key_hi"] if playing else COLORS["text"],
                "index": idx,
            })
        self.queue_list.set_rows(rows)
        self.queue_meta.setText(
            f"{len(rows)} tracks · {format_duration(total_ms)}" if rows else "empty"
        )
        if 0 <= current_index < len(rows):
            self.queue_list.setCurrentRow(current_index)
            self.queue_list.scrollToItem(self.queue_list.item(current_index))

    def _on_queue_tapped(self, payload: Optional[dict]) -> None:
        if payload and payload.get("index") is not None:
            self.ctx.player.jump_to(payload["index"])

    def _remove_selected(self) -> None:
        payload = self.queue_list.current_payload()
        if payload and payload.get("index") is not None:
            self.ctx.player.remove_at(payload["index"])
