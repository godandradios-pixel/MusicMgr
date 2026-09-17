"""Reusable touch-first widgets."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QThread, Signal, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScroller,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...services import artist_bio_downloader as bio_dl
from ...services import lyrics_downloader as lyrics_dl
from ...services import lastfm_popularity as popularity_dl
from ..theme import COLORS

ROLE_PAYLOAD = Qt.UserRole + 1
ROLE_NODE = Qt.UserRole + 4

#: the round transport button's "playing" glyph, shared by player_bar.py and
#: video_panel.py's fullscreen controls (both build a TransportMain button
#: with the same play/pause states). ⏸ (U+23F8) sits in the "Miscellaneous
#: Technical" block Windows hands off to the color emoji font, showing up as
#: a small blue square instead of a flat glyph regardless of the app's own
#: font stack - two plain ASCII "|" (U+007C) bars avoid that fallback the
#: same way the single "‖" (U+2016) glyph used to, but being two separate
#: characters means the gap between them is a real, adjustable space rather
#: than baked into one fixed glyph. James: "make that ... more space between
#: them" (2026-09-05) - a bare single space read as too tight to clearly be
#: two bars at this button's 24px size, so it's two.
PAUSE_GLYPH = "|  |"

#: shared with track_details_table.py's RatingDelegate (Title Details' Rating
#: column) - defined here rather than there so this module doesn't have to
#: import from a widget one layer up its own dependency chain (cover_grid.py
#: already imports from here). A rating is 0-5; 0 means unrated.
STAR_COUNT = 5
STAR_FULL = "★"
STAR_EMPTY = "☆"

#: used by this module's own JukeboxToggle below (Now Playing's jukebox
#: indicator) - a filled vs. outline circle, continuing this codebase's
#: plain-monochrome-Unicode icon convention rather than emoji, and echoing
#: `services/jukebox.py`'s own "physical record behind the glass" metaphor
#: - a filled record is on the board, an empty ring is off it (2026-09-07
#: follow-up). Also used by track_details_table.py's `JukeboxDelegate`
#: (Title Details' own Jukebox column) from 2026-09-07 until that column's
#: 2026-09-13 removal - see that file's module docstring.
JUKEBOX_ON = "●"  # ●  BLACK CIRCLE
JUKEBOX_OFF = "○"  # ○  WHITE CIRCLE


# --------------------------------------------------------------------------
# small building blocks
# --------------------------------------------------------------------------


def title_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Title")
    return lbl


def dim_label(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Dim")
    return lbl


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    return line


class TouchButton(QPushButton):
    def __init__(self, text: str = "", primary: bool = False, parent=None) -> None:
        super().__init__(text, parent)
        if primary:
            self.setObjectName("Primary")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(TOUCH["button_height"] - 12)


class ChipButton(QPushButton):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Chip")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)


class _CrumbLink(QLabel):
    """One tappable ancestor crumb in a `Breadcrumb` trail - a `QLabel`
    rather than a `QPushButton`, unlike an earlier version of this widget.

    2026-09-07 follow-up - James: "can you make it so my artist breadcrumb
    doesn't shrink." A flat, borderless `QPushButton` still picks up the
    native platform style's own button-chrome font metrics for whatever the
    stylesheet doesn't fully override (a well-known Qt gotcha: partially
    styling a native widget falls back to native sizing for the rest), which
    was rendering "Artists" visibly smaller than the plain-QLabel current
    crumb right next to it even though both declared the exact same
    font-size - guessing at more QSS overrides to fight that chrome would
    only be papering over it, not removing it. A `QLabel` has no button
    chrome to fight in the first place, so it renders at the same size as
    `#Crumb`/`#CrumbCurrent` (this same trail's
    separator and current-page text) by construction rather than by tuning -
    click handling is done by hand (`mousePressEvent`), the same "clickable
    label" idiom `StarRating` already uses elsewhere in this file.
    """

    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("CrumbLink")
        self.setCursor(Qt.PointingHandCursor)
        # QLabel doesn't repaint for a QSS `:hover` rule out of the box -
        # opting into hover events is what makes #CrumbLink:hover fire, the
        # same way a QPushButton's built-in hover state already does.
        self.setAttribute(Qt.WA_Hover, True)

    def mousePressEvent(self, event) -> None:  # noqa: D102 - Qt override
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Breadcrumb(QWidget):
    """A ›-separated navigation trail: every crumb but the last is a tappable
    link back to that level, and the last is the current page - inert and
    bold, the way a breadcrumb reads everywhere else. Used in place of a lone
    "‹ Back" pill (see `release_panel.py`/`artist_panel.py`, which each own
    one instance) so a release opened from an artist page can jump straight
    back to the artist grid *or* to that one artist, not just one step back.
    """

    crumbActivated = Signal(int)  # index into the most recent set_path() list

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._row.addStretch(1)
        self._crumbs: list[str] = []
        self.setVisible(False)

    @property
    def path(self) -> list[str]:
        return list(self._crumbs)

    def set_path(self, labels: Sequence[str]) -> None:
        """Rebuild the trail. `labels` runs root-first, current page last -
        e.g. `["Artists", "Dana Voss", "Paper Cathedral"]`. An empty sequence
        hides the whole bar (nothing to show a trail for)."""
        self._crumbs = list(labels)
        # clear everything except the trailing stretch, which always stays
        # at the end so newly inserted crumbs land before it
        while self._row.count() > 1:
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # taking it out of the layout only stops layout management -
                # the widget stays a visible child at its old position until
                # deleteLater()'s deferred delete actually runs, which a
                # reused panel (a release opened via one route, then another)
                # would otherwise briefly show as overlapping stale crumbs.
                # hide() first so it's gone from the screen immediately.
                widget.hide()
                widget.deleteLater()
        for index, label in enumerate(self._crumbs):
            end = self._row.count() - 1
            if index > 0:
                sep = QLabel("›")
                sep.setObjectName("Crumb")
                self._row.insertWidget(end, sep)
                end += 1
            if index == len(self._crumbs) - 1:
                current = QLabel(label)
                current.setObjectName("CrumbCurrent")
                self._row.insertWidget(end, current)
            else:
                link = _CrumbLink(label)
                link.clicked.connect(lambda i=index: self.crumbActivated.emit(i))
                self._row.insertWidget(end, link)
        self.setVisible(bool(self._crumbs))


def placeholder_pixmap(size: int, seed_text: str = "") -> QPixmap:
    """Deterministic striped tile with initials, for releases with no cover art.

    The hue is derived from the text, so the same album always gets the same
    colour and the grid stays recognisable at a glance.
    """
    pix = QPixmap(size, size)
    hue = (sum(ord(c) for c in seed_text) % 360) if seed_text else 210
    pix.fill(QColor.fromHsv(hue, 90, 70))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(255, 255, 255, 40), 2))
    step = max(12, size // 7)
    for i in range(-size, size * 2, step):
        painter.drawLine(i, 0, i + size, size)
    initials = "".join(w[0] for w in seed_text.split()[:2]).upper() or "?"
    font = QFont()
    font.setPixelSize(int(size * 0.34))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255, 200))
    painter.drawText(pix.rect(), Qt.AlignCenter, initials)
    painter.end()
    return pix


#: (path, size, seed) -> QPixmap. Grids re-render constantly while scrolling;
#: decoding the same JPEG each time is the difference between smooth and not.
_ART_CACHE: dict[tuple, QPixmap] = {}
_ART_CACHE_LIMIT = 600


def cover_pixmap(
    path: Optional[str], size: int, seed_text: str = "", crop: bool = True
) -> QPixmap:
    """Load cover art, falling back to a placeholder.

    crop=True (default) fills the tile edge-to-edge via a centered square
    crop - right for a circular artist portrait, where the tile itself
    defines the shape. crop=False instead fits the whole image inside the
    tile unmodified, letterboxed on a neutral background - right for
    album/release art, where cropping a cover that isn't already square (a
    wide box-set spread, a tall single sleeve) would throw away part of the
    actual artwork.
    """
    key = (path or "", size, seed_text if not path else "", crop)
    cached = _ART_CACHE.get(key)
    if cached is not None:
        return cached
    pix: Optional[QPixmap] = None
    if path and Path(path).exists():
        loaded = QPixmap(path)
        if not loaded.isNull():
            if crop:
                scaled = loaded.scaled(
                    size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
                x = max(0, (scaled.width() - size) // 2)
                y = max(0, (scaled.height() - size) // 2)
                pix = scaled.copy(x, y, size, size)
            else:
                scaled = loaded.scaled(
                    size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                pix = QPixmap(size, size)
                pix.fill(QColor(COLORS["surface_alt"]))
                painter = QPainter(pix)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                x = (size - scaled.width()) // 2
                y = (size - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
                painter.end()
    if pix is None:
        pix = placeholder_pixmap(size, seed_text)
    if len(_ART_CACHE) > _ART_CACHE_LIMIT:
        _ART_CACHE.clear()
    _ART_CACHE[key] = pix
    return pix


def circular_pixmap(source: QPixmap) -> QPixmap:
    """Crop a square pixmap to a circle, for artist portraits."""
    size = source.width()
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, source)
    painter.end()
    return out


class CoverArt(QLabel):
    """Square (or circular) artwork with a generated fallback when there is no
    embedded image."""

    def __init__(
        self, size: int = TOUCH["art_size"], circular: bool = False, parent=None
    ) -> None:
        super().__init__(parent)
        self._size = size
        self._circular = circular
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setScaledContents(False)
        self.set_source(None, "")

    def set_source(self, path: Optional[str], seed_text: str = "") -> None:
        pix = cover_pixmap(path, self._size, seed_text)
        self.setPixmap(circular_pixmap(pix) if self._circular else pix)


class StarRating(QWidget):
    """A row of tappable stars, mirroring Title Details' Rating column
    (`track_details_table.py`'s `RatingDelegate`) but as a standalone widget
    rather than an item-view delegate - built for Now Playing's rating stars
    (James: "having the same stars under the track title on Now Playing
    would let you rate what you're hearing in the moment", 2026-09-06),
    which sit under a track title rather than inside a table cell.

    Tapping the star matching the *current* rating clears it to 0, the same
    toggle-off behavior the Rating column uses - the only way to unrate a
    track from either place.
    """

    ratingChanged = Signal(int)  # new rating, 0-5; 0 = cleared

    def __init__(self, star_size: int = 30, parent=None) -> None:
        super().__init__(parent)
        self._rating = 0
        self._labels: list[QLabel] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for position in range(1, STAR_COUNT + 1):
            lbl = QLabel(STAR_EMPTY)
            font = lbl.font()
            font.setPixelSize(star_size)
            lbl.setFont(font)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.mousePressEvent = lambda _e, p=position: self._on_star_clicked(p)
            row.addWidget(lbl)
            self._labels.append(lbl)
        row.addStretch(1)
        self._refresh()

    def _on_star_clicked(self, position: int) -> None:
        new_rating = 0 if position == self._rating else position
        self.set_rating(new_rating)
        self.ratingChanged.emit(new_rating)

    def set_rating(self, rating: int) -> None:
        """Update the displayed stars without emitting `ratingChanged` - for
        loading a track's existing rating from the database, where the value
        is already known rather than being a change the person just made."""
        self._rating = max(0, min(STAR_COUNT, rating))
        self._refresh()

    def rating(self) -> int:
        return self._rating

    def _refresh(self) -> None:
        # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for this
        # whole cleanup) - filled stars, walnut brown now instead of
        # red-orange.
        for position, lbl in enumerate(self._labels, start=1):
            filled = position <= self._rating
            lbl.setText(STAR_FULL if filled else STAR_EMPTY)
            lbl.setStyleSheet(
                f"color: {COLORS['jukebox_key_hi'] if filled else COLORS['text_dim']};"
            )


class JukeboxToggle(QWidget):
    """A tappable jukebox on/off indicator, the same "clickable label" idiom
    `StarRating` uses just above but for a single toggle glyph rather than a
    1-5 scale. James, 2026-09-07 follow-up: "I also want to be able to
    click to add a jukebox entry from the track detail page" - originally
    Now Playing's counterpart to Title Details' own Jukebox column
    (`track_details_table.py`'s `JukeboxDelegate`), reached from wherever a
    track is actually playing instead of from the table. That column was
    removed 2026-09-13 (see track_details_table.py's module docstring);
    this toggle is unaffected and is now, along with the Jukebox page's own
    "+ Add to jukebox" picker, one of only two remaining ways to add a
    track to the jukebox board.

    Unlike `StarRating`, which can compute and display its new value the
    moment a star is tapped, this can't flip its own glyph on tap - turning
    a track *onto* the board can fail server-side (no resolvable album
    artist to file a slot under), a branch this widget has no way to
    predict on its own. So a tap only emits `toggled`; the glyph only
    changes once the caller reports what actually happened via `set_on()` -
    the same request/confirm split `TrackDetailsTable.
    jukeboxToggleRequested`/`set_jukebox_state` already use for the same
    reason."""

    toggled = Signal()

    def __init__(self, glyph_size: int = 22, parent=None) -> None:
        super().__init__(parent)
        self._on = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._glyph = QLabel(JUKEBOX_OFF)
        font = self._glyph.font()
        font.setPixelSize(glyph_size)
        self._glyph.setFont(font)
        self._glyph.setCursor(Qt.PointingHandCursor)
        self._glyph.mousePressEvent = lambda _e: self.toggled.emit()
        self._label = QLabel("Jukebox")
        self._label.setObjectName("Dim")
        self._label.setCursor(Qt.PointingHandCursor)
        self._label.mousePressEvent = lambda _e: self.toggled.emit()
        row.addWidget(self._glyph)
        row.addWidget(self._label)
        self._refresh()

    def set_on(self, on: bool) -> None:
        """Update the displayed glyph without emitting `toggled` - for
        loading a track's existing membership from the database, or for
        applying the confirmed result of a toggle this widget itself just
        requested (see the class docstring)."""
        self._on = on
        self._refresh()

    def is_on(self) -> bool:
        return self._on

    def _refresh(self) -> None:
        self._glyph.setText(JUKEBOX_ON if self._on else JUKEBOX_OFF)
        # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for this
        # whole cleanup) - "on" state, walnut brown now instead of
        # red-orange (and a better fit for a jukebox-themed toggle besides).
        color = COLORS["jukebox_key_hi"] if self._on else COLORS["text_dim"]
        self._glyph.setStyleSheet(f"color: {color};")
        self._label.setStyleSheet(f"color: {color};")


# --------------------------------------------------------------------------
# two-line touch list
# --------------------------------------------------------------------------


class RowDelegate(QStyledItemDelegate):
    """Renders a payload dict as: [lead] Primary / Secondary ......... [trail]"""

    def __init__(self, row_height: int, parent=None) -> None:
        super().__init__(parent)
        self.row_height = row_height

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.row_height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        payload = index.data(ROLE_PAYLOAD) or {}
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # 2026-09-08 follow-up - James: "have the progress bar appear on the
        # line with the folder" (Settings' watched-folders list, while a
        # scan is running - see SettingsView._on_progress/TouchList.
        # update_row). A payload carrying "progress" is a live progress
        # readout, not a selectable/hoverable row in the normal sense, so
        # this paints instead of the ordinary selection background rather
        # than under it - the two never made sense stacked together.
        progress = payload.get("progress")
        if progress is not None:
            self._paint_progress_fill(painter, rect, progress)
        else:
            self._paint_selection_background(painter, option, rect)
        fallback_text = str(index.data(Qt.DisplayRole) or "")
        self._paint_payload(painter, rect, payload, fallback_text=fallback_text)
        painter.restore()

    def _paint_selection_background(self, painter: QPainter, option: QStyleOptionViewItem, rect) -> None:
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected or hovered:
            painter.setBrush(QColor(COLORS["surface_hi"] if selected else COLORS["surface_alt"]))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect.adjusted(4, 2, -4, -2), 8, 8)

    def _paint_progress_fill(self, painter: QPainter, rect, progress: float) -> None:
        """An actual progress bar drawn as this row's own background - an
        empty track the full row width, then a filled portion scaled to
        `progress` (0.0-1.0), in the same walnut-brown ProgressWarm/
        PrimaryWarm pallet the rest of Settings uses (see theme.py)."""
        progress = max(0.0, min(1.0, progress))
        track_rect = rect.adjusted(4, 2, -4, -2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS["surface_hi"]))
        painter.drawRoundedRect(track_rect, 8, 8)
        if progress <= 0:
            return
        fill_rect = QRect(
            track_rect.left(), track_rect.top(),
            max(1, int(track_rect.width() * progress)), track_rect.height(),
        )
        painter.setBrush(QColor(COLORS["jukebox_key"]))
        painter.drawRoundedRect(fill_rect, 8, 8)

    def _paint_payload(self, painter: QPainter, rect, payload: dict, fallback_text: str = "") -> None:
        """Draw one payload's [lead] Primary / Secondary ... [trail] content
        into `rect`. Split out from `paint()` so TwoColumnRowDelegate can
        reuse it twice per row - once per half - for the release page's
        two-column track list (2026-09-06)."""
        pad = 16
        x = rect.left() + pad
        right = rect.right() - pad

        # leading badge (rank / track number)
        lead = payload.get("lead")
        if lead:
            painter.setPen(QColor(payload.get("lead_color", COLORS["text_dim"])))
            font = painter.font()
            font.setPixelSize(TOUCH["font_base"] + 3)
            font.setBold(bool(payload.get("lead_bold")))
            painter.setFont(font)
            lead_w = 52
            painter.drawText(
                x, rect.top(), lead_w, rect.height(),
                Qt.AlignVCenter | Qt.AlignLeft, str(lead),
            )
            x += lead_w

        # thumbnail
        thumb = payload.get("thumb_pixmap")
        if thumb is not None and not thumb.isNull():
            side = rect.height() - 16
            painter.drawPixmap(x, rect.top() + 8, side, side, thumb)
            x += side + 14

        # trailing text (duration, play count) - or, when `trail2` is set,
        # a two-line trailing label mirroring the primary/secondary layout
        # on the left (added for Charts' file-location column, 2026-09-17 -
        # James: "list the file location in that space" - the matched
        # file's own name on top, its full path underneath in smaller,
        # dimmer text). Capped and elided from the left (rather than the
        # unbounded exact-fit the single-line case always used) so a long
        # absolute path can't blow out this column and crush the primary/
        # secondary text down to its 60px floor - short trailing text (a
        # duration, a play count) never reaches that cap, so this is a
        # strict superset of the old behaviour for every existing caller.
        trail = payload.get("trail")
        trail2 = payload.get("trail2")
        trail_w = 0
        if trail or trail2:
            font = painter.font()
            font.setPixelSize(TOUCH["font_base"])
            font.setBold(False)
            metrics = QFontMetrics(font)
            max_trail_w = 260
            natural_w = max(
                metrics.horizontalAdvance(str(trail)) if trail else 0,
                metrics.horizontalAdvance(str(trail2)) if trail2 else 0,
            )
            trail_w = max(70, min(max_trail_w, natural_w + 16))
            painter.setFont(font)
            if trail2:
                painter.setPen(QColor(payload.get("trail_color", COLORS["text_dim"])))
                painter.drawText(
                    right - trail_w, rect.top() + 12, trail_w, rect.height() // 2,
                    Qt.AlignVCenter | Qt.AlignRight,
                    metrics.elidedText(str(trail or ""), Qt.ElideLeft, trail_w),
                )
                font2 = painter.font()
                font2.setPixelSize(TOUCH["font_base"] - 2)
                painter.setFont(font2)
                painter.setPen(QColor(payload.get("trail2_color", COLORS["text_dim"])))
                metrics2 = QFontMetrics(font2)
                painter.drawText(
                    right - trail_w, rect.center().y(), trail_w, rect.height() // 2 - 6,
                    Qt.AlignVCenter | Qt.AlignRight,
                    metrics2.elidedText(str(trail2), Qt.ElideLeft, trail_w),
                )
            elif trail:
                painter.setPen(QColor(payload.get("trail_color", COLORS["text_dim"])))
                painter.drawText(
                    right - trail_w, rect.top(), trail_w, rect.height(),
                    Qt.AlignVCenter | Qt.AlignRight,
                    metrics.elidedText(str(trail), Qt.ElideLeft, trail_w),
                )

        text_w = max(60, right - trail_w - x - 12)
        primary = str(payload.get("primary", fallback_text))
        secondary = payload.get("secondary")

        font = painter.font()
        font.setPixelSize(TOUCH["font_base"] + 2)
        font.setBold(bool(payload.get("bold", False)))
        painter.setFont(font)
        painter.setPen(QColor(payload.get("color", COLORS["text"])))
        metrics = QFontMetrics(font)
        elided = metrics.elidedText(primary, Qt.ElideRight, text_w)
        if secondary:
            painter.drawText(x, rect.top() + 12, text_w, rect.height() // 2,
                             Qt.AlignVCenter | Qt.AlignLeft, elided)
            font.setPixelSize(TOUCH["font_base"] - 1)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(COLORS["text_dim"]))
            metrics2 = QFontMetrics(font)
            painter.drawText(
                x, rect.center().y(), text_w, rect.height() // 2 - 6,
                Qt.AlignVCenter | Qt.AlignLeft,
                metrics2.elidedText(str(secondary), Qt.ElideRight, text_w),
            )
        else:
            painter.drawText(x, rect.top(), text_w, rect.height(),
                             Qt.AlignVCenter | Qt.AlignLeft, elided)


class TwoColumnRowDelegate(RowDelegate):
    """Paints two payloads side by side in one row - a left track and an
    optional right track - reusing RowDelegate's own `_paint_payload()` for
    each half. Added 2026-09-06, James: "Can we have 2 columns of track
    listings when you select an album release. Like this image" (a 15-track
    tracklist split 8/7 into left/right columns, aligned row-by-row). Each
    QListWidgetItem holds a `(left_payload, right_payload_or_None)` tuple in
    ROLE_PAYLOAD instead of a single payload dict; the trailing row of an
    odd-length list simply leaves its right half blank."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        pair = index.data(ROLE_PAYLOAD) or (None, None)
        left, right = pair if isinstance(pair, tuple) else (pair, None)
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_selection_background(painter, option, rect)

        half_w = rect.width() // 2
        left_rect = QRect(rect.left(), rect.top(), half_w, rect.height())
        right_rect = QRect(rect.left() + half_w, rect.top(), rect.width() - half_w, rect.height())

        if left:
            self._paint_payload(painter, left_rect, left)
        if right:
            self._paint_payload(painter, right_rect, right)
        painter.restore()

    def half_at(self, rect, x: int) -> str:
        """Which half of `rect` an x-coordinate (viewport space) falls in -
        used by TouchList.mousePressEvent to route a click to the left or
        right track."""
        return "left" if x < rect.left() + rect.width() // 2 else "right"


class TouchList(QListWidget):
    """List with kinetic (drag-to-scroll) behaviour and fat rows."""

    itemActivatedPayload = Signal(object)

    def __init__(self, row_height: Optional[int] = None, parent=None, two_column: bool = False) -> None:
        super().__init__(parent)
        self._two_column = two_column
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        delegate_cls = TwoColumnRowDelegate if two_column else RowDelegate
        self.setItemDelegate(delegate_cls(row_height or TOUCH["row_height"], self))
        QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
        self.itemClicked.connect(self._emit_payload)

    def _emit_payload(self, item: QListWidgetItem) -> None:
        if self._two_column:
            # two-column rows hold a (left, right) pair - routed by
            # mousePressEvent instead, which knows which half was clicked.
            return
        self.itemActivatedPayload.emit(item.data(ROLE_PAYLOAD))

    def mousePressEvent(self, event) -> None:
        if self._two_column:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None:
                pair = item.data(ROLE_PAYLOAD)
                left, right = pair if isinstance(pair, tuple) else (pair, None)
                half = self.itemDelegate().half_at(self.visualItemRect(item), pos.x())
                chosen = left if half == "left" else right
                if chosen is not None:
                    self.itemActivatedPayload.emit(chosen)
        super().mousePressEvent(event)

    def add_row(self, payload: dict) -> QListWidgetItem:
        item = QListWidgetItem(str(payload.get("primary", "")))
        item.setData(ROLE_PAYLOAD, payload)
        self.addItem(item)
        return item

    def add_row_pair(self, left: dict, right: Optional[dict]) -> QListWidgetItem:
        item = QListWidgetItem(str(left.get("primary", "")))
        item.setData(ROLE_PAYLOAD, (left, right))
        self.addItem(item)
        return item

    def set_rows(self, payloads: Sequence[dict]) -> None:
        self.setUpdatesEnabled(False)
        self.clear()
        if self._two_column:
            half = (len(payloads) + 1) // 2
            left_col = payloads[:half]
            right_col = payloads[half:]
            for i, left in enumerate(left_col):
                right = right_col[i] if i < len(right_col) else None
                self.add_row_pair(left, right)
        else:
            for payload in payloads:
                self.add_row(payload)
        self.setUpdatesEnabled(True)

    def payload_at(self, row: int) -> Optional[dict]:
        item = self.item(row)
        return item.data(ROLE_PAYLOAD) if item else None

    def current_payload(self) -> Optional[dict]:
        item = self.currentItem()
        return item.data(ROLE_PAYLOAD) if item else None

    def update_row(self, match_value, match_field: str = "path", **changes) -> bool:
        """Patch fields into the payload of whichever row's `match_field`
        equals `match_value`, in place - no clear()/rebuild.

        2026-09-08 follow-up - James: "have the progress bar appear on the
        line with the folder" (Settings' watched-folders list). Built for
        SettingsView's per-file scan progress, which fires every few files
        per folder (see services/scanner.py's `idx % 5 == 0` throttling) -
        set_rows() rebuilding the whole list that often would be wasteful
        and would drop the list's own transient state (scroll position,
        selection) on every tick. Returns whether a matching row was found,
        so a stale/renamed folder failing to match is at least detectable
        rather than silently doing nothing."""
        for row in range(self.count()):
            item = self.item(row)
            payload = item.data(ROLE_PAYLOAD) or {}
            if payload.get(match_field) == match_value:
                payload = dict(payload)
                payload.update(changes)
                item.setData(ROLE_PAYLOAD, payload)
                self.viewport().update(self.visualItemRect(item))
                return True
        return False


class TouchTree(QTreeWidget):
    """Two-column tree (name, a small right-aligned meta column) for nested
    things like playlist folders.

    A folder's ▸/▾ is drawn into its own label text rather than relying on
    Qt's native branch arrow, which is a handful of pixels - hopeless as a
    touch target. The *whole row* toggles expansion on tap instead, same idea
    as `JumpBar`: make the target bigger than the glyph, not the glyph bigger.
    Tapping a leaf (anything not of type "folder") just reports itself.

    Callers build the QTreeWidgetItem hierarchy themselves via `add_folder`/
    `add_leaf` (a tree's shape is too caller-specific for one generic
    `set_rows`-style API) - this class only adds touch sizing and tap
    semantics on top.
    """

    itemActivatedPayload = Signal(object)  # the tapped node's ROLE_NODE payload

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(TOUCH["tree_indent"])
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.setColumnWidth(1, 56)
        QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
        self.itemClicked.connect(self._on_clicked)
        self.itemExpanded.connect(self._sync_glyph)
        self.itemCollapsed.connect(self._sync_glyph)

    def _on_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        payload = item.data(0, ROLE_NODE) or {}
        if payload.get("type") == "folder":
            item.setExpanded(not item.isExpanded())
        self.itemActivatedPayload.emit(payload)

    def _sync_glyph(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, ROLE_NODE) or {}
        if payload.get("type") == "folder":
            item.setText(0, ("▾ " if item.isExpanded() else "▸ ") + payload.get("name", ""))

    def add_folder(
        self,
        parent: QTreeWidgetItem | "TouchTree",
        name: str,
        node: dict,
        expanded: bool = False,
        meta: str = "",
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [("▾ " if expanded else "▸ ") + name, meta])
        item.setData(0, ROLE_NODE, {**node, "type": "folder", "name": name})
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        item.setExpanded(expanded)
        return item

    def add_leaf(
        self, parent: QTreeWidgetItem | "TouchTree", text: str, node: dict, meta: str = ""
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [text, meta])
        item.setData(0, ROLE_NODE, node)
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        return item

    def payload_of(self, item: Optional[QTreeWidgetItem]) -> Optional[dict]:
        return item.data(0, ROLE_NODE) if item is not None else None

    def current_payload(self) -> Optional[dict]:
        return self.payload_of(self.currentItem())

    def select_node(self, matches: Callable[[dict], bool]) -> bool:
        """Select and reveal the first item whose payload satisfies `matches`."""
        it = QTreeWidgetItemIterator(self)
        while it.value():
            item = it.value()
            payload = self.payload_of(item) or {}
            if matches(payload):
                self.setCurrentItem(item)
                self.scrollToItem(item)
                return True
            it += 1
        return False


class FolderPickerDialog(QDialog):
    """Pick a destination folder (or the top level) for a movable item.

    Works with any folder-shaped row that has `.id`, `.parent_id` and `.name`
    - `PlaylistFolder`, `ChartFolder`, or anything else built the same way.
    The dialog never touches any other attribute, so no adapter is needed to
    reuse it across domains. `exclude_ids` keeps a folder that's being moved
    from being offered as a destination inside itself or one of its own
    subfolders.
    """

    def __init__(
        self,
        parent,
        folders: Sequence[Any],
        current_folder_id: Optional[int],
        exclude_ids: Optional[set[int]] = None,
        title: str = "Move to folder",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(420, 480)
        layout = QVBoxLayout(self)

        self.tree = TouchTree()
        layout.addWidget(self.tree, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        exclude_ids = exclude_ids or set()
        root_item = self.tree.add_leaf(self.tree, "Top level", {"type": "root"})
        by_parent: dict[Optional[int], list[Any]] = {}
        for f in folders:
            if f.id not in exclude_ids:
                by_parent.setdefault(f.parent_id, []).append(f)

        def add_children(parent_item, parent_id) -> None:
            for folder in sorted(by_parent.get(parent_id, []), key=lambda f: f.name.lower()):
                item = self.tree.add_folder(
                    parent_item, folder.name, {"id": folder.id}, expanded=True
                )
                add_children(item, folder.id)

        add_children(root_item, None)
        self.tree.expandAll()
        if current_folder_id is not None:
            self.tree.select_node(
                lambda n: n.get("type") == "folder" and n.get("id") == current_folder_id
            )
        else:
            self.tree.setCurrentItem(root_item)

    def selected_folder_id(self) -> Optional[int]:
        current = self.tree.current_payload() or {}
        return current.get("id") if current.get("type") == "folder" else None


class EmptyState(QWidget):
    """`action_text`/`on_action` are optional - added for the lyrics panel's
    "Download lyrics" button (nothing found locally, offer to go get it),
    and left unused by this widget's two other callers (jukebox.py,
    bio_panel.py), which still only pass headline/detail."""

    def __init__(
        self,
        headline: str,
        detail: str = "",
        action_text: str = "",
        on_action: Optional[Callable[[], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        head = QLabel(headline)
        head.setObjectName("Title")
        head.setAlignment(Qt.AlignCenter)
        layout.addWidget(head)
        self.detail_label: Optional[QLabel] = None
        if detail:
            sub = QLabel(detail)
            sub.setObjectName("Subtitle")
            sub.setAlignment(Qt.AlignCenter)
            sub.setWordWrap(True)
            layout.addWidget(sub)
            self.detail_label = sub
        self.action_button: Optional[TouchButton] = None
        if action_text:
            btn = TouchButton(action_text)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            if on_action:
                btn.clicked.connect(on_action)
            layout.addWidget(btn, 0, Qt.AlignCenter)
            self.action_button = btn


class LyricsDownloadThread(QThread):
    """Runs `services.lyrics_downloader.download_lyrics_for_album` off the
    UI thread - shared by the lyrics panel's per-track button and the
    release page's per-album button (a single track is just an
    album-of-one, so both reuse the same worker and the same
    `AlbumLyricsResult` shape rather than there being two parallel paths).

    Network-bound and rate-limited on purpose (see lyrics_downloader.py),
    so an album's worth of tracks can take several seconds - same reason
    `ScanThread` (ui/views/settings.py) exists for library scans."""

    progress = Signal(int, int, str)
    finished_with = Signal(object)  # AlbumLyricsResult

    def __init__(self, tracks: list, overwrite: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.tracks = tracks
        self.overwrite = overwrite

    def run(self) -> None:  # pragma: no cover - exercised interactively
        result = lyrics_dl.download_lyrics_for_album(
            self.tracks,
            overwrite=self.overwrite,
            progress=lambda done, total, name: self.progress.emit(done, total, name),
        )
        self.finished_with.emit(result)


class BioDownloadThread(QThread):
    """Runs `services.artist_bio_downloader.download_bios_for_artists` off
    the UI thread - shared by the artist page's per-artist "Fetch bio"
    button and Settings' bulk "Download artist profiles…" button (a single
    artist is just a batch of one) - the same shape as LyricsDownloadThread
    just above, for the same reason.

    2026-09-16 follow-up (James: "Add a new menu option called artist
    profile...collect a brief writeup of the artist" - see
    artist_bio_downloader.py's own docstring for the full story and why a
    fetched bio, unlike a downloaded lyric, is a database write rather than
    a standalone sidecar file."""

    progress = Signal(int, int, str)
    finished_with = Signal(object)  # BioDownloadResult

    def __init__(self, artist_ids: list, overwrite: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.artist_ids = artist_ids
        self.overwrite = overwrite

    def run(self) -> None:  # pragma: no cover - exercised interactively
        result = bio_dl.download_bios_for_artists(
            self.artist_ids,
            overwrite=self.overwrite,
            progress=lambda done, total, name: self.progress.emit(done, total, name),
        )
        self.finished_with.emit(result)


class PopularityDownloadThread(QThread):
    """Runs `services.lastfm_popularity.update_popularity_for_artists` off
    the UI thread - shared by the artist page's per-artist "Fetch
    popularity" button and Settings' bulk "Update track popularity from
    Last.fm…" button (a single artist is just a batch of one) - the same
    shape as BioDownloadThread just above, for the same reason.

    2026-09-16 same-day follow-up (James, asked where "Top Tracks" ranking
    came from and offered a choice of authoritative outside sources: "I was
    thinking more like youtube playlist count or some authoritative
    source" → picked Last.fm's artist top-tracks endpoint, after a first
    pass at Spotify's equivalent hit a dead end - see
    lastfm_popularity.py's own docstring for the full story)."""

    progress = Signal(int, int, str)
    finished_with = Signal(object)  # PopularityResult

    def __init__(self, artist_ids: list, parent=None) -> None:
        super().__init__(parent)
        self.artist_ids = artist_ids

    def run(self) -> None:  # pragma: no cover - exercised interactively
        result = popularity_dl.update_popularity_for_artists(
            self.artist_ids,
            progress=lambda done, total, name: self.progress.emit(done, total, name),
        )
        self.finished_with.emit(result)


# --------------------------------------------------------------------------
# on-screen keyboard
# --------------------------------------------------------------------------


class VirtualKeyboard(QFrame):
    """Compact QWERTY for panels with no physical keyboard attached."""

    keyPressedText = Signal(str)
    backspacePressed = Signal()
    clearPressed = Signal()
    closePressed = Signal()

    ROWS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        for row in self.ROWS:
            line = QHBoxLayout()
            line.setSpacing(6)
            line.addStretch(1)
            for ch in row:
                btn = QPushButton(ch)
                btn.setObjectName("KeyCap")
                btn.clicked.connect(lambda _=False, c=ch: self.keyPressedText.emit(c))
                line.addWidget(btn)
            line.addStretch(1)
            outer.addLayout(line)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        space = QPushButton("space")
        space.setObjectName("KeyCap")
        space.setMinimumWidth(260)
        space.clicked.connect(lambda: self.keyPressedText.emit(" "))
        back = QPushButton("⌫")
        back.setObjectName("KeyCap")
        back.clicked.connect(self.backspacePressed.emit)
        clear = QPushButton("clear")
        clear.setObjectName("KeyCap")
        clear.clicked.connect(self.clearPressed.emit)
        close = QPushButton("hide")
        close.setObjectName("KeyCap")
        close.clicked.connect(self.closePressed.emit)
        bottom.addStretch(1)
        for w in (clear, space, back, close):
            bottom.addWidget(w)
        bottom.addStretch(1)
        outer.addLayout(bottom)


class SearchBar(QWidget):
    """Line edit + optional on-screen keyboard toggle."""

    textChanged = Signal(str)
    submitted = Signal(str)

    def __init__(self, placeholder: str = "Search", parent=None, password: bool = False) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        if password:
            # 2026-09-16 follow-up - reused for the Last.fm credentials
            # dialog's API-key field (ui/views/settings.py); a credential
            # typed once during setup is still worth masking on a shared
            # touch panel the way a password field always is.
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.textChanged.connect(self.textChanged.emit)
        self.edit.returnPressed.connect(lambda: self.submitted.emit(self.edit.text()))
        self.kb_button = TouchButton("⌨")
        self.kb_button.setFixedWidth(64)
        self.kb_button.setCheckable(True)
        self.kb_button.setToolTip("On-screen keyboard")
        row.addWidget(self.edit, 1)
        row.addWidget(self.kb_button)
        layout.addLayout(row)

        self.keyboard = VirtualKeyboard()
        self.keyboard.setVisible(False)
        self.keyboard.keyPressedText.connect(self._insert)
        self.keyboard.backspacePressed.connect(self.edit.backspace)
        self.keyboard.clearPressed.connect(self.edit.clear)
        self.keyboard.closePressed.connect(lambda: self.kb_button.setChecked(False))
        self.kb_button.toggled.connect(self.keyboard.setVisible)
        layout.addWidget(self.keyboard)

    def _insert(self, text: str) -> None:
        self.edit.insert(text)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)
