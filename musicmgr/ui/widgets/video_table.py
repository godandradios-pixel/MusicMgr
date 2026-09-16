"""Sortable, groupable video table - the touch-sized answer to MusicBee's
video list (Title/Artist/Album/Genre/Rating/Time/Video Kind columns,
sortable, grouped by artist).

Columns here are Title / Artist / Year / Time rather than MusicBee's, because
that's what `video_scanner.py` actually knows (filename convention, no tag
reader - see its module docstring): there is no Album, Genre or Rating to
show for a video, and a column that always reads blank is worse than not
having it. "Group by: None / Artist" mirrors MusicBee's own artist-grouped
view (its only grouping dimension we have real data for).

Deliberately NOT Qt's native `QTreeWidget.setSortingEnabled(True)` column-click
sorting: native per-column sort only compares each level's own item *text*,
which breaks two ways here - Year/Time need numeric comparison, not string
("2:00" would sort before "10:00"), and a header click should always re-sort
*within* each artist group while the groups themselves stay alphabetical,
which native sorting has no concept of at all. So sorting is manual
(`_rebuild`, keyed by `_sort_value`) - the same "own the sort, rebuild the
widget" approach `CoverGrid._sorted`/`_rebuild` already uses elsewhere in
this app, just with grouping layered on top. `QTreeWidget` itself is used
only for its native two-level (group header + spanned row) rendering and
column layout - there is never a third level, so this is a table with
section dividers, not a real tree.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QHeaderView,
    QScroller,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...services.library import format_duration
from .common import ChipButton, dim_label

#: which VideoRow.key a leaf row represents; absent (None) on a group header
ROLE_KEY = Qt.UserRole + 3
#: which artist a group-header row represents (absent on a leaf row) - lets
#: focus_artist() find the right header without parsing its display text
#: (2026-09-06)
ROLE_GROUP_ARTIST = Qt.UserRole + 4

#: (column key, header label)
COLUMNS = (
    ("title", "Title"),
    ("artist", "Artist"),
    ("year", "Year"),
    ("duration", "Time"),
)
COL_YEAR = 2
COL_DURATION = 3

GROUP_NONE = "none"
GROUP_ARTIST = "artist"

#: below this, an A-Z bar is pure chrome - matches cover_grid.py's own
#: MIN_TILES_FOR_SORTING threshold for the same reason
MIN_ROWS_FOR_JUMP = 5


@dataclass
class VideoRow:
    """One row's worth of data. `sort_key` is the same lower-cased,
    punctuation-normalised title key the rest of the app already sorts
    titles by (`Video.title_key`)."""

    key: int
    title: str
    artist: str
    year: Optional[int]
    duration_ms: Optional[int]
    sort_key: str


class VideoTable(QWidget):
    """Group-by chips above a sortable table."""

    rowActivated = Signal(int)  # VideoRow.key
    #: fires whenever rows, the filter text, the sort, or the group-by
    #: change - VideosView listens so it can refresh the shared A-Z bar's
    #: available letters/visibility, same contract as CoverGrid.updated
    updated = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[VideoRow] = []
        self._group = GROUP_NONE
        self._sort_col = 0
        self._sort_asc = True
        self._filter_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(dim_label("Group by"))
        self._group_buttons = QButtonGroup(self)
        self._group_buttons.setExclusive(True)
        none_chip = ChipButton("None")
        none_chip.setProperty("group", GROUP_NONE)
        none_chip.setChecked(True)
        artist_chip = ChipButton("Artist")
        artist_chip.setProperty("group", GROUP_ARTIST)
        for chip in (none_chip, artist_chip):
            self._group_buttons.addButton(chip)
            bar.addWidget(chip)
        self._group_buttons.buttonClicked.connect(
            lambda b: self._set_group(b.property("group"))
        )
        bar.addStretch(1)
        root.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setObjectName("VideoTable")
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels([label for _, label in COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.setMouseTracking(True)
        # native sorting is off on purpose - see module docstring
        self.tree.setSortingEnabled(False)

        header = self.tree.header()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_YEAR, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_DURATION, QHeaderView.Fixed)
        header.resizeSection(COL_YEAR, 90)
        header.resizeSection(COL_DURATION, 110)
        header.sectionClicked.connect(self._on_header_clicked)

        QScroller.grabGesture(self.tree.viewport(), QScroller.LeftMouseButtonGesture)
        self.tree.itemClicked.connect(self._on_item_clicked)
        root.addWidget(self.tree, 1)

        self._apply_sort_indicator()

    # -- data -----------------------------------------------------------

    def set_rows(self, rows: Sequence[VideoRow]) -> None:
        self._rows = list(rows)
        self._rebuild()

    def set_filter_text(self, text: str) -> None:
        """Narrow to videos whose title OR artist matches `text` - both, not
        either/or a picked field, since a video has no separate "search by"
        control the way Library's grids have a whole view each to search
        within (2026-09-15, James: "the search bar for videos to include
        artists and track titles"). Same shape as CoverGrid.set_filter_text:
        matching, not re-sorting - the active sort/group stays put, this
        just drops rows that don't qualify. An empty string shows
        everything again."""
        text = text.strip().lower()
        if text == self._filter_text:
            return
        self._filter_text = text
        self._rebuild()

    def _matches_filter(self, row: VideoRow) -> bool:
        if not self._filter_text:
            return True
        needle = self._filter_text
        return needle in (row.title or "").lower() or needle in (row.artist or "").lower()

    @property
    def group_by(self) -> str:
        return self._group

    def count(self) -> int:
        return len(self._rows)

    def focus_artist(self, artist: str) -> None:
        """Switch to Group by Artist (if not already there) and scroll that
        artist's section into view - the landing spot for a collapsed
        "N videos" tile tapped in a Library search (2026-09-06), since
        there's no per-artist filter here to jump to instead. A no-op,
        landing wherever the table already was, if the artist can't be
        found (shouldn't happen for a name this table itself supplied)."""
        if self._group != GROUP_ARTIST:
            for button in self._group_buttons.buttons():
                if button.property("group") == GROUP_ARTIST:
                    button.setChecked(True)
                    break
            self._set_group(GROUP_ARTIST)
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            if header.data(0, ROLE_GROUP_ARTIST) == artist:
                self.tree.scrollToItem(header, QAbstractItemView.PositionAtTop)
                # matches CoverGrid.scroll_to_letter's own pattern - marks
                # the landing spot as current, not just scrolled into view,
                # which also gives tests something to assert on
                self.tree.setCurrentItem(header)
                return

    # -- grouping / sorting ----------------------------------------------

    def _set_group(self, group: str) -> None:
        if group == self._group:
            return
        self._group = group
        self._rebuild()

    def _on_header_clicked(self, col: int) -> None:
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._apply_sort_indicator()
        self._rebuild()

    def _apply_sort_indicator(self) -> None:
        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        self.tree.header().setSortIndicator(self._sort_col, order)

    def _sort_value(self, row: VideoRow):
        key = COLUMNS[self._sort_col][0]
        if key == "artist":
            return ((row.artist or "").lower(), row.sort_key)
        if key == "year":
            return (row.year if row.year is not None else -1, row.sort_key)
        if key == "duration":
            return (row.duration_ms if row.duration_ms is not None else -1, row.sort_key)
        return row.sort_key  # "title", and the fallback

    def _sorted(self, rows: Sequence[VideoRow]) -> list[VideoRow]:
        return sorted(rows, key=self._sort_value, reverse=not self._sort_asc)

    def _visible_rows(self) -> list[VideoRow]:
        return [r for r in self._rows if self._matches_filter(r)]

    # -- A-Z jump bar -----------------------------------------------------

    def jump_letters(self) -> Optional[set[str]]:
        """Letters worth showing on the shared A-Z bar right now, or None to
        hide it entirely - same contract as cover_grid.py's
        CoverGrid.jump_letters. Grouped by artist, letters index the group
        headers (what a tap actually jumps to); ungrouped, they index
        whichever column is currently sorted, and only when that's a text
        column - Year/Time have no alphabetical order to jump within, the
        same reason Library's Title Details table hides the bar for those
        sorts."""
        rows = self._visible_rows()
        if len(rows) <= MIN_ROWS_FOR_JUMP:
            return None
        if self._group == GROUP_ARTIST:
            return {self._letter_of(r.artist or "Unknown artist") for r in rows}
        sort_field = COLUMNS[self._sort_col][0]
        if sort_field not in ("title", "artist"):
            return None
        return {self._letter_of(getattr(r, sort_field) or "") for r in rows}

    def _letter_of(self, text: str) -> str:
        first = text.strip()[:1].upper()
        return first if first in string.ascii_uppercase else "#"

    def scroll_to_letter(self, letter: str) -> None:
        """Jump straight to this letter - the matching group header when
        grouped by artist, or the first leaf row whose sorted column starts
        with it otherwise. A no-op if nothing matches, which shouldn't
        happen for a letter jump_letters() itself just offered."""
        if self._group == GROUP_ARTIST:
            for i in range(self.tree.topLevelItemCount()):
                header = self.tree.topLevelItem(i)
                artist = header.data(0, ROLE_GROUP_ARTIST)
                if artist is not None and self._letter_of(artist) == letter:
                    self.tree.scrollToItem(header, QAbstractItemView.PositionAtTop)
                    self.tree.setCurrentItem(header)
                    return
            return
        col = 1 if COLUMNS[self._sort_col][0] == "artist" else 0
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, ROLE_KEY) is None:
                continue
            if self._letter_of(item.text(col)) == letter:
                self.tree.scrollToItem(item, QAbstractItemView.PositionAtTop)
                self.tree.setCurrentItem(item)
                return

    # -- building -----------------------------------------------------------

    def _rebuild(self) -> None:
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        rows = self._visible_rows()
        if self._group == GROUP_ARTIST:
            groups: dict[str, list[VideoRow]] = {}
            for row in rows:
                groups.setdefault(row.artist or "Unknown artist", []).append(row)
            for artist in sorted(groups, key=str.lower):
                members = groups[artist]
                plural = "" if len(members) == 1 else "s"
                header_item = QTreeWidgetItem(
                    [f"{artist}  ·  {len(members)} video{plural}"]
                )
                font = header_item.font(0)
                font.setBold(True)
                header_item.setFont(0, font)
                # a divider, not a selectable/checkable row - no ROLE_KEY is
                # set on it, so _on_item_clicked is a no-op for it
                header_item.setFlags(Qt.ItemIsEnabled)
                header_item.setData(0, ROLE_GROUP_ARTIST, artist)
                self.tree.addTopLevelItem(header_item)
                header_item.setFirstColumnSpanned(True)
                for row in self._sorted(members):
                    self._add_row(header_item, row)
                # groups are dividers, not collapsible sections (matching
                # MusicBee's own grouped view) - a QTreeWidgetItem's children
                # start collapsed by default, so without this every group
                # would render with no visible rows underneath it at all
                header_item.setExpanded(True)
        else:
            for row in self._sorted(rows):
                self._add_row(self.tree, row)
        self.tree.setUpdatesEnabled(True)
        self.updated.emit()

    def _add_row(self, parent, row: VideoRow) -> None:
        item = QTreeWidgetItem([
            row.title,
            row.artist,
            str(row.year) if row.year else "",
            format_duration(row.duration_ms),
        ])
        item.setData(0, ROLE_KEY, row.key)
        if isinstance(parent, QTreeWidget):
            parent.addTopLevelItem(item)
        else:
            parent.addChild(item)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        key = item.data(0, ROLE_KEY)
        if key is not None:
            self.rowActivated.emit(key)

    # -- test / inspection helpers -------------------------------------------

    def leaf_titles(self) -> list[str]:
        """Titles of every non-header row, in the order currently displayed -
        i.e. respecting both grouping and the active sort."""
        titles = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, ROLE_KEY) is not None:
                titles.append(top.text(0))
            for j in range(top.childCount()):
                titles.append(top.child(j).text(0))
        return titles

    def group_headers(self) -> list[str]:
        return [
            self.tree.topLevelItem(i).text(0)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).data(0, ROLE_KEY) is None
        ]
