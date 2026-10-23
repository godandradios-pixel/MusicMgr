"""Jukebox: a persistent 45-selector favorites board.

Each title strip (ui/widgets/jukebox_strip.py) holds one artist's two
sides. Slots are filled from this view's "+ Add to jukebox" button, Title
Details' Jukebox column and Now Playing's Jukebox toggle - all three open
`JukeboxPickerDialog`; see services/jukebox.py's module docstring.

Tapping a side's key plays it immediately, like a real jukebox, through
`ctx.play_tracks`. The "NOW PLAYING — A7" header follows
`ctx.player.trackChanged`, so it lights up however a board song is played.

Layout: the number of rows and columns (MIN_/MAX_ROWS, MIN_/MAX_COLUMNS)
is decided at every `refresh()` and resize (`_rows_that_fit` /
`_cols_that_fit`); card size never changes, only how many fit per page. A
MAX_ROWS x MAX_COLUMNS pool of strips is built up front and
`_arrange_grid()` re-places them (QGridLayout must be told every cell).
The pager sits in the header so the grid gets that row. `_root`'s left
margin and `self.grid`'s contents margins are zeroed so the cards sit
close to the nav rail; what's left is the strip's own 4px frame.

Genres: a chip row filters the board (resetting to page 0). The genre list
is persisted and editable (services/jukebox.py). Holding a `_GenreChip`
(GENRE_CHIP_LONG_PRESS_MS) or right-clicking it opens its menu (Rename,
Delete, Play all, Shuffle); dragging a chip sideways reorders the genres.
Rename swaps the chip for an
`_InlineGenreEdit` (pre-filled, selected, focused); the trailing "+" chip
opens the same box blank to add a genre. Enter, focus-out or Escape end an
edit through `_finish_genre_edit`; a rejected name (blank or a
case-insensitive duplicate) is reported, not silently reverted. Only one
edit is open at a time (`self._genre_editor`). Delete confirms, naming how
many cards it relabels (services/jukebox.py:delete_genre), and refuses the
last genre. Every edit rebuilds the chip row (`_build_genre_chips`); the
QButtonGroup itself is kept, so its one connection stays. Chips use the
object name "ChipWarm" (ui/theme.py).

Card actions (see jukebox_strip.py for the widget side):

- **Organize card…** (`JukeboxOrganizeDialog`): move to any slot number,
  on any page, and/or re-file under another genre (`swap_slots` /
  `set_slot_genre`).
- **Remove from jukebox…**: confirms, then `remove_slot` (the tracks
  themselves are untouched).
- **An open side's banner**: `_on_fill_requested` opens the picker
  pre-filled with the card's artist and genre, capped at one pick.
- **Edit songs…** (`JukeboxEditSongsDialog`): change or clear either side;
  `_on_edit_requested` applies the plan via `set_slot_side`.
- **Drag a card onto another**: `_on_reorder_requested` ->
  `reorder_slot`, which inserts it there and shifts the rest down (the
  last card overflows to the next page). Hovering a dragged card over a
  page arrow for PAGE_FLIP_HOVER_MS flips the page, and keeps flipping
  while held (`_flip_page_during_drag`); QDrag.exec()'s nested event loop
  keeps the timer and repaints running. The arrows never take the drop.
  A drag only reorders within the genre showing; Organize moves a card
  across genres.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...db.models import Track
from ...services import jukebox as jkb_svc
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import ChipButton, EmptyState, TouchButton, dim_label
from ..widgets.jukebox_strip import SLOT_MIME_TYPE, JukeboxStripWidget
from .base import BaseView

#: 3-6 columns and 3-4 rows, decided at runtime from how much space
#: `grid_host` has (`JukeboxView._rows_that_fit` / `_cols_that_fit`) - a
#: full page readable at arm's length without scrolling. MAX_* is also the
#: size of the strip pool built in `__init__`. This view always passes its
#: own per_page rather than services/jukebox.py's SLOTS_PER_PAGE default.
MIN_ROWS = 3
MAX_ROWS = 4
MIN_COLUMNS = 3
MAX_COLUMNS = 6

#: a physical 45 only has two sides - the picker refuses a third pick rather
#: than silently bumping one back off, so the person always sees exactly
#: what they're about to add.
MAX_PICKS = 2

#: how long a card has to hover over a page-arrow button, mid-drag, before
#: that direction's page flips underneath it. Long enough that passing
#: over the arrow on the way to a normal in-page drop doesn't flip a page
#: by accident; short enough that walking a card several pages over by
#: holding it there doesn't feel like waiting.
PAGE_FLIP_HOVER_MS = 600


class JukeboxPickerDialog(QDialog):
    """The standard "Add to jukebox" picker, used by both places that add a
    track to the board:

    - The Jukebox page's "+ Add to jukebox" button
      (`JukeboxView._open_add_dialog`).
    - A track-level Jukebox toggle turning ON - Title Details' column
      (`ui/views/library.py`'s `_on_jukebox_toggle_requested`) and Now
      Playing's toggle (`ui/views/nowplaying.py`'s `_on_jukebox_toggled`).
      There `initial_artist_query`/`initial_track_query`/`check_track`
      pre-fill and pre-check the tapped track, so the person just picks a
      genre and taps OK.

    Two live search boxes, `artist_search` and `track_search`, are ANDed
    (`search_tracks`, backed by `services/jukebox.py:search_addable_tracks`):
    pick an artist, then search within it for a song. Picks are capped at
    `max_picks` (MAX_PICKS, or 1 when only one card side is open); each row
    is a real `QCheckBox` so a tap anywhere on the label toggles it.

    Picks persist across searches: `self._picks` (`{track_id: artist_id}`)
    is the source of truth, not what's checked in the visible list, which
    `_render_results` rebuilds on every search. Results can span artists,
    hence `(track_id, artist_id)` pairs from `selected_picks()`.

    Also the nested "pick a replacement" step in `JukeboxEditSongsDialog`:
    `show_genre=False` hides the genre control, and `selected_rows()` adds
    each pick's title.

    Searching is debounced (`_search_timer`, 220ms) because each query
    scans the whole library; searching on every keystroke froze the dialog
    on a big library, and clicks queued during the freeze moved focus.
    """

    def __init__(
        self,
        parent,
        search_tracks: Callable[[str, str], list[dict]],
        genres: Sequence[str] = (),
        default_genre: Optional[str] = None,
        initial_artist_query: str = "",
        initial_track_query: str = "",
        max_picks: int = MAX_PICKS,
        title: str = "Add to jukebox",
        show_genre: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        # wide enough to show long "Title — Artist (Album)" rows (each is one
        # unwrapped line); `resize` sets the opening size, and the dialog
        # stays resizable
        self.setMinimumSize(460, 560)
        self.resize(820, 640)
        #: `(artist_query, track_query) -> results`, ANDed
        self._search_tracks = search_tracks
        #: MAX_PICKS by default; 1 when only one side of a card is open
        self._max_picks = max_picks
        #: {track_id: artist_id} - see class docstring
        self._picks: dict[int, int] = {}
        #: {track_id: title} - kept alongside `self._picks` for
        #: `selected_rows()`
        self._pick_titles: dict[int, str] = {}
        #: debounces `_run_search` (see class docstring) - the same 220ms
        #: debounce as ui/views/search.py's SearchView, whose
        #: `_schedule`/`_run_search` naming this mirrors
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self._run_search)

        layout = QVBoxLayout(self)

        layout.addWidget(dim_label("Search by artist"))
        self.artist_search = QLineEdit()
        self.artist_search.setPlaceholderText("Artist name…")
        self.artist_search.textChanged.connect(self._schedule_search)
        layout.addWidget(self.artist_search)

        layout.addWidget(dim_label("Search by track"))
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Track name…")
        self.track_search.textChanged.connect(self._schedule_search)
        layout.addWidget(self.track_search)

        self.genre_label = dim_label("Genre page")
        layout.addWidget(self.genre_label)
        self.genre_combo = QComboBox()
        for genre in genres:
            self.genre_combo.addItem(genre, genre)
        if default_genre is not None:
            idx = self.genre_combo.findData(default_genre)
            if idx >= 0:
                self.genre_combo.setCurrentIndex(idx)
        layout.addWidget(self.genre_combo)
        if not show_genre:
            # editing a slot's songs never touches its genre
            self.genre_label.hide()
            self.genre_combo.hide()

        layout.addWidget(dim_label(f"Songs (pick up to {self._max_picks})"))
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

        self._update_hint()
        if initial_artist_query or initial_track_query:
            # blocked while both boxes are filled in, then searched once
            self.artist_search.blockSignals(True)
            self.track_search.blockSignals(True)
            self.artist_search.setText(initial_artist_query)
            self.track_search.setText(initial_track_query)
            self.artist_search.blockSignals(False)
            self.track_search.blockSignals(False)
            # a pre-filled query searches immediately, without the debounce
            self._run_search()

    def _schedule_search(self, _text: str = "") -> None:
        """`textChanged` handler for both search boxes: restarts the
        debounce timer; `_run_search` queries once typing pauses."""
        self._search_timer.start()

    def _run_search(self) -> None:
        artist_query = self.artist_search.text().strip()
        track_query = self.track_search.text().strip()
        results = (
            self._search_tracks(artist_query, track_query)
            if (artist_query or track_query)
            else []
        )
        self._render_results(results)

    def _render_results(self, results: list[dict]) -> None:
        # QListWidget.clear() leaves widgets set via setItemWidget() behind
        # as orphaned children of the viewport, so each search would leak
        # its checkboxes; detach and dispose of them first (see
        # QListWidget::removeItemWidget())
        for row_index in range(self.track_list.count()):
            item = self.track_list.item(row_index)
            widget = self.track_list.itemWidget(item)
            if widget is not None:
                self.track_list.removeItemWidget(item)
                widget.deleteLater()
        self.track_list.clear()
        for row in results:
            track_id = row["track_id"]
            item = QListWidgetItem()
            item.setData(Qt.UserRole, track_id)
            # tall enough that the checkbox's own click area (its label +
            # indicator, not the bare row) is a real touch target
            item.setSizeHint(QSize(0, TOUCH["row_height"]))
            label = f"{row['title']} — {row['artist_name']}"
            if row.get("album"):
                label += f" ({row['album']})"
            checkbox = QCheckBox(label)
            checkbox.setProperty("track_id", track_id)
            checkbox.setProperty("artist_id", row["artist_id"])
            checkbox.setProperty("title", row["title"])
            # reflects a pick made under an earlier search term
            checkbox.setChecked(track_id in self._picks)
            checkbox.toggled.connect(self._on_checkbox_toggled)
            self.track_list.addItem(item)
            self.track_list.setItemWidget(item, checkbox)
        self._update_hint()

    def _on_checkbox_toggled(self, checked: bool) -> None:
        box = self.sender()
        track_id = box.property("track_id")
        artist_id = box.property("artist_id")
        title = box.property("title")
        if checked:
            if len(self._picks) >= self._max_picks and track_id not in self._picks:
                # over the limit - put it back rather than silently bumping
                # an earlier pick
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
                return
            self._picks[track_id] = artist_id
            self._pick_titles[track_id] = title or ""
        else:
            self._picks.pop(track_id, None)
            self._pick_titles.pop(track_id, None)
        self._update_hint()

    def _sync_checkbox_for(self, track_id: int) -> None:
        """Checks `track_id`'s row if it's currently in the visible results
        list - used by `check_track` below, which updates `self._picks`
        (the real source of truth) regardless of whether that row happens
        to be on screen right now."""
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            if item.data(Qt.UserRole) == track_id:
                checkbox = self.track_list.itemWidget(item)
                if checkbox is not None and not checkbox.isChecked():
                    checkbox.setChecked(True)
                return

    def check_track(self, track_id: int, artist_id: int, title: str = "") -> None:
        """Pre-selects one track - used when opened from a track-level
        Jukebox toggle so the tapped track starts out picked. Call after
        construction, once the initial queries have narrowed the results to
        it. `title` is only needed by `selected_rows()` callers."""
        self._picks[track_id] = artist_id
        if title:
            self._pick_titles[track_id] = title
        self._sync_checkbox_for(track_id)
        self._update_hint()

    def _update_hint(self) -> None:
        count = len(self._picks)
        self.hint.setText(f"{count} of {self._max_picks} selected")
        self.ok_button.setEnabled(count > 0)

    def selected_picks(self) -> list[tuple[int, int]]:
        """Every current pick as `(track_id, artist_id)` pairs - one
        `place_track` call per pair is what both call sites do with this
        (see `JukeboxView._open_add_dialog`)."""
        return list(self._picks.items())

    def selected_genre(self) -> Optional[str]:
        return self.genre_combo.currentData()

    def selected_rows(self) -> list[dict]:
        """Every current pick as `{"track_id", "artist_id", "title"}`
        dicts - like `selected_picks()` but with the title, for
        `JukeboxEditSongsDialog`'s summary."""
        return [
            {"track_id": tid, "artist_id": aid, "title": self._pick_titles.get(tid, "")}
            for tid, aid in self._picks.items()
        ]


