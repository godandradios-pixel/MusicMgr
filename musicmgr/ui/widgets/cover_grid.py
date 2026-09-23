"""Tappable cover-art grid, shared by the Albums and Artists browsers.

Two pieces, stacked top to bottom: a sort chip row and the grid itself. The
grid is generic over what a tile represents - `GridTile.key` is a release id
for albums and an artist id for artists - so both browsers get the same
scrolling and sorting behaviour for free.

The A-Z jump bar used to live here too, one per grid; it's now a single bar
shared by all four Library presentations, owned by `LibraryView` and shown
directly under the search box (see that module's docstring). `jump_letters()`
and `scroll_to_letter()` are the seam - `LibraryView` calls them to ask "what
letters make sense to show right now" and "jump to this one", the same
questions the old per-grid bar used to answer for itself. `JumpBar` (the
widget class) stays here since `LibraryView` still needs one instance of it.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, replace
from typing import Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScroller,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ..theme import COLORS
from .common import ChipButton, cover_pixmap

ROLE_TILE = Qt.UserRole + 2

TILE_ART = 190
TILE_CAPTION = 62
TILE_PAD = 10

SHAPE_SQUARE = "square"
SHAPE_CIRCLE = "circle"

#: below this, sort chips and the jump bar are hidden as needless chrome
MIN_TILES_FOR_SORTING = 5


@dataclass
class GridTile:
    """One cell. `count`/`extra` back the numeric sorts and the caption.

    `kind="video"` marks a tile that represents a matching video woven into
    Albums/Artists search results (2026-09-06) rather than the grid's own
    native content (a release or an artist) - see `CoverGrid._rebuild()`
    (hidden unless the search box actually matches one) and `_on_clicked()`
    (routed to `videoActivated` instead of `tileActivated`, since `key` is a
    video id there, not a release/artist id). `initials_source` overrides
    what `cover_pixmap` uses to generate a placeholder when there's no
    `cover_path` - a video tile sets it to the artist's name so a cover-less
    video gets an artist-shaped placeholder ("DV" for Dana Voss) rather than
    one built from its own title, which produced meaningless glyphs
    ("18 And Life (Official Music Video)" -> "1A") the one time this
    project tried titling a placeholder off a video's own name (see
    architecture.md's "Videos" section, 2026-08-30).

    `kind="video_group"` is a second, later addition (2026-09-06): when a
    search matches more than one of the same artist's videos, `_rebuild()`
    collapses all of them into a single stand-in - one search hit on the
    artist's *name* is what qualifies every video of theirs at once (a
    video's own title is never enough by itself here - see `_rebuild()`'s
    matching loop and `set_filter_text`'s docstring), and an artist with
    nine videos was otherwise flooding the grid with nine near-identical
    tiles for that one name match - every video's only stand-in cover is
    that same artist's portrait (see `list_videos_for_search`), so two or
    more of them look identical apart from a caption. A *lone* matching
    video is not that problem and stays its own ordinary `kind="video"`
    tile, directly tappable to play (see `_rebuild()`). This is the
    *fallback* shape of
    the multi-video collapse, used only when the artist has no tile of
    their own among the results to fold the note into instead (no
    releases in the library, or - for Albums - more than one matching
    release with no single unambiguous one to pick); the common case (an
    artist search matching their own already-shown tile, e.g. James's
    original "Journey" report) instead just appends "· ▶ N videos" onto
    that existing tile's caption and never creates one of these at all -
    see `_rebuild()`'s `host_indices` handling, added the same day once
    collapsing to a same-artist *second tile* turned out to still read as
    two results for one artist. `key` is meaningless here (arbitrarily one
    of the collapsed videos' ids - never read); `_on_clicked()` routes it
    to `videoGroupActivated` with `subtitle` (the artist name) instead,
    since there's no single video to play."""

    key: int
    title: str
    subtitle: str = ""
    year: Optional[int] = None
    cover_path: Optional[str] = None
    count: int = 0
    extra: int = 0
    #: lower-cased string the alphabetical sort and jump bar work from
    sort_key: str = ""
    kind: str = "item"
    initials_source: Optional[str] = None


@dataclass(frozen=True)
class SortOption:
    label: str
    key: str
    #: which field the A-Z bar indexes when this sort is active; None hides it
    alpha_field: Optional[str] = None


ALBUM_SORTS = (
    SortOption("Title", "title", "sort_key"),
    SortOption("Artist", "subtitle", "subtitle"),
    SortOption("Year", "year"),
    SortOption("Recently added", "added"),
)

ARTIST_SORTS = (
    SortOption("Name", "title", "sort_key"),
    SortOption("Albums", "count"),
    SortOption("Tracks", "extra"),
    SortOption("Recently added", "added"),
)


class CoverDelegate(QStyledItemDelegate):
    """Rounded (or circular) artwork with a two-line caption underneath."""

    def __init__(self, shape: str = SHAPE_SQUARE, art_size: int = TILE_ART, parent=None) -> None:
        super().__init__(parent)
        self.shape = shape
        self.art_size = art_size

    def sizeHint(self, option, index) -> QSize:
        return QSize(
            self.art_size + TILE_PAD * 2, self.art_size + TILE_CAPTION + TILE_PAD * 2
        )

    def paint(self, painter: QPainter, option, index) -> None:
        tile: Optional[GridTile] = index.data(ROLE_TILE)
        if tile is None:
            return
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected or hovered:
            painter.setBrush(
                QColor(COLORS["surface_hi"] if selected else COLORS["surface_alt"])
            )
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 12, 12)

        art_x = rect.left() + TILE_PAD
        art_y = rect.top() + TILE_PAD
        size = self.art_size
        # circular tiles (artist portraits) fill edge-to-edge via a crop;
        # square tiles (albums/releases) show the whole cover, letterboxed,
        # since cropping a non-square cover would cut off real artwork
        pix = cover_pixmap(
            tile.cover_path,
            size,
            tile.initials_source or tile.title or tile.subtitle,
            crop=self.shape == SHAPE_CIRCLE,
        )

        clip = QPainterPath()
        if self.shape == SHAPE_CIRCLE:
            clip.addEllipse(art_x, art_y, size, size)
        else:
            clip.addRoundedRect(art_x, art_y, size, size, 10, 10)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(art_x, art_y, size, size, pix)
        painter.restore()

        if selected:
            # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for
            # this whole cleanup) - selection outline, walnut brown now
            # instead of red-orange.
            painter.setPen(QColor(COLORS["jukebox_key_hi"]))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(clip)

        centred = self.shape == SHAPE_CIRCLE
        align = (Qt.AlignHCenter if centred else Qt.AlignLeft) | Qt.AlignVCenter
        text_x, text_w = art_x, size
        text_y = art_y + size + 8

        font = painter.font()
        font.setPixelSize(TOUCH["font_base"])
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        metrics = QFontMetrics(font)
        painter.drawText(
            text_x, text_y, text_w, 22, align,
            metrics.elidedText(tile.title, Qt.ElideRight, text_w),
        )

        # -3, not -2 (2026-09-23): the second caption line is the one that
        # elides first, and since the 115px shrink both Library grids use
        # (see LibraryView._build_album_grid) it was losing real words -
        # "5 releases · 96 tr…" in Artists. A point off the subtitle buys
        # ~8% more characters everywhere without touching the tile size or
        # the bold title line above, which stays at font_base. The Artists
        # caption also dropped its trailing "tracks" in the same pass (see
        # LibraryView._load_artists).
        font.setPixelSize(TOUCH["font_base"] - 3)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_dim"]))
        metrics = QFontMetrics(font)
        caption = tile.subtitle or ""
        if tile.year:
            caption = f"{tile.year} · {caption}" if caption else str(tile.year)
        painter.drawText(
            text_x, text_y + 21, text_w, 20, align,
            metrics.elidedText(caption, Qt.ElideRight, text_w),
        )
        painter.restore()


