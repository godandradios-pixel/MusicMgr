"""Full-screen Now Playing: big artwork, track metadata/chart history, synced
lyrics, and the live queue. Transport (play/pause, prev/next, skip) and the
seek bar live only on the persistent PlayerBar at the bottom of the window
(ui/widgets/player_bar.py), as does the pulse visualizer.

The queue `TouchList` holds a real widget item per queued track, and Title
Details' "queue what's on screen" rule (LibraryView._play_from_details) can
queue the entire library - so `_refresh_queue()` only does its expensive
rebuild while this is the current page (`_is_current_page()`); `refresh()`,
called by MainWindow.navigate() on arrival, rebuilds it then.

A `JukeboxToggle` next to the rating stars loads the playing track onto
(or off) the jukebox board without leaving this page - see
`_on_jukebox_toggled`. This and the Jukebox page's "+ Add to jukebox"
picker are the two places a track is added to the board.

The Lyrics pane's "Edit lyrics" / "Sync lyrics myself" buttons open
`LyricsEditorDialog` for the playing track - see `_open_lyrics_editor` and
ui/widgets/lyrics_editor.py."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from ...db.models import Chart, ChartEntry, ChartIssue, RadioEpisode, Track
from ...services import tracknum
from ...services import updater
from ...services import heard as heard_svc
from ...services import jukebox as jukebox_svc
from ...services import library as lib
from ...services.library import format_duration
from ...services.player import QueueItem
from ..context import AppContext
from ..theme import COLORS, make_compact
from ..widgets.common import (
    ChipButton,
    CoverArt,
    JukeboxToggle,
    StarRating,
    TouchButton,
    TouchList,
    dim_label,
)
from ..widgets.lyrics_editor import LyricsEditorDialog
from ..widgets.lyrics_panel import LyricsPanel
from ..widgets.table_radio import TableRadio
from ..widgets.turntable import RPM_78, RPM_LP, Turntable
from .base import BaseView
from .jukebox import JukeboxPickerDialog

#: Settings key - Now Playing shows the turntable (1, default) or the flat
#: album cover (0); see set_turntable_shown
PREF_TURNTABLE = "nowplaying_turntable"


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

        # Now Playing is usually reached mid-browse (the persistent bar's
        # "Now Playing"/Queue button), so unlike its sibling nav pages it
        # needs a way back. ctx.nowPlayingBackRequested is a generic request:
        # only MainWindow knows what was on screen before (see
        # MainWindow.navigate/_go_back_from_nowplaying).
        back = self.back_button = TouchButton("‹ Back")
        back.clicked.connect(ctx.nowPlayingBackRequested.emit)
        self.header.insertWidget(0, back)

        row = QHBoxLayout()
        row.setSpacing(28)

        # ---- left: artwork and metadata ----
        left = QVBoxLayout()
        left.setSpacing(14)
        left.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        # tappable, jumping to the album's page - the same mousePressEvent-
        # override trick player_bar.py's own cover thumbnail uses to reach
        # Now Playing
        self.cover = CoverArt(340)
        self.cover.setCursor(Qt.PointingHandCursor)
        self.cover.mousePressEvent = lambda e: (
            self._open_current_release() if e.button() == Qt.LeftButton else None)
        # The turntable (ui/widgets/turntable.py) - the album cover spinning
        # on a record player - is shown instead of
        # the flat cover by default; right-click or hold either one to
        # switch, remembered per PC. A tap still opens the album.
        self.turntable = Turntable(340)
        self.turntable.setCursor(Qt.PointingHandCursor)
        self.turntable.mousePressEvent = lambda e: (
            self._open_current_release() if e.button() == Qt.LeftButton else None)
        # A spinning record makes no sense for a station, so a station or
        # an old-time radio show shows a tabletop radio
        # instead (ui/widgets/table_radio.py); tapping it opens the Radio
        # page. The same right-click switches it to the flat logo.
        self.table_radio = TableRadio(340)
        self.table_radio.setCursor(Qt.PointingHandCursor)
        self.table_radio.mousePressEvent = lambda e: (
            ctx.navigateRequested.emit("radio") if e.button() == Qt.LeftButton else None)
        for art in (self.cover, self.turntable, self.table_radio):
            art.contextMenuEvent = lambda e: self._art_menu().popup(e.globalPos())
        self.art_stack = QStackedWidget()
        self.art_stack.setFixedSize(340, 340)
        self.art_stack.addWidget(self.cover)
        self.art_stack.addWidget(self.turntable)
        self.art_stack.addWidget(self.table_radio)
        with ctx.session() as db:
            self._show_turntable = updater.get_bool_pref(db, PREF_TURNTABLE, True)
        self._on_radio_item = False
        #: songs each station sent since it was tuned in (services/heard.py)
        self._heard = heard_svc.HeardLog()
        self._station_id: Optional[int] = None
        self._apply_art()
        left.addWidget(self.art_stack, 0, Qt.AlignHCenter)

        self.track_title = QLabel("Nothing playing")
        self.track_title.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.track_title.setAlignment(Qt.AlignCenter)
        self.track_title.setWordWrap(True)
        # tappable - jumps to the artist's page. Same trick as self.cover
        # above.
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

        # rating stars for the playing track - the same stars as Title
        # Details' Rating column (track_details_table.py's RatingDelegate),
        # so a track can be rated in the moment.
        self.rating_stars = StarRating(star_size=26)
        self.rating_stars.ratingChanged.connect(self._on_rating_changed)
        # jukebox toggle alongside the stars, so the playing track can be
        # loaded onto (or pulled off) the board without leaving this page.
        self.jukebox_toggle = JukeboxToggle()
        self.jukebox_toggle.toggled.connect(self._on_jukebox_toggled)
        # a plain QWidget container (not addLayout directly) so the row is
        # centered as one unit: StarRating and JukeboxToggle size to their
        # content, and centering the container keeps them together in the
        # middle rather than spread across the column.
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

        # no transport here - it lives only on the PlayerBar.
        # the artwork column's content (a 340px cover, centered text) is a
        # fixed size, so it gets the smaller share and the queue/lyrics
        # column the larger
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
        # while a station plays, the only tab - what it's played
        # since it was tuned in (services/heard.py)
        heard_chip = ChipButton("Heard on this station")
        heard_chip.setVisible(False)
        self.queue_chip, self.lyrics_chip, self.heard_chip = queue_chip, lyrics_chip, heard_chip
        for chip in (queue_chip, lyrics_chip, heard_chip):
            self._right_chips.addButton(chip)
            qhead.addWidget(chip)
        qhead.addStretch(1)

        # the tab row's right end is context-sensitive: a stack swapped by
        # the same chips as `right_panes` below shows the queue's track
        # count/duration and "Clear queue" on Up next, and "Edit lyrics" on
        # Lyrics.
        self.right_actions = QStackedWidget()

        queue_actions = QWidget()
        qa_layout = QHBoxLayout(queue_actions)
        qa_layout.setContentsMargins(0, 0, 0, 0)
        qa_layout.setSpacing(12)
        self.queue_meta = dim_label("")
        qa_layout.addWidget(self.queue_meta)
        # radio (services/radio.py): an endless mix seeded by
        # whatever's playing; the same button stops it
        self.radio_button = make_compact(TouchButton("Start radio"))
        self.radio_button.setToolTip("Keep playing songs like this one")
        self.radio_button.setEnabled(False)
        self.radio_button.clicked.connect(self._toggle_radio)
        qa_layout.addWidget(self.radio_button)
        # wide enough for "Stop radio" so the label swap doesn't jiggle the row
        self.radio_button.setMinimumWidth(self.radio_button.sizeHint().width() + 12)
        self.clear_queue_button = make_compact(TouchButton("Clear queue"))
        self.clear_queue_button.clicked.connect(ctx.player.clear_queue)
        qa_layout.addWidget(self.clear_queue_button)
        self.right_actions.addWidget(queue_actions)  # 0

        self.lyrics_panel = LyricsPanel(inline_edit=False)
        self.lyrics_panel.editRequested.connect(self._open_lyrics_editor)
        lyrics_actions = QWidget()
        la_layout = QHBoxLayout(lyrics_actions)
        la_layout.setContentsMargins(0, 0, 0, 0)
        la_layout.addStretch(1)
        la_layout.addWidget(self.lyrics_panel.edit_button)
        self.right_actions.addWidget(lyrics_actions)  # 1

        heard_actions = QWidget()
        ha_layout = QHBoxLayout(heard_actions)
        ha_layout.setContentsMargins(0, 0, 0, 0)
        ha_layout.addStretch(1)
        self.heard_meta = dim_label("")
        ha_layout.addWidget(self.heard_meta)
        self.right_actions.addWidget(heard_actions)  # 2

        qhead.addWidget(self.right_actions)

        # The chip row sits in the page header, beside "‹ Back" / "Now
        # Playing", leaving more room for the lyrics. The header is two
        # columns with the same 2:3 split and 28px gap as `row` below, so
        # the chips line up over the right column.
        self.right_head = QWidget()
        self.right_head.setLayout(qhead)
        qhead.setContentsMargins(0, 0, 0, 0)
        while self.header.count():
            self.header.takeAt(0)
        head_left = QWidget()
        hl_layout = QHBoxLayout(head_left)
        hl_layout.setContentsMargins(0, 0, 0, 0)
        hl_layout.setSpacing(12)
        hl_layout.addWidget(back)
        hl_layout.addWidget(self.title)
        hl_layout.addStretch(1)
        self.header.setSpacing(28)
        self.header.addWidget(head_left, 2)
        self.header.addWidget(self.right_head, 3)

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

        self.right_panes.addWidget(self.lyrics_panel)  # 1

        heard_pane = QWidget()
        heard_layout = QVBoxLayout(heard_pane)
        heard_layout.setContentsMargins(0, 0, 0, 0)
        heard_layout.setSpacing(8)
        self.heard_list = TouchList()
        self.heard_list.itemActivatedPayload.connect(lambda _p: self._sync_heard_button())
        heard_layout.addWidget(self.heard_list, 1)
        self.heard_add = TouchButton(f"♥  Add to “{heard_svc.PLAYLIST_NAME}”")
        self.heard_add.setToolTip("Find the selected song in your library and add it to that playlist")
        self.heard_add.clicked.connect(self._add_heard_to_playlist)
        heard_layout.addWidget(self.heard_add)
        self.right_panes.addWidget(heard_pane)  # 2

        right.addWidget(self.right_panes, 1)

        def _switch_right(btn) -> None:
            idx = {queue_chip: 0, lyrics_chip: 1, heard_chip: 2}.get(btn, 0)
            self.right_panes.setCurrentIndex(idx)
            self.right_actions.setCurrentIndex(idx)

        self._switch_right = _switch_right

        self._right_chips.buttonClicked.connect(_switch_right)
        row.addLayout(right, 3)

        self.body().addLayout(row, 1)

        ctx.player.trackChanged.connect(self._on_track_changed)
        ctx.player.queueChanged.connect(self._refresh_queue)
        ctx.radio.stateChanged.connect(self._on_radio_state)
        ctx.player.positionChanged.connect(self._on_position)
        ctx.player.playbackStateChanged.connect(self._on_playback_state)
        ctx.player.streamTitleChanged.connect(self._on_stream_title)
        ctx.player.signalChanged.connect(self.table_radio.set_signal)
        ctx.tuner.tunedChanged.connect(lambda _k, _i: self._sync_radio_spot())

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
            self.turntable.set_source(None, "")
            self.turntable.set_playing(False)
            self.table_radio.set_playing(False)
            self._set_radio_mode(False, None)
            self.rating_stars.set_rating(0)
            self.jukebox_toggle.set_on(False)
            self.lyrics_panel.load_for_path(None)
            self._refresh_queue()
            self._sync_radio_button()
            return
        self._current_track_id = item.track_id if item.is_track else None
        self._sync_radio_button()
        self.track_title.setText(item.title)
        self.track_artist.setText(item.artist)
        self.track_album.setText(tracknum.with_side(item.album, item.position))
        self.cover.set_source(item.cover_path, item.album or item.artist)
        # a 78's side ("A"/"B") spins at 78 rpm, everything else at 33 1/3
        self.turntable.set_source(
            item.cover_path, item.album or item.artist,
            rpm=RPM_78 if tracknum.is_side(item.position) else RPM_LP)
        self.turntable.set_playing(self.ctx.player.is_playing())
        self.rating_stars.setVisible(item.is_track)
        self.jukebox_toggle.setVisible(item.is_track)
        self._set_radio_mode(not item.is_track, item)
        if not item.is_track:
            # an old-time radio episode or an internet station: no chart
            # history, rating, jukebox, lyrics or library
            # pages behind it
            self.chart_note.setText(self._now_on_air())
            self._current_artist_id = None
            self._current_release_id = None
            self.lyrics_panel.load_for_path(None)
            self._refresh_queue()
            return
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
        `QueueItem` already carries: the chart-history note, the track's
        rating (see `StarRating`), whether it's on the jukebox board (see
        `JukeboxToggle`), and the artist/release ids the tappable artist
        name/album art jump to. Returns
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
        artist's Library page. A no-op when nothing's
        playing or the release has no artist on file (album_artist_id can be
        NULL - see db/models.py:Release.album_artist_id)."""
        if self._current_artist_id is None:
            return
        self.ctx.navigateRequested.emit("library")
        self.ctx.openArtistRequested.emit(self._current_artist_id)

    def _open_current_release(self) -> None:
        """The tappable album art/title - jumps straight to that release's
        Library page. A no-op when nothing's playing."""
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
        `_on_rating_changed`, this can't be fire-and-forget: turning OFF is
        an immediate flip, but turning ON opens the standard
        `JukeboxPickerDialog` pre-filled and pre-checked for the current
        track (see `LibraryView._on_jukebox_toggle_requested`)."""
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
            # fetched inside this session: the genre list is a user-editable
            # `Setting` row (see services/jukebox.py), and the session is
            # closed by the time the dialog below is constructed.
            genres = jukebox_svc.get_jukebox_genres(session)
        if artist_id is None:
            self.ctx.notify("This track has no album artist to file a jukebox slot under")
            return
        dialog = JukeboxPickerDialog(
            self,
            self._search_addable_tracks,
            genres=genres,
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

    def _make_lyrics_editor(self) -> Optional[LyricsEditorDialog]:
        path = self.lyrics_panel.audio_path
        if not path:
            return None
        dialog = LyricsEditorDialog(
            self.ctx.player, path, **self.lyrics_panel.track_meta, parent=self
        )
        window = self.window()
        if window is not None and window is not self and window.width() > 0:
            dialog.resize(int(window.width() * 0.92), int(window.height() * 0.92))
        return dialog

    def _open_lyrics_editor(self) -> None:
        """The Lyrics pane's "Edit lyrics"/"Sync lyrics myself" - edits the
        track the pane has loaded (the one playing), then reloads the pane
        so a saved .lrc shows up straight away."""
        dialog = self._make_lyrics_editor()
        if dialog is None:
            return
        if dialog.exec() == QDialog.Accepted and dialog.saved_path is not None:
            self.lyrics_panel.reload()
            self.ctx.notify("Lyrics saved")

    def _on_position(self, ms: int) -> None:
        self.lyrics_panel.update_position(ms)
        self.turntable.set_progress(ms, self.ctx.player.duration())

    def _art_menu(self) -> QMenu:
        """Right-click / hold on the artwork: switch between the turntable
        (or, for a station or show, the radio) and the flat cover -
        remembered in Settings' table."""
        menu = QMenu(self)
        shown = self._show_turntable
        if self._on_radio_item:
            text = "Show station logo" if shown else "Show radio"
        else:
            text = "Show album cover" if shown else "Show turntable"
        action = menu.addAction(text)
        action.triggered.connect(lambda: self.set_turntable_shown(not shown))
        return menu

    def set_turntable_shown(self, shown: bool) -> None:
        self._show_turntable = shown
        self._apply_art()
        with self.ctx.session() as db:
            updater.set_pref(db, PREF_TURNTABLE, "1" if shown else "0")

    def _apply_art(self) -> None:
        if not self._show_turntable:
            self.art_stack.setCurrentWidget(self.cover)
        else:
            self.art_stack.setCurrentWidget(self.table_radio if self._on_radio_item else self.turntable)

    def _on_playback_state(self, state: str) -> None:
        playing = state == "playing"
        self.turntable.set_playing(playing)
        self.table_radio.set_playing(playing and self._on_radio_item)

    # -- radio stations and shows -------------------------------------------

    def _set_radio_mode(self, on: bool, item: Optional[QueueItem]) -> None:
        """A station or show is playing (on) or not: the radio in place of
        the turntable, and for a station "Heard on this station" in place
        of Up next / Lyrics."""
        self._on_radio_item = on
        station = item.station_id if on and item is not None and item.is_stream else None
        self._station_id = station
        self._apply_art()
        if on:
            self._sync_radio_spot(item)
            self.table_radio.set_signal(self.ctx.player.signal)
            self.table_radio.set_playing(self.ctx.player.is_playing())
        streaming = station is not None
        self.queue_chip.setVisible(not streaming)
        self.lyrics_chip.setVisible(not streaming)
        self.heard_chip.setVisible(streaming)
        if streaming:
            self.heard_chip.setChecked(True)
            self._switch_right(self.heard_chip)
            self._refresh_heard()
        elif self.heard_chip.isChecked():
            self.queue_chip.setChecked(True)
            self._switch_right(self.queue_chip)

    def _sync_radio_spot(self, item: Optional[QueueItem] = None) -> None:
        """Point the radio's dial where the Radio page has this station or
        show (ui/views/tuner.py:dial_spot)."""
        from .tuner import dial_spot

        item = item or self.ctx.player.current
        if not self._on_radio_item or item is None:
            return
        kind, ident = self.ctx.tuner.tuned
        with self.ctx.session() as db:
            if kind != "onair":
                if item.is_stream and item.station_id is not None:
                    kind, ident = "station", item.station_id
                elif item.episode_id is not None:
                    episode = db.get(RadioEpisode, item.episode_id)
                    if episode is not None:
                        kind, ident = "show", episode.show_id
            band, pos, label = dial_spot(db, kind, ident)
        self.table_radio.set_station(band or ("fm" if item.is_stream else "am"), pos,
                                     label or item.title, item.cover_path)

    def _now_on_air(self) -> str:
        heard = self._heard.for_station(self._station_id)
        return f"♪ {heard[0].text}" if heard else ""

    def _on_stream_title(self, text: str) -> None:
        if self._station_id is None:
            return
        if self._heard.add(self._station_id, text):
            self.chart_note.setText(self._now_on_air())
            self._refresh_heard()

    def _refresh_heard(self) -> None:
        heard = self._heard.for_station(self._station_id)
        rows = []
        for idx, h in enumerate(heard):
            now = idx == 0
            rows.append({
                "lead": h.when.strftime("%I:%M").lstrip("0"),
                "lead_color": COLORS["jukebox_key_hi"] if now else COLORS["text_dim"],
                "primary": h.title or h.text,
                "secondary": h.artist,
                "trail": "on now" if now else "",
                "bold": now,
                "color": COLORS["jukebox_key_hi"] if now else COLORS["text"],
                "index": idx,
            })
        self.heard_list.set_rows(rows)
        if heard:
            since = heard[-1].when.strftime("%I:%M %p").lstrip("0")
            self.heard_meta.setText(f"{len(heard)} song{'s' if len(heard) != 1 else ''} since {since}")
        else:
            self.heard_meta.setText("Nothing yet — not every station sends its songs")
        self._sync_heard_button()

    def _sync_heard_button(self) -> None:
        payload = self.heard_list.current_payload()
        self.heard_add.setEnabled(bool(payload) and payload.get("index") is not None)

    def _add_heard_to_playlist(self) -> None:
        payload = self.heard_list.current_payload()
        heard = self._heard.for_station(self._station_id)
        if not payload or payload.get("index") is None or payload["index"] >= len(heard):
            return
        song = heard[payload["index"]]
        with self.ctx.session() as db:
            track_id = heard_svc.find_track(db, song)
            if track_id is None:
                self.ctx.notify(f"“{song.title or song.text}” isn’t in your library")
                return
            added = heard_svc.add_to_heard_playlist(db, track_id)
            title = db.get(Track, track_id).title
        if added:
            self.ctx.notify(f"Added “{title}” to {heard_svc.PLAYLIST_NAME}")
        else:
            self.ctx.notify(f"“{title}” is already on {heard_svc.PLAYLIST_NAME}")

    def _refresh_queue(self) -> None:
        if not self._is_current_page():
            # off-screen: skip the rebuild (a QListWidgetItem per queued
            # track, and the queue can be the whole library - see the
            # module docstring). refresh(), called by MainWindow.navigate()
            # on arrival, rebuilds it then.
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
                # the currently-playing row's highlight, walnut brown (see
                # ui/theme.py's #Primary comment).
                "lead_color": COLORS["jukebox_key_hi"] if playing else COLORS["text_dim"],
                "primary": item.title,
                "secondary": " · ".join(
                    x for x in (item.artist, f"Side {item.position}"
                                if tracknum.is_side(item.position) else "") if x
                ),
                "trail": format_duration(item.duration_ms),
                "bold": playing,
                "color": COLORS["jukebox_key_hi"] if playing else COLORS["text"],
                "index": idx,
            })
        self.queue_list.set_rows(rows)
        meta = f"{len(rows)} tracks · {format_duration(total_ms)}" if rows else "empty"
        if self.ctx.radio.active:
            meta = f"Radio · {len(rows)} tracks"
        self.queue_meta.setText(meta)
        self.queue_meta.setToolTip(
            f"Radio from {self.ctx.radio.label}" if self.ctx.radio.active else ""
        )
        if 0 <= current_index < len(rows):
            self.queue_list.setCurrentRow(current_index)
            self.queue_list.scrollToItem(self.queue_list.item(current_index))

    # -- radio ---------------------------------------------------------------

    def _toggle_radio(self) -> None:
        radio = self.ctx.radio
        if radio.active:
            radio.stop()
            self.ctx.notify("Radio off — the queue stays as it is")
        elif self._current_track_id is not None:
            radio.start_from_track(self._current_track_id)

    def _on_radio_state(self, active: bool, _label: str) -> None:
        self._sync_radio_button()
        self._refresh_queue()

    def _sync_radio_button(self) -> None:
        active = self.ctx.radio.active
        self.radio_button.setText("Stop radio" if active else "Start radio")
        self.radio_button.setEnabled(active or self._current_track_id is not None)

    def _on_queue_tapped(self, payload: Optional[dict]) -> None:
        if payload and payload.get("index") is not None:
            self.ctx.player.jump_to(payload["index"])

    def _remove_selected(self) -> None:
        payload = self.queue_list.current_payload()
        if payload and payload.get("index") is not None:
            self.ctx.player.remove_at(payload["index"])