class JukeboxEditSongsDialog(QDialog):
    """"Edit songs…": this slot's two sides, each with its current title
    (or "— empty —") and Change…/Clear buttons. "Change…" opens a nested
    `JukeboxPickerDialog` (one pick, no genre) scoped to the slot's artist.

    Only tracks a plan (`self._plan`) while open; `plan()` returns just the
    sides touched, so `JukeboxView._on_edit_requested` leaves the rest
    as-is. All writes happen there, via `services.jukebox.set_slot_side`,
    after the dialog is accepted - the dialog reports intent, the view owns
    the database, like every other card action."""

    #: sentinel meaning "this side hasn't been touched" - distinct from a
    #: real planned value of `(None, "")` (an explicit Clear).
    _UNCHANGED = object()

    def __init__(self, parent, row: dict, search_tracks: Callable[[str, str], list[dict]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit songs — Slot {row['slot_number']}")
        self._row = row
        self._search_tracks = search_tracks
        self._plan: dict[str, object] = {"A": self._UNCHANGED, "B": self._UNCHANGED}
        self._labels: dict[str, QLabel] = {}
        self._clear_buttons: dict[str, TouchButton] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(dim_label(row.get("artist_name", "")))

        for side in ("A", "B"):
            row_widget = QWidget()
            h = QHBoxLayout(row_widget)
            h.setContentsMargins(0, 0, 0, 0)
            label = QLabel()
            self._labels[side] = label
            h.addWidget(label, 1)
            change_btn = TouchButton("Change…")
            change_btn.clicked.connect(lambda _=False, s=side: self._on_change_clicked(s))
            h.addWidget(change_btn)
            clear_btn = TouchButton("Clear")
            clear_btn.clicked.connect(lambda _=False, s=side: self._on_clear_clicked(s))
            self._clear_buttons[side] = clear_btn
            h.addWidget(clear_btn)
            layout.addWidget(row_widget)
            self._refresh_label(side)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _current(self, side: str) -> tuple[Optional[int], str]:
        """`(track_id_or_None, title)` for `side` - whatever's planned so
        far if this side has already been touched this session, otherwise
        the slot's actual current value from `row` (as `get_slot_row`
        shaped it: `row["side_a"]`/`row["side_b"]`, each `None` or a dict
        with a `"title"` key)."""
        plan_value = self._plan[side]
        if plan_value is not self._UNCHANGED:
            return plan_value
        data = self._row.get("side_a" if side == "A" else "side_b")
        if data is None:
            return None, ""
        return data["track_id"], data["title"]

    def _refresh_label(self, side: str) -> None:
        track_id, title = self._current(side)
        self._labels[side].setText(f"Side {side}: {title}" if title else f"Side {side}: — empty —")
        self._clear_buttons[side].setEnabled(track_id is not None)

    def _on_change_clicked(self, side: str) -> None:
        dialog = JukeboxPickerDialog(
            self,
            self._search_tracks,
            initial_artist_query=self._row.get("artist_name", ""),
            max_picks=1,
            title=f"Choose a song for side {side}",
            show_genre=False,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        rows = dialog.selected_rows()
        if not rows:
            return
        picked = rows[0]
        self._plan[side] = (picked["track_id"], picked["title"])
        self._refresh_label(side)

    def _on_clear_clicked(self, side: str) -> None:
        self._plan[side] = (None, "")
        self._refresh_label(side)

    def plan(self) -> dict[str, tuple[Optional[int], str]]:
        """`{"A": (track_id_or_None, title), ...}` - only sides changed or
        cleared this session."""
        return {side: value for side, value in self._plan.items() if value is not self._UNCHANGED}


class JukeboxOrganizeDialog(QDialog):
    """"Organize card…": a genre combo plus a "move to slot #" picker -
    re-filing a card's genre and moving its board position are independent
    operations (`services.jukebox.set_slot_genre` vs. `swap_slots`).

    The location picker lists every *other* slot in the selected genre, by
    number and artist, rather than taking a typed number: slot numbers are
    never reused or renumbered (see services/jukebox.py:JukeboxSlot), so
    there can be gaps. `slots` is (slot_number, artist_name, genre) for the
    whole board, not just the current page, so source and target never
    need to be visible together; changing the genre re-filters the list
    live. A leading "Stay in place" entry (`itemData=None`) makes the move
    optional - a genre-only change is a complete action, so OK is never
    disabled.
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
        """Rebuilds the location list for `genre` - up front and whenever
        the genre combo changes, so the two controls always agree."""
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


#: how long a genre chip has to be held down, without moving or letting go,
#: before its menu opens. Long enough that an ordinary tap (picking which
#: genre's board is showing) never opens it by accident.
GENRE_CHIP_LONG_PRESS_MS = 600

#: custom MIME type carrying a dragged genre chip's name (UTF-8) - chip
#: drag-to-reorder (see _GenreChip). Distinct from jukebox_strip.py's
#: SLOT_MIME_TYPE so a card can't be dropped on a chip or vice versa.
GENRE_MIME_TYPE = "application/x-musicmgr-jukebox-genre"


def _decode_genre(mime: QMimeData) -> Optional[str]:
    if not mime.hasFormat(GENRE_MIME_TYPE):
        return None
    try:
        return bytes(mime.data(GENRE_MIME_TYPE)).decode("utf-8") or None
    except UnicodeDecodeError:
        return None


class _GenreChip(ChipButton):
    """One genre chip on the Jukebox page's filter row - `ChipButton` plus
    a long-press menu, a right-click menu and drag-to-reorder. Subclassed
    here, not added to `ChipButton`, so every other chip row in the app
    keeps plain tap-to-toggle behaviour.

    `renameRequested`/`deleteRequested` carry nothing - `JukeboxView`
    reads the chip's `property("genre")` when it connects them.

    A long press fires *while the button is still held*: `_press_timer`
    starts on press and any move or release before it fires cancels it.
    `_long_press_fired` tells `mouseReleaseEvent` to swallow the click that
    would otherwise follow and toggle the chip. A quick tap is untouched.

    The right-click menu offers the same actions for pointer users, like
    `ui/widgets/jukebox_strip.py`'s card menu."""

    renameRequested = Signal()
    deleteRequested = Signal()
    #: "Play all" / "Shuffle" on the chip's menu. Argument: shuffle.
    playRequested = Signal(bool)
    #: (dragged genre, genre it was dropped on, dropped on its right half) -
    #: drag-to-reorder. Press a chip and slide it sideways
    #: past the drag threshold to pick it up; drop it on another chip's
    #: left or right half to land before or after it. A bar on that edge
    #: shows where it'll go while hovering. JukeboxView turns this into
    #: services.jukebox.move_genre. A quick tap and a long press (rename)
    #: are unchanged - any movement already cancelled the long-press timer,
    #: and a drag swallows the click that would otherwise follow.
    reorderRequested = Signal(str, str, bool)

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self._long_press_fired = False
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_long_press_timeout)
        self._drag_start_pos = None
        #: None, "left" or "right" - which edge the drop-position bar is
        #: painted on while another chip hovers over this one
        self._drop_side: Optional[str] = None
        #: (target genre, after) recorded by the chip this one was dropped
        #: on - emitted as reorderRequested only once this chip's own
        #: drag.exec() has returned (see _start_drag)
        self._pending_drop: Optional[tuple[str, bool]] = None
        self.setAcceptDrops(True)

    @property
    def genre(self) -> str:
        return self.property("genre") or self.text()

    def mousePressEvent(self, event) -> None:  # noqa: D102 - Qt override
        if event.button() == Qt.LeftButton:
            self._long_press_fired = False
            self._drag_start_pos = event.position().toPoint()
            self._press_timer.start(GENRE_CHIP_LONG_PRESS_MS)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: D102 - Qt override
        if self._press_timer.isActive():
            self._press_timer.stop()
        if (
            self._drag_start_pos is not None
            and not self._long_press_fired
            and event.buttons() & Qt.LeftButton
        ):
            moved = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._drag_start_pos = None
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def _start_drag(self) -> None:
        """Same QDrag shape as jukebox_strip.py's card drag. `exec()` runs
        its own event loop until release, and the release it consumes
        never reaches this button - so the pressed-down look is cleared by
        hand afterwards, and `_long_press_fired` is reused to swallow a
        stray release/click if one does arrive.

        The drop target only *records* where this chip landed
        (`_pending_drop`); the reorder signal goes out from here, after
        `exec()` returns. Emitting it from inside the target's dropEvent
        would rebuild the whole chip row (and deleteLater this very chip)
        while this method is still on the stack inside the drag's nested
        event loop - Windows' drag loop can process that deferred delete
        before exec() returns."""
        self._pending_drop = None
        mime = QMimeData()
        mime.setData(GENRE_MIME_TYPE, self.genre.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(self.mapFromGlobal(QCursor.pos()))
        self._long_press_fired = True
        drag.exec(Qt.MoveAction)
        self.setDown(False)
        pending, self._pending_drop = self._pending_drop, None
        if pending is not None:
            self.reorderRequested.emit(self.genre, pending[0], pending[1])

    def _side_for(self, x: float) -> str:
        return "right" if x >= self.width() / 2 else "left"

    def _set_drop_side(self, side: Optional[str]) -> None:
        if side != self._drop_side:
            self._drop_side = side
            self.update()

    def dragEnterEvent(self, event) -> None:  # noqa: D102 - Qt override
        source = _decode_genre(event.mimeData())
        if source is not None and source != self.genre:
            event.acceptProposedAction()
            self._set_drop_side(self._side_for(event.position().x()))
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: D102 - Qt override
        source = _decode_genre(event.mimeData())
        if source is not None and source != self.genre:
            event.acceptProposedAction()
            self._set_drop_side(self._side_for(event.position().x()))
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._set_drop_side(None)

    def dropEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._set_drop_side(None)
        source = _decode_genre(event.mimeData())
        if source is None or source == self.genre:
            event.ignore()
            return
        event.acceptProposedAction()
        dragged = event.source()
        if isinstance(dragged, _GenreChip):
            dragged._pending_drop = (self.genre, self._side_for(event.position().x()) == "right")

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        super().paintEvent(event)
        if self._drop_side is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS["jukebox_key_hi"]))
        bar_w = 4
        x = 0 if self._drop_side == "left" else self.width() - bar_w
        painter.drawRoundedRect(x, 4, bar_w, self.height() - 8, 2, 2)
        painter.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._press_timer.stop()
        self._drag_start_pos = None
        if self._long_press_fired:
            # swallow the click a long press already handled - see the
            # class docstring - rather than also toggling checked state.
            self._long_press_fired = False
            return
        super().mouseReleaseEvent(event)

    def _on_long_press_timeout(self) -> None:
        """Holding a chip opens the same menu a right-click does (Rename,
        Delete, Play all, Shuffle). `popup`, not `exec`, so it doesn't
        block."""
        self._long_press_fired = True
        self._menu = self._build_context_menu()
        self._menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def contextMenuEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._build_context_menu().exec(event.globalPos())

    def _build_context_menu(self) -> QMenu:
        """Split out from `contextMenuEvent` so a test can trigger an
        action directly (find it by text, `.trigger()`) without driving
        `QMenu.exec()` - as `ui/widgets/jukebox_strip.py` does for cards."""
        menu = QMenu(self)
        rename_action = menu.addAction("Rename genre…")
        rename_action.triggered.connect(self.renameRequested.emit)
        menu.addSeparator()
        delete_action = menu.addAction("Delete genre…")
        delete_action.triggered.connect(self.deleteRequested.emit)
        menu.addSeparator()
        play_action = menu.addAction("▶ Play all")
        play_action.triggered.connect(lambda: self.playRequested.emit(False))
        shuffle_action = menu.addAction("⇄ Shuffle")
        shuffle_action.triggered.connect(lambda: self.playRequested.emit(True))
        return menu


class _InlineGenreEdit(QLineEdit):
    """The text box a genre chip turns into mid-rename (or that the "+"
    add-chip turns into). Plain `QLineEdit` plus one addition: `QLineEdit` has no built-in
    "the user backed out" signal, so Escape is caught here and turned into
    one (`cancelled`) rather than doing nothing, or - Qt's usual default
    for Escape inside a dialog - closing a window this box doesn't own.
    `JukeboxView` treats `cancelled` and the ordinary `editingFinished`
    (Enter, or focus lost) as the two ways an edit can end; see
    `_finish_genre_edit`."""

    cancelled = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: D102 - Qt override
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


class _GridHost(QWidget):
    """The plain container `JukeboxView.grid` lives in - overrides
    `minimumSizeHint()` to always report zero rather than the default Qt
    behavior of asking its layout, which would otherwise report however
    much space the *currently placed* fixed-size strips need.

    Without it, growing to a wide arrangement (e.g. MAX_COLUMNS on a
    maximized window) pinned the whole window's minimum width: the grid's
    SetNoConstraint stops it forcing this widget's `minimumSize()` up, but
    a parent layout (`BaseView._root`) consults `minimumSizeHint()`, which
    still delegates to the grid's computed minimum. `_rows_that_fit` /
    `_cols_that_fit` decide how many strips fit, not the last
    arrangement."""

    def minimumSizeHint(self) -> QSize:  # noqa: D102 - Qt override
        return QSize(0, 0)


class JukeboxView(BaseView):
    title_text = "Jukebox"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        # no left margin on this page (BaseView's shared 20px is left alone
        # for the other views): with `self.grid`'s margins zeroed too, the
        # only gap left by the rail is the cards' own 4px frame
        # (ui/widgets/jukebox_strip.py)
        _left, top, right, bottom = self._root.getContentsMargins()
        self._root.setContentsMargins(0, top, right, bottom)
        self._page = 0
        # settled by the first refresh()/resize measurement; a starting
        # value for `resizeEvent`'s "did this change" check
        self._current_rows = MIN_ROWS
        self._current_cols = MIN_COLUMNS
        # which genre chip is active. Starts on DEFAULT_JUKEBOX_GENRE rather
        # than the first chip, the genre a card filed without a choice gets
        self._genre = jkb_svc.DEFAULT_JUKEBOX_GENRE

        # drag-and-drop page-flip (see the module docstring): fires _flip_page_during_drag once a dragged card has
        # hovered a page arrow for PAGE_FLIP_HOVER_MS; _page_flip_delta is
        # which direction that hover was over (-1/prev_btn, +1/next_btn),
        # set fresh on every DragEnter in eventFilter below.
        self._page_flip_timer = QTimer(self)
        self._page_flip_timer.setSingleShot(True)
        self._page_flip_timer.timeout.connect(self._flip_page_during_drag)
        self._page_flip_delta = 0

        self.now_playing_label = QLabel("NOW PLAYING — —")
        self.now_playing_label.setObjectName("JukeboxNowPlayingText")
        self.header.addWidget(self.now_playing_label)
        self.header.addStretch(1)


        # the pager lives in the header, giving its row to the grid.
        # "JukeboxPageArrow", not the shared "RowPageArrow", so this page's
        # arrows can be styled bigger on their own (see ui/theme.py).
        self.prev_btn = TouchButton("‹")
        self.prev_btn.setObjectName("JukeboxPageArrow")
        self.prev_btn.clicked.connect(lambda: self._change_page(-1))
        self.page_label = dim_label("")
        self.next_btn = TouchButton("›")
        self.next_btn.setObjectName("JukeboxPageArrow")
        self.next_btn.clicked.connect(lambda: self._change_page(1))
        # lets a dragged card flip the page by hovering over either arrow -
        # see eventFilter/_flip_page_during_drag below
        self.prev_btn.setAcceptDrops(True)
        self.next_btn.setAcceptDrops(True)
        self.prev_btn.installEventFilter(self)
        self.next_btn.installEventFilter(self)
        self.header.addWidget(self.prev_btn)
        self.header.addWidget(self.page_label)
        self.header.addWidget(self.next_btn)
        self.header.addSpacing(20)

        add_btn = TouchButton("+ Add to jukebox", primary=True)
        # its own object name so ui/theme.py's #PrimaryWarm rule styles this
        # button apart from the generic #Primary
        add_btn.setObjectName("PrimaryWarm")
        add_btn.clicked.connect(self._open_add_dialog)
        self.header.addWidget(add_btn)

        # genre chips - exclusive, like every other chip row in the app; they
        # pick which board is showing. Built by `_build_genre_chips` from the
        # persisted list, in its order, and rebuilt after every edit -
        # hence `self.chip_row` / `self._add_chip`. The trailing stretch is
        # added once and every rebuild inserts before it (`_stretch_index`),
        # keeping the chips left-aligned.
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        self.chip_row = chip_row
        self._genre_chips = QButtonGroup(self)
        self._genre_chips.setExclusive(True)
        self._genre_chips.buttonClicked.connect(self._on_genre_chip_clicked)
        self._add_chip: Optional[QWidget] = None
        # the chip/add-button currently swapped out for `_genre_editor`, and
        # what its edit is doing - see `_begin_rename_genre` /
        # `_begin_add_genre` / `_finish_genre_edit`. `_genre_editor is None`
        # means nothing is being edited; only one editor opens at a time.
        self._genre_editor: Optional[_InlineGenreEdit] = None
        self._genre_editor_target: Optional[QWidget] = None
        self._genre_editor_original: Optional[str] = None
        self._genre_editor_mode: Optional[str] = None
        chip_row.addStretch(1)
        self._build_genre_chips()
        self.body().addLayout(chip_row)

        # "+ Add to jukebox" is the only way onto the board
        self.empty_state = EmptyState(
            "No favorites yet",
            "Tap “+ Add to jukebox” to load one.",
        )
        self.body().addWidget(self.empty_state)

        # _GridHost plus SetNoConstraint keep this container's size from
        # being dictated by however many strips are placed in it - see
        # _GridHost's docstring
        self.grid_host = _GridHost()
        self.grid = QGridLayout(self.grid_host)
        # rows get 8px, not 16: on a typical maximized window a 4th row
        # (4 strips + 3 gaps) was just a few px short at 16
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(8)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        # QGridLayout has its own default ~9px contents margin on top of
        # `self._root`'s; zeroed so the outer margin alone sets how far the
        # grid sits from the rail
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.body().addWidget(self.grid_host, 1)

        # built MAX_ROWS x MAX_COLUMNS up front regardless of what fits now -
        # a strip is cheap, and this avoids constructing one mid-resize.
        # `_arrange_grid()` places them once the column count is known.
        self.strips: list[JukeboxStripWidget] = []
        for _ in range(MAX_ROWS * MAX_COLUMNS):
            strip = JukeboxStripWidget()
            strip.sideActivated.connect(self._on_side_activated)
            strip.fillRequested.connect(self._on_fill_requested)
            strip.editRequested.connect(self._on_edit_requested)
            strip.moveRequested.connect(self._on_organize_requested)
            strip.removeRequested.connect(self._on_remove_requested)
            strip.reorderRequested.connect(self._on_reorder_requested)
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
        short (or not-yet-shown) window still gets MIN_ROWS rows at their
        fixed size rather than smaller cards."""
        strip_height = self.strips[0].sizeHint().height()
        spacing = self.grid.verticalSpacing()
        available = self.grid_host.height()
        for rows in range(MAX_ROWS, MIN_ROWS, -1):
            needed = rows * strip_height + (rows - 1) * spacing
            if available >= needed:
                return rows
        return MIN_ROWS

    def _cols_that_fit(self) -> int:
        """`_rows_that_fit`'s logic against `grid_host.width()`. Never
        returns below MIN_COLUMNS."""
        strip_width = self.strips[0].sizeHint().width()
        spacing = self.grid.horizontalSpacing()
        available = self.grid_host.width()
        for cols in range(MAX_COLUMNS, MIN_COLUMNS, -1):
            needed = cols * strip_width + (cols - 1) * spacing
            if available >= needed:
                return cols
        return MIN_COLUMNS

    def _arrange_grid(self) -> None:
        """Places however many strips the current page size
        (`_current_rows` x `_current_cols`) needs into `self.grid` at their
        (row, col) for that column count, in reading order. `QGridLayout`
        has to be told every cell, and a strip's position is only correct
        for the column count it was placed under, so this runs whenever
        either dimension changes. Clear-and-rebuild is fine for a few dozen
        strips."""
        for strip in self.strips:
            self.grid.removeWidget(strip)
        per_page = self._current_cols * self._current_rows
        for i, strip in enumerate(self.strips[:per_page]):
            self.grid.addWidget(strip, i // self._current_cols, i % self._current_cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Qt may not have finished re-laying-out grid_host to its new size
        # by the time this handler runs - layout updates for a resized
        # widget's children can be posted rather than applied synchronously
        # - so a zero-delay singleShot defers the actual measurement to the
        # next trip through the event loop, after Qt has settled the new
        # geometry. Same "recompute, only act if it changed" idiom as
        # ui/widgets/album_tracks.py's TrackListWidget.resizeEvent. The
        # later second recheck is a no-op when the first got it right, and
        # covers a big jump that settles late.
        QTimer.singleShot(0, self._apply_grid_for_current_size)
        QTimer.singleShot(200, self._apply_grid_for_current_size)

    def _apply_grid_for_current_size(self) -> None:
        """Re-measure rows and columns; refresh only if either changed."""
        rows = self._rows_that_fit()
        cols = self._cols_that_fit()
        if rows == self._current_rows and cols == self._current_cols:
            return
        self._current_rows = rows
        self._current_cols = cols
        self.refresh()

    def _on_genre_chip_clicked(self, button) -> None:
        genre = button.property("genre")
        if genre == self._genre:
            return
        self._genre = genre
        self._page = 0
        self.refresh()

    def _select_genre(self, genre: str) -> None:
        """Switch the board to `genre` and check its chip, as a tap would."""
        for chip in self._genre_chips.buttons():
            if chip.property("genre") == genre:
                chip.setChecked(True)
        self._genre = genre
        self._page = 0
        self.refresh()

    # -- genre management ----------------------------------------------------
    # The chip row's side of the editable genre list (services/jukebox.py
    # holds the list): building the row, `_GenreChip`'s signals, and the
    # inline `_InlineGenreEdit` box a chip (or the "+" chip) turns into.

    def _stretch_index(self) -> int:
        """`chip_row`'s trailing stretch (added once in `__init__` and
        never removed) is always the last item - every insert below
        targets this index, so a new chip always lands just before it
        regardless of how many chips currently exist."""
        return self.chip_row.count() - 1

    def _build_genre_chips(self) -> None:
        """(Re)builds the whole genre row from the persisted list -
        called once from `__init__` and again after any add/rename/delete
        commits (`_finish_genre_edit`, `_on_delete_genre_requested`).
        Tearing the whole row down and rebuilding it fresh, rather than
        trying to patch in just what changed, mirrors this same class's
        `refresh()` for the card grid - genre edits are rare.

        `self._genre_chips` (the `QButtonGroup`) is reused across rebuilds,
        so the `buttonClicked` connection made in `__init__` keeps
        working."""
        for button in list(self._genre_chips.buttons()):
            self._genre_chips.removeButton(button)
            self.chip_row.removeWidget(button)
            button.deleteLater()
        if self._add_chip is not None:
            self.chip_row.removeWidget(self._add_chip)
            self._add_chip.deleteLater()
            self._add_chip = None

        with self.ctx.session() as session:
            genres = jkb_svc.get_jukebox_genres(session)
        # the active chip's genre may itself have just been renamed or
        # deleted (this same rebuild is what both of those call) - fall
        # back to whatever's now first rather than pointing at a genre
        # that no longer has a chip.
        if self._genre not in genres:
            self._genre = genres[0] if genres else jkb_svc.DEFAULT_JUKEBOX_GENRE

        for genre in genres:
            # "&&": a lone "&" is a Qt keyboard-shortcut marker, so "R&B"
            # would show as "RB" with the B underlined. The real
            # name lives in the "genre" property, set just below.
            chip = _GenreChip(genre.replace("&", "&&"))
            # overrides ChipButton's "Chip" object name so only these chips
            # pick up ui/theme.py's #ChipWarm:checked rule
            chip.setObjectName("ChipWarm")
            chip.setProperty("genre", genre)
            chip.setChecked(genre == self._genre)
            chip.renameRequested.connect(
                lambda c=chip, g=genre: self._begin_rename_genre(c, g)
            )
            chip.deleteRequested.connect(lambda g=genre: self._on_delete_genre_requested(g))
            chip.playRequested.connect(lambda shuffle, g=genre: self.play_genre(shuffle, genre=g))
            chip.reorderRequested.connect(self._on_genre_reorder_requested)
            self._genre_chips.addButton(chip)
            self.chip_row.insertWidget(self._stretch_index(), chip)

        add_chip = ChipButton("+")
        add_chip.setObjectName("ChipWarm")
        add_chip.setCheckable(False)  # a command, not a filter - never "checked"
        add_chip.setToolTip("Add a genre")
        add_chip.clicked.connect(self._begin_add_genre)
        self.chip_row.insertWidget(self._stretch_index(), add_chip)
        self._add_chip = add_chip

    def _begin_rename_genre(self, chip: "_GenreChip", genre: str) -> None:
        """Fired by a chip's `renameRequested` ("Rename genre…" on its
        menu) - swaps that chip for an `_InlineGenreEdit` pre-filled with
        its name, selected and focused, so typing replaces it. Ignored if
        an edit is already open - only one at a time."""
        if self._genre_editor is not None:
            return
        self._open_genre_editor(chip, initial_text=genre, mode="rename", original=genre)

    def _begin_add_genre(self) -> None:
        """Fired by tapping the trailing "+" chip - the same inline
        text-box swap `_begin_rename_genre` uses, just starting blank and
        in place of the "+" chip instead of an existing one."""
        if self._genre_editor is not None:
            return
        self._open_genre_editor(self._add_chip, initial_text="", mode="add", original=None)

    def _open_genre_editor(
        self, target: QWidget, *, initial_text: str, mode: str, original: Optional[str]
    ) -> None:
        target.hide()
        editor = _InlineGenreEdit(initial_text)
        editor.setObjectName("GenreChipEditor")
        if mode == "add":
            editor.setPlaceholderText("New genre")
        editor.setMinimumWidth(max(target.sizeHint().width(), 90))
        editor.cancelled.connect(lambda: self._finish_genre_edit(commit=False))
        editor.editingFinished.connect(lambda: self._finish_genre_edit(commit=True))
        self.chip_row.insertWidget(self.chip_row.indexOf(target), editor)
        editor.setFocus(Qt.OtherFocusReason)
        editor.selectAll()
        self._genre_editor = editor
        self._genre_editor_target = target
        self._genre_editor_original = original
        self._genre_editor_mode = mode

    def _finish_genre_edit(self, commit: bool) -> None:
        """Ends whichever edit `_open_genre_editor` started - reached from
        `_InlineGenreEdit.cancelled` (Escape) or `editingFinished` (Enter,
        or focus lost). The `self._genre_editor is None` guard stops the
        two double-handling one edit: removing the box after Escape can
        still deliver `editingFinished` a moment later.

        Either way the box goes and the chip/add-button it replaced comes
        back; a commit then calls the matching `services/jukebox.py`
        mutation and, if it changed something, rebuilds the row
        (`_build_genre_chips`) and refreshes the board."""
        if self._genre_editor is None:
            return
        editor = self._genre_editor
        target = self._genre_editor_target
        original = self._genre_editor_original
        mode = self._genre_editor_mode
        self._genre_editor = None
        self._genre_editor_target = None
        self._genre_editor_original = None
        self._genre_editor_mode = None

        new_name = editor.text().strip()
        self.chip_row.removeWidget(editor)
        editor.deleteLater()
        if target is not None:
            target.show()

        if not commit or not new_name:
            return

        with self.ctx.session() as session:
            if mode == "rename":
                ok = jkb_svc.rename_genre(session, original, new_name)
            else:
                ok = jkb_svc.add_genre(session, new_name)
        if not ok:
            self.ctx.notify(f'A genre named "{new_name}" already exists')
            return
        if mode == "rename" and self._genre == original:
            self._genre = new_name
        self._build_genre_chips()
        self._page = 0
        self.refresh()

    def _on_genre_reorder_requested(self, source: str, target: str, after: bool) -> None:
        """A chip was dragged onto another (see `_GenreChip.reorderRequested`)
        - lands `source` just before `target`, or just after it when
        dropped on `target`'s right half. The board showing and its page
        don't change; only the row order does, so this rebuilds the chips
        but skips `refresh()`."""
        if self._genre_editor is not None:
            return
        with self.ctx.session() as session:
            genres = list(jkb_svc.get_jukebox_genres(session))
            if source not in genres or target not in genres:
                return
            index = genres.index(target) + (1 if after else 0)
            # positions after the dragged chip shift left by one once it's
            # lifted out of the row
            if genres.index(source) < index:
                index -= 1
            moved = jkb_svc.move_genre(session, source, index)
        if moved:
            self._build_genre_chips()

    def _on_delete_genre_requested(self, genre: str) -> None:
        """Fired by a chip's `deleteRequested` ("Delete genre…" on its
        menu). Confirms first, naming how many cards will move - deleting
        a genre can't be undone and relabels every card still filed under
        it (see `services/jukebox.py:delete_genre`)."""
        if self._genre_editor is not None:
            return
        with self.ctx.session() as session:
            genres = jkb_svc.get_jukebox_genres(session)
            card_count = jkb_svc.slot_count(session, genre=genre)
        if len(genres) <= 1:
            self.ctx.notify("At least one genre must remain")
            return
        if card_count:
            message = (
                f'Delete "{genre}"? {card_count} card{"s" if card_count != 1 else ""} '
                f"currently on this board will move to another genre."
            )
        else:
            message = f'Delete "{genre}"?'
        confirm = QMessageBox.question(self, "Delete genre", message)
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            deleted = jkb_svc.delete_genre(session, genre)
        if not deleted:
            return
        self._build_genre_chips()
        self._page = 0
        self.refresh()

    def refresh(self) -> None:
        self._current_rows = self._rows_that_fit()
        self._current_cols = self._cols_that_fit()
        self._arrange_grid()
        per_page = self._current_cols * self._current_rows
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
        """Keep the "NOW PLAYING" display current, and turn to the page
        holding the playing song's card when that card is on the genre
        board being shown (so a shuffled genre's cards follow the music)."""
        if item is not None:
            per_page = max(1, self._current_cols * self._current_rows)
            with self.ctx.session() as session:
                index = jkb_svc.slot_position(session, self._genre, item.track_id)
            if index is not None:
                self._page = index // per_page
        self.refresh()

    def _change_page(self, delta: int) -> None:
        self._page += delta
        self.refresh()

    # -- drag-and-drop page-flip ----------------------------------------------

    def eventFilter(self, obj, event):  # noqa: D102 - Qt override
        """Watches `prev_btn`/`next_btn` (installed on them in `__init__`)
        for a jukebox card hovering during a drag - see the module
        docstring. Every branch here returns True: neither arrow has any
        other event-filtered behavior of its own to fall through to, and
        swallowing a non-jukebox drag's events here (rather than letting
        Qt fall back to `QWidget`'s defaults) keeps an unrelated drag from
        ever landing on a page arrow as if it were a real drop target."""
        if obj is self.prev_btn or obj is self.next_btn:
            etype = event.type()
            if etype == QEvent.DragEnter:
                if event.mimeData().hasFormat(SLOT_MIME_TYPE):
                    event.acceptProposedAction()
                    self._page_flip_delta = -1 if obj is self.prev_btn else 1
                    self._maybe_start_page_flip_timer()
                else:
                    event.ignore()
                return True
            if etype == QEvent.DragMove:
                if event.mimeData().hasFormat(SLOT_MIME_TYPE):
                    event.acceptProposedAction()
                else:
                    event.ignore()
                return True
            if etype == QEvent.DragLeave:
                self._page_flip_timer.stop()
                return True
            if etype == QEvent.Drop:
                # neither arrow is a real drop target for a card
                event.ignore()
                self._page_flip_timer.stop()
                return True
        return super().eventFilter(obj, event)

    def _maybe_start_page_flip_timer(self) -> None:
        can_flip = (
            self._page_flip_delta < 0 and self.prev_btn.isEnabled()
        ) or (self._page_flip_delta > 0 and self.next_btn.isEnabled())
        if can_flip:
            self._page_flip_timer.start(PAGE_FLIP_HOVER_MS)

    def _flip_page_during_drag(self) -> None:
        self._change_page(self._page_flip_delta)
        self._maybe_start_page_flip_timer()

    # -- playing ------------------------------------------------------------

    def play_genre(self, shuffle: bool, genre: Optional[str] = None) -> None:
        """Play (or shuffle) every song on a genre's cards - the chip menu's
        "Play all" / "Shuffle". Shows that genre's board first, so the cards
        can follow the music."""
        if genre is not None and genre != self._genre:
            self._select_genre(genre)
        with self.ctx.session() as session:
            tracks = jkb_svc.genre_tracks(session, self._genre)
            if not tracks:
                self.ctx.notify(f"No songs on the {self._genre} cards yet")
                return
            self.ctx.player.set_shuffle(shuffle)
            # stay on the Jukebox page, like a real jukebox - the cards
            # flip to each song as it plays (_on_track_changed)
            self.ctx.play_tracks(tracks, start=0, source="jukebox", navigate=False)
        verb = "Shuffling" if shuffle else "Playing"
        self.ctx.notify(f"{verb} {len(tracks)} {self._genre} songs")

    def _on_side_activated(self, side: str, track_id: int) -> None:
        # play_tracks() must be called while the session is still open -
        # QueueItem.from_track() lazily reads track.release/primary_file,
        # which raises DetachedInstanceError (silently, from inside this
        # Qt slot) once the session that loaded `track` has closed.
        with self.ctx.session() as session:
            track = session.get(Track, track_id)
            tracks = [track] if track is not None else []
            if tracks:
                self.ctx.play_tracks(tracks, source="jukebox")

    def _on_fill_requested(self, slot_number: int) -> None:
        """A tap on a one-song card's empty "OPEN" banner. Looks the slot
        up fresh (`jkb_svc.get_slot_row`) since the board may have changed
        since the strip rendered; a vanished slot is a silent no-op.

        Opens `JukeboxPickerDialog` pre-filled with this card's artist and
        genre, capped at one pick - a second pick would have nowhere on
        this card to land (`place_track` would start a new card)."""
        with self.ctx.session() as session:
            row = jkb_svc.get_slot_row(session, slot_number)
            genres = jkb_svc.get_jukebox_genres(session)
        if row is None:
            return
        dialog = JukeboxPickerDialog(
            self,
            self._search_addable_tracks,
            genres=genres,
            default_genre=row["genre"],
            initial_artist_query=row.get("artist_name", ""),
            max_picks=1,
            title="Add a second song",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        picks = dialog.selected_picks()
        genre = dialog.selected_genre() or row["genre"]
        if not picks:
            return
        with self.ctx.session() as session:
            for track_id, artist_id in picks:
                jkb_svc.place_track(session, artist_id, track_id, genre=genre)
        self.ctx.notify("Added to the jukebox")
        self.refresh()

    def _on_edit_requested(self, slot_number: int) -> None:
        """"Edit songs…". Same fresh lookup as `_on_fill_requested`.
        `JukeboxEditSongsDialog.plan()` only reports sides actually
        touched, so an empty plan is a no-op."""
        with self.ctx.session() as session:
            row = jkb_svc.get_slot_row(session, slot_number)
        if row is None:
            return
        dialog = JukeboxEditSongsDialog(self, row, self._search_addable_tracks)
        if dialog.exec() != QDialog.Accepted:
            return
        plan = dialog.plan()
        if not plan:
            return
        with self.ctx.session() as session:
            for side, (track_id, _title) in plan.items():
                jkb_svc.set_slot_side(session, slot_number, side, track_id)
        self.ctx.notify("Jukebox card updated")
        self.refresh()

    def _on_organize_requested(self, slot_number: int) -> None:
        """"Organize card…" - hands the dialog every slot on the board with
        its genre, once, so `JukeboxOrganizeDialog._refresh_slot_options`
        can filter by the selected genre without another DB round-trip.
        A genre-only change is a complete action; moving is a `swap_slots`."""
        with self.ctx.session() as session:
            total = jkb_svc.slot_count(session)
            rows = jkb_svc.list_slot_rows(session, page=0, per_page=max(total, 1))
            genres = jkb_svc.get_jukebox_genres(session)
        slots = [(r["slot_number"], r["artist_name"], r["genre"]) for r in rows]
        current = next((r for r in rows if r["slot_number"] == slot_number), None)
        current_genre = current["genre"] if current is not None else self._genre

        dialog = JukeboxOrganizeDialog(
            self, slot_number, slots, current_genre, genres=genres
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

    def _on_reorder_requested(self, source_slot: int, target_slot: int) -> None:
        """A card dropped onto a different card. Unlike Organize's
        two-way `swap_slots`, `services.jukebox.reorder_slot` gives the
        dragged card the target's rank and shifts every card after it by
        one, so the last card on a full page moves to the next one;
        `refresh()` shows the new order. False (the board changed under the
        drag) is a silent no-op."""
        with self.ctx.session() as session:
            reordered = jkb_svc.reorder_slot(session, source_slot, target_slot)
        if reordered:
            self.refresh()

    def _on_remove_requested(self, slot_number: int) -> None:
        """"Remove from jukebox…" - takes the whole card off the board
        after a confirmation (the songs stay in the library - see
        services.jukebox.remove_slot)."""
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
            genres = jkb_svc.get_jukebox_genres(session)
        dialog = JukeboxPickerDialog(
            self,
            self._search_addable_tracks,
            genres=genres,
            default_genre=self._genre,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        picks = dialog.selected_picks()
        genre = dialog.selected_genre() or jkb_svc.DEFAULT_JUKEBOX_GENRE
        if not picks:
            return
        with self.ctx.session() as session:
            for track_id, artist_id in picks:
                jkb_svc.place_track(session, artist_id, track_id, genre=genre)
        self.ctx.notify(
            f"Added {len(picks)} song{'s' if len(picks) != 1 else ''} to the jukebox"
        )
        self.refresh()

    def _search_addable_tracks(self, artist_query: str, track_query: str) -> list[dict]:
        """`JukeboxPickerDialog`'s `search_tracks` callback - straight to
        `services/jukebox.py:search_addable_tracks`, debounced by the
        dialog's own `_search_timer`."""
        with self.ctx.session() as session:
            return jkb_svc.search_addable_tracks(session, artist_query, track_query)


def _side_if_matching(code: Optional[str], slot_number: int) -> Optional[str]:
    """"A7" matches slot 7's A side; anything else (a different slot, or no
    code at all because nothing playing is on the board) matches neither."""
    if not code or code[1:] != str(slot_number):
        return None
    return code[0]