class JumpBar(QWidget):
    """Horizontal #-A-Z strip.

    27 letters cannot each be a 44px touch target without running off the
    screen, so instead of relying on precise taps the whole strip is a scrub
    surface: press anywhere and slide your thumb, and the grid follows. Tapping
    a single letter still works, it is just no longer the only way in.
    """

    letterPicked = Signal(str)  # scroll target - snaps to the nearest enabled letter
    letterTyped = Signal(str)  # the literal letter tapped, for spelling into search

    LETTERS = ["#"] + list(string.ascii_uppercase)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # same touch sizing and look as the Tracks document's own horizontal
        # A-Z strip (album_tracks.py:LetterBar) - one visual language for
        # "a row of letters" across the app, not two
        self.setFixedHeight(TOUCH["letter_bar_height"])
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._buttons: dict[str, QPushButton] = {}
        self._available: set[str] = set(self.LETTERS)
        self._last_emitted: Optional[str] = None
        for letter in self.LETTERS:
            btn = QPushButton(letter)
            btn.setObjectName("LetterKey")
            btn.setFlat(True)
            btn.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            btn.setFocusPolicy(Qt.NoFocus)
            layout.addWidget(btn, 1)
            self._buttons[letter] = btn

    def set_available(self, letters: set[str]) -> None:
        """Which letters `_letter_at`'s scroll-snap is allowed to land on.

        This used to also grey out the rest via QPushButton's disabled
        look, but every letter is always tappable - it always types into
        the search box (`letterTyped`) whether or not it currently has
        anything to scroll to (`letterPicked`) - so a disabled look told
        the user the opposite of the truth: dimming "S" while spelling
        "RUSH" read as "you can't press this", when tapping it worked
        exactly the same as any other letter. All keys now render
        identically; `_available` still drives `_letter_at`'s snap, just
        nothing about how the letters are drawn."""
        self._available = set(letters)

    def _index_at(self, x: int) -> int:
        idx = int(len(self.LETTERS) * max(0, min(x, self.width() - 1)) / self.width())
        return max(0, min(idx, len(self.LETTERS) - 1))

    def _raw_letter_at(self, x: int) -> Optional[str]:
        """The literal letter under this x position, ignoring which letters
        are currently enabled. Unlike `_letter_at`'s nearest-available snap
        (built for smooth scroll-scrubbing - see its own docstring), spelling
        a word into the search box has to type exactly the letter tapped, even
        one with nothing to jump to right now: e.g. typing "RU" already
        narrows the Artists grid down to nothing starting with "S", but "S" is
        still the next real letter of "RUSH" and has to land in the box."""
        if self.width() <= 0:
            return None
        return self.LETTERS[self._index_at(x)]

    def _letter_at(self, x: int) -> Optional[str]:
        if self.width() <= 0:
            return None
        idx = self._index_at(x)
        letter = self.LETTERS[idx]
        if letter in self._available:
            return letter
        for offset in range(1, len(self.LETTERS)):
            for candidate in (idx - offset, idx + offset):
                if 0 <= candidate < len(self.LETTERS):
                    nearby = self.LETTERS[candidate]
                    if nearby in self._available:
                        return nearby
        return None

    def _handle(self, x: int) -> None:
        letter = self._letter_at(x)
        if letter is not None and letter != self._last_emitted:
            self._last_emitted = letter
            self.letterPicked.emit(letter)

    def mousePressEvent(self, event) -> None:
        self._last_emitted = None
        x = int(event.position().x())
        self._handle(x)
        # typing fires once per press, never while dragging - a scrub across
        # the strip is for scrolling, and re-firing this on every letter the
        # thumb slides over while scrolling would spell garbage into the
        # search box instead of the one letter that was actually tapped.
        raw = self._raw_letter_at(x)
        if raw is not None:
            self.letterTyped.emit(raw)

    def mouseMoveEvent(self, event) -> None:
        self._handle(int(event.position().x()))

    def mouseReleaseEvent(self, event) -> None:
        self._last_emitted = None


