"""Thumbnail grid for the Videos page - the default way to browse videos.

16:9 tiles rather than `CoverGrid`'s square ones: a video's picture is a
frame grab or poster (services/video_details.py), and squaring it either
crops off the sides or shrinks it into a letterbox. Each tile shows the
picture with its running time in the corner, then the title and
"artist · year" underneath. Tap a tile to play it (`rowActivated`).

Takes the same `VideoRow`s as `VideoTable` (the page's List layout) and
offers the same seams - `set_rows`, `set_filter_text`, `jump_letters`,
`scroll_to_letter`, `updated` - so `VideosView` can drive either one
without caring which is on screen.

Sorting is a chip row (Title / Artist / Year / Recently added), the same
shape as the Library's Albums grid. Search matches a video's title *or*
artist, like `VideoTable` - unlike `CoverGrid`, whose search only matches
a tile's own name because it browses by name.
"""

from __future__ import annotations

import string
from typing import Optional, Sequence

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QScroller,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...services.library import format_duration
from ..theme import COLORS
from .common import ChipButton, cover_pixmap
from .video_table import VideoRow

ROLE_ROW = Qt.UserRole + 5

#: picture size, 16:9
THUMB_W, THUMB_H = 256, 144
#: room for a title wrapped onto two lines, then "artist · year"
CAPTION_H = 80
TITLE_LINE_H = 21
PAD = 10

SORT_TITLE = "title"
SORT_ARTIST = "artist"
SORT_YEAR = "year"
SORT_ADDED = "added"

#: (key, chip label, sorts alphabetically - i.e. the A-Z bar makes sense)
SORTS = (
    (SORT_TITLE, "Title", True),
    (SORT_ARTIST, "Artist", True),
    (SORT_YEAR, "Year", False),
    (SORT_ADDED, "Recently added", False),
)

#: below this, an A-Z bar is pure chrome - same threshold as VideoTable
MIN_TILES_FOR_JUMP = 5

_THUMB_CACHE: dict[tuple, QPixmap] = {}


