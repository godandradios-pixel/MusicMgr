"""Title Details: a flat, sortable table of every track in the library.

One row per track: Genre, Album Artist, Album, Track #, Title, Time, Year,
a tap-to-set Rating out of 5 stars, and Comment.

Built on `QAbstractTableModel`/`QTableView` rather than the `QTreeWidget`
"own the sort" pattern `VideoTable`/`ChartTable` use (see video_table.py's
docstring): that pattern allocates a `QTreeWidgetItem` per row, which is fine
for hundreds to low thousands of rows, but this table holds every track in
the library (tens of thousands). A model only supplies data the view is
about to paint, so building or re-sorting stays fast at any size. `sort()`
re-orders the Python list backing the model, avoiding the numeric-vs-string
comparison problem `VideoTable`'s docstring describes for Year/Time.
Row height is fixed for the same reason: `setWordWrap(False)` plus a `Fixed`
vertical header keeps row height an O(1) lookup (word-wrapped cells make Qt
measure every row, which hangs a ~100k-track library); Qt elides overlong
values with "...".

The table joins the shared A-Z jump bar `LibraryView` shows under the search
box. `jump_letters()`/`scroll_to_letter()` only apply while sorted by one of
the four text columns (`ALPHA_SORT_COLS`); `LibraryView` hides the bar for
any other sort, as it does for a grid sorted non-alphabetically. The search
box searches every column shown (see `_row_matches`).

The Rating column is interactive: `RatingDelegate` paints five stars and a
tap sets the rating to the tapped star; tapping the star matching the
current rating clears it, so no separate "no rating" control is needed.

`TrackDetailRow.on_jukebox` has no column, but `_row_matches` lets a search
for "jukebox" find those tracks. Adding a track to the jukebox board is done
from Now Playing's toggle or the Jukebox page's "+ Add to jukebox" picker.

A search match can also be a video (see `LibraryView._load_details` and the
module docstring in `views/library.py`), woven in as an ordinary
`TrackDetailRow` with `is_video=True`, a blank genre (a video's kind is
`media_type`, never a made-up "Video" genre), and no rating.
`RatingDelegate` refuses to paint or edit stars for one, and tapping the row
plays it (`_on_clicked`, `videoActivated`). Video rows only appear as search
matches, never in the default table - browsing videos is the Videos page's
job (ui/views/videos.py), which replaced this table's old "Videos only"
checkbox.

A "Group by" chip bar (None / Genre / Artist / Album) mirrors `VideoTable`'s.
Genre adds one level of `GroupHeaderRow` dividers, alphabetical, with header
clicks still re-sorting within each group. Artist / Album is two levels deep
and uses a fixed natural order at every level - artist alphabetical, album
by year-then-title (as `artist_panel.py`'s release query sorts), track by
track number - with header sorting disabled, because a column sort reaching
into one album's track order would scramble it. See
`TrackDetailsModel._rebuild_display()` for the row-list mechanics
(`GroupHeaderRow`, `QTableView.setSpan()`).

Rows are selectable and a double click on a real (non-video) row plays it:
`trackActivated` fires the track_id and `LibraryView._play_from_details`
queues the whole visible table in its current order starting at that row,
the same "queue what's on screen" rule as `ui/widgets/track_panel.py`.
`TrackDetailRow.to_queue_item()`/`.playable` need `artist`/`path`/
`cover_path`, which `services.library.list_track_details` selects (see that
function's docstring).
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QScroller,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...services.search_text import fold
from ...services.player import QueueItem
from ..theme import COLORS
from .common import STAR_COUNT, STAR_EMPTY, STAR_FULL, ChipButton, TouchButton, dim_label
from .cover_grid import MIN_TILES_FOR_SORTING

COL_GENRE = 0
COL_ALBUM_ARTIST = 1
COL_ALBUM = 2
COL_TRACK_NO = 3
COL_TITLE = 4
COL_TIME = 5
COL_YEAR = 6
COL_RATING = 7
COL_COMMENT = 8

HEADERS = (
    "Genre", "Album Artist", "Album", "Track #", "Title", "Time", "Year", "Rating",
    "Comment",
)

#: the only columns with an alphabetical order worth jumping within - Track
#: #/Time/Year/Rating are numeric, so the shared A-Z bar hides itself while
#: one of those is the active sort (see `jump_letters()`)
ALPHA_SORT_COLS = (COL_GENRE, COL_ALBUM_ARTIST, COL_ALBUM, COL_TITLE)

#: STAR_COUNT/STAR_FULL/STAR_EMPTY live in widgets/common.py, shared with Now
#: Playing's StarRating widget - re-exported here (via the import above) for
#: callers of `track_details_table.STAR_COUNT` etc.

#: index().data() role RatingDelegate reads to know how many stars to fill -
#: DisplayRole is left returning None for this column since the delegate,
#: not QStyledItemDelegate's default text painting, owns how it looks
ROLE_RATING = Qt.UserRole + 1

#: the three "Group by" chip options (see module docstring). GROUP_NONE is
#: the flat, fully-sortable table; the other two insert `GroupHeaderRow` dividers into the model's row list (see
#: `TrackDetailsModel._rebuild_display`).
GROUP_NONE = "none"
GROUP_GENRE = "genre"
GROUP_ARTIST_ALBUM = "artist_album"

#: fallback bucket labels for a blank grouping field - same idea as
#: `VideoTable`'s "Unknown artist" for a blank artist, just for the two
#: fields this table can group by.
_NO_GENRE = "No genre"
_NO_ALBUM = "No album"


def _first_letter(text: str) -> str:
    """Uppercased first letter for the shared A-Z jump bar, or '#' for
    anything that doesn't start with A-Z (blank text, a leading digit or
    symbol) - the same catch-all bucket every jump bar in this app uses."""
    first = (text or "").strip()[:1].upper()
    return first if first in string.ascii_uppercase else "#"


def _media_label(media_type: str) -> str:
    """What a search for a video row's kind matches - "movie" or
    "music video" (see TrackDetailRow.media_type)."""
    return "movie" if media_type == "movie" else "music video"


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
    #: playback fields - let `to_queue_item()` build a `QueueItem` from the
    #: row already in memory rather than a second per-track query. A video
    #: row (see `is_video`) never sets these; it plays via `videoActivated`.
    artist: str = ""
    #: raw tag comment (see services/scanner.py's `_first_id3_comment`).
    #: Blank for a video row. Must stay after every field without a default
    #: (`sort_key` included) - a dataclass raises at class-definition time
    #: otherwise, which `py_compile`/`ast.parse` checks don't catch.
    comment: str = ""
    path: Optional[str] = None
    cover_path: Optional[str] = None
    #: what the Track # column shows - a 78's side ("A"/"B") when it has
    #: one, else the number (services/tracknum.py)
    track_label: str = ""
    #: True for a video woven in as a search match (see
    #: `LibraryView._load_details`) - `genre` is blank, there's no rating
    #: (RatingDelegate skips these), and a tap plays the video. `video_id` is
    #: the video's primary key; `track_id` is the sentinel 0 (never a real
    #: Track id).
    is_video: bool = False
    video_id: Optional[int] = None
    #: a video row's Video.media_type ("music_video"/"movie"); "" for a
    #: track. Searching "movie" or "music video" finds those rows.
    media_type: str = ""
    #: True if this track is loaded onto the jukebox board (independent of
    #: `rating`). No column shows it; `_row_matches` lets a search for
    #: "jukebox" find these tracks. Always False for a video row.
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
            position=self.track_label or (str(self.track_no) if self.track_no else None),
        )


@dataclass
class GroupHeaderRow:
    """One divider row inserted into `TrackDetailsModel`'s display list by
    `_rebuild_display()` when grouping is on. Not a real track -
    `TrackDetailsModel.row_at()` returns None for one, so click/rating
    handlers treat it as "nothing here".

    `level` is 0 for a top-level divider (a genre, or an artist in Artist /
    Album mode) and 1 for the album sub-divider nested under an artist in
    Artist / Album mode - `TrackDetailsTable._apply_spans()` spans every
    level the same way (the whole row, all columns), but only level 0
    dividers are jump-bar targets (`letter_source` is None on a level-1
    one, so `TrackDetailsModel.top_level_headers()` skips it)."""

    text: str
    level: int = 0
    #: the raw (un-formatted) grouping key this divider represents - "Rock",
    #: "Rush" - read by the shared A-Z jump bar. None for a level-1 (album)
    #: divider, which isn't a jump target at all (see class docstring).
    letter_source: Optional[str] = None


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
    return _first_letter(source)


class TrackDetailsModel(QAbstractTableModel):
    """Backs the table. `set_rows()` replaces the whole (already filtered)
    row set; `sort()` is Qt's own hook, called automatically by
    `QTableView.setSortingEnabled(True)` whenever a header is tapped.

    `self._rows` is the flat, filtered leaf list `set_rows()` is handed -
    the source of truth, untouched by grouping. The view reads
    `self._display_rows`, which `_rebuild_display()` rebuilds from
    `self._rows` whenever the row set, sort, or `self._group` changes:
    plain leaves when ungrouped, leaves interleaved with `GroupHeaderRow`
    dividers otherwise. Two lists keep dividers out of the search/filter/
    rating logic; only the methods that render or navigate the table
    (`data`, `flags`, `row_at`, `header_at`, `rows`, `top_level_headers`)
    know about them."""

    ratingChanged = Signal(int, int)  # track_id, new rating (0 = cleared)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[TrackDetailRow] = []
        self._display_rows: list = []  # TrackDetailRow | GroupHeaderRow
        self._group = GROUP_NONE
        self._sort_col = COL_TITLE
        self._sort_asc = True

    # -- Qt model plumbing ---------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._display_rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        item = self._display_rows[index.row()]
        if isinstance(item, GroupHeaderRow):
            # a divider, not a selectable row - matches VideoTable's group
            # headers; clicks on it fall through as "nothing to act on"
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self._display_rows[index.row()]
        col = index.column()
        if isinstance(item, GroupHeaderRow):
            if role == Qt.DisplayRole and col == 0:
                return item.text
            if role == Qt.FontRole and col == 0:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
            return None
        row = item
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
            return row.track_label or (str(row.track_no) if row.track_no else "")
        if col == COL_TITLE:
            return row.title
        if col == COL_TIME:
            from ...services.library import format_duration
            return format_duration(row.duration_ms)
        if col == COL_YEAR:
            return str(row.year) if row.year else ""
        if col == COL_COMMENT:
            return row.comment
        return None  # Rating - painted by its own delegate, not drawn as text

    # -- data ------------------------------------------------------------------

    def set_rows(self, rows: Sequence[TrackDetailRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._rebuild_display()
        self.endResetModel()

    def row_at(self, row: int) -> Optional[TrackDetailRow]:
        """The real track at this display row, or None for a group header
        (or an out-of-range row)."""
        if not (0 <= row < len(self._display_rows)):
            return None
        item = self._display_rows[row]
        return item if isinstance(item, TrackDetailRow) else None

    def header_at(self, row: int) -> Optional[GroupHeaderRow]:
        if not (0 <= row < len(self._display_rows)):
            return None
        item = self._display_rows[row]
        return item if isinstance(item, GroupHeaderRow) else None

    def set_rating(self, row: int, rating: int) -> None:
        detail = self.row_at(row)
        if detail is None or (detail.rating or 0) == rating:
            return
        detail.rating = rating or None
        index = self.index(row, COL_RATING)
        self.dataChanged.emit(index, index, [ROLE_RATING])
        self.ratingChanged.emit(detail.track_id, rating)

    def count(self) -> int:
        """How many real tracks are loaded - `len(self._rows)`, not the
        display list, so group headers never inflate it."""
        return len(self._rows)

    def rows(self) -> list[TrackDetailRow]:
        """The current (filtered, sorted, and - if grouped - flattened)
        leaf rows, in on-screen display order, with any group headers
        stripped out - what `jump_letters()`/`scroll_to_letter()` walk."""
        return [r for r in self._display_rows if isinstance(r, TrackDetailRow)]

    def top_level_headers(self) -> list[tuple[int, str]]:
        """(display row index, raw grouping-key text) for every top-level
        `GroupHeaderRow` currently shown - a genre name, or an artist name
        in Artist / Album mode - what the shared A-Z jump bar indexes while
        grouped (see `TrackDetailsTable.jump_letters`/`scroll_to_letter`).
        Empty while `group == GROUP_NONE`; skips the album sub-dividers
        (`level == 1`)."""
        return [
            (i, item.letter_source)
            for i, item in enumerate(self._display_rows)
            if isinstance(item, GroupHeaderRow)
            and item.level == 0
            and item.letter_source is not None
        ]

    @property
    def sort_column(self) -> int:
        return self._sort_col

    @property
    def group(self) -> str:
        return self._group

    def set_group(self, group: str) -> None:
        if group == self._group:
            return
        self.beginResetModel()
        self._group = group
        self._rebuild_display()
        self.endResetModel()

    # -- sorting -----------------------------------------------------------------

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        self._sort_col = column
        self._sort_asc = order == Qt.AscendingOrder
        self.layoutAboutToBeChanged.emit()
        self._rebuild_display()
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
        if col == COL_COMMENT:
            return (row.comment.lower(), row.sort_key)
        return row.sort_key  # Title, and the fallback

    # -- grouping (see class docstring) -----------------------------------------

    def _rebuild_display(self) -> None:
        if self._group == GROUP_GENRE:
            self._display_rows = self._grouped_by_genre()
        elif self._group == GROUP_ARTIST_ALBUM:
            self._display_rows = self._grouped_by_artist_album()
        else:
            self._display_rows = sorted(
                self._rows, key=self._sort_value, reverse=not self._sort_asc
            )

    def _grouped_by_genre(self) -> list:
        """One divider per genre, alphabetical, tracks within each genre
        ordered by whatever column is currently sorted - the same contract
        as `VideoTable`'s Artist grouping (see that widget's docstring)."""
        groups: dict[str, list[TrackDetailRow]] = {}
        for row in self._rows:
            groups.setdefault(row.genre or _NO_GENRE, []).append(row)
        display: list = []
        for genre in sorted(groups, key=str.lower):
            members = groups[genre]
            plural = "" if len(members) == 1 else "s"
            display.append(
                GroupHeaderRow(
                    text=f"{genre}  ·  {len(members)} track{plural}",
                    level=0,
                    letter_source=genre,
                )
            )
            display.extend(sorted(members, key=self._sort_value, reverse=not self._sort_asc))
        return display

    def _grouped_by_artist_album(self) -> list:
        """Two dividers deep - an artist, then each of their albums under
        it - with a fixed, natural order at every level rather than the
        sorted column (which would scramble an album's track order; see
        the module docstring). Artist alphabetical; album by year-then-title
        (as `artist_panel.py`'s release query sorts); track by track number,
        falling back to the title `sort_key`."""
        by_artist: dict[str, list[TrackDetailRow]] = {}
        for row in self._rows:
            by_artist.setdefault(row.album_artist or "Unknown artist", []).append(row)
        display: list = []
        for artist in sorted(by_artist, key=str.lower):
            artist_rows = by_artist[artist]
            by_album: dict[str, list[TrackDetailRow]] = {}
            for row in artist_rows:
                by_album.setdefault(row.album or _NO_ALBUM, []).append(row)
            album_plural = "" if len(by_album) == 1 else "s"
            track_plural = "" if len(artist_rows) == 1 else "s"
            display.append(
                GroupHeaderRow(
                    text=(
                        f"{artist}  ·  {len(by_album)} album{album_plural}, "
                        f"{len(artist_rows)} track{track_plural}"
                    ),
                    level=0,
                    letter_source=artist,
                )
            )

            def _album_year(album_name: str, _by_album=by_album) -> int:
                years = [r.year for r in _by_album[album_name] if r.year is not None]
                return min(years) if years else 9999

            for album in sorted(by_album, key=lambda a: (_album_year(a), a.lower())):
                album_rows = by_album[album]
                plural = "" if len(album_rows) == 1 else "s"
                year = next((r.year for r in album_rows if r.year is not None), None)
                caption = f"{album}  ·  {year}" if year else album
                display.append(
                    GroupHeaderRow(
                        text=f"{caption}  ·  {len(album_rows)} track{plural}",
                        level=1,
                        letter_source=None,
                    )
                )
                display.extend(
                    sorted(
                        album_rows,
                        key=lambda r: (r.track_no if r.track_no is not None else 9999, r.sort_key),
                    )
                )
        return display


class RatingDelegate(QStyledItemDelegate):
    """Paints and edits the Rating column only; every other column falls
    through to the default text rendering.

    Stars are kept small (16px, 4px gap) to save column width; the view
    sizes `COL_RATING` from `column_width()`. `editorEvent`'s hit-testing
    uses the full row height and pads each star's band past its glyph, so
    the small glyphs stay easy to tap."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.star_size = 16
        self.gap = 4

    def column_width(self) -> int:
        """The fixed `COL_RATING` width this delegate needs, computed from
        `star_size`/`gap` so the column tracks any change to them."""
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
        # row_at() is also None for a group header; setSpan() normally keeps
        # this delegate from painting one, but don't rely on spans being
        # (re)applied yet.
        if row is None or row.is_video:
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
            # filled stars in walnut brown, matching ui/widgets/common.py's
            # StarRating (see ui/theme.py's #Primary comment)
            painter.setPen(QColor(COLORS["jukebox_key_hi"] if filled else COLORS["text_dim"]))
            painter.drawText(rect, Qt.AlignCenter, STAR_FULL if filled else STAR_EMPTY)
        painter.restore()

    def editorEvent(self, event, model, option, index: QModelIndex) -> bool:
        if index.column() != COL_RATING:
            return super().editorEvent(event, model, option, index)
        row = model.row_at(index.row())
        if row is None or row.is_video:
            # no rating field to set on a video, or nothing to set at all
            # on a group header divider - refuse the tap before any star
            # hit-testing logic runs
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
    a star-rating delegate on its Rating column.

    `set_rows()` loads the full (unfiltered) row set; `set_filter_text()`
    narrows it live by title, genre, album artist or album - the same
    case-insensitive substring rule every other Library presentation's
    search box already uses. `jump_letters()`/`scroll_to_letter()` are the
    same seam `CoverGrid`/`AlbumTracksPanel` expose for the shared A-Z bar
    (see module docstring) - `updated` fires whenever the visible row set or
    the active sort column changes, either of which can change what letters
    make sense to show right now, so `LibraryView` knows to re-ask.

    Rows are selectable and a double click on a real track row plays it
    (`trackActivated`); `LibraryView` does the queueing (see
    `_play_from_details`) - the table only emits, the view acts, as with
    `ratingChanged`/`videoActivated`. A video row plays on a single tap
    (`_on_clicked`) instead.
    """

    ratingChanged = Signal(int, int)  # track_id, new rating
    #: a video row tapped as a search match - see
    #: TrackDetailRow.is_video and _on_clicked
    videoActivated = Signal(int)  # video_id
    #: a real (non-video) track row double-clicked - see the class
    #: docstring and _on_double_clicked
    trackActivated = Signal(int)  # track_id
    #: "Edit tags…" on the selected songs, from the button beside Group by
    #: or a right-click. LibraryView opens the editor (ui/widgets/tag_editor.py).
    editTagsRequested = Signal(list)  # track ids
    #: "Remove from library" on songs with no file left (services/track_removal.py)
    removeFromLibraryRequested = Signal(list)  # track ids
    updated = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_rows: list[TrackDetailRow] = []
        self._filter_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)


        # "Group by: None / Genre / Artist / Album" - same chip-bar shape as
        # VideoTable's. Kept in this table's own layout since it only
        # affects this table's row list.
        group_bar = QHBoxLayout()
        group_bar.setSpacing(8)
        group_bar.addWidget(dim_label("Group by"))
        self._group_buttons = QButtonGroup(self)
        self._group_buttons.setExclusive(True)
        none_chip = ChipButton("None")
        none_chip.setProperty("group", GROUP_NONE)
        none_chip.setChecked(True)
        genre_chip = ChipButton("Genre")
        genre_chip.setProperty("group", GROUP_GENRE)
        artist_album_chip = ChipButton("Artist / Album")
        artist_album_chip.setProperty("group", GROUP_ARTIST_ALBUM)
        for chip in (none_chip, genre_chip, artist_album_chip):
            self._group_buttons.addButton(chip)
            group_bar.addWidget(chip)
        self._group_buttons.buttonClicked.connect(
            lambda b: self._set_group(b.property("group"))
        )
        group_bar.addStretch(1)
        # the tag editor: Ctrl/Shift-click picks several songs; a
        # right-click on a row offers the same thing
        self.edit_tags_btn = TouchButton("Edit tags…")
        self.edit_tags_btn.setToolTip(
            "Fix the selected songs' tags in the music files (Ctrl- or Shift-click to "
            "pick several)")
        self.edit_tags_btn.setEnabled(False)
        self.edit_tags_btn.clicked.connect(self._request_edit_tags)
        group_bar.addWidget(self.edit_tags_btn)
        root.addLayout(group_bar)

        self.model = TrackDetailsModel(self)
        self.model.ratingChanged.connect(self.ratingChanged)
        self.model.modelReset.connect(self.updated)
        # modelReset (set_rows/set_group) and layoutChanged (sort) both
        # rebuild the display list, so header rows must be re-spanned after
        # either, or a stale span would merge the wrong cells
        self.model.modelReset.connect(self._apply_spans)
        self.model.layoutChanged.connect(self._apply_spans)

        self.view = QTableView()
        self.view.setObjectName("TitleDetails")
        self.view.setModel(self.model)
        # several rows at once (Ctrl/Shift-click) for editing a set of
        # songs' tags together - a double click still plays the one row
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setSortingEnabled(True)
        self.view.setShowGrid(False)
        self.view.setAlternatingRowColors(False)
        self.view.setMouseTracking(True)
        # no word wrap + a `Fixed` vertical header (not just a default size
        # content can grow past): wrapped cells make Qt measure every row's
        # height, which hangs a library of tens of thousands of tracks. Qt
        # still elides overlong values with "...".
        self.view.setWordWrap(False)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.view.verticalHeader().setDefaultSectionSize(TOUCH["row_height"] - 16)
        header = self.view.horizontalHeader()
        header.setStretchLastSection(False)
        for col in (COL_GENRE, COL_ALBUM_ARTIST, COL_ALBUM, COL_TITLE, COL_COMMENT):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        for col in (COL_TRACK_NO, COL_TIME, COL_YEAR, COL_RATING):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
        self.view.setColumnWidth(COL_TRACK_NO, 90)
        self.view.setColumnWidth(COL_TIME, 90)
        self.view.setColumnWidth(COL_YEAR, 90)
        self.rating_delegate = RatingDelegate(self.view)
        # sized from the star geometry (see RatingDelegate's docstring);
        # remaining width flows into the Stretch columns above
        self.view.setColumnWidth(COL_RATING, self.rating_delegate.column_width())
        self.view.setItemDelegateForColumn(COL_RATING, self.rating_delegate)
        QScroller.grabGesture(self.view.viewport(), QScroller.LeftMouseButtonGesture)
        root.addWidget(self.view, 1)

        # clicking a header changes which letters (if any) make sense for the
        # shared A-Z bar - e.g. switching from Title to Year - so LibraryView
        # needs to hear about a sort change the same way it hears about a
        # filtered/reloaded row set
        header.sortIndicatorChanged.connect(lambda *_: self.updated.emit())

        # a video row (search match only, see is_video) plays on tap
        self.view.clicked.connect(self._on_clicked)
        # a real track row plays on double click; a single click selects it
        self.view.doubleClicked.connect(self._on_double_clicked)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._sync_edit_button())
        self.model.modelReset.connect(self._sync_edit_button)

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

    # -- tag editing --------------------------------------------------------

    def selected_track_ids(self) -> list[int]:
        """The real (non-video) songs selected, top to bottom."""
        ids: list[int] = []
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        for r in rows:
            row = self.model.row_at(r)
            if row is not None and not row.is_video and row.track_id and row.track_id not in ids:
                ids.append(row.track_id)
        return ids

    def _sync_edit_button(self) -> None:
        n = len(self.selected_track_ids())
        self.edit_tags_btn.setEnabled(n > 0)
        self.edit_tags_btn.setText(f"Edit tags ({n})…" if n > 1 else "Edit tags…")

    def _request_edit_tags(self) -> None:
        ids = self.selected_track_ids()
        if ids:
            self.editTagsRequested.emit(ids)

    def _on_context_menu(self, pos) -> None:  # pragma: no cover - menu
        index = self.view.indexAt(pos)
        if index.isValid() and not self.view.selectionModel().isRowSelected(index.row(), QModelIndex()):
            self.view.selectRow(index.row())
        ids = self.selected_track_ids()
        if not ids:
            return
        menu = QMenu(self.view)
        label = f"Edit tags of {len(ids)} songs…" if len(ids) > 1 else "Edit tags…"
        menu.addAction(label).triggered.connect(lambda: self.editTagsRequested.emit(ids))
        fileless = self.selected_fileless_track_ids()
        if fileless and len(fileless) == len(ids):
            menu.addSeparator()
            remove = (f"Remove {len(ids)} songs from library…" if len(ids) > 1
                      else "Remove from library…")
            menu.addAction(remove).triggered.connect(
                lambda: self.removeFromLibraryRequested.emit(fileless))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def selected_fileless_track_ids(self) -> list[int]:
        """The selected songs with no file left - `path` is only set for a
        file that isn't flagged missing (lib.list_track_details). Only
        these can be removed from the library by hand."""
        ids: list[int] = []
        for r in sorted({i.row() for i in self.view.selectionModel().selectedRows()}):
            row = self.model.row_at(r)
            if (row is not None and not row.is_video and row.track_id and not row.path
                    and row.track_id not in ids):
                ids.append(row.track_id)
        return ids

    # -- grouping (see class docstring) -----------------------------------------

    @property
    def group_by(self) -> str:
        return self.model.group

    def _set_group(self, group: str) -> None:
        if group == self.model.group:
            return
        self.model.set_group(group)
        # Artist / Album has a fixed natural order (see
        # TrackDetailsModel._grouped_by_artist_album), so header clicks are
        # disabled there rather than appearing to do nothing. Genre and
        # None keep header-click sorting.
        self.view.setSortingEnabled(group != GROUP_ARTIST_ALBUM)
        self.updated.emit()

    def _apply_spans(self) -> None:
        """Every `GroupHeaderRow` currently in the model's display list
        gets its one cell (column 0) spanned across every column - the
        `QTableView` equivalent of `QTreeWidgetItem.setFirstColumnSpanned`,
        so a header reads as one full-width bar. Called after every rebuild
        (`modelReset`/`layoutChanged`) since a stale span would merge the
        wrong cells."""
        self.view.clearSpans()
        for row in range(self.model.rowCount()):
            if self.model.header_at(row) is not None:
                self.view.setSpan(row, 0, 1, len(HEADERS))

    # -- data ---------------------------------------------------------------

    def set_rows(self, rows: Sequence[TrackDetailRow]) -> None:
        self._all_rows = list(rows)
        self._apply_filter()

    def set_filter_text(self, text: str) -> None:
        """Live-narrow the table to rows matching `text` in *any* of its
        columns - see `_row_matches`. A case-insensitive
        substring rule, same as everywhere else in Library. An empty string
        restores every row."""
        text = fold(text.strip())   # accent/case-blind: "expose" finds Exposé
        if text == self._filter_text:
            return
        self._filter_text = text
        self._apply_filter()

    @staticmethod
    def _row_matches(row: TrackDetailRow, needle: str) -> bool:
        """Every column the table shows is searchable. Track #, Time and
        Year match the text the column displays ("3:45", "1978", "4"), and
        Rating matches its bare star count ("5"). A track on the jukebox
        board matches the word "jukebox". A video row has no track #/time/
        year/rating/jukebox state, so it surfaces only through Title or its
        "Video" genre."""
        from ...services.library import format_duration

        return (
            needle in fold(row.title)
            or needle in fold(row.genre)
            or needle in fold(row.album_artist)
            or needle in fold(row.album)
            or (row.track_no is not None and needle in str(row.track_no))
            or needle in format_duration(row.duration_ms).lower()
            or (row.year is not None and needle in str(row.year))
            or (row.rating is not None and needle in str(row.rating))
            or (row.on_jukebox and needle in "jukebox")
            or needle in fold(row.comment)
            or (row.is_video and needle in _media_label(row.media_type))
        )

    def _apply_filter(self) -> None:
        needle = self._filter_text
        if not needle:
            # video rows only ever appear as search matches - see
            # TrackDetailRow.is_video and the module docstring
            rows = [r for r in self._all_rows if not r.is_video]
        else:
            rows = [r for r in self._all_rows if self._row_matches(r, needle)]
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
        applies for the Albums/Artists grids.

        While grouped, letters index the top-level group headers instead
        of a column, as in `VideoTable.jump_letters`."""
        if self.model.group != GROUP_NONE:
            headers = self.model.top_level_headers()
            if len(headers) <= MIN_TILES_FOR_SORTING:
                return None
            return {_first_letter(text) for _row, text in headers}
        col = self.model.sort_column
        rows = self.model.rows()
        if col not in ALPHA_SORT_COLS or len(rows) <= MIN_TILES_FOR_SORTING:
            return None
        return {_alpha_letter(r, col) for r in rows}

    def scroll_to_letter(self, letter: str) -> None:
        if self.model.group != GROUP_NONE:
            for row_index, text in self.model.top_level_headers():
                if _first_letter(text) == letter:
                    self.view.scrollTo(
                        self.model.index(row_index, 0), QAbstractItemView.PositionAtTop
                    )
                    return
            return
        col = self.model.sort_column
        for row_index, row in enumerate(self.model.rows()):
            if _alpha_letter(row, col) == letter:
                self.view.scrollTo(
                    self.model.index(row_index, col), QAbstractItemView.PositionAtTop
                )
                return