class CoverGrid(QWidget):
    """Sort chips + cover grid.

    horizontal=True swaps the wrapping, page-filling grid for a single row
    that scrolls left-to-right instead - for a spot where the tiles are one
    section among several on a page (an artist's releases, say) rather than
    the whole browsable library, so wrapping into more rows and pushing
    everything below it down the page is the wrong shape. The sort chips
    still work there, since re-sorting still changes what's under your thumb
    as you scroll sideways; `jump_letters()` (see module docstring) is never
    consulted for a horizontal grid - nothing calls it there.

    A horizontal row pages with `‹`/`›` buttons in its own header row rather
    than a native scrollbar (2026-09-06, James: "instead of space for a
    scroll bar, can you put the < > ... like shown in the picture" -
    referencing a reference screenshot of an artist's full discography with
    paging arrows in place of a scrollbar). The row still scrolls by drag
    too (`QScroller`, unchanged) - the buttons are an additional, more
    discoverable way to move a full page at a time, not a replacement for
    the gesture.

    show_sort_picker=False removes the chip row entirely and pins the grid on
    `default_sort` (a `SortOption.key`) for good - for a caller that has
    decided its grid only ever needs the one order and offering others would
    just be chrome (see the Library view's Albums/Artists grids, 2026-09-05).
    """

    tileActivated = Signal(int)  # GridTile.key - a release or artist id
    videoActivated = Signal(int)  # a video-tile GridTile.key - a video id
    #: a collapsed "N videos" tile (kind="video_group") was tapped - carries
    #: the artist name (GridTile.subtitle), not a video id, since it stands
    #: in for several videos at once. See GridTile's docstring.
    videoGroupActivated = Signal(str)
    #: fires whenever tiles, the active sort, or the filter text change -
    #: LibraryView listens so it can refresh the shared A-Z bar's available
    #: letters and visibility
    updated = Signal()

    def __init__(
        self,
        sorts: Sequence[SortOption] = ALBUM_SORTS,
        shape: str = SHAPE_SQUARE,
        noun: str = "album",
        horizontal: bool = False,
        art_size: int = TILE_ART,
        default_sort: Optional[str] = None,
        show_sort_picker: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tiles: list[GridTile] = []
        self._ordered_cache: list[GridTile] = []
        self._sorts = tuple(sorts)
        if default_sort is not None:
            self._sort = next(s for s in self._sorts if s.key == default_sort)
        else:
            self._sort = self._sorts[0]
        self._noun = noun
        self._horizontal = horizontal
        self._art_size = art_size
        self._filter_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # show_sort_picker=False permanently fixes the grid on `default_sort`
        # (or sorts[0]) with no chip row at all - for a caller that has
        # decided a single sort is always the right one and doesn't want the
        # chrome of offering others. _sort_widgets stays empty in that case,
        # which is fine: _rebuild()'s visibility loop below is just a no-op.
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._sort_group: Optional[QButtonGroup] = None
        self._sort_widgets: list[QWidget] = []
        if show_sort_picker:
            sort_label = QLabel("Sort")
            sort_label.setObjectName("Dim")
            bar.addWidget(sort_label)
            self._sort_group = QButtonGroup(self)
            self._sort_group.setExclusive(True)
            for idx, option in enumerate(self._sorts):
                chip = ChipButton(option.label)
                chip.setProperty("sort_index", idx)
                self._sort_group.addButton(chip)
                bar.addWidget(chip)
                if option is self._sort:
                    chip.setChecked(True)
            self._sort_group.buttonClicked.connect(self._on_sort_changed)
            self._sort_widgets = [sort_label] + list(self._sort_group.buttons())
        bar.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Dim")
        bar.addWidget(self.count_label)
        # Paging arrows replace the native horizontal scrollbar for a
        # horizontal row (see the class docstring for the report this
        # answers) - built here, in the header row alongside the count
        # label, rather than overlaid on the tiles themselves. Wired up
        # once `self.view` exists, below.
        self.prev_btn: Optional[QPushButton] = None
        self.next_btn: Optional[QPushButton] = None
        if horizontal:
            self.prev_btn = QPushButton("‹")
            self.prev_btn.setObjectName("RowPageArrow")
            self.prev_btn.setCursor(Qt.PointingHandCursor)
            self.prev_btn.clicked.connect(lambda: self._page(-1))
            self.next_btn = QPushButton("›")
            self.next_btn.setObjectName("RowPageArrow")
            self.next_btn.setCursor(Qt.PointingHandCursor)
            self.next_btn.clicked.connect(lambda: self._page(1))
            bar.addWidget(self.prev_btn)
            bar.addWidget(self.next_btn)
        root.addLayout(bar)

        body = QHBoxLayout()
        body.setSpacing(6)

        self.view = QListWidget()
        self.view.setViewMode(QListView.IconMode)
        self.view.setMovement(QListView.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(6)
        self.view.setWordWrap(False)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setMouseTracking(True)
        self.view.setItemDelegate(CoverDelegate(shape, art_size=art_size, parent=self))
        QScroller.grabGesture(self.view.viewport(), QScroller.LeftMouseButtonGesture)
        self.view.itemClicked.connect(self._on_clicked)

        # +12 is breathing room for the delegate's own sizeHint (art_size +
        # TILE_CAPTION + TILE_PAD * 2). A horizontal row's own scrollbar is
        # always off (see below) - the ‹/› buttons page it instead - so
        # there's no scrollbar height to budget for here the way an earlier
        # version of this fix had to (James's "Rush" layout report,
        # 2026-09-06: "don't truncate the album artwork and scroll bar in
        # the middle" - the scrollbar itself was clipping tile artwork by
        # eating into this fixed height; removing the scrollbar removed
        # that problem at the root rather than just budgeting around it).
        row_height = art_size + TILE_CAPTION + TILE_PAD * 2 + 12
        if horizontal:
            self.view.setFlow(QListView.LeftToRight)
            self.view.setWrapping(False)
            self.view.setResizeMode(QListView.Fixed)
            self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.view.setFixedHeight(row_height)
            hbar = self.view.horizontalScrollBar()
            hbar.valueChanged.connect(self._update_page_buttons)
            hbar.rangeChanged.connect(self._update_page_buttons)
        else:
            self.view.setResizeMode(QListView.Adjust)
            self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body.addWidget(self.view, 0 if horizontal else 1)
        root.addLayout(body, 0 if horizontal else 1)

    # -- horizontal-row paging --------------------------------------------

    def _page(self, direction: int) -> None:
        """Scroll a horizontal row roughly one page (one screenful of
        tiles) in `direction` (-1 for ‹, +1 for ›). `QScrollBar.setValue`
        clamps to the real range on its own, so paging off either end just
        stops at the edge rather than needing its own bounds check here."""
        hbar = self.view.horizontalScrollBar()
        step = hbar.pageStep() or self.view.viewport().width()
        hbar.setValue(hbar.value() + direction * step)

    def _update_page_buttons(self, *_args) -> None:
        """Keeps ‹/› enabled only while there's somewhere left to page to -
        connected to the row's scrollbar `valueChanged`/`rangeChanged`
        (paging, dragging, or the tile list changing all move it), and
        called once after every `_rebuild()` so a freshly (re)loaded row
        starts with the right enabled state rather than whatever the
        buttons happened to show before. A no-op for a non-horizontal grid,
        where `prev_btn`/`next_btn` are never created."""
        if self.prev_btn is None or self.next_btn is None:
            return
        hbar = self.view.horizontalScrollBar()
        self.prev_btn.setEnabled(hbar.value() > hbar.minimum())
        self.next_btn.setEnabled(hbar.value() < hbar.maximum())

    # -- data ---------------------------------------------------------------

    def set_tiles(self, tiles: Sequence[GridTile]) -> None:
        self._tiles = list(tiles)
        self._rebuild()

    def set_filter_text(self, text: str) -> None:
        """Live-narrow the grid to tiles whose *own* title matches `text` -
        an album's own title in the Albums grid, an artist's own name in the
        Artists grid (James, 2026-09-06: "on the artist page ... only search
        on Artists", "when on Albums, only ... on albums" - searching Albums
        used to also match the release's artist-name subtitle, and Artists'
        subtitle happened to be a "N releases · M tracks" count that could
        stray-match a typed number). A video woven into either grid's search
        results matches the same way a native tile does here - by *name*,
        not its own title: only the video's artist name has to match (see
        `_rebuild()`). James, 2026-09-06: searching "rush" in Artists was
        surfacing a "Rush, Rush" video by Paula Abdul purely because the
        video's title contained "rush" - Albums/Artists browse by name, so
        a video's own title is never a reason for it to show up there.

        Matching, not re-sorting - the sort chip still decides order, this
        just drops tiles that don't qualify. An empty string shows everything
        again.
        """
        text = text.strip().lower()
        if text == self._filter_text:
            return
        self._filter_text = text
        self._rebuild()

    @property
    def sort_key(self) -> str:
        return self._sort.key

    def _on_sort_changed(self, button) -> None:
        self._sort = self._sorts[button.property("sort_index")]
        self._rebuild()

    def _sorted(self, tiles: Optional[Sequence[GridTile]] = None) -> list[GridTile]:
        """Sorts `tiles` (default: `self._tiles`) by whichever `SortOption`
        is active. Taking an explicit list, rather than always reading
        `self._tiles`, lets `_rebuild()` re-apply the same order to the
        post-filter/post-collapse tile list too (see its "video_group"
        handling, 2026-09-06) without a second, parallel sort
        implementation."""
        source = self._tiles if tiles is None else tiles
        key = self._sort.key
        if key == "subtitle":
            return sorted(
                source,
                key=lambda t: (t.subtitle.lower(), t.year or 0, t.title.lower()),
            )
        if key == "year":
            return sorted(source, key=lambda t: (-(t.year or 0), t.title.lower()))
        if key == "count":
            return sorted(source, key=lambda t: (-t.count, t.sort_key))
        if key == "extra":
            return sorted(source, key=lambda t: (-t.extra, t.sort_key))
        if key == "added":
            return list(source)  # the query already returns newest first
        return sorted(source, key=lambda t: t.sort_key)

    def _rebuild(self) -> None:
        ordered = self._sorted()
        video_match_groups = 0  # for the count label below - see its comment
        individual_video_count = 0
        if self._filter_text:
            needle = self._filter_text
            kept: list[GridTile] = []
            #: every matching video, gathered per artist so more than one
            #: from the same artist can be collapsed into a single stand-in
            #: below rather than shown one tile each. Originally this only
            #: covered a match on the *artist's* name rather than the
            #: video's own title (2026-09-06: searching an artist with nine
            #: videos was flooding the grid with nine near-identical tiles
            #: for one name match); briefly widened the same day to group
            #: title matches too, after James asked about a search result
            #: full of apparent "duplications" that turned out to be several
            #: of one artist's videos, each a distinct title but all showing
            #: that same artist's portrait as their only stand-in "cover"
            #: (a video has no artwork of its own - see
            #: `list_videos_for_search`). Narrowed back to name-only the
            #: same day, once that widening let an unrelated video attach a
            #: wrong artist's tile to the results: searching "rush" in
            #: Artists surfaced "Rush, Rush" by Paula Abdul, who doesn't
            #: match "rush" as an artist at all - Albums/Artists browse by
            #: name, so a video's *own* title is never a reason for it (or
            #: its artist) to show up there; see the loop below and
            #: `set_filter_text`'s docstring. Keyed by `subtitle` (the
            #: artist name), preserving first-seen order.
            video_groups: dict[str, list[GridTile]] = {}
            for t in ordered:
                if t.kind == "video":
                    # only the video's *artist* has to match here - its own
                    # title never qualifies it (see the comment above)
                    if needle not in (t.subtitle or "").lower():
                        continue
                    video_groups.setdefault(t.subtitle, []).append(t)
                else:
                    # a native tile (a release in Albums, an artist in
                    # Artists) matches only its own title - not its
                    # subtitle (the release's artist-name caption, or the
                    # artist's "N releases · M tracks" count)
                    if needle not in t.title.lower():
                        continue
                    kept.append(t)
            for artist, videos in video_groups.items():
                if len(videos) == 1:
                    kept.append(videos[0])
                    individual_video_count += 1
                    continue
                video_match_groups += 1
                note = f"▶ {len(videos)} videos"
                # if this artist already has exactly one tile of their own
                # among the results - their own tile in Artists (`title ==
                # artist`), or their one matching release in Albums
                # (`subtitle == artist`) - fold the note into its existing
                # caption instead of showing a second circle/card for the
                # same artist (2026-09-06, second round: James - "why are
                # we still showing 2 Artist circles, I thought they would
                # be collapsed into 1" - the first cut of this fix always
                # added a separate stand-in tile, even when the real one
                # was already sitting right there). Two or more candidates
                # (an artist with several matching albums, say) has no
                # single unambiguous home to fold into, so that - and an
                # artist with no tile of their own at all, e.g. one with no
                # releases in the library - both still fall back to the
                # stand-in tile below.
                host_indices = [
                    i for i, h in enumerate(kept)
                    if h.kind == "item" and (h.title == artist or h.subtitle == artist)
                ]
                if len(host_indices) == 1:
                    idx = host_indices[0]
                    host = kept[idx]
                    kept[idx] = replace(
                        host,
                        subtitle=f"{host.subtitle} · {note}" if host.subtitle else note,
                    )
                    continue
                kept.append(GridTile(
                    key=videos[0].key,  # never read - see GridTile's docstring
                    title=note,
                    subtitle=artist,
                    cover_path=videos[0].cover_path,
                    sort_key=artist.lower(),
                    kind="video_group",
                    initials_source=artist,
                ))
            # re-sort: the video groups above were appended out of order,
            # and even the kept tiles need re-ranking now that some of
            # their neighbors have been replaced by a single stand-in
            ordered = self._sorted(kept)
        else:
            # video tiles (and video_group tiles, though none exist here -
            # they're only ever produced by the filtering above) only ever
            # appear as search matches, never in the default unfiltered
            # browse - see GridTile's docstring
            ordered = [t for t in ordered if t.kind != "video"]
        self.view.setUpdatesEnabled(False)
        self.view.clear()
        size = self._art_size
        self.view.setGridSize(
            QSize(size + TILE_PAD * 2 + 6, size + TILE_CAPTION + TILE_PAD * 2 + 6)
        )
        for tile in ordered:
            item = QListWidgetItem()
            item.setData(ROLE_TILE, tile)
            item.setSizeHint(QSize(size + TILE_PAD * 2, size + TILE_CAPTION + TILE_PAD * 2))
            self.view.addItem(item)
        self.view.setUpdatesEnabled(True)
        # native items/tiles counted separately from videos woven into the
        # search results, so a handful of collapsed "N videos" matches don't
        # inflate "N artists"/"N albums" with things that aren't one
        # (2026-09-06). individual_video_count covers a lone match that
        # stayed its own tile; video_match_groups covers a folded match
        # (2+ videos from one artist, 2026-09-06 third round - see the
        # video_groups comment above) as a single count regardless of how
        # many videos it folded together, since it no longer shows as more
        # than one tile.
        native_count = sum(1 for t in ordered if t.kind not in ("video", "video_group"))
        video_count = individual_video_count + video_match_groups
        plural = "" if native_count == 1 else "s"
        label = f"{native_count} {self._noun}{plural}"
        if video_count:
            label += f" · {video_count} video match{'' if video_count == 1 else 'es'}"
        self.count_label.setText(label)

        # a handful of tiles fits on one row; sort chips would be pure chrome
        # there (an artist page with two albums, say) - jump_letters() below
        # applies the same threshold for the shared A-Z bar
        worth_sorting = len(ordered) > MIN_TILES_FOR_SORTING
        for widget in self._sort_widgets:
            widget.setVisible(worth_sorting)

        self._ordered_cache = ordered
        # a fresh tile list changes the scrollbar's range (fewer/more tiles
        # to page through), so the buttons' enabled state needs re-checking
        # even though nothing dragged/paged anything - a no-op for a
        # non-horizontal grid (see _update_page_buttons's own guard).
        self._update_page_buttons()
        self.updated.emit()

    def jump_letters(self) -> Optional[set[str]]:
        """Letters worth showing on the shared A-Z bar right now, or None if
        jumping makes no sense for the active sort (e.g. Albums sorted by
        Year has no alphabetical order to jump within) or there's too little
        on screen to be worth it - the same two conditions that used to hide
        the old per-grid jump bar outright."""
        if self._sort.alpha_field is None or len(self._ordered_cache) <= MIN_TILES_FOR_SORTING:
            return None
        return {self._letter_of(t) for t in self._ordered_cache}

    def _letter_of(self, tile: GridTile) -> str:
        field = self._sort.alpha_field or "sort_key"
        source = getattr(tile, field, "") or ""
        first = str(source).strip()[:1].upper()
        return first if first in string.ascii_uppercase else "#"

    def scroll_to_letter(self, letter: str) -> None:
        for row in range(self.view.count()):
            tile = self.view.item(row).data(ROLE_TILE)
            if tile is None:
                continue
            if self._letter_of(tile) == letter:
                item = self.view.item(row)
                self.view.scrollToItem(item, QAbstractItemView.PositionAtTop)
                self.view.setCurrentItem(item)
                return

    def _on_clicked(self, item: QListWidgetItem) -> None:
        tile = item.data(ROLE_TILE)
        if tile is None:
            return
        if tile.kind == "video":
            self.videoActivated.emit(tile.key)
        elif tile.kind == "video_group":
            self.videoGroupActivated.emit(tile.subtitle)
        else:
            self.tileActivated.emit(tile.key)

    def count(self) -> int:
        return self.view.count()

    def tile_at(self, row: int) -> Optional[GridTile]:
        item = self.view.item(row)
        return item.data(ROLE_TILE) if item else None
