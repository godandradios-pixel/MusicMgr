"""Gatefold coverflow: a folding, scroll-driven alternative to CoverGrid's
horizontal release row on the artist page.

Each release is drawn as a two-leaf "gatefold" card, hinged down the
middle. The centered/focused release lies flat, showing its full cover;
releases further from center fold shut toward a thin edge-on sliver, the
way a closed digipak or record sleeve would sit in a rack. The fold is a
pure 2D illusion - Qt's `QPainter`/`QTransform` only do affine (non-
perspective) transforms, so there is no real 3D rotation here. What sells
the fold is `painter.scale(cos(angle), 1.0)` applied around the hinge:
squeezing each half's horizontal extent by cos(angle) is exactly the
foreshortening a true rotateY would produce for a flat card facing the
camera, without needing perspective math at all.

Only released as a scroll-driven browser for one artist's releases (tens,
not thousands) - `_MAX_VISIBLE_DELTA` skips both the transform math and
the `cover_pixmap()` call for anything far enough off-screen not to be
visible, so a large discography doesn't pay to lay out release 40 while
looking at release 2.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import COLORS
from .common import cover_pixmap, dim_label
from .cover_grid import GridTile

#: panels this far from the focused one (in whole-release units) are
#: skipped entirely - not laid out, not painted, no cover_pixmap() call.
#: Kept small on purpose (2026-09-15, James: "too much wasted space...a
#: bunch of albums showing a bunch of lines is not useful") - a coverflow
#: is for one release in focus plus a few readable neighbors, not for
#: scanning a whole discography at once (that's what the plain grid this
#: replaced was for). Past this point it's just a closed sliver anyway
#: (see _FOLD_RATE), so there's nothing gained by laying out more of them.
_MAX_VISIBLE_DELTA = 4

#: how far apart panel centers sit, as a fraction of one panel's width.
#: Tuned so even the least-folded neighbor's near edge just clears the
#: fully-open focused panel's edge instead of overlapping it - see the
#: geometry note on _FOLD_RATE below. Slowing the fold down (below) means
#: neighbors stay wider for longer, so this had to grow to match, or a
#: half-open neighbor would bleed into the focused cover.
_SPACING_RATIO = 0.92

#: foldT (0 = flat/open, 1= fully closed sliver) reaches 1.0 a bit past
#: three releases away from focus, not two - slowed down from the first
#: cut so 2-3 releases on each side of focus stay recognizable instead of
#: closing down to an unreadable line almost immediately. Only the last
#: release or two before _MAX_VISIBLE_DELTA read as a bare sliver now - a
#: hint that more exist, not the majority of what's on screen.
_FOLD_RATE = 0.3
_FOLD_MAX_DEG = 87.0


class _CoverflowCanvas(QWidget):
    """Owns the actual fold math, animation, and mouse/wheel/keyboard input.

    `GatefoldCoverflow` (below) is the public widget - this class is its
    painted interior, split out the same way CoverGrid keeps its sort-chip
    bar separate from `self.view`.
    """

    tileOpened = Signal(int)  # GridTile.key of the already-focused tile, tapped again
    focusChanged = Signal(int)  # index into self._tiles nearest the current scroll position

    def __init__(self, panel_size: int, parent=None) -> None:
        super().__init__(parent)
        self._panel_size = panel_size
        self._spacing = panel_size * _SPACING_RATIO
        self._tiles: list[GridTile] = []
        self._scroll_pos = 0.0
        self._target_pos = 0.0
        self._last_focus = -1
        self._press_pos: Optional[QPointF] = None
        self._press_target = 0.0
        self._dragged = False

        self.setFixedHeight(panel_size + 34)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

        # debounced snap-to-nearest after a wheel gesture goes idle - a
        # direct jump_to() would make every wheel notch feel like it's
        # fighting the previous one's in-flight animation
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(140)
        self._snap_timer.timeout.connect(self._snap)

    # -- data -----------------------------------------------------------

    def set_tiles(self, tiles: Sequence[GridTile]) -> None:
        # matches CoverGrid's own default_sort="title" behaviour for this
        # row (see cover_grid.py's _sorted - "title" sorts by sort_key,
        # not the SQL query's chronological order) so swapping widgets
        # doesn't silently change what order releases appear in
        self._tiles = sorted(tiles, key=lambda t: t.sort_key)
        # the focused panel always draws dead-center in the canvas (see
        # _panel_geometry) - starting on release 0 left the entire left
        # half of the row empty since nothing exists before it. Starting in
        # the middle instead fills the row from the first paint.
        start = float(len(self._tiles) // 2) if self._tiles else 0.0
        self._scroll_pos = start
        self._target_pos = start
        self._last_focus = -1
        self._emit_focus(force=True)
        self.update()

    def step(self, delta: int) -> None:
        self.jump_to(round(self._target_pos) + delta)

    def jump_to(self, index: int) -> None:
        if not self._tiles:
            return
        self._target_pos = float(max(0, min(len(self._tiles) - 1, index)))
        self._timer.start()

    # -- animation --------------------------------------------------------

    def _tick(self) -> None:
        diff = self._target_pos - self._scroll_pos
        if abs(diff) < 0.001:
            self._scroll_pos = self._target_pos
            self._timer.stop()
        else:
            self._scroll_pos += diff * 0.3
        self._emit_focus()
        self.update()

    def _snap(self) -> None:
        if self._tiles:
            self.jump_to(round(self._target_pos))

    def _emit_focus(self, force: bool = False) -> None:
        if not self._tiles:
            nearest = -1
        else:
            nearest = max(0, min(len(self._tiles) - 1, int(round(self._scroll_pos))))
        if nearest != self._last_focus or force:
            self._last_focus = nearest
            self.focusChanged.emit(nearest)

    # -- layout math, shared by paintEvent and hit-testing -----------------

    def _panel_geometry(self, index: int):
        """(center_x, center_y, scale, cos_theta, fold_t) for panel
        `index` at the current scroll position, or None once it's past
        _MAX_VISIBLE_DELTA and not worth laying out at all."""
        d = index - self._scroll_pos
        ad = abs(d)
        if ad > _MAX_VISIBLE_DELTA:
            return None
        half = self._panel_size / 2
        scale = max(0.35, 1 - min(ad, 3) * 0.12)
        fold_t = max(0.0, min(1.0, ad * _FOLD_RATE))
        cos_t = max(0.02, math.cos(math.radians(fold_t * _FOLD_MAX_DEG)))
        cx = self.width() / 2 + d * self._spacing
        cy = self.height() / 2 - 4
        return cx, cy, scale, cos_t, fold_t

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        if self._tiles:
            half = self._panel_size / 2
            visible = [
                (i, self._panel_geometry(i))
                for i in range(len(self._tiles))
            ]
            visible = [(i, g) for i, g in visible if g is not None]
            # farthest first, so the focused panel always paints last and
            # sits on top of anything a half-open neighbor might overlap
            visible.sort(key=lambda pair: -abs(pair[0] - self._scroll_pos))

            for index, (cx, cy, scale, cos_t, fold_t) in visible:
                self._paint_panel(painter, self._tiles[index], cx, cy, scale, cos_t, fold_t,
                                   half, index)

        painter.end()

        if self.hasFocus():
            outline = QPainter(self)
            outline.setRenderHint(QPainter.Antialiasing)
            outline.setPen(QColor(COLORS["jukebox_key_hi"]))
            outline.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 6, 6)
            outline.end()

    def _paint_panel(self, painter, tile, cx, cy, scale, cos_t, fold_t, half, index) -> None:
        pix = cover_pixmap(tile.cover_path, self._panel_size, tile.title, crop=False)
        op = max(0.45, 1 - min(abs(index - self._scroll_pos) / 5, 1) * 0.45)

        painter.save()
        painter.setOpacity(op)
        painter.translate(cx, cy)
        painter.scale(scale, scale)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 70))
        painter.drawRoundedRect(
            QRectF(-half - 3, -half + 4, self._panel_size + 6, self._panel_size + 6), 10, 10
        )

        # clip to the panel's *unfolded* footprint before either leaf's own
        # squeeze-scale is applied, so a folding leaf shrinks inward from
        # this fixed rounded frame rather than the frame itself shrinking
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(-half, -half, self._panel_size, self._panel_size), 9, 9)
        painter.setClipPath(clip)

        full_w, full_h = float(pix.width()), float(pix.height())
        for side in (-1, 1):
            painter.save()
            painter.scale(cos_t, 1.0)  # the fold, folding toward x=0 (the hinge)
            target = QRectF(0 if side > 0 else -half, -half, half, self._panel_size)
            source = QRectF(full_w / 2 if side > 0 else 0, 0, full_w / 2, full_h)
            painter.drawPixmap(target, pix, source)
            if fold_t > 0:
                painter.fillRect(target, QColor(0, 0, 0, int(fold_t * 130)))
            painter.restore()

        if fold_t > 0.02:
            painter.setPen(QColor(0, 0, 0, int(min(fold_t * 2.2, 1) * 140)))
            painter.drawLine(QPointF(0, -half * 0.94), QPointF(0, half * 0.94))

        painter.restore()

    # -- interaction ----------------------------------------------------

    def _hit_test(self, pos: QPointF) -> Optional[int]:
        half = self._panel_size / 2
        candidates = [
            (i, self._panel_geometry(i)) for i in range(len(self._tiles))
        ]
        candidates = [(i, g) for i, g in candidates if g is not None]
        # closest-to-focus first, matching paint order's topmost-on-top
        candidates.sort(key=lambda pair: abs(pair[0] - self._scroll_pos))
        for index, (cx, cy, scale, cos_t, _fold_t) in candidates:
            # hit-test against the panel's actual folded (squeezed) width,
            # not its full unfolded footprint - a thin sliver should only
            # catch a click that actually lands on the sliver
            hw, hh = half * cos_t * scale, half * scale
            if abs(pos.x() - cx) <= hw and abs(pos.y() - cy) <= hh:
                return index
        return None

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self._tiles:
            return
        delta = event.angleDelta()
        raw = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
        self._target_pos = max(
            0.0, min(len(self._tiles) - 1, self._target_pos - raw / 120 * 0.4)
        )
        self._timer.start()
        self._snap_timer.start()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._tiles:
            return
        self.setFocus(Qt.MouseFocusReason)
        self._press_pos = event.position()
        self._press_target = self._target_pos
        self._dragged = False
        self.setCursor(Qt.ClosedHandCursor)
        self._snap_timer.stop()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is None or not self._tiles:
            return
        dx = event.position().x() - self._press_pos.x()
        if abs(dx) > 4:
            self._dragged = True
        self._target_pos = max(
            0.0, min(len(self._tiles) - 1, self._press_target - dx / self._spacing)
        )
        self._timer.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is None:
            return
        self.setCursor(Qt.OpenHandCursor)
        if self._dragged:
            self.jump_to(round(self._target_pos))
        else:
            hit = self._hit_test(event.position())
            if hit is not None:
                # a tap on the already-focused cover opens it; a tap on
                # any other cover just brings it to focus - browse, then
                # confirm, rather than one tap doing both at once
                if hit == int(round(self._scroll_pos)):
                    self.tileOpened.emit(self._tiles[hit].key)
                else:
                    self.jump_to(hit)
        self._press_pos = None
        self._dragged = False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Right:
            self.step(1)
        elif event.key() == Qt.Key_Left:
            self.step(-1)
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()


class GatefoldCoverflow(QWidget):
    """Drop-in alternative to CoverGrid(horizontal=True) for one artist's
    releases - same `set_tiles(list[GridTile])` and `tileActivated(int)`
    surface, so callers swap the class name and constructor kwargs only.

    Own layout is the canvas flanked by large prev/next arrow buttons, plus
    a caption underneath naming the currently-open release (2026-09-15,
    James: "move the title of the album below the album" - it used to sit
    in a header bar above the strip, disconnected from the cover it
    actually named; centered under the canvas it lines up with the open
    panel, which the fold math always draws dead-center - see
    _CoverflowCanvas._panel_geometry).

    2026-09-16 follow-up (James: the "N releases ‹ ›" pager's arrows, up
    in the artist page's breadcrumb row, were "too small" - screenshot
    showed the 32px `RowPageArrow` pair easy to miss next to the count
    text): `prev_btn`/`next_btn` moved from that small shared style into
    this widget's own row, sized up to the new 56px `CoverflowNavArrow`
    style (matching `PlayerBar`'s own `#Transport` touch targets) and
    docked directly against the canvas's left/right edges instead of
    living somewhere else on the page. `count_label` is still built here
    and still deliberately left off `root` - it's plain status text, not a
    control, so the caller (ArtistDetailPanel) still places it wherever it
    wants (today: the breadcrumb row, where the pager used to sit).
    """

    tileActivated = Signal(int)  # GridTile.key - a release id

    def __init__(self, noun: str = "release", panel_size: int = 150, parent=None) -> None:
        super().__init__(parent)
        self._noun = noun
        self._tiles: list[GridTile] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.canvas = _CoverflowCanvas(panel_size, parent=self)
        self.canvas.tileOpened.connect(self.tileActivated.emit)
        self.canvas.focusChanged.connect(self._on_focus_changed)

        # large, easy-to-hit prev/next targets flanking the artwork itself
        # (see the class docstring's 2026-09-16 follow-up) rather than the
        # small shared #RowPageArrow style used for other pagers in the
        # app (cover_grid.py's own horizontal rows, jukebox.py's page
        # header) - this pair is dedicated to this widget, so restyling
        # them doesn't affect those other pagers.
        self.prev_btn = QPushButton("‹")
        self.prev_btn.setObjectName("CoverflowNavArrow")
        self.prev_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn = QPushButton("›")
        self.next_btn.setObjectName("CoverflowNavArrow")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.prev_btn.clicked.connect(lambda: self.canvas.step(-1))
        self.next_btn.clicked.connect(lambda: self.canvas.step(1))

        canvas_row = QHBoxLayout()
        canvas_row.setSpacing(12)
        canvas_row.addWidget(self.prev_btn, 0, Qt.AlignVCenter)
        canvas_row.addWidget(self.canvas, 1)
        canvas_row.addWidget(self.next_btn, 0, Qt.AlignVCenter)
        root.addLayout(canvas_row, 0)

        self.focus_label = dim_label("")
        self.focus_label.setAlignment(Qt.AlignHCenter)
        root.addWidget(self.focus_label, 0, Qt.AlignHCenter)

        self.count_label = QLabel("")
        self.count_label.setObjectName("Dim")

        self._on_focus_changed(-1)

    def set_tiles(self, tiles: Sequence[GridTile]) -> None:
        self._tiles = sorted(tiles, key=lambda t: t.sort_key)
        self.canvas.set_tiles(tiles)
        n = len(self._tiles)
        plural = "" if n == 1 else "s"
        self.count_label.setText(f"{n} {self._noun}{plural}")

    def _on_focus_changed(self, index: int) -> None:
        if 0 <= index < len(self._tiles):
            tile = self._tiles[index]
            caption = f"{tile.title} · {tile.year}" if tile.year else tile.title
            self.focus_label.setText(caption)
        else:
            self.focus_label.setText("")
        self.prev_btn.setEnabled(index > 0)
        self.next_btn.setEnabled(0 <= index < len(self._tiles) - 1)