def video_thumb_pixmap(row: VideoRow, width: int = THUMB_W, height: int = THUMB_H) -> QPixmap:
    """The tile picture: the video's own thumbnail filling the 16:9 frame
    (centre-cropped), else its artist's portrait - or the generated
    initials placeholder - centred on a plain background."""
    key = (row.thumb or "", row.fallback_art or "", row.artist, width, height)
    cached = _THUMB_CACHE.get(key)
    if cached is not None:
        return cached
    pix = QPixmap(width, height)
    pix.fill(QColor(COLORS["surface_alt"]))
    loaded = QPixmap(row.thumb) if row.thumb else QPixmap()
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    if not loaded.isNull():
        scaled = loaded.scaled(width, height, Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, (scaled.height() - height) // 2)
        painter.drawPixmap(0, 0, scaled, x, y, width, height)
    else:
        square = cover_pixmap(row.fallback_art, height, row.artist or row.title, crop=True)
        painter.drawPixmap((width - height) // 2, 0, square)
    painter.end()
    _THUMB_CACHE[key] = pix
    return pix


def title_lines(text: str, metrics: QFontMetrics, width: int, max_lines: int = 2) -> list[str]:
    """`text` word-wrapped to `width`, at most `max_lines` lines, the last
    one elided with "…" if the title still doesn't fit. A single word too
    long for a line is elided rather than split mid-word."""
    words = text.split()
    lines: list[str] = []
    while words and len(lines) < max_lines:
        if len(lines) == max_lines - 1:
            lines.append(metrics.elidedText(" ".join(words), Qt.ElideRight, width))
            break
        line = words.pop(0)
        while words and metrics.horizontalAdvance(f"{line} {words[0]}") <= width:
            line = f"{line} {words.pop(0)}"
        lines.append(metrics.elidedText(line, Qt.ElideRight, width))
    return lines or [""]


class VideoTileDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:
        return QSize(THUMB_W + PAD * 2, THUMB_H + CAPTION_H + PAD * 2)

    def paint(self, painter: QPainter, option, index) -> None:
        row: Optional[VideoRow] = index.data(ROLE_ROW)
        if row is None:
            return
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected or hovered:
            painter.setBrush(QColor(COLORS["surface_hi"] if selected else COLORS["surface_alt"]))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 12, 12)

        x, y = rect.left() + PAD, rect.top() + PAD
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(x, y, THUMB_W, THUMB_H), 10, 10)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(x, y, video_thumb_pixmap(row))
        painter.restore()

        if selected:
            painter.setPen(QColor(COLORS["jukebox_key_hi"]))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(clip)

        font = painter.font()
        # running time, bottom-right of the picture
        if row.duration_ms:
            font.setPixelSize(TOUCH["font_base"] - 3)
            font.setBold(True)
            painter.setFont(font)
            text = format_duration(row.duration_ms)
            metrics = QFontMetrics(font)
            w, h = metrics.horizontalAdvance(text) + 12, metrics.height() + 4
            badge = QRectF(x + THUMB_W - w - 6, y + THUMB_H - h - 6, w, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(badge, Qt.AlignCenter, text)

        # a play button over the picture on hover
        if hovered:
            r = 26
            cx, cy = x + THUMB_W / 2, y + THUMB_H / 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 160))
            painter.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
            tri = QPainterPath()
            tri.moveTo(cx - 8, cy - 12)
            tri.lineTo(cx - 8, cy + 12)
            tri.lineTo(cx + 13, cy)
            tri.closeSubpath()
            painter.setBrush(QColor("#ffffff"))
            painter.drawPath(tri)

        text_y = y + THUMB_H + 6
        font.setPixelSize(TOUCH["font_base"])
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        metrics = QFontMetrics(font)
        lines = title_lines(row.title, metrics, THUMB_W)
        for i, line in enumerate(lines):
            painter.drawText(x, text_y + i * TITLE_LINE_H, THUMB_W, TITLE_LINE_H,
                             Qt.AlignLeft | Qt.AlignVCenter, line)

        font.setPixelSize(TOUCH["font_base"] - 3)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_dim"]))
        metrics = QFontMetrics(font)
        caption = " · ".join(p for p in (row.artist, str(row.year) if row.year else "") if p)
        # right under the title, one line or two
        painter.drawText(x, text_y + len(lines) * TITLE_LINE_H + 1, THUMB_W, 20,
                         Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(caption, Qt.ElideRight, THUMB_W))
        painter.restore()


