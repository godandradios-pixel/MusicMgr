"""Title Details: a flat, sortable table of every track in the library.

Replaces the old three-pane Genre browser (genre -> releases -> tracks),
which filed tracks under a release-level tag nobody actually browses music
by. One row per track instead, with the columns James asked for: Genre,
Album Artist, Album, Track #, Title, Time, Year, and a tap-to-set Rating
out of 5 stars (2026-09-05; Album added 2026-09-06). A Jukebox column
lived here too from 2026-09-07 until 2026-09-13, when it was removed
again - see "The Jukebox column" below for that whole arc.

Built on `QAbstractTableModel`/`QTableView` rather than the `QTreeWidget`
"own the sort" pattern `VideoTable`/`ChartTable` use (see video_table.py's
docstring) - that pattern allocates a real `QTreeWidgetItem` per row, fine at
the hundreds-to-low-thousands scale a video or chart library runs at, but
this table is every track in the whole library - tens of thousands of rows
on a real collection. A model only ever asks for data the view is actually
about to paint, so building or re-sorting the table stays fast regardless of
how large the library gets. Sorting is still manual in spirit - `sort()`
just re-orders the Python list backing the model rather than trying to make
per-item text comparison do something it was never built for (the same
numeric-vs-string problem `VideoTable`'s docstring describes for Year/Time).
Row *height* needs the same rigor at this scale and, until a 2026-09-06
follow-up (James: a real ~98,000-track library going "Not Responding"),
didn't have it - `QTableView` word-wraps by default, so a long Genre/Album/
Title value wrapping to two lines made Qt compute every row's height from
its content instead of a single fixed number. `setWordWrap(False)` plus
pinning the vertical header to `Fixed` (not just a default row height that
content can still grow past) makes row height an O(1) lookup again
regardless of library size; Qt still elides an overlong value with "..." on
its own, so nothing becomes unreadable.

This table joins the shared A-Z jump bar `LibraryView` shows under the search
box, the same as Albums/Artists/Tracks (2026-09-06 - it briefly opted out,
closer to Charts' admin-table treatment, until James asked for it back for
consistency with the rest of Library). `jump_letters()`/`scroll_to_letter()`
only make sense while the table is sorted by one of its four text columns -
Genre, Album Artist, Album, or Title (`ALPHA_SORT_COLS`) - since Track #/
Time/Year/Rating have no alphabetical order to jump within; `LibraryView`
hides the shared bar automatically whenever the active sort isn't one of
those four, the same way it already hides the bar for a grid sorted by
something non-alphabetical (Albums by Year, say). Column headers remain the
primary browsing tool here regardless; the search box searches every column
shown here, not just the four text ones (James, 2026-09-06 - see
`_row_matches`), unlike Artists/Albums/Tracks, which each search only their
own kind of thing now.

The Rating column is interactive: `RatingDelegate` paints five stars per row
and a tap sets the rating to however many stars sit at or left of it -
tapping the star that already matches the current rating clears it back to
unrated, so there's always a way to remove a rating without a separate
control. The stars themselves shrank on a 2026-09-13 follow-up (see below)
- same interaction, smaller footprint.

The Jukebox column's whole arc: added 2026-09-07 (James: "I don't like
using my 5 stars to get a track on the Jukebox cards. Can we create
another column to denote what track is included as a card... to select,
maybe to the right of the 5 star ratings we can add a little Jukebox
label or picture that can be clicked to get on the jukebox cards.") as a
second, independent interactive column - `JukeboxDelegate` painted one
glyph, a filled circle when the track was loaded onto the jukebox board
and an outline when it wasn't, flipped by a tap anywhere in the cell.
Before that, a track's star rating and its jukebox membership were the
same thing (5 stars auto-loaded it, dropping below 5 auto-removed it);
this column split them into fully independent controls side by side.
Unlike `RatingDelegate`, which computes the new rating from tap position
alone and applies it immediately, the Jukebox toggle couldn't be
optimistic the same way - flipping a track *onto* the board can fail (no
resolvable album artist to file a slot under), and, since a 2026-09-13
follow-up earlier the same day this column was removed, turning it on
opened `ui/views/jukebox.py`'s `JukeboxPickerDialog` for a genre choice
rather than landing immediately either - so the delegate's tap only asked
the model to emit `jukeboxToggleRequested`, with `on_jukebox` updated only
once `LibraryView` called back with the confirmed result.

Removed the same day, a follow-up later still - James: "remove the
jukebox option on the Title details table to give me more space for the
title of the song." `COL_JUKEBOX`, `JukeboxDelegate`,
`jukeboxToggleRequested`, `request_jukebox_toggle`, `set_on_jukebox`, and
`ROLE_ON_JUKEBOX` are all gone from this file, along with
`LibraryView`'s handler side of the same feature
(`_on_jukebox_toggle_requested`/`_open_jukebox_picker_for`/that view's own
`_search_addable_tracks`, all now-unreachable without a column to tap).
`TrackDetailRow.on_jukebox` itself stays - `services.library.
list_track_details` still computes it, and `_row_matches` still lets a
search for the word "jukebox" find those tracks - only the dedicated
column and its tap-to-toggle affordance are gone. Adding a track to the
jukebox board is still possible from Now Playing's own toggle or the
Jukebox page's own "+ Add to jukebox" picker; Title Details just isn't a
third entry point for it any more.

A search match can also be a video (2026-09-06, see `LibraryView.
_load_details` and the module docstring in `views/library.py`) - woven in as
an ordinary `TrackDetailRow` with `is_video=True`, genre "Video", and no
rating, rather than a separate row type or panel. `RatingDelegate` refuses to
paint or edit stars for one (a video has no rating field to set), and
tapping anywhere else on the row plays it instead (`_on_clicked`,
`videoActivated`). Video rows only ever appear as a search match, never in
the default unfiltered table - the same rule the other three Library
presentations apply to their own video items.

Rows are selectable and a double click on a real (non-video) row plays it
(2026-09-06 follow-up, James: "I want to be able to select or click on a
title. A double click should automatically start playing that track") -
`trackActivated` fires the track_id, and `LibraryView._play_from_details`
does the actual queueing: the whole currently visible/sorted table, in its
current order, starting at that row - the same "queue what's on screen"
rule `ui/widgets/track_panel.py`'s own flat all-library list already uses
for the Tracks presentation, since this table is exactly that same kind of
thing (every track in the library, one row each). `TrackDetailRow.
to_queue_item()`/`.playable` need `artist`/`path`/`cover_path` on the row,
so `services.library.list_track_details` now selects those too (a
`MediaFile` join, same "lowest-id present file" rule `track_panel.py`'s own
query already uses) - see that function's docstring.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QScroller,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...services.player import QueueItem
from ..theme import COLORS
from .common import STAR_COUNT, STAR_EMPTY, STAR_FULL
from .cover_grid import MIN_TILES_FOR_SORTING

COL_GENRE = 0
COL_ALBUM_ARTIST = 1
COL_ALBUM = 2
COL_TRACK_NO = 3
COL_TITLE = 4
COL_TIME = 5
COL_YEAR = 6
COL_RATING = 7

HEADERS = (
    "Genre", "Album Artist", "Album", "Track #", "Title", "Time", "Year", "Rating",
)

#: the only columns with an alphabetical order worth jumping within - Track
#: #/Time/Year/Rating are numeric, so the shared A-Z bar hides itself while
#: one of those is the active sort (see `jump_letters()`)
ALPHA_SORT_COLS = (COL_GENRE, COL_ALBUM_ARTIST, COL_ALBUM, COL_TITLE)

#: STAR_COUNT/STAR_FULL/STAR_EMPTY live in widgets/common.py, shared with Now
#: Playing's standalone StarRating widget (2026-09-06) - re-exported here
#: (via the import above) so any other existing callers of
#: `track_details_table.STAR_COUNT` etc. keep working unchanged.
#:
#: JUKEBOX_ON/JUKEBOX_OFF used to be re-exported the same way for
#: `JukeboxDelegate`'s glyphs - removed along with that whole column and
#: delegate on 2026-09-13 (see the module docstring's "Removed the same
#: day" paragraph); common.py's own JukeboxToggle widget (Now Playing)
#: still imports them directly from there, unaffected.

#: index().data() role RatingDelegate reads to know how many stars to fill -
#: DisplayRole is left returning None for this column since the delegate,
#: not QStyledItemDelegate's default text painting, owns how it looks
ROLE_RATING = Qt.UserRole + 1


@dataclass
class TrackDetailRow:
    track_id: int
    genre: str
    album_artist: str
    album: str
    track_no: Optional[int]
    title: str
    duration_ms: Optional[int]
    year: Optional[int]
    rating: Optional[int]
    #: lower-cased sort key - the same convention `Track.title_key` already
    #: gives every other title sort in this app
    sort_key: str
    #: playback fields (2026-09-06 follow-up, James: "double click should
    #: automatically start playing that track") - let `to_queue_item()`
    #: build a real `QueueItem` straight from the row already in memory
    #: rather than a second per-track query. A video row (see `is_video`
    #: below) never sets these; it plays through `videoActivated` instead.
    artist: str = ""
    path: Optional[str] = None
    cover_path: Optional[str] = None
    #: True for a video woven in as a search match (2026-09-06, see
    #: `LibraryView._load_details`) rather than a real track row - `genre` is
    #: always "Video" for these, there's no rating (RatingDelegate skips
    #: painting and editing for them), and a tap plays the video instead of
    #: doing nothing. `video_id` is the video's own primary key; `track_id`
    #: is set to the safe sentinel 0 (never a real Track id) rather than left
    #: to collide with one.
    is_video: bool = False
    video_id: Optional[int] = None
    #: True if this track is currently loaded onto the jukebox board -
    #: independent of `rating` since the 2026-09-07 follow-up (see the
    #: module docstring). No column shows this any more (see the module
    #: docstring's 2026-09-13 "Removed the same day" paragraph) but the
    #: field itself stays: `_row_matches` still lets a search for the word
    #: "jukebox" find these tracks. Always False for a video row, same as
    #: `rating` - there's nothing to toggle on one.
    on_jukebox: bool = False

    @property
    def playable(self) -> bool:
        """False for a video row (no `path` - it plays through
        `videoActivated` instead) and for a real track with no present
        media file to actually play."""
        return bool(self.path) and not self.is_video

    def to_queue_item(self) -> QueueItem:
        return QueueItem(
            track_id=self.track_id,
            title=self.title,
            artist=self.artist,
            album=self.album,
            path=self.path or "",
            duration_ms=self.duration_ms or 0,
            cover_path=self.cover_path,
            position=str(self.track_no) if self.track_no else None,
        )


def _alpha_letter(row: TrackDetailRow, col: int) -> str:
    """First letter of whichever field `col` sorts by, uppercased - the same
    rule `CoverGrid._letter_of` uses for the Albums/Artists grids, extended
    here across the table's four text columns instead of one fixed field.
    Falls back to '#' for anything that doesn't start with A-Z (blank genre,
    a leading digit or symbol), same catch-all bucket every jump bar uses."""
    if col == COL_GENRE:
        source = row.genre
    elif col == COL_ALBUM_ARTIST:
        source = row.album_artist
    elif col == COL_ALBUM:
        source = row.album
    else:  # COL_TITLE, and the only other caller is already ALPHA_SORT_COLS-gated
        source = row.title
    first = (source or "").strip()[:1].upper()
    return first if first in string.ascii_uppercase else "#"


class TrackDetailsModel(QAbstractTableModel):
    """Backs the table. `set_rows()` replaces the whole (already filtered)
    row set; `sort()` is Qt's own hook, called automatically by
    `QTableView.setSortingEnabled(True)` whenever a header is tapped."""

    ratingChanged = Signal(int, int)  # track_id, new rating (0 = cleared)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[TrackDetailRow] = []
        self._sort_col = COL_TITLE
        self._sort_asc = True

    # -- Qt model plumbing ---------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return self._display(row, col)
        if role == ROLE_RATING and col == COL_RATING:
            return row.rating or 0
        if role == Qt.TextAlignmentRole:
            if col in (COL_TRACK_NO, COL_TIME, COL_YEAR):
                return Qt.AlignRight | Qt.AlignVCenter
            if col == COL_RATING:
                return Qt.AlignCenter
            return Qt.AlignLeft | Qt.AlignVCenter
        return None

    @staticmethod
    def _display(row: TrackDetailRow, col: int):
        if col == COL_GENRE:
            return row.genre
        if col == COL_ALBUM_ARTIST:
            return row.album_artist
        if col == COL_ALBUM:
            return row.album
        if col == COL_TRACK_NO:
            return str(row.track_no) if row.track_no else ""
        if col == COL_TITLE:
            return row.title
        if col == COL_TIME:
            from ...services.library import format_duration
            return format_duration(row.duration_ms)
        if col == COL_YEAR:
            return str(row.year) if row.year else ""
        return None  # Rating - painted by its own delegate, not drawn as text

    # -- data ------------------------------------------------------------------

    def set_rows(self, rows: Sequence[TrackDetailRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._apply_sort()
        self.endResetModel()

    def row_at(self, row: int) -> Optional[TrackDetailRow]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def set_rating(self, row: int, rating: int) -> None:
        detail = self.row_at(row)
        if detail is None or (detail.rating or 0) == rating:
            return
        detail.rating = rating or None
        index = self.index(row, COL_RATING)
        self.dataChanged.emit(index, index, [ROLE_RATING])
        self.ratingChanged.emit(detail.track_id, rating)

    def count(self) -> int:
        return len(self._rows)

    def rows(self) -> list[TrackDetailRow]:
        """The current (filtered, sorted) row set, in display order - what
        `jump_letters()`/`scroll_to_letter()` walk to answer "what letters
        are on screen right now" without duplicating the model's own state."""
        return self._rows

    @property
    def sort_column(self) -> int:
        return self._sort_col

    # -- sorting -----------------------------------------------------------------

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        self._sort_col = column
        self._sort_asc = order == Qt.AscendingOrder
        self.layoutAboutToBeChanged.emit()
        self._apply_sort()
        self.layoutChanged.emit()

    def _sort_value(self, row: TrackDetailRow):
        col = self._sort_col
        if col == COL_GENRE:
            return (row.genre.lower(), row.sort_key)
        if col == COL_ALBUM_ARTIST:
            return (row.album_artist.lower(), row.year or 0, row.sort_key)
        if col == COL_ALBUM:
            return (row.album.lower(), row.sort_key)
        if col == COL_TRACK_NO:
            return (row.track_no if row.track_no is not None else -1, row.sort_key)
        if col == COL_TIME:
            return (row.duration_ms if row.duration_ms is not None else -1, row.sort_key)
        if col == COL_YEAR:
            return (row.year if row.year is not None else -1, row.sort_key)
        if col == COL_RATING:
            return (row.rating if row.rating is not None else -1, row.sort_key)
        return row.sort_key  # Title, and the fallback

    def _apply_sort(self) -> None:
        self._rows.sort(key=self._sort_value, reverse=not self._sort_asc)


