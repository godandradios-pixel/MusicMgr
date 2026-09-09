"""Jukebox: a persistent 45-selector favorites board (2026-09-06).

James: "I want a new sidebar option called jukebox with something like a 45
RPM selector panel... make it feel very authentic... I would like the user
to be able to select an artist and add 2 songs per title strip. This becomes
a favorites capability. Any record with 5 star rating gets added to a title
strip." (The automatic 5-star route described here was removed in a
2026-09-07 follow-up - see services/jukebox.py's module docstring for why -
so this founding quote is kept as-is for history, not as current
behavior.) See services/jukebox.py for how slots get filled today (only
through this view's "+ Add to jukebox" dialog, or Title Details' own
Jukebox toggle column) and ui/widgets/jukebox_strip.py for how one title
strip is drawn and played.

Tapping a side's key plays it immediately, the same way a real jukebox
button does (James, when asked whether a tap should just queue the song
silently: "play like a normal jukebox") - `ctx.play_tracks` is the one seam
every other "hit play" action in the app already funnels through, so this
gets Now Playing navigation for free rather than needing its own.

The "NOW PLAYING — A7" header display tracks `ctx.player.trackChanged`
directly rather than only reacting to jukebox taps, so it also lights up
when a 5-star favorite already on the board gets played some other way
(Now Playing's own controls, a playlist, Title Details) - the display
should never lag behind what's actually on the speakers.

2026-09-07 follow-up (James: "is there a way I can move these jukebox
cards around?"): tapping a strip's "⇄" button opens `JukeboxMoveDialog`
- a picker of every other slot currently on the board, by number and
artist - and moving `_on_move_requested` below calls
`services.jukebox.swap_slots` with whichever one was chosen, then
refreshes. Both sides of a strip travel together, same as picking up a
real record.

An actual drag-and-drop version of this shipped first the same day and
was pulled back out once James tried it: there was no way to drag a card
from page 2 onto a target back on page 1, since `self.strips` only ever
holds one page's worth of widgets - there was nothing on screen to drop
onto once the target had paged out of view. Picking a slot *number*
instead of a target *card* doesn't have that problem: `JukeboxMoveDialog`
lists every slot regardless of which page it would show up on, so source
and destination never need to be visible together.

2026-09-07, later same-day follow-up (James: "I would like to right click
on the jukebox card and delete it by removing both songs from that
card"): the same right-click menu gained a "Remove from jukebox…" action
alongside "Move to a different slot…". `_on_remove_requested` below
confirms first (`QMessageBox.question` - this only takes the card off the
board, not the underlying tracks, but there's no undo once it's gone) then
calls `services.jukebox.remove_slot`, which deletes the whole
`JukeboxSlot` regardless of what's loaded on either side.

2026-09-07, a third same-day follow-up - James, looking at the same 3x3
board: "Does this need to be a fixed 3x3? I want the cards to stay the
same size but would like it to expand to 3x4 if the page is high enough."
The grid now builds MAX_ROWS x GRID_COLUMNS strip widgets up front instead
of a fixed nine, and `_rows_that_fit` decides - at every `refresh()` and
again on every resize, via `resizeEvent` - whether the window actually has
room for a fourth row, always falling back to MIN_ROWS when it doesn't.
Card size itself never changes either way, only how many of the
already-fixed-size strips get shown per page, matching the same "cards
don't resize" constraint from the two 2026-09-07 follow-ups above.

2026-09-07, a fourth same-day follow-up: James asked to make this the
app's default startup page (see ui/app.py's `MainWindow.__init__`), and
pointed out the pager ("Page 1 of 3") was eating a whole row of its own
under the grid for not much information - `self.prev_btn`/`page_label`/
`next_btn` moved into the header, just left of "+ Add to jukebox", giving
that row back to the grid (see `__init__` below; `refresh()`'s own
`page_label.setText(...)` call didn't need to change, only where the
label lives). He also asked for the strip key badges ("A7"/"B7") to move
off red, which read as an alert color against this app's own warm brown/
gold branding - see ui/widgets/jukebox_strip.py's docstring for the new
color.

2026-09-07, a fifth same-day follow-up (James: "I would like the jukebox
page to have a chip of 5 genres: Classic Rock, Country, Pop, Hairbands,
Rock. Then have the pages of the cards where you can select the location
and what genre page a track will be organized by"): a row of
`ChipButton`s (`services.jukebox.JUKEBOX_GENRES`) sits between the header
and the grid, exclusive like every other chip row in this app
(cover_grid.py's sort chips, Now Playing's "Up next"/"Lyrics" chips).
Picking a chip is purely a display filter - `refresh()` passes
`genre=self._genre` through to `slot_count`/`page_count`/`list_slot_rows`
- and resets to page 0, since a page number from one genre's board has no
particular meaning on another's. `JukeboxAddDialog` grew a genre combo
(preselected to whichever chip is active) so a freshly-added card is filed
under the right board from the start; the right-click dialog, renamed
`JukeboxOrganizeDialog` (from `JukeboxMoveDialog`) and reached through the
renamed `_on_organize_requested` (from `_on_move_requested`), grew a
second, independent genre combo alongside its existing slot-location
picker, since re-filing a card's genre and moving its board position are
orthogonal operations (`services.jukebox.set_slot_genre` vs. `swap_slots`)
that this one dialog now exposes together. Unlike the old move-only
dialog, the Organize dialog's OK button is never disabled for having no
other slot to swap with - reassigning just the genre is a complete,
valid action even on a single-card board, so the "No other slot to move
this to yet" early bailout is gone.

2026-09-07, a sixth same-day follow-up - James: "great job on the Genre
on the jukebox option. Please add 2 more chips: Christian, 80's. Then
reorder them from left to right: Country, Christian, Classic Rock, Rock,
80's, Hairbands, Pop. And then make the pills not red but the brown
color." `services.jukebox.JUKEBOX_GENRES` grew from five entries to seven
and its tuple order changed to match - the chip row here builds one
`ChipButton` per entry in that same order, so reordering the tuple is the
entire fix for the on-screen left-to-right order, and every genre combo
box (`JukeboxAddDialog`, `JukeboxOrganizeDialog`) picks up the new order
and members for free, being built the same way. Each chip's object name
is overridden from `ChipButton`'s own default ("Chip") to "ChipWarm"
right after construction, so `ui/theme.py`'s new `#ChipWarm:checked` rule
(walnut-brown, reusing the jukebox key palette) applies only to these
genre chips - every other chip row in the app (the ones named in the
paragraph above) keeps the ordinary red `#Chip:checked` look.

2026-09-08 follow-up - James: "add 3 more genres to the jukebox: Metal,
R&B, Hip/Hop." `services.jukebox.JUKEBOX_GENRES` grew from seven entries
to ten, appended after Pop rather than interleaved into the existing
order (unlike the sixth follow-up above, this one didn't ask for a
reorder). Nothing else here changed - same ChipButton-per-entry loop,
same ChipWarm styling, same genre combo boxes built off the same tuple.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...db.models import Track
from ...services import jukebox as jkb_svc
from ...services import library as lib_svc
from ..context import AppContext
from ..widgets.common import ChipButton, EmptyState, TouchButton, dim_label
from ..widgets.jukebox_strip import JukeboxStripWidget
from .base import BaseView

#: 3 columns, with either 3 or 4 rows depending on how much vertical space
#: is actually available - a full page readable at arm's length on a touch
#: panel without its own scrolling (James, 2026-09-06: "make the jukebox 3
#: columns instead of 2"). Row count is decided at runtime by
#: `JukeboxView._rows_that_fit` (James, 2026-09-07 follow-up: "Does this
#: need to be a fixed 3x3? I want the cards to stay the same size but
#: would like it to expand to 3x4 if the page is high enough") - MIN_ROWS
#: is what the board always shows even when there's no room for a fourth
#: row, MAX_ROWS is the most it will ever show. services/jukebox.py's
#: SLOTS_PER_PAGE (9) stays put as that module's own documented default
#: for any other caller; this view always passes its own dynamically
#: computed per_page instead of relying on it.
GRID_COLUMNS = 3
MIN_ROWS = 3
MAX_ROWS = 4

#: a physical 45 only has two sides - the picker refuses a third pick rather
#: than silently bumping one back off, so the person always sees exactly
#: what they're about to add.
MAX_PICKS = 2


class JukeboxAddDialog(QDialog):
    """"Select an artist and add 2 songs per title strip" - James's own
    words for the manual half of populating the board. Picking an artist
    loads every track on their own releases (`list_tracks_for_album_artist`
    - the same source the artist page's own tracklist uses); checking a
    track adds it to the pick list, capped at two since that's all one
    physical 45 can hold. `services/jukebox.py:place_track` decides where
    each picked song actually lands (an open side on one of this artist's
    existing slots, or a fresh one) - this dialog only collects the picks.

    2026-09-07 follow-up - James reported "I can't seem to be able to
    select 2 songs and hit OK." Each row used to be a plain `QListWidgetItem`
    with `Qt.ItemIsUserCheckable` and selection disabled - Qt only toggles
    that kind of checkbox when the click lands on its small native
    indicator glyph (a few pixels), not from clicking the row's text, and
    with selection off there was no other feedback that a click had landed
    anywhere. Tapping the song title itself (the obvious, large target) did
    nothing. Fixed by giving every row a real `QCheckBox` as its item
    widget (`setItemWidget`) sized to the app's own `TOUCH["row_height"]` -
    a `QCheckBox` toggles from a click anywhere across its own label and
    indicator, matching this app's "sized for fingers rather than mouse
    pointers" touch-first design instead of fighting it.

    2026-09-07 follow-up - James's genre chips (see the module docstring's
    fifth same-day follow-up) added a `genre_combo` here too, preselected
    to whichever chip is active on the board when "+ Add to jukebox" is
    tapped, so a freshly-picked song files onto the right genre page from
    the start rather than always landing on the default board.

    2026-09-07, later same-day follow-up - James, after the list below was
    re-sorted to group by album ("the selection of songs needs to be
    sorted alphabetically by title and grouped by album"): "can you show
    the album in the list of songs? I can't tell how they are grouped in
    the list." `tracks_for_artist` now hands back (id, title, album)
    triples instead of (id, title) pairs, and each row's checkbox label is
    "Title — Album" so the grouping the sort already does is actually
    visible, not just an invisible reordering.
    """

    def __init__(
        self,
        parent,
        artists: list[tuple[int, str]],
        tracks_for_artist: Callable[[int], list[tuple[int, str, str]]],
        genres: Sequence[str] = (),
        default_genre: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add to jukebox")
        self.setMinimumSize(460, 520)
        self._tracks_for_artist = tracks_for_artist
        self._artist_id: Optional[int] = None

        layout = QVBoxLayout(self)

        layout.addWidget(dim_label("Artist"))
        self.artist_combo = QComboBox()
        for artist_id, name in artists:
            self.artist_combo.addItem(name, artist_id)
        self.artist_combo.currentIndexChanged.connect(self._on_artist_changed)
        layout.addWidget(self.artist_combo)

        layout.addWidget(dim_label("Genre page"))
        self.genre_combo = QComboBox()
        for genre in genres:
            self.genre_combo.addItem(genre, genre)
        if default_genre is not None:
            idx = self.genre_combo.findData(default_genre)
            if idx >= 0:
                self.genre_combo.setCurrentIndex(idx)
        layout.addWidget(self.genre_combo)

        layout.addWidget(dim_label(f"Songs (pick up to {MAX_PICKS})"))
        self.track_list = QListWidget()
        self.track_list.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(self.track_list, 1)

        self.hint = dim_label("")
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if artists:
            self._on_artist_changed(0)

    def _on_artist_changed(self, index: int) -> None:
        self._artist_id = self.artist_combo.itemData(index)
        self.track_list.clear()
        for track_id, title, album in self._tracks_for_artist(self._artist_id):
            item = QListWidgetItem()
            item.setData(Qt.UserRole, track_id)
            # tall enough that the checkbox's own click area (its label +
            # indicator, not the bare row) is a real touch target - see the
            # class docstring's 2026-09-07 follow-up
            item.setSizeHint(QSize(0, TOUCH["row_height"]))
            # "Title — Album" (2026-09-07, later same-day follow-up) - makes
            # the list's own album grouping visible instead of the same
            # bare titles it showed before that grouping existed
            label = f"{title} — {album}" if album else title
            checkbox = QCheckBox(label)
            checkbox.toggled.connect(self._on_checkbox_toggled)
            self.track_list.addItem(item)
            self.track_list.setItemWidget(item, checkbox)
        self._update_hint()

    def _on_checkbox_toggled(self, checked: bool) -> None:
        if checked and len(self._checked_items()) > MAX_PICKS:
            # last one over the limit - put it back rather than silently
            # bumping an earlier pick, so the person's own two choices stick
            box = self.sender()
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)
            return
        self._update_hint()

    def _row_checkbox(self, row: int) -> Optional[QCheckBox]:
        item = self.track_list.item(row)
        return self.track_list.itemWidget(item) if item is not None else None

    def _checked_items(self) -> list[QListWidgetItem]:
        items = []
        for row in range(self.track_list.count()):
            checkbox = self._row_checkbox(row)
            if checkbox is not None and checkbox.isChecked():
                items.append(self.track_list.item(row))
        return items

    def _update_hint(self) -> None:
        count = len(self._checked_items())
        self.hint.setText(f"{count} of {MAX_PICKS} selected")
        self.ok_button.setEnabled(count > 0)

    def selected_artist_id(self) -> Optional[int]:
        return self._artist_id

    def selected_track_ids(self) -> list[int]:
        return [item.data(Qt.UserRole) for item in self._checked_items()]

    def selected_genre(self) -> Optional[str]:
        return self.genre_combo.currentData()


class JukeboxOrganizeDialog(QDialog):
    """"Organize card…" (2026-09-07 follow-up; renamed from
    `JukeboxMoveDialog` the same day genre chips were added) - combines the
    original "move to slot #" picker with a second, independent genre
    combo, since re-filing a card's genre chip and moving its board
    position are orthogonal operations (`services.jukebox.set_slot_genre`
    vs. `swap_slots`) that James asked to control from the one card menu
    ("select the location and what genre page a track will be organized
    by").

    The location picker lists every *other* slot on the board that shares
    the genre currently selected just above it, by number and artist,
    rather than asking for a bare number to type in: slot numbers are
    assigned once and never reused or renumbered even after a slot is
    removed (see services/jukebox.py:JukeboxSlot), so an in-use library can
    easily have gaps - a plain 1..N spinner would happily suggest numbers
    that don't exist. `slots` is a list of (slot_number, artist_name,
    genre) triples for every slot on the whole board, not just the current
    page - the genre combo starts on the card's current genre (the page
    the "Organize card…" menu was opened from) but changing it re-filters
    the location list live to that genre's own slots instead, since
    picking a target slot on a board you're not filing this card onto
    would be confusing (2026-09-07, same-day follow-up - James: "I only
    want to be able to see [the currently selected genre] on my slots to
    move dropdown"). The entire point of this half of the dialog over the
    drag-and-drop it originally replaced is that source and target never
    need to be visible together. A leading "Stay in place" entry
    (`itemData=None`) means the location half is optional now that
    genre-only reassignment is a complete action on its own - OK is never
    disabled here, unlike the old move-only dialog, which refused to open
    with nothing else on the board to swap with.
    """

    def __init__(
        self,
        parent,
        current_slot: int,
        slots: list[tuple[int, str, str]],
        current_genre: str,
        genres: Sequence[str] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Organize card")
        self._current_slot = current_slot
        self._slots = slots

        layout = QVBoxLayout(self)
        layout.addWidget(dim_label(f"Slot {current_slot} — genre page"))

        self.genre_combo = QComboBox()
        for genre in genres:
            self.genre_combo.addItem(genre, genre)
        idx = self.genre_combo.findData(current_genre)
        if idx >= 0:
            self.genre_combo.setCurrentIndex(idx)
        self.genre_combo.currentIndexChanged.connect(
            lambda _idx: self._refresh_slot_options(self.genre_combo.currentData())
        )
        layout.addWidget(self.genre_combo)

        layout.addWidget(dim_label("Move to a different slot (optional)"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)
        self._refresh_slot_options(current_genre)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_slot_options(self, genre: str) -> None:
        """Rebuilds the location list from scratch for `genre` - called once
        up front for the card's starting genre, and again every time the
        genre combo above changes, so the two controls never disagree about
        which board a target slot belongs to."""
        self.combo.clear()
        self.combo.addItem("Stay in place", None)
        for number, artist_name, slot_genre in self._slots:
            if number == self._current_slot or slot_genre != genre:
                continue
            label = f"Slot {number} — {artist_name}" if artist_name else f"Slot {number}"
            self.combo.addItem(label, number)

    def target_slot(self) -> Optional[int]:
        return self.combo.currentData()

    def selected_genre(self) -> str:
        return self.genre_combo.currentData()


class JukeboxView(BaseView):
    title_text = "Jukebox"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._page = 0
        # settled by the first refresh()/resize measurement - MIN_ROWS is
        # just the starting guess so `resizeEvent`'s "did this actually
        # change" check has something to compare against before that.
        self._current_rows = MIN_ROWS
        # which genre chip is active - see the module docstring's fifth
        # same-day 2026-09-07 follow-up. Starts on DEFAULT_JUKEBOX_GENRE
        # ("Rock") rather than the first chip in the row, so a track filed
        # with no genre picker of its own (Title Details'/Now Playing's
        # one-tap toggle, or a pre-existing card from before this follow-up
        # shipped) shows up on whichever page is already open at launch.
        self._genre = jkb_svc.DEFAULT_JUKEBOX_GENRE

        self.now_playing_label = QLabel("NOW PLAYING — —")
        self.now_playing_label.setObjectName("JukeboxNowPlayingText")
        self.header.addWidget(self.now_playing_label)
        self.header.addStretch(1)

        # the pager (2026-09-07 follow-up, James: "Now at the bottom of the
        # jukebox page there is a page 1 of 3 which takes up a whole row of
        # space. Recommend we move that to the left of the top + Add to
        # jukebox button") - used to be its own centered row under the grid
        # (see refresh()'s page_label.setText below, unchanged); moving it
        # into the header next to "+ Add to jukebox" gives that whole row
        # back to the grid instead.
        self.prev_btn = TouchButton("‹")
        self.prev_btn.setObjectName("RowPageArrow")
        self.prev_btn.clicked.connect(lambda: self._change_page(-1))
        self.page_label = dim_label("")
        self.next_btn = TouchButton("›")
        self.next_btn.setObjectName("RowPageArrow")
        self.next_btn.clicked.connect(lambda: self._change_page(1))
        self.header.addWidget(self.prev_btn)
        self.header.addWidget(self.page_label)
        self.header.addWidget(self.next_btn)
        self.header.addSpacing(20)

        add_btn = TouchButton("+ Add to jukebox", primary=True)
        # 2026-09-07 follow-up - James: "let's also change the red color on
        # the Now Playing, Add to Jukebox and the Play button at the
        # bottom to the same MusicMgr color palett[e]." Overriding the
        # object name TouchButton(primary=True) just set swaps this one
        # button onto ui/theme.py's #PrimaryWarm (brown) rule instead of
        # the ordinary #Primary (red-orange) every other "primary" button
        # in the app still uses - see theme.py's COLORS comment for why
        # the shared accent token itself wasn't retinted.
        add_btn.setObjectName("PrimaryWarm")
        add_btn.clicked.connect(self._open_add_dialog)
        self.header.addWidget(add_btn)

        # genre chips (2026-09-07 follow-up) - exclusive like every other
        # chip row in this app (cover_grid.py's sort chips, Now Playing's
        # "Up next"/"Lyrics"); which board is showing, not a search filter.
        # Built in `JUKEBOX_GENRES` order, which is exactly the left-to-
        # right order James asked for (sixth same-day follow-up) - no
        # separate ordering logic needed here.
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        self._genre_chips = QButtonGroup(self)
        self._genre_chips.setExclusive(True)
        for genre in jkb_svc.JUKEBOX_GENRES:
            chip = ChipButton(genre)
            # "make the pills not red but the brown color" (sixth same-day
            # follow-up) - overrides ChipButton's own "Chip" object name so
            # only these genre chips pick up ui/theme.py's brown
            # #ChipWarm:checked rule; every other chip row in the app keeps
            # the ordinary red #Chip:checked look.
            chip.setObjectName("ChipWarm")
            chip.setProperty("genre", genre)
            chip.setChecked(genre == self._genre)
            self._genre_chips.addButton(chip)
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        self._genre_chips.buttonClicked.connect(self._on_genre_chip_clicked)
        self.body().addLayout(chip_row)

        # "Rate a track 5 stars..." dropped from this hint 2026-09-07 - the
        # automatic 5-star route it described was removed the same day
        # (see services/jukebox.py's module docstring: James, "I don't
        # like using my 5 stars to get a track on the Jukebox cards") and
        # the leftover copy kept advertising a way in that no longer
        # existed. "+ Add to jukebox" is the only route this empty state
        # needs to point at now.
        self.empty_state = EmptyState(
            "No favorites yet",
            "Tap “+ Add to jukebox” to load one.",
        )
        self.body().addWidget(self.empty_state)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(16)
        self.body().addWidget(self.grid_host, 1)

        self.strips: list[JukeboxStripWidget] = []
        for i in range(MAX_ROWS * GRID_COLUMNS):
            strip = JukeboxStripWidget()
            strip.sideActivated.connect(self._on_side_activated)
            strip.moveRequested.connect(self._on_organize_requested)
            strip.removeRequested.connect(self._on_remove_requested)
            self.grid.addWidget(strip, i // GRID_COLUMNS, i % GRID_COLUMNS)
            self.strips.append(strip)

        self.ctx.player.trackChanged.connect(self._on_track_changed)

    # -- populating -------------------------------------------------------

    def _rows_that_fit(self) -> int:
        """How many rows of fixed-size strips actually fit in the space
        `grid_host` has right now. `grid_host` carries the only stretch
        factor in this view's top-level layout (see `BaseView.body()`), so
        its height already reflects whatever room is left over after the
        header and pager - independent of how many strips happen to be
        visible inside it at the moment - which is what makes measuring it
        here safe rather than circular. Never returns below MIN_ROWS: a
        short (or not-yet-shown) window still gets a full page of MIN_ROWS
        rows at their fixed size, same as before this follow-up, rather
        than shrinking the cards or the row count any further."""
        strip_height = self.strips[0].sizeHint().height()
        spacing = self.grid.verticalSpacing()
        available = self.grid_host.height()
        for rows in range(MAX_ROWS, MIN_ROWS, -1):
            needed = rows * strip_height + (rows - 1) * spacing
            if available >= needed:
                return rows
        return MIN_ROWS

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Qt may not have finished re-laying-out grid_host to its new size
        # by the time this handler runs - layout updates for a resized
        # widget's children can be posted rather than applied synchronously
        # - so a zero-delay singleShot defers the actual measurement to the
        # next trip through the event loop, after Qt has settled the new
        # geometry. Same "recompute, only act if it changed" idiom
        # ui/widgets/album_tracks.py's TrackListWidget.resizeEvent already
        # uses for width instead of height.
        QTimer.singleShot(0, self._apply_rows_for_current_size)

    def _apply_rows_for_current_size(self) -> None:
        rows = self._rows_that_fit()
        if rows == self._current_rows:
            return
        self._current_rows = rows
        self.refresh()

    def _on_genre_chip_clicked(self, button) -> None:
        genre = button.property("genre")
        if genre == self._genre:
            return
        self._genre = genre
        self._page = 0
        self.refresh()

    def refresh(self) -> None:
        self._current_rows = self._rows_that_fit()
        per_page = GRID_COLUMNS * self._current_rows
        with self.ctx.session() as session:
            total = jkb_svc.slot_count(session, genre=self._genre)
            total_pages = jkb_svc.page_count(session, per_page=per_page, genre=self._genre)
            self._page = max(0, min(self._page, total_pages - 1))
            rows = jkb_svc.list_slot_rows(
                session, page=self._page, per_page=per_page, genre=self._genre
            )
            now_playing_code = self._resolve_now_playing_code(session)

        self.empty_state.setVisible(total == 0)
        self.grid_host.setVisible(total > 0)

        for i, strip in enumerate(self.strips):
            if i < len(rows):
                strip.set_slot(rows[i])
                strip.set_now_playing(_side_if_matching(now_playing_code, rows[i]["slot_number"]))
                strip.show()
            else:
                strip.clear()
                strip.hide()

        self.page_label.setText(f"Page {self._page + 1} of {total_pages}")
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page < total_pages - 1)
        self.now_playing_label.setText(
            f"NOW PLAYING — {now_playing_code}" if now_playing_code else "NOW PLAYING — —"
        )

    def _resolve_now_playing_code(self, session) -> Optional[str]:
        current = self.ctx.player.current
        if current is None:
            return None
        return jkb_svc.find_code_for_track(session, current.track_id)

    def _on_track_changed(self, item) -> None:
        self.refresh()

    def _change_page(self, delta: int) -> None:
        self._page += delta
        self.refresh()

    # -- playing ------------------------------------------------------------

    def _on_side_activated(self, side: str, track_id: int) -> None:
        # play_tracks() must be called while the session is still open -
        # QueueItem.from_track() lazily reads track.release/primary_file,
        # which raises DetachedInstanceError (silently, from inside this
        # Qt slot) once the session that loaded `track` has closed. Fixed
        # 2026-09-06 after James reported a key tap not starting playback.
        with self.ctx.session() as session:
            track = session.get(Track, track_id)
            tracks = [track] if track is not None else []
            if tracks:
                self.ctx.play_tracks(tracks, source="jukebox")

    def _on_organize_requested(self, slot_number: int) -> None:
        """"Organize card…" (2026-09-07 follow-up; renamed from
        `_on_move_requested` the same day genre chips were added) - fetches
        every slot on the whole board (with its genre) so the dialog can
        filter the location list down to whichever genre is selected in its
        own genre combo, live, as James asked when he found the list showing
        every board's cards mixed together ("I only want to be able to see
        [the currently selected genre] on my slots to move dropdown") -
        `JukeboxOrganizeDialog._refresh_slot_options` does the actual
        filtering; this just hands it the full unfiltered set once so
        changing the genre combo doesn't need another DB round-trip.
        Swapping board position itself is still a pure `swap_slots`
        operation unaffected by genre. Unlike the old move-only handler,
        there's no "only one slot on the board" early bailout any more -
        reassigning just the genre chip is a complete, valid action even
        when there's nothing else to swap with."""
        with self.ctx.session() as session:
            total = jkb_svc.slot_count(session)
            rows = jkb_svc.list_slot_rows(session, page=0, per_page=max(total, 1))
        slots = [(r["slot_number"], r["artist_name"], r["genre"]) for r in rows]
        current = next((r for r in rows if r["slot_number"] == slot_number), None)
        current_genre = current["genre"] if current is not None else self._genre

        dialog = JukeboxOrganizeDialog(
            self, slot_number, slots, current_genre, genres=jkb_svc.JUKEBOX_GENRES
        )
        if dialog.exec() != QDialog.Accepted:
            return
        target = dialog.target_slot()
        new_genre = dialog.selected_genre()

        with self.ctx.session() as session:
            if new_genre != current_genre:
                jkb_svc.set_slot_genre(session, slot_number, new_genre)
            if target is not None:
                jkb_svc.swap_slots(session, slot_number, target)
        self.refresh()

    def _on_remove_requested(self, slot_number: int) -> None:
        """James, 2026-09-07: "I would like to right click on the jukebox
        card and delete it by removing both songs from that card." A
        confirmation prompt first since this is destructive to the board
        (though not to the library - see services.jukebox.remove_slot's
        docstring), matching the confirm-before-delete pattern playlists
        and folders already use elsewhere in the app."""
        confirm = QMessageBox.question(
            self,
            "Remove from jukebox",
            "Remove this card from the jukebox? Both songs stay in your "
            "library and can be added back later.",
        )
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            jkb_svc.remove_slot(session, slot_number)
        self.refresh()

    # -- manual add ---------------------------------------------------------

    def _open_add_dialog(self) -> None:
        with self.ctx.session() as session:
            artists = [(a.id, a.name) for a in lib_svc.list_artists(session)]
        if not artists:
            self.ctx.notify("No artists in your library yet")
            return

        dialog = JukeboxAddDialog(
            self,
            artists,
            self._load_tracks_for_artist,
            genres=jkb_svc.JUKEBOX_GENRES,
            default_genre=self._genre,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        artist_id = dialog.selected_artist_id()
        track_ids = dialog.selected_track_ids()
        genre = dialog.selected_genre() or jkb_svc.DEFAULT_JUKEBOX_GENRE
        if artist_id is None or not track_ids:
            return
        with self.ctx.session() as session:
            for track_id in track_ids:
                jkb_svc.place_track(session, artist_id, track_id, genre=genre)
        self.ctx.notify(
            f"Added {len(track_ids)} song{'s' if len(track_ids) != 1 else ''} to the jukebox"
        )
        self.refresh()

    def _load_tracks_for_artist(self, artist_id: int) -> list[tuple[int, str, str]]:
        """`list_tracks_for_album_artist` itself orders chronologically by
        release then track position - right for a discography, but the
        "pick up to 2" checklist below is scanned for a specific song, not
        browsed like an album's tracklist, so it's re-sorted here to group
        by album (alphabetically) and then by title within each album
        (2026-09-07, James: "the selection of songs needs to be sorted
        alphabetically by title and grouped by album"). Sorting here rather
        than changing the shared service function keeps every other caller
        of `list_tracks_for_album_artist` (the artist page's own
        tracklist) on its original chronological order.

        Returns (id, title, album) triples, not just (id, title) - the
        album name rides along so `JukeboxAddDialog` can show it next to
        each song (2026-09-07, later same-day follow-up: "can you show the
        album in the list of songs? I can't tell how they are grouped in
        the list") rather than the grouping above being invisible."""
        with self.ctx.session() as session:
            tracks = list(lib_svc.list_tracks_for_album_artist(session, artist_id))
            tracks.sort(
                key=lambda t: ((t.release.title if t.release else "").lower(), t.title.lower())
            )
            return [(t.id, t.title, t.release.title if t.release else "") for t in tracks]


def _side_if_matching(code: Optional[str], slot_number: int) -> Optional[str]:
    """"A7" matches slot 7's A side; anything else (a different slot, or no
    code at all because nothing playing is on the board) matches neither."""
    if not code or code[1:] != str(slot_number):
        return None
    return code[0]
