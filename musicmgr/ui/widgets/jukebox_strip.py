"""One numbered title strip on the Jukebox page (ui/views/jukebox.py).

Layout, modelled on a 45 sleeve:

- One large square cover (`_CardCover`) on the left, rounded-corner
  clipped the same way cover_grid.py/album_tracks.py clip their square art
  (an inline `QPainterPath.addRoundedRect`). Sourced from the A side's
  cover, falling back to the B side's: the card shows one piece of sleeve
  art per physical record even though the data model tracks a cover per
  track.
- Two pointed "chevron" banners (`_ChevronBanner`, one per side) on the
  right: a solid code box ("A7") feeding into a cream title panel, pointed
  at the right edge - a shape QPushButton stylesheets can't produce, hence
  the custom paintEvent. The banner itself is the tap target. An empty
  side shows a greyed banner reading "OPEN".
- One artist-name badge between the two banners. Colors come from the
  `COLORS["jukebox_key"]` family in ui/theme.py.

The card has no edit/share overlay buttons and stays inside the 3x3 grid;
the cover size and banner `sizeHint` are tuned to fit it. Tapping a banner
doesn't animate anything (a spinning rounded square looks broken).

Every child has a fixed, content-independent size (the artist badge has a
fixed width and elides by hand), and `__init__` locks the card with
`setFixedSize(self.sizeHint())`, so a long artist name or a hidden sibling
in `JukeboxView`'s grid can't stretch or shrink it. The vertical
footprint is kept tight so `JukeboxView._rows_that_fit`, which never drops
below `MIN_ROWS` (3), doesn't clip the third row on short displays; it
measures `self.strips[0].sizeHint()` fresh, so size changes here are
picked up automatically.

Signals - the widget only reports gestures; `JukeboxView` owns dialogs and
database calls:

- `sideActivated`: a filled banner was tapped (send to Now Playing).
- `fillRequested`: an empty ("OPEN") banner was tapped; the view opens
  `JukeboxPickerDialog` pre-filled with this card's artist and genre,
  `max_picks=1`.
- Right-click menu (`_build_context_menu`): "Organize card…"
  (`moveRequested` - pick a slot number and genre; picking a number rather
  than a target card works across pages), "Edit songs…" (`editRequested`
  - `JukeboxEditSongsDialog`, either side, via
  `services.jukebox.set_slot_side`), and "Remove from jukebox…"
  (`removeRequested` - takes both sides off the board; tracks, files and
  ratings are untouched). This widget doesn't display a card's genre.
- Drag-and-drop: a strip is both a drag source and a drop target.
  `SLOT_MIME_TYPE` carries the dragged card's `slot_number`; dropping onto
  another card emits `reorderRequested(source_slot, target_slot)`, which
  the view turns into `services.jukebox.reorder_slot` (an insert that
  shifts the board, not a two-way swap - see that function's docstring).
  Only the cover and the artist badge row are drag handles, since the
  banners are already tap targets. A card accepts a drop only while it has
  a `slot_number` and isn't the source; it highlights via a `dropTarget`
  dynamic property (ui/theme.py). Crossing pages works because
  `JukeboxView`'s pager flips pages while a drag hovers its "‹"/"›"
  arrows - see that view's module docstring.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRectF, Qt, QSize, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDrag,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...services.library import format_duration
from ..theme import COLORS
from .common import cover_pixmap

_SIDES = ("A", "B")

#: custom MIME type carrying a dragged card's slot_number as plain decimal
#: text. Shared with ui/views/jukebox.py, which watches for the same type
#: hovering its page-arrow buttons to flip pages mid-drag.
SLOT_MIME_TYPE = "application/x-musicmgr-jukebox-slot"


def _decode_slot(mime: QMimeData) -> Optional[int]:
    """The dragged card's slot_number out of `mime`, or None if `mime`
    doesn't carry this type or carries something unparseable (defensive
    against a foreign drag)."""
    if not mime.hasFormat(SLOT_MIME_TYPE):
        return None
    try:
        return int(bytes(mime.data(SLOT_MIME_TYPE)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class _CardCover(QWidget):
    """The one square cover a whole title strip shows - see the module
    docstring for why there's only one, not one per side."""

    def __init__(self, size: int = 74, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._pixmap = None
        self.set_source(None, "")

    def set_source(self, path: Optional[str], seed_text: str) -> None:
        self._pixmap = cover_pixmap(path, self._size, seed_text, crop=True)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, self._size, self._size, 10, 10)
        painter.setClipPath(clip)
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)
        painter.end()