class RatingDelegate(QStyledItemDelegate):
    """Paints and edits the Rating column only; every other column falls
    through to the default text rendering.

    2026-09-13 follow-up (James: "see if you can make the 5 star rating
    take up less column space," the same message that asked for the
    Jukebox column's removal - see the module docstring) - `star_size`/
    `gap` shrank from 22px/6px to 16px/4px, and `column_width()` below
    (read by the view when it sets `COL_RATING`'s fixed width, rather than
    a hand-copied number that could quietly drift out of sync with these
    two) shrank to match: was a hardcoded `6 * 22 + 40 = 172`, now
    computed from the actual star geometry plus the same 20px of padding
    the old number implied. `editorEvent`'s hit-testing already forgives
    an imprecise tap - full row height, and each star's tap "band" already
    pads a few pixels past its own glyph - so the smaller glyphs stay
    perfectly tappable; they just draw and lay out smaller."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.star_size = 16
        self.gap = 4

    def column_width(self) -> int:
        """The fixed `COL_RATING` width this delegate actually needs -
        read by the view instead of a hardcoded number, so a future change
        to `star_size`/`gap` can't silently leave the column too narrow (or
        wider than necessary) again."""
        return STAR_COUNT * self.star_size + (STAR_COUNT - 1) * self.gap + 20

    def _star_rects(self, rect: QRect) -> list[QRect]:
        total_w = STAR_COUNT * self.star_size + (STAR_COUNT - 1) * self.gap
        x = rect.center().x() - total_w // 2
        y = rect.center().y() - self.star_size // 2
        return [
            QRect(x + i * (self.star_size + self.gap), y, self.star_size, self.star_size)
            for i in range(STAR_COUNT)
        ]

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        if index.column() != COL_RATING:
            super().paint(painter, option, index)
            return
        row = index.model().row_at(index.row())
        if row is not None and row.is_video:
            # a video has no rating field at all - leave the cell blank
            # rather than paint stars for something that can't be rated
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(COLORS["surface_hi"]))
        rating = index.data(ROLE_RATING) or 0
        font = painter.font()
        font.setPixelSize(self.star_size)
        painter.setFont(font)
        for position, rect in enumerate(self._star_rects(option.rect), start=1):
            filled = position <= rating
            # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for
            # this whole cleanup) - filled stars, walnut brown now instead
            # of red-orange (matches ui/widgets/common.py's StarRating).
            painter.setPen(QColor(COLORS["jukebox_key_hi"] if filled else COLORS["text_dim"]))
            painter.drawText(rect, Qt.AlignCenter, STAR_FULL if filled else STAR_EMPTY)
        painter.restore()

    def editorEvent(self, event, model, option, index: QModelIndex) -> bool:
        if index.column() != COL_RATING:
            return super().editorEvent(event, model, option, index)
        row = model.row_at(index.row())
        if row is not None and row.is_video:
            # no rating field to set on a video - refuse the tap before any
            # star hit-testing logic runs
            return False
        if event.type() == QEvent.MouseButtonRelease:
            point = event.position().toPoint()
            current = index.data(ROLE_RATING) or 0
            #: a touch tap rarely lands pixel-perfect on a 22px glyph, so any
            #: tap within a star's row height counts, not just inside the
            #: glyph's own tight box
            hit_row = QRect(option.rect.left(), option.rect.top(), option.rect.width(), option.rect.height())
            if not hit_row.contains(point):
                return False
            for position, rect in enumerate(self._star_rects(option.rect), start=1):
                band = QRect(rect.left() - self.gap // 2, option.rect.top(),
                             rect.width() + self.gap, option.rect.height())
                if band.contains(point):
                    # tapping the star that already sets the current rating
                    # clears it - the only way to remove a rating, since there
                    # is no separate "no rating" control
                    new_rating = 0 if position == current else position
                    model.set_rating(index.row(), new_rating)
                    return True
        return False


class TrackDetailsTable(QWidget):
    """The Title Details table: a `QTableView` over `TrackDetailsModel` with
    a star-rating delegate on its Rating column. It carried a second toggle
    delegate on a Jukebox column from 2026-09-07 to 2026-09-13 - see the
    module docstring's "Removed the same day" paragraph for that whole arc;
    `JukeboxDelegate` no longer exists in this file.

    `set_rows()` loads the full (unfiltered) row set; `set_filter_text()`
    narrows it live by title, genre, album artist or album - the same
    case-insensitive substring rule every other Library presentation's
    search box already uses. `jump_letters()`/`scroll_to_letter()` are the
    same seam `CoverGrid`/`AlbumTracksPanel` expose for the shared A-Z bar
    (see module docstring) - `updated` fires whenever the visible row set or
    the active sort column changes, either of which can change what letters
    make sense to show right now, so `LibraryView` knows to re-ask.

    Rows are selectable (2026-09-06 follow-up, James: "I want to be able to
    select or click on a title") and a double click on a real track row
    plays it (`trackActivated`) - `LibraryView` does the actual queueing
    (see `_play_from_details`), the same "the table only emits, the view
    acts" split `ratingChanged`/`videoActivated` already use. A video row
    keeps its existing single-tap-to-play behaviour (`_on_clicked`) rather
    than also reacting to a double click.

    2026-09-13 follow-up (James: "on the track details, I want a checkbox
    where it filters only on videos") - a "Videos only" `QCheckBox`
    (`self.videos_only_checkbox`), backed by purely local state
    (`self._videos_only` - no `LibraryView` involvement needed since
    nothing here writes to the database). Every video row was already
    loaded into `self._all_rows` alongside every track (see the module
    docstring and `TrackDetailRow.is_video`) but, with the checkbox off,
    stayed hidden unless the search box happened to match one -
    `_apply_filter` now ANDs the checkbox with whatever's typed in the
    search box rather than replacing it: checked with no search text
    shows every video; checked with search text narrows to videos
    matching it; unchecked is exactly the pre-existing behavior in both
    cases.

    Same-day fix: the checkbox's native indicator rendered as a bare
    caret with no visible box in either state on James's machine -
    nothing that read as "checkbox" at a glance. Given an explicit
    stylesheet instead: an always-visible bordered square that fills
    solid with the app's accent color when checked, rather than relying
    on the platform to draw a checkmark glyph inside it.

    Same day, second and third follow-ups (James: "move the Videos only
    checkbox up on the Search this View..., to the right of the Space and
    backspace buttons" - "don't take up an entire row for just that
    Videos only" - then "move checkbox and Videos only completely to the
    right, justified right"): this class still builds and owns the
    checkbox (construction, its stylesheet, wiring `toggled` into
    `_on_videos_only_toggled`/`_apply_filter`), but no longer places it in
    `root`'s own layout - `LibraryView._build_details()` reparents the
    one widget into its shared search header instead, flush against that
    header's right edge, and shows/hides it there depending on which
    presentation is active. See that method's own docstring for the
    header-side half of this.
    """

    ratingChanged = Signal(int, int)  # track_id, new rating
    #: a video row tapped as a search match (2026-09-06) - see
    #: TrackDetailRow.is_video and _on_clicked
    videoActivated = Signal(int)  # video_id
    #: a real (non-video) track row double-clicked (2026-09-06 follow-up) -
    #: see the class docstring and _on_double_clicked
    trackActivated = Signal(int)  # track_id
    updated = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_rows: list[TrackDetailRow] = []
        self._filter_text = ""
        #: 2026-09-13 follow-up - see the class docstring's newest entry.
        self._videos_only = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # 2026-09-13, second and third follow-ups (James: "move the Videos
        # only checkbox up on the Search this View..., to the right of the
        # Space and backspace buttons" - "don't take up an entire row for
        # just that Videos only" - then "move checkbox and Videos only
        # completely to the right, justified right") - constructed here
        # (this class still owns the checkbox itself: its styling and the
        # toggle wiring into `_apply_filter` below), but *not* added to
        # `root` any more. `LibraryView._build_details()` reparents this
        # one widget into its own shared search header instead, flush
        # against that header's right edge, and shows/hides it alongside
        # the rest of the header depending on whether Title Details is the
        # active presentation.
        self.videos_only_checkbox = QCheckBox("Videos only")
        self.videos_only_checkbox.setToolTip(
            "Show only music videos, hiding every audio track - the same "
            "video rows normally surfaced here only as a search match"
        )
        # 2026-09-13 fix: James reported the plain, unstyled QCheckBox's
        # native indicator was barely readable as a checkbox at all - on
        # his machine, checked looked like a bare "˅" caret floating next
        # to the label with no visible box around it either way, nothing
        # like the bordered square this app's other real checkboxes get
        # for free from the platform elsewhere. Drawn explicitly instead
        # of trusting native rendering: an always-visible bordered square
        # (`surface_alt`, matching this table's own alternating-row tint)
        # that fills solid with the app's jukebox_key_hi accent - already
        # used for every other toggle-ish affordance in this file, e.g.
        # the star rating / jukebox delegates just above - when checked.
        # No reliance on the platform drawing a checkmark glyph inside it
        # at all; the fill *is* the checked state.
        self.videos_only_checkbox.setStyleSheet(
            f"""
            QCheckBox {{ color: {COLORS['text']}; spacing: 8px; }}
            QCheckBox::indicator {{
                width: 22px;
                height: 22px;
                border: 2px solid {COLORS['text_dim']};
                border-radius: 4px;
                background: {COLORS['surface_alt']};
            }}
            QCheckBox::indicator:hover {{
                border-color: {COLORS['jukebox_key_hi']};
            }}
            QCheckBox::indicator:checked {{
                background: {COLORS['jukebox_key_hi']};
                border-color: {COLORS['jukebox_key_hi']};
            }}
            """
        )
        self.videos_only_checkbox.toggled.connect(self._on_videos_only_toggled)

        self.model = TrackDetailsModel(self)
        self.model.ratingChanged.connect(self.ratingChanged)
        self.model.modelReset.connect(self.updated)

        self.view = QTableView()
        self.view.setObjectName("TitleDetails")
        self.view.setModel(self.model)
        # 2026-09-06 follow-up: rows used to be unselectable (nothing here
        # needed it while a tap only mattered for the Rating column or a
        # video row) - James asked to be able to select a title, and a
        # double click needs *something* to distinguish it from a plain
        # click on the same row anyway
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setSortingEnabled(True)
        self.view.setShowGrid(False)
        self.view.setAlternatingRowColors(False)
        self.view.setMouseTracking(True)
        # 2026-09-06 follow-up (James: "Not Responding" on a real ~98,000-
        # track library): QTableView word-wraps a cell's text by default, so
        # a long Genre/Album/Title value ("2010s / Alternative / Country /
        # Dance & ...") was wrapping to two lines and forcing Qt to compute
        # each row's height from its content instead of using one fixed
        # number - fine at a few thousand rows, ruinous at this table's real
        # scale (tens of thousands), and exactly the kind of per-row cost
        # this widget's own module docstring already chose QTableView over
        # QTreeWidget to avoid for column layout - row height fell through
        # the same gap. Turning word wrap off and pinning every row to the
        # same fixed height (`Fixed`, not just a default that content can
        # still grow past) makes row height an O(1) lookup regardless of
        # library size; Qt still elides an overlong value with "..." on its
        # own, so nothing is unreadable, just single-line like every other
        # column here already was.
        self.view.setWordWrap(False)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.view.verticalHeader().setDefaultSectionSize(TOUCH["row_height"] - 16)
        header = self.view.horizontalHeader()
        header.setStretchLastSection(False)
        for col in (COL_GENRE, COL_ALBUM_ARTIST, COL_ALBUM, COL_TITLE):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        for col in (COL_TRACK_NO, COL_TIME, COL_YEAR, COL_RATING):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
        self.view.setColumnWidth(COL_TRACK_NO, 90)
        self.view.setColumnWidth(COL_TIME, 90)
        self.view.setColumnWidth(COL_YEAR, 90)
        self.rating_delegate = RatingDelegate(self.view)
        # narrower than the old hardcoded 172px (2026-09-13 follow-up - see
        # RatingDelegate's own docstring) - freed width flows into the four
        # Stretch columns above, Title included, same as the Jukebox
        # column's removal just below does
        self.view.setColumnWidth(COL_RATING, self.rating_delegate.column_width())
        self.view.setItemDelegateForColumn(COL_RATING, self.rating_delegate)
        QScroller.grabGesture(self.view.viewport(), QScroller.LeftMouseButtonGesture)
        root.addWidget(self.view, 1)

        # clicking a header changes which letters (if any) make sense for the
        # shared A-Z bar - e.g. switching from Title to Year - so LibraryView
        # needs to hear about a sort change the same way it hears about a
        # filtered/reloaded row set
        header.sortIndicatorChanged.connect(lambda *_: self.updated.emit())

        # a video row (search match only, see is_video) plays on tap - every
        # other column already does nothing on click, so this is a new
        # capability rather than replacing existing row-click behaviour
        self.view.clicked.connect(self._on_clicked)
        # a real track row plays on double click (2026-09-06 follow-up) -
        # a single click just selects it, matching ordinary desktop table
        # behaviour now that rows are selectable
        self.view.doubleClicked.connect(self._on_double_clicked)

        self.view.sortByColumn(COL_TITLE, Qt.AscendingOrder)

    def _on_clicked(self, index: QModelIndex) -> None:
        if index.column() == COL_RATING:
            return  # handled by RatingDelegate's own editorEvent
        row = self.model.row_at(index.row())
        if row is not None and row.is_video and row.video_id is not None:
            self.videoActivated.emit(row.video_id)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        if index.column() == COL_RATING:
            return  # double-tapping a star acts on it twice, not a play request
        row = self.model.row_at(index.row())
        if row is None or row.is_video:
            # a video row already plays on the first click above - nothing
            # extra for a double click to do here
            return
        self.trackActivated.emit(row.track_id)

    # -- data ---------------------------------------------------------------

    def set_rows(self, rows: Sequence[TrackDetailRow]) -> None:
        self._all_rows = list(rows)
        self._apply_filter()

    def set_filter_text(self, text: str) -> None:
        """Live-narrow the table to rows matching `text` in *any* of its
        columns (James, 2026-09-06: "the search can be by all the columns
        available for a title") - see `_row_matches`. A case-insensitive
        substring rule, same as everywhere else in Library. An empty string
        restores every row."""
        text = text.strip().lower()
        if text == self._filter_text:
            return
        self._filter_text = text
        self._apply_filter()

    @staticmethod
    def _row_matches(row: TrackDetailRow, needle: str) -> bool:
        """Every column the table shows is searchable, not just the four
        text ones (Title/Genre/Album Artist/Album) this used to check -
        Track #, Time and Year match against the same plain text the
        column itself displays ("3:45" finds a track of that length,
        "1978" finds that year, "4" finds Track 4), and Rating matches its
        bare star count ("5" finds a 5-star track). A track on the jukebox
        board matches the word "jukebox" (2026-09-07 follow-up), same idea
        as Rating's bare number - there's no glyph to type, so the column's
        name doubles as its search term. A video row has no track #/time/
        year/rating/jukebox state of its own (`TrackDetailRow.is_video`),
        so those simply never match one - it still surfaces through Title
        or its "Video" genre, both already covered below."""
        from ...services.library import format_duration

        return (
            needle in row.title.lower()
            or needle in row.genre.lower()
            or needle in row.album_artist.lower()
            or needle in row.album.lower()
            or (row.track_no is not None and needle in str(row.track_no))
            or needle in format_duration(row.duration_ms).lower()
            or (row.year is not None and needle in str(row.year))
            or (row.rating is not None and needle in str(row.rating))
            or (row.on_jukebox and needle in "jukebox")
        )

    def _on_videos_only_toggled(self, checked: bool) -> None:
        self._videos_only = checked
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self._filter_text
        candidates = self._all_rows
        if self._videos_only:
            # 2026-09-13 follow-up (see class docstring) - narrows the pool
            # before the text filter below runs, so a search term with the
            # checkbox on matches only within videos, not the whole library
            candidates = [r for r in candidates if r.is_video]
        if not needle:
            if self._videos_only:
                rows = candidates
            else:
                # video rows only ever appear as search matches - see
                # TrackDetailRow.is_video and the module docstring
                rows = [r for r in candidates if not r.is_video]
        else:
            rows = [r for r in candidates if self._row_matches(r, needle)]
        self.model.set_rows(rows)

    def count(self) -> int:
        return self.model.count()

    # -- shared A-Z jump bar --------------------------------------------------

    def jump_letters(self) -> Optional[set[str]]:
        """Letters worth showing on the shared A-Z bar right now, or None
        when jumping makes no sense for whatever column the table is
        currently sorted by (Track #/Time/Year/Rating are numeric - see
        `ALPHA_SORT_COLS`) or there's too little on screen to be worth it -
        the same `MIN_TILES_FOR_SORTING` threshold `CoverGrid.jump_letters()`
        already applies for the Albums/Artists grids."""
        col = self.model.sort_column
        rows = self.model.rows()
        if col not in ALPHA_SORT_COLS or len(rows) <= MIN_TILES_FOR_SORTING:
            return None
        return {_alpha_letter(r, col) for r in rows}

    def scroll_to_letter(self, letter: str) -> None:
        col = self.model.sort_column
        for row_index, row in enumerate(self.model.rows()):
            if _alpha_letter(row, col) == letter:
                self.view.scrollTo(
                    self.model.index(row_index, col), QAbstractItemView.PositionAtTop
                )
                return