class VideoGrid(QWidget):
    """Sort chips above a wrapping grid of 16:9 video tiles."""

    rowActivated = Signal(int)  # VideoRow.key
    #: the visible tiles or the sort changed - VideosView re-asks
    #: jump_letters() for the shared A-Z bar
    updated = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[VideoRow] = []
        self._visible: list[VideoRow] = []
        self._filter_text = ""
        self._sort = SORT_TITLE

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.sort_label = QLabel("Sort")
        self.sort_label.setObjectName("Dim")
        bar.addWidget(self.sort_label)
        self._sort_group = QButtonGroup(self)
        self._sort_group.setExclusive(True)
        for key, label, _alpha in SORTS:
            chip = ChipButton(label)
            chip.setProperty("sort", key)
            chip.setChecked(key == self._sort)
            self._sort_group.addButton(chip)
            bar.addWidget(chip)
        self._sort_group.buttonClicked.connect(lambda b: self.set_sort(b.property("sort")))
        bar.addStretch(1)
        root.addLayout(bar)

        self.view = QListWidget()
        self.view.setObjectName("VideoGrid")
        self.view.setViewMode(QListView.IconMode)
        self.view.setMovement(QListView.Static)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(6)
        self.view.setWordWrap(False)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setMouseTracking(True)
        self.view.setItemDelegate(VideoTileDelegate(self))
        self.view.setGridSize(QSize(THUMB_W + PAD * 2 + 6, THUMB_H + CAPTION_H + PAD * 2 + 6))
        QScroller.grabGesture(self.view.viewport(), QScroller.LeftMouseButtonGesture)
        self.view.itemClicked.connect(self._on_clicked)
        root.addWidget(self.view, 1)

    # -- data -------------------------------------------------------------

    def set_rows(self, rows: Sequence[VideoRow], keep_scroll: bool = False) -> None:
        """`keep_scroll`: the same videos refreshed (new pictures from the
        background filler) - stay where the viewer is rather than jumping
        back to the top."""
        bar = self.view.verticalScrollBar()
        at = bar.value()
        self._rows = list(rows)
        self._rebuild()
        if keep_scroll:
            bar.setValue(at)

    def set_filter_text(self, text: str) -> None:
        text = text.strip().lower()
        if text == self._filter_text:
            return
        self._filter_text = text
        self._rebuild()

    @property
    def sort(self) -> str:
        return self._sort

    def set_sort(self, key: str) -> None:
        self._sort = key
        for chip in self._sort_group.buttons():
            chip.setChecked(chip.property("sort") == key)
        self._rebuild()

    def _matches(self, row: VideoRow) -> bool:
        needle = self._filter_text
        return not needle or needle in row.title.lower() or needle in row.artist.lower()

    def _sorted(self, rows: Sequence[VideoRow]) -> list[VideoRow]:
        if self._sort == SORT_ARTIST:
            return sorted(rows, key=lambda r: (not r.artist, r.artist.lower(), r.sort_key))
        if self._sort == SORT_YEAR:
            # newest first; undated last
            return sorted(rows, key=lambda r: (r.year is None, -(r.year or 0), r.sort_key))
        if self._sort == SORT_ADDED:
            return sorted(rows, key=lambda r: (-r.added_rank, r.sort_key))
        return sorted(rows, key=lambda r: r.sort_key)

    def _rebuild(self) -> None:
        self._visible = self._sorted([r for r in self._rows if self._matches(r)])
        self.view.setUpdatesEnabled(False)
        self.view.clear()
        hint = QSize(THUMB_W + PAD * 2, THUMB_H + CAPTION_H + PAD * 2)
        for row in self._visible:
            item = QListWidgetItem()
            item.setData(ROLE_ROW, row)
            item.setSizeHint(hint)
            self.view.addItem(item)
        self.view.setUpdatesEnabled(True)
        worth_sorting = len(self._rows) > MIN_TILES_FOR_JUMP
        self.sort_label.setVisible(worth_sorting)
        for chip in self._sort_group.buttons():
            chip.setVisible(worth_sorting)
        self.updated.emit()

    def _on_clicked(self, item: QListWidgetItem) -> None:
        row = item.data(ROLE_ROW)
        if row is not None:
            self.rowActivated.emit(row.key)

    # -- shared A-Z bar -----------------------------------------------------

    def _letter_of(self, row: VideoRow) -> str:
        source = row.artist if self._sort == SORT_ARTIST else row.sort_key
        first = (source or "").strip()[:1].upper()
        return first if first in string.ascii_uppercase else "#"

    def jump_letters(self) -> Optional[set[str]]:
        alpha = next(a for k, _l, a in SORTS if k == self._sort)
        if not alpha or len(self._visible) <= MIN_TILES_FOR_JUMP:
            return None
        return {self._letter_of(r) for r in self._visible}

    def scroll_to_letter(self, letter: str) -> None:
        for i, row in enumerate(self._visible):
            if self._letter_of(row) == letter:
                item = self.view.item(i)
                self.view.scrollToItem(item, QAbstractItemView.PositionAtTop)
                self.view.setCurrentItem(item)
                return

    # -- inspection (tests) ---------------------------------------------------

    def count(self) -> int:
        return self.view.count()

    def titles(self) -> list[str]:
        return [r.title for r in self._visible]