class _ChevronBanner(QAbstractButton):
    """One side (A/B) of a title strip, painted as a pointed pennant. See
    the module docstring for why this is a custom paintEvent and why the
    whole banner is the tap target.

    States are driven purely by `_code`/`isDown()`/`_now_playing`: a
    filled, not-playing side is cream with a brown outline and code box;
    the currently-playing side fills solid brown with white text; an empty
    side is greyed out and reads "OPEN". The empty side stays enabled
    (tapping it emits `JukeboxStripWidget.fillRequested`), so its grey look
    is purely paint - see `paintEvent`'s `filled = bool(self._code)`.

    Titles paint in their stored case (no `.upper()`); `_CODE_BOX_RATIO`/
    `_POINT_RATIO` and `sizeHint()` are tuned to leave room for longer
    mixed-case titles. "OPEN" is passed in already uppercase from
    `_set_side`.
    """

    _CODE_BOX_RATIO = 1.3  # code box width, as a multiple of the banner height
    _POINT_RATIO = 0.45  # right-hand point length, as a multiple of height

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._code = ""
        self._title = ""
        self._now_playing = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(140)
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: D102 - Qt override
        return QSize(210, 30)

    def set_content(self, code: str, title: str) -> None:
        self._code = code
        self._title = title
        self.update()

    def set_now_playing(self, active: bool) -> None:
        if self._now_playing != active:
            self._now_playing = active
            self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        h = rect.height()
        point_len = min(h * self._POINT_RATIO, rect.width() * 0.2)
        code_box_w = min(h * self._CODE_BOX_RATIO, rect.width() * 0.35)

        path = QPainterPath()
        path.moveTo(rect.left(), rect.top())
        path.lineTo(rect.right() - point_len, rect.top())
        path.lineTo(rect.right(), rect.top() + h / 2)
        path.lineTo(rect.right() - point_len, rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()

        # an empty side is still clickable (see `fillRequested`), so the
        # grey "OPEN" look is decided by `_code` being blank, not isEnabled()
        filled = bool(self._code)
        if not filled:
            fill = QColor(COLORS["surface_hi"])
            border = QColor(COLORS["border"])
            code_fill = QColor(COLORS["surface"])
            code_text_color = QColor(COLORS["text_dim"])
            title_color = QColor(COLORS["text_dim"])
        elif self._now_playing:
            fill = QColor(COLORS["jukebox_key"])
            border = QColor(COLORS["jukebox_key_hi"])
            code_fill = QColor(COLORS["jukebox_key_pressed"])
            code_text_color = QColor(COLORS["on_accent"])
            title_color = QColor(COLORS["on_accent"])
        else:
            fill = QColor(COLORS["jukebox_paper"])
            border = QColor(COLORS["jukebox_key"])
            code_fill = QColor(COLORS["jukebox_key"])
            code_text_color = QColor(COLORS["on_accent"])
            title_color = QColor(COLORS["jukebox_ink"])
        if self.isDown():
        # both states are tap targets, so both darken under a press
            fill = fill.darker(112)

        painter.setPen(QPen(border, 2))
        painter.setBrush(fill)
        painter.drawPath(path)

        code_rect = QRectF(rect.left(), rect.top(), code_box_w, h)
        if self._code:
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(code_rect, code_fill)
            painter.restore()

            code_font = QFont(self.font())
            code_font.setBold(True)
            code_font.setPointSize(max(9, int(h * 0.34)))
            painter.setFont(code_font)
            painter.setPen(code_text_color)
            painter.drawText(code_rect, Qt.AlignCenter, self._code)
            title_left = code_rect.right() + 6
        else:
            title_left = rect.left() + 10

        title_rect = QRectF(
            title_left, rect.top(), rect.right() - point_len - title_left - 3, h
        )
        title_font = QFont(self.font())
        title_font.setBold(True)
        title_font.setPointSize(max(8, int(h * 0.26)))
        painter.setFont(title_font)
        painter.setPen(title_color)
        elided = self._elided_title(painter.fontMetrics(), title_rect.width())
        painter.drawText(title_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.end()

    def _elided_title(self, metrics: QFontMetrics, width: float) -> str:
        """Split out from `paintEvent` so a test can check the title's case
        is preserved (no `.upper()`) without inspecting rendered pixels."""
        return metrics.elidedText(self._title, Qt.ElideRight, max(0, int(width)))


class JukeboxStripWidget(QFrame):
    """One numbered strip - see the module docstring."""

    #: side ("A"/"B"), track_id - emitted when a filled side's banner is tapped
    sideActivated = Signal(str, int)
    #: this_slot_number - emitted when an *empty* ("OPEN") side's banner is
    #: tapped. Only the slot is reported, not which side -
    #: `services.jukebox.place_track` decides where a picked song lands.
    fillRequested = Signal(int)
    #: this_slot_number - emitted when "Organize card…" is chosen from the
    #: card's right-click menu (the dialog moves the card and/or changes its
    #: genre; the signal keeps its older `moveRequested` name).
    moveRequested = Signal(int)
    #: this_slot_number - emitted when "Remove from jukebox…" is chosen
    #: from the same right-click menu
    removeRequested = Signal(int)
    #: this_slot_number - emitted when "Edit songs…" is chosen from the
    #: same right-click menu. Unlike `fillRequested`, this covers either
    #: side, filled or not - `JukeboxView._on_edit_requested` opens a dialog
    #: that can replace or clear either song via
    #: `services.jukebox.set_slot_side`.
    editRequested = Signal(int)
    #: source_slot_number, target_slot_number - emitted when a card is
    #: dropped onto a different card. `JukeboxView` turns this into a
    #: `services.jukebox.reorder_slot` call - the dragged card takes the
    #: target's rank and everything from there shifts over by one, *not* a
    #: two-way trade (that's `moveRequested`/"Organize card…"'s `swap_slots`).
    reorderRequested = Signal(int, int)

    #: fixed width of the artist-name badge, whatever the artist's name is
    #: (see the module docstring for why the card can't size to content).
    #: Equals `_ChevronBanner.sizeHint()`'s width so the badge's edges land
    #: flush with the banners'; fits ordinary billings ("Steve Miller Band",
    #: "Earth, Wind & Fire") and elides genuinely long ones.
    _ARTIST_LABEL_WIDTH = 210

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("JukeboxStripOuter")
        self._slot_number: Optional[int] = None
        self._now_playing_side: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("JukeboxStrip")
        card.setToolTip(
            "Drag the cover to reorder · right-click for more options "
            "(organize or remove this card)"
        )
        outer.addWidget(card)
        self.card = card
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(10, 6, 10, 6)
        card_layout.setSpacing(10)

        self.cover = _CardCover(size=74)
        card_layout.addWidget(self.cover)

        right_col = QVBoxLayout()
        right_col.setSpacing(4)

        self._rows: dict[str, dict] = {}

        top_banner = _ChevronBanner()
        top_banner.clicked.connect(lambda _=False: self._on_key_clicked("A"))
        right_col.addWidget(top_banner)
        self._rows["A"] = {"banner": top_banner, "track_id": None}

        self._artist_row = self._build_artist_row()
        right_col.addWidget(self._artist_row)

        bottom_banner = _ChevronBanner()
        bottom_banner.clicked.connect(lambda _=False: self._on_key_clicked("B"))
        right_col.addWidget(bottom_banner)
        self._rows["B"] = {"banner": bottom_banner, "track_id": None}

        card_layout.addLayout(right_col, 1)

        # every child above has a fixed, content-independent sizeHint, so
        # locking to the combined sizeHint means nothing set later, and no
        # sibling strip being hidden or shown, can resize this card.
        self.setFixedSize(self.sizeHint())

        # drag-and-drop reordering: only the cover and the artist badge row
        # are drag handles, watched via an event filter so this widget stays
        # the one place the card's interaction logic lives.
        self._drag_start_pos: Optional[QPoint] = None
        self.cover.installEventFilter(self)
        self._artist_row.installEventFilter(self)
        self.setAcceptDrops(True)

    def _build_artist_row(self) -> QWidget:
        # the label is the row's only child, so the badge reaches flush with
        # the banners above/below (see `_ARTIST_LABEL_WIDTH`).
        row = QWidget()
        row.setObjectName("JukeboxArtistRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.artist_label = QLabel("")
        self.artist_label.setObjectName("JukeboxArtistBadge")
        self.artist_label.setAlignment(Qt.AlignCenter)
        self.artist_label.setFixedWidth(self._ARTIST_LABEL_WIDTH)
        h.addWidget(self.artist_label)
        return row

    def _set_artist_text(self, artist_name: str) -> None:
        """Elide by hand rather than letting the label size to its text -
        `artist_label` has a fixed width so a long name can't grow this card
        wider than any other on the page."""
        # slack for the QSS badge's padding/border (ui/theme.py's
        # QLabel#JukeboxArtistBadge): 10px padding + 1px border each side
        available = self._ARTIST_LABEL_WIDTH - 22
        metrics = self.artist_label.fontMetrics()
        elided = metrics.elidedText(artist_name.upper(), Qt.ElideRight, available)
        self.artist_label.setText(elided or "—")

    # -- populating -------------------------------------------------------

    def set_slot(self, slot: dict) -> None:
        """`slot` is the dict shape from services/jukebox.py:slot_to_dict."""
        self._slot_number = slot["slot_number"]
        artist_name = slot.get("artist_name", "")
        self._set_artist_text(artist_name)

        side_a = slot.get("side_a")
        side_b = slot.get("side_b")
        # one cover per card, not per side - see the module docstring
        cover_path = (side_a or {}).get("cover_path") or (side_b or {}).get("cover_path")
        self.cover.set_source(cover_path, artist_name)

        for key, side in (("side_a", "A"), ("side_b", "B")):
            self._set_side(side, slot.get(key), artist_name)
        self._sync_indicator()

    def _set_side(self, side: str, data: Optional[dict], artist_name: str) -> None:
        widgets = self._rows[side]
        banner = widgets["banner"]
        code = f"{side}{self._slot_number}"
        if data is None:
            widgets["track_id"] = None
            banner.set_content("", "OPEN")
            # left enabled: this side is a real tap target (fillRequested)
            banner.setToolTip("Tap to add a second song")
        else:
            widgets["track_id"] = data["track_id"]
            banner.set_content(code, data["title"])
            duration = format_duration(data.get("duration_ms"))
            banner.setToolTip(f"{artist_name} · {duration}" if artist_name else duration)

    def clear(self) -> None:
        """Blank the strip out for a page with fewer slots than there are
        strip widgets to fill - JukeboxView hides the widget outright rather
        than showing a cleared one, but clearing first keeps a stale title
        from flashing if it's ever shown again before the next set_slot()."""
        self._slot_number = None
        self.artist_label.setText("")
        self.cover.set_source(None, "")
        for side in _SIDES:
            self._set_side(side, None, "")
        self.set_now_playing(None)

    # -- playback state -----------------------------------------------------

    def slot_number(self) -> Optional[int]:
        return self._slot_number

    def set_now_playing(self, side: Optional[str]) -> None:
        self._now_playing_side = side
        self._sync_indicator()

    def _sync_indicator(self) -> None:
        for side, widgets in self._rows.items():
            widgets["banner"].set_now_playing(side == self._now_playing_side)

    def _on_key_clicked(self, side: str) -> None:
        widgets = self._rows[side]
        track_id = widgets["track_id"]
        if track_id is None:
            # an empty ("OPEN") side requests a fill; an unused page-filler
            # strip has no slot number to report, so it stays a no-op.
            if self._slot_number is not None:
                self.fillRequested.emit(self._slot_number)
            return
        self.sideActivated.emit(side, track_id)

    # -- reordering ---------------------------------------------------------

    def contextMenuEvent(self, event) -> None:  # noqa: D102 - Qt override
        if self._slot_number is None:
            return  # an unused page-filler strip has nothing to move/remove
        self._build_context_menu().exec(event.globalPos())

    def _build_context_menu(self) -> QMenu:
        """Split out from `contextMenuEvent` so a test can trigger an action
        directly (find it by its text, then `.trigger()`) rather than
        driving `QMenu.exec()`'s real popup event loop.

        "Organize card…" and "Edit songs…" adjust the card in place;
        "Remove from jukebox…", below the separator, is the one destructive
        action."""
        menu = QMenu(self)
        move_action = menu.addAction("Organize card…")
        move_action.triggered.connect(self._on_move_clicked)
        edit_action = menu.addAction("Edit songs…")
        edit_action.triggered.connect(self._on_edit_clicked)
        menu.addSeparator()
        remove_action = menu.addAction("Remove from jukebox…")
        remove_action.triggered.connect(self._on_remove_clicked)
        return menu

    def _on_move_clicked(self) -> None:
        if self._slot_number is not None:
            self.moveRequested.emit(self._slot_number)

    def _on_edit_clicked(self) -> None:
        if self._slot_number is not None:
            self.editRequested.emit(self._slot_number)

    def _on_remove_clicked(self) -> None:
        if self._slot_number is not None:
            self.removeRequested.emit(self._slot_number)

    # -- drag-and-drop reordering -------------------------------------------

    def eventFilter(self, obj, event):  # noqa: D102 - Qt override
        """Watches `self.cover`/`self._artist_row` (installed on them in
        `__init__`) for a press-and-drag gesture, rather than overriding
        their own mouse handlers - see the class docstring for why those
        two are the drag handles and the chevron banners aren't."""
        if obj is self.cover or obj is self._artist_row:
            etype = event.type()
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_start_pos = event.position().toPoint()
            elif etype == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
                if self._drag_start_pos is not None and self._slot_number is not None:
                    moved = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
                    if moved >= QApplication.startDragDistance():
                        self._drag_start_pos = None
                        self._start_drag()
            elif etype in (QEvent.MouseButtonRelease, QEvent.Leave):
                self._drag_start_pos = None
        return super().eventFilter(obj, event)

    def _start_drag(self) -> None:
        """Picks the card up - only ever called once `_slot_number` is
        known to be set (the `eventFilter` guard above), so an unused
        page-filler strip can never start one. `QDrag.exec()` runs its own
        nested event loop until the mouse is released somewhere, which is
        also what lets `JukeboxView`'s page-arrow hover timer keep firing
        (and its own `refresh()` keep repainting the grid underneath the
        still-active drag) while this call is on the stack."""
        mime = QMimeData()
        mime.setData(SLOT_MIME_TYPE, str(self._slot_number).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(self.mapFromGlobal(QCursor.pos()))
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: D102 - Qt override
        source_slot = _decode_slot(event.mimeData())
        if (
            self._slot_number is not None
            and source_slot is not None
            and source_slot != self._slot_number
        ):
            event.acceptProposedAction()
            self._set_drop_highlight(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: D102 - Qt override
        if _decode_slot(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._set_drop_highlight(False)

    def dropEvent(self, event) -> None:  # noqa: D102 - Qt override
        self._set_drop_highlight(False)
        source_slot = _decode_slot(event.mimeData())
        if source_slot is None or self._slot_number is None or source_slot == self._slot_number:
            event.ignore()
            return
        event.acceptProposedAction()
        self.reorderRequested.emit(source_slot, self._slot_number)

    def _set_drop_highlight(self, on: bool) -> None:
        """Toggles the `dropTarget` dynamic property `ui/theme.py`'s
        `QFrame#JukeboxStrip[dropTarget="true"]` rule reads -
        unpolish/polish is what makes Qt's style engine actually re-read a
        dynamic property after it's already painted the widget once."""
        self.card.setProperty("dropTarget", on)
        self.card.style().unpolish(self.card)
        self.card.style().polish(self.card)
