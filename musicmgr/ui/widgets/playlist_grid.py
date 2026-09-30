"""Touch-first tile grid for the Playlists page - 2026-09-26.

James: "I would like playlist to have larger touch areas like Artist, Album
or Jukebox. Maybe have a folder allow an image to press. But then the
subfolders makes this a challenge." The old page was a MusicBee-style tree
(small expand arrows, indented rows); this replaces it with one level of
tiles at a time. A folder is just another tile - tapping it drills in and
the page's breadcrumb walks back out - so nesting depth never costs any
space (the Billboard > 2020-29 > year chart folders are three taps, not
three indents).

Artwork, in priority order:
  1. an image James chose for that tile ("Choose image…") - `custom_path`;
  2. a 2x2 mosaic of the first four different album covers in the playlist
     (for a folder: gathered from the playlists inside it) - the same trick
     Spotify/Apple Music use, so a playlist is recognisable at a glance
     with no setup at all;
  3. a single cover when there are only one to three to pick from;
  4. the app's usual striped initials placeholder.

Folders additionally draw as a small stack (two card edges peeking out
behind the art) with a folder badge, so they read as "opens into more"
rather than "plays". Smart / chart / most-played playlists get a small
labelled pill in the corner.

Interaction: tap = `tileActivated(payload)`. Right-click, or a long press
without moving (same `GENRE_CHIP_LONG_PRESS_MS`-style hold the Jukebox genre
chips use), = `contextRequested(payload, global_pos)` - the page turns that
into a menu of Rename / Move / Choose image / Delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from PySide6.QtCore import QElapsedTimer, QEvent, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QListView,
    QListWidget,
    QListWidgetItem,
    QScroller,
    QStyle,
    QStyledItemDelegate,
)

from ...config import TOUCH
from ..theme import COLORS
from .common import cover_pixmap, placeholder_pixmap

ROLE_TILE = Qt.UserRole + 3

#: artwork edge, px - a little larger than the Library grids' 115px since a
#: playlists page has far fewer tiles to fit and they're primary targets
TILE_ART = 130
#: two title lines (names like "Most Played - This Month" need them) + one
#: subtitle line
TILE_CAPTION = 58
#: 2026-09-30 tightened 12 -> 4 (with art 150 -> 130, caption 68 -> 58 and
#: grid spacing 6 -> 4) so three rows of tiles fit the Playlists page
TILE_PAD = 4
#: how far the "stack" card edges peek out behind a folder's artwork
STACK_OFFSET = 8

LONG_PRESS_MS = 600

KIND_FOLDER = "folder"
KIND_PLAYLIST = "playlist"

#: Playlist.kind -> corner pill text (manual playlists get none)
BADGE_TEXT = {
    "smart": "SMART",
    "chart": "CHART",
    "playback": "PLAYS",
}


@dataclass
class PlaylistTile:
    kind: str  # KIND_FOLDER | KIND_PLAYLIST
    id: int
    title: str
    subtitle: str = ""
    #: up to four album covers for the automatic mosaic
    cover_paths: list[str] = field(default_factory=list)
    #: an image James chose for this tile - wins over the mosaic
    custom_path: Optional[str] = None
    #: Playlist.kind for a playlist tile ("manual", "smart", ...); "" for folders
    playlist_kind: str = ""

    def payload(self) -> dict:
        return {"type": self.kind, "id": self.id, "kind": self.playlist_kind}


_MOSAIC_CACHE: dict[tuple, QPixmap] = {}
_MOSAIC_CACHE_LIMIT = 300


def tile_pixmap(tile: PlaylistTile, size: int) -> QPixmap:
    """The artwork for one tile at `size` px - see the module docstring for
    the priority order. Mosaics are cached by their four paths + size, the
    same reason `cover_pixmap` caches single covers: grids repaint
    constantly while scrolling."""
    if tile.custom_path:
        pix = cover_pixmap(tile.custom_path, size, tile.title, crop=True)
        # cover_pixmap falls back to a placeholder when the file is gone -
        # that's fine, but a vanished custom image should still show the
        # mosaic if there is one, so only accept a *real* load here
        if not _is_placeholder(tile.custom_path):
            return pix
    paths = tile.cover_paths
    if len(paths) >= 4:
        key = (tuple(paths[:4]), size)
        cached = _MOSAIC_CACHE.get(key)
        if cached is not None:
            return cached
        pix = QPixmap(size, size)
        pix.fill(QColor(COLORS["surface_alt"]))
        half = size // 2
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        for i, path in enumerate(paths[:4]):
            quad = cover_pixmap(path, size - half if i % 2 else half, tile.title, crop=True)
            x = 0 if i % 2 == 0 else half
            y = 0 if i < 2 else half
            painter.drawPixmap(x, y, quad)
        painter.end()
        if len(_MOSAIC_CACHE) > _MOSAIC_CACHE_LIMIT:
            _MOSAIC_CACHE.clear()
        _MOSAIC_CACHE[key] = pix
        return pix
    if paths:
        return cover_pixmap(paths[0], size, tile.title, crop=True)
    return placeholder_pixmap(size, tile.title)


def _is_placeholder(path: Optional[str]) -> bool:
    from .common import _resolve_art_path  # same resolver cover_pixmap uses

    return _resolve_art_path(path) is None


class PlaylistTileDelegate(QStyledItemDelegate):
    def __init__(self, art_size: int = TILE_ART, parent=None) -> None:
        super().__init__(parent)
        self.art_size = art_size

    def sizeHint(self, option, index) -> QSize:
        return QSize(
            self.art_size + TILE_PAD * 2 + STACK_OFFSET,
            self.art_size + TILE_CAPTION + TILE_PAD * 2 + STACK_OFFSET,
        )

    def paint(self, painter: QPainter, option, index) -> None:
        tile: Optional[PlaylistTile] = index.data(ROLE_TILE)
        if tile is None:
            return
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        pressed = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if pressed or hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["surface_hi"] if pressed else COLORS["surface_alt"]))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 12, 12)

        size = self.art_size
        is_folder = tile.kind == KIND_FOLDER
        # folders sit up-and-left by STACK_OFFSET so the stack edges drawn
        # behind them stay inside the cell; playlists centre in the same cell
        art_x = rect.left() + TILE_PAD + (0 if is_folder else STACK_OFFSET // 2)
        art_y = rect.top() + TILE_PAD + (0 if is_folder else STACK_OFFSET // 2)

        if is_folder:
            painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
            for step, alpha in ((2, 110), (1, 190)):
                off = STACK_OFFSET * step / 2
                painter.setBrush(QColor(92, 100, 116, alpha))
                painter.drawRoundedRect(QRectF(art_x + off, art_y + off, size, size), 10, 10)

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(art_x, art_y, size, size), 10, 10)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(art_x, art_y, size, size, tile_pixmap(tile, size))
        painter.restore()

        if is_folder:
            _paint_folder_badge(painter, art_x + 8, art_y + size - 8 - 30, 30)
        else:
            badge = BADGE_TEXT.get(tile.playlist_kind)
            if badge:
                _paint_pill(painter, badge, art_x + 8, art_y + 8)

        if pressed:
            painter.setPen(QColor(COLORS["jukebox_key_hi"]))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(clip)

        text_x, text_w = art_x, size
        # same caption line for folders and playlists, just under the stack
        text_y = rect.top() + TILE_PAD + size + STACK_OFFSET + 4
        font = painter.font()
        font.setPixelSize(TOUCH["font_base"])
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        metrics = QFontMetrics(font)
        lines = _wrap_two_lines(tile.title, metrics, text_w)
        for n, line in enumerate(lines):
            painter.drawText(
                text_x, text_y + n * 19, text_w, 20, Qt.AlignLeft | Qt.AlignVCenter, line
            )
        text_y += (len(lines) - 1) * 19
        font.setPixelSize(TOUCH["font_base"] - 3)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_dim"]))
        metrics = QFontMetrics(font)
        painter.drawText(
            text_x, text_y + 18, text_w, 17, Qt.AlignLeft | Qt.AlignVCenter,
            metrics.elidedText(tile.subtitle, Qt.ElideRight, text_w),
        )
        painter.restore()


def _wrap_two_lines(text: str, metrics: QFontMetrics, width: int) -> list[str]:
    """Word-wrap `text` into at most two lines that fit `width`, eliding the
    second if it still doesn't fit - so "Most Played - This Month" reads in
    full instead of "Most Played - T…"."""
    if metrics.horizontalAdvance(text) <= width:
        return [text]
    words = text.split()
    first = ""
    rest_index = 0
    for i, word in enumerate(words):
        candidate = f"{first} {word}".strip()
        if metrics.horizontalAdvance(candidate) > width:
            break
        first = candidate
        rest_index = i + 1
    if not first:  # one very long word - just elide it on one line
        return [metrics.elidedText(text, Qt.ElideRight, width)]
    rest = " ".join(words[rest_index:])
    return [first, metrics.elidedText(rest, Qt.ElideRight, width)]


def _paint_pill(painter: QPainter, text: str, x: float, y: float) -> None:
    painter.save()
    font = QFont(painter.font())
    font.setPixelSize(11)
    font.setBold(True)
    painter.setFont(font)
    metrics = QFontMetrics(font)
    w = metrics.horizontalAdvance(text) + 14
    h = 20
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 170))
    painter.drawRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
    painter.setPen(QColor(255, 255, 255, 235))
    painter.drawText(QRectF(x, y, w, h), Qt.AlignCenter, text)
    painter.restore()


def _paint_folder_badge(painter: QPainter, x: float, y: float, d: float) -> None:
    """A dark disc with a simple drawn folder glyph - drawn rather than an
    emoji so it looks the same on Windows and Linux Mint."""
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 175))
    painter.drawEllipse(QRectF(x, y, d, d))
    fw, fh = d * 0.56, d * 0.40
    fx, fy = x + (d - fw) / 2, y + (d - fh) / 2 + 1
    path = QPainterPath()
    path.moveTo(fx, fy + fh * 0.2)
    path.lineTo(fx, fy - fh * 0.08)
    path.lineTo(fx + fw * 0.38, fy - fh * 0.08)
    path.lineTo(fx + fw * 0.48, fy + fh * 0.08)
    path.lineTo(fx + fw, fy + fh * 0.08)
    path.lineTo(fx + fw, fy + fh)
    path.lineTo(fx, fy + fh)
    path.closeSubpath()
    painter.setBrush(QColor(COLORS["jukebox_key_hi"]))
    painter.drawPath(path)
    painter.restore()


class PlaylistGrid(QListWidget):
    """The tile grid itself - see the module docstring."""

    tileActivated = Signal(dict)
    contextRequested = Signal(dict, QPoint)

    def __init__(self, art_size: int = TILE_ART, parent=None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)
        self.setUniformItemSizes(True)
        self.setSpacing(4)
        self.setWordWrap(False)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setItemDelegate(PlaylistTileDelegate(art_size, parent=self))
        QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
        self.itemClicked.connect(self._on_clicked)

        self._tiles: list[PlaylistTile] = []
        self._press_pos: Optional[QPoint] = None
        self._long_press_fired = False
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_long_press)
        #: a touch panel's own press-and-hold can *also* arrive as a
        #: right-click; this keeps one hold from opening two menus
        self._since_context = QElapsedTimer()
        self.viewport().installEventFilter(self)

    # -- data ---------------------------------------------------------------

    def set_tiles(self, tiles: Sequence[PlaylistTile]) -> None:
        self._tiles = list(tiles)
        self.clear()
        for tile in self._tiles:
            item = QListWidgetItem()
            item.setData(ROLE_TILE, tile)
            item.setToolTip(tile.title)
            self.addItem(item)

    def tiles(self) -> list[PlaylistTile]:
        return list(self._tiles)

    def tile_at(self, pos: QPoint) -> Optional[PlaylistTile]:
        item = self.itemAt(pos)
        return item.data(ROLE_TILE) if item is not None else None

    # -- interaction ----------------------------------------------------------

    def _on_clicked(self, item: QListWidgetItem) -> None:
        if self._long_press_fired:
            # the tap that ends a long press already opened the menu
            self._long_press_fired = False
            return
        tile: Optional[PlaylistTile] = item.data(ROLE_TILE)
        if tile is not None:
            self.tileActivated.emit(tile.payload())

    def eventFilter(self, obj, event):  # noqa: D102 - Qt override
        if obj is self.viewport():
            etype = event.type()
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._press_pos = event.position().toPoint()
                self._long_press_fired = False
                if self.itemAt(self._press_pos) is not None:
                    self._press_timer.start(LONG_PRESS_MS)
            elif etype == QEvent.MouseMove and self._press_pos is not None:
                moved = (event.position().toPoint() - self._press_pos).manhattanLength()
                if moved >= QApplication.startDragDistance():
                    self._press_timer.stop()
            elif etype == QEvent.MouseButtonRelease:
                self._press_timer.stop()
                self._press_pos = None
        return super().eventFilter(obj, event)

    def _on_long_press(self) -> None:
        if self._press_pos is None:
            return
        tile = self.tile_at(self._press_pos)
        if tile is None:
            return
        self._long_press_fired = True
        self._emit_context(tile, self.viewport().mapToGlobal(self._press_pos))

    def contextMenuEvent(self, event) -> None:  # noqa: D102 - Qt override
        if self._long_press_fired:
            # the same hold already opened a menu (a touch panel can report
            # press-and-hold as a right-click as well)
            return
        tile = self.tile_at(self.viewport().mapFromGlobal(event.globalPos()))
        if tile is not None:
            self._emit_context(tile, event.globalPos())

    def _emit_context(self, tile: PlaylistTile, global_pos: QPoint) -> None:
        if self._since_context.isValid() and self._since_context.elapsed() < 800:
            return
        self._since_context.start()
        self.contextRequested.emit(tile.payload(), global_pos)
