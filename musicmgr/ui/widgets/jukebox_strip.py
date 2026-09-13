"""One numbered title strip on the Jukebox page (ui/views/jukebox.py).

2026-09-07, second follow-up - James sent a reference mockup (styled after
a physical Beatles "Abbey Road" 45 sleeve) and said "let's be consistent
with the size and format of the cards. I would like them to look like this
image." This replaced the strip's original cream-paper/two-row look
(chrome frame, small circular record-label thumbnails per side, red round
key buttons - see git history if that look is ever wanted back) with:

- One large square cover (`_CardCover`) on the left, rounded-corner
  clipped the same way cover_grid.py/album_tracks.py clip their own square
  art (an inline `QPainterPath.addRoundedRect`, not a new shared helper -
  matching the existing convention rather than adding one). Sourced from
  the A side's cover art, falling back to the B side's, since the
  reference shows one piece of sleeve art per physical record regardless
  of which side is loaded - the data model still tracks a cover per track,
  but the card only ever shows one.
- Two pointed "chevron" banners (`_ChevronBanner`, one per side) on the
  right, each a custom-painted button: a solid number/code box ("A7") on
  the left feeding into a wider cream panel carrying the title, pointed at
  the right edge - a shape no amount of QPushButton stylesheet can
  produce, hence the custom paintEvent. The banner itself *is* the tap
  target now (no separate round key button, matching the reference, which
  has none) - tapping a filled banner emits `sideActivated` exactly like
  the old key button did. An empty side shows a disabled, greyed banner
  reading "OPEN" instead of a dimmed placeholder row.
- One artist-name badge on a colored line between the two banners,
  matching the reference's "THE BEATLES" strip (this line, the banner
  border/code-box, and the currently-playing fill were all originally red
  - see the 2026-09-07, sixth follow-up below for why that changed to
  brown).

Two things from the reference were deliberately left out, both by James's
own instruction when asked: the mockup's "Edit" and share-icon overlay
buttons aren't reproduced at all (James: "remove the Edit and share
buttons") - the card carries no direct edit/share affordance. The layout
also stayed inside the existing 3x3 grid (James: "keep 3x3, shrink the new
design to fit") rather than the grid changing shape to fit the reference's
wider proportions - `_CardCover`'s size and the banners' `sizeHint` are
what actually got tuned down to make that fit, not the grid.

One more casualty of the redesign: the old per-side "record drops and
spins up" flourish (a small circular thumbnail spinning briefly on tap)
doesn't carry over. It worked because a *circle* looks identical at any
rotation; the new cover is a rounded square, and a spinning square just
looks broken partway through, so tapping a banner no longer animates
anything on it.

2026-09-07, first follow-up (James: "is there a way I can move these
jukebox cards around?"): moving a card emits `moveRequested
(this_slot_number)` - `JukeboxView` is what actually shows a "move to
slot #" picker and calls `services.jukebox.swap_slots`, the same "widget
only reports the gesture, the view owns the database" split
`sideActivated` already uses.

(An actual drag-and-drop version of reordering shipped first and was
pulled back out the same day: James reported it "didn't work out too
well" - specifically, there was no way to drag a card from page 2 onto a
target back on page 1, since `JukeboxView` only ever builds enough strip
widgets for one page. Picking a target slot *number* instead of a target
*card* sidesteps that - source and destination never need to be visible
at the same time - which is why this replaced dragging rather than
sitting alongside it.)

2026-09-07, fifth follow-up - James: "I would like to right click on the
jukebox card and delete it by removing both songs from that card." The
same right-click menu the fourth follow-up introduced for moving a card
now also carries a "Remove from jukebox…" action below a separator -
`_build_move_menu()` was renamed `_build_context_menu()` since it's no
longer just the move action, and gained a `removeRequested(this_slot_
number)` signal alongside `moveRequested`, same shape. `JukeboxView` owns
the actual confirmation prompt and the `services.jukebox.remove_slot`
call, the same "widget only reports the gesture, the view owns the
database" split every other jukebox-card action already follows. Removing
a card takes both sides off the board regardless of what's loaded on
either one - it does not delete the underlying tracks or their files, and
does not touch either song's star rating.

2026-09-07, sixth follow-up - James, looking at the board: the strip
key badges ("A12"/"B12"), banner borders, and the currently-playing fill
had all been a bright red (`COLORS["jukebox_key"]`/`jukebox_key_hi`/
`jukebox_key_pressed`) since the very first version of this widget, which
read as an alert/warning color against the rest of the app's warm brown/
gold branding (see the MusicMgr logo). Retinted those three theme tokens
from red to a walnut-brown family in `ui/theme.py` - nothing in this file
changed, since every reference to them was already through `COLORS[...]`
rather than a hardcoded hex, exactly the kind of retheme that indirection
is for.

2026-09-07, fourth follow-up - James, looking at the redesigned cards:
"I really don't like all the space that is used by the sorting and how
it sticks up from the top right of the card. Should we consider a right
click on the card with a move option?" The move trigger moved from a
small "⇄" button living in its own header strip above the card (added
in the first follow-up, before the reference-image redesign existed) to
a right-click context menu on the card itself - `contextMenuEvent`
override, one "Move to a different slot…" `QAction`, built by what was
then called `_build_move_menu()` (renamed `_build_context_menu()` once a
second action joined it - see the fifth follow-up above) and left as its
own method precisely so a test can trigger the action directly without
needing to drive a real popup menu event loop. The header row is gone
entirely, so the card is shorter and nothing sits above it anymore -
`moveRequested` still carries the exact same `this_slot_number` payload,
so `JukeboxView`'s wiring didn't need to change at all. The card's tooltip
originally said "Right-click to move"; it now mentions both options (see
above).

2026-09-07, third follow-up - James sent a screenshot of a full page:
cards holding a long artist name ("Bob Seger & The Silver Bullet Band")
had visibly grown wider than their neighbors, and said "I don't want the
cards to resize. They should always be the same size even when there are
not enough cards to fill a page." Two separate causes, one fix:

- `artist_label` was a plain `QLabel` with no width cap, so its sizeHint
  (and therefore its column's width in `JukeboxView`'s `QGridLayout`)
  grew with the artist name's text width - a short name like "ABBA" and a
  long one like Bob Seger's full billing produced two different card
  widths in the same grid.
- A page with fewer than 9 slots (`JukeboxView.refresh()` calls
  `strip.hide()` on the unused ones) left `QGridLayout` with fewer
  occupied rows than usual; Qt's grid doesn't reserve space for a hidden
  widget, so with no minimum row/column size pinned down elsewhere, the
  strips that *were* visible had nothing stopping them from stretching to
  fill the extra room.

Both are the same underlying gap: nothing about this widget's *own* size
was ever pinned down, so it was always at the mercy of whatever its
busiest sibling or its container asked for. `artist_label` now gets a
fixed width (`_ARTIST_LABEL_WIDTH`) with the name manually elided to fit
via `QFontMetrics.elidedText` (a long name now reads "BOB SEGER & THE
SILVER B…" rather than stretching anything) - the same "measure the font,
elide by hand" approach `_ChevronBanner.paintEvent` already uses for the
title, just via `QLabel.setText` instead of a paintEvent, since this one
isn't custom-painted. With every child now reporting a fixed, content-
independent sizeHint, `__init__` finishes by calling
`self.setFixedSize(self.sizeHint())` once - the whole card locks to
that size for good, so a hidden sibling elsewhere on the page, or any
future content the card doesn't currently expect, can no longer stretch
or shrink it either.

2026-09-07, seventh follow-up - James added genre chips to the Jukebox
page ("I would like the jukebox page to have a chip of 5 genres... select
the location and what genre page a track will be organized by",
`services/jukebox.py`/`ui/views/jukebox.py`). The right-click menu's move
action grew a paired genre control in the same dialog, so its label
changed from "Move to a different slot…" to "Organize card…" to cover
both. The `moveRequested(this_slot_number)` signal itself is unchanged in
name and shape - it still just reports "the card's context menu wants to
reorganize this slot," and `JukeboxView` (now via `_on_organize_requested`,
renamed from `_on_move_requested`) decides what that means. This widget
doesn't display a card's genre at all - `set_slot()` picks up the new
`"genre"` key in the `slot_to_dict` payload without needing to read it;
only the view/dialog layer cares.

2026-09-13 follow-up - James, looking at a one-song card's greyed "OPEN"
banner: "when I have a jukebox card with one song, I would like to click
on the card and have the picker allow me to select a 2nd song." An empty
side's banner used to be `setEnabled(False)`, which - on top of the
greyed paint style this class already gave it - also made it a dead
click target: a disabled `QAbstractButton` never fires `clicked` at all.
The banner is now left enabled for both states; `_ChevronBanner.paintEvent`
switches its look on `bool(self._code)` (empty sides are always painted
with an empty `_code`, filled ones never are) instead of `isEnabled()`,
so the visual is identical to before but no longer gates whether a tap
does anything. `JukeboxStripWidget._on_key_clicked` now emits the new
`fillRequested(this_slot_number)` signal for a tap on the still-open
side instead of silently no-op'ing; `JukeboxView._on_fill_requested`
opens the same `JukeboxPickerDialog` the page's own "+ Add to jukebox"
button uses, pre-filled with this card's own artist and genre and capped
at one pick (`max_picks=1`, a new constructor parameter - there's only
one open side to fill), rather than a second, purpose-built dialog.

Same day, a fourth follow-up (James: "let me right click on a jukebox
card and allow me to edit the songs on the card") - this covers a side
that's already filled too, not just an open one, which is why it's a new
right-click menu action ("Edit songs…", added to `_build_context_menu`
right after "Organize card…") rather than another tap target on the
banners themselves: a filled banner's tap is already spoken for
(`sideActivated`, which sends that side to Now Playing). The new
`editRequested(this_slot_number)` signal reports only the slot, the same
as `fillRequested` - `JukeboxView._on_edit_requested` opens a
`JukeboxEditSongsDialog` (in `ui/views/jukebox.py`) showing both sides at
once with Change/Clear controls, and applies whatever changed via the new
`services.jukebox.set_slot_side`.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt, QSize, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
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
    """One side (A/B) of a title strip, painted as a pointed pennant -
    James's reference's "A1 — COME TOGETHER" style banner. See the module
    docstring for why this is a custom paintEvent rather than a styled
    QPushButton, and why the whole banner (not a separate key) is the tap
    target.

    States, all driven purely by `_code`/`isDown()`/`_now_playing` rather
    than separate widgets: a filled, not-playing side is cream with a
    brown outline and brown code box (the reference's resting look, red
    before the 2026-09-07 sixth follow-up retinted `COLORS["jukebox_key"]`
    et al. - see the module docstring); the currently-playing side fills
    solid brown with white text (there's no round key light to flash
    instead); an empty side (no second favorite paired onto this slot yet)
    is greyed out, reading "OPEN" instead of a code - but, since a
    2026-09-13 follow-up (see the module docstring), no longer actually
    disabled: tapping it is now a real gesture (`JukeboxStripWidget`'s new
    `fillRequested` signal), so the grey "OPEN" look is purely paint, not
    `isEnabled()` - see `paintEvent`'s `filled = bool(self._code)`.

    2026-09-07 follow-up - James, looking at a card: "The song titles
    should NOT be in all caps. They can be mixed case and hopefully
    providing more space for the entire name." The title used to be forced
    through `.upper()` to match the reference mockup's all-caps look;
    that's gone now, so a title paints in whatever case it's actually
    stored as. `_CODE_BOX_RATIO`/`_POINT_RATIO` were also trimmed down and
    `sizeHint()` widened, since the reference's own proportions (a fairly
    wide code box and a long pointed tip) were tuned for short all-caps
    titles like "COME TOGETHER," not a real, possibly-longer, mixed-case
    library title - reclaiming that width buys back some of what forcing
    the case had been quietly making room for. "OPEN" (the empty-side
    placeholder) is passed in already-uppercase from `_set_side` now,
    since it's a fixed UI word rather than someone's song title.
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
        self.setMinimumHeight(32)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: D102 - Qt override
        return QSize(210, 34)

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

        # 2026-09-13 follow-up (see the class docstring): an empty side is
        # still clickable now (see `fillRequested`), so the grey "OPEN"
        # look is decided by `_code` being blank, not `isEnabled()`.
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
            code_text_color = QColor("#ffffff")
            title_color = QColor("#ffffff")
        else:
            fill = QColor(COLORS["jukebox_paper"])
            border = QColor(COLORS["jukebox_key"])
            code_fill = QColor(COLORS["jukebox_key"])
            code_text_color = QColor("#ffffff")
            title_color = QColor(COLORS["jukebox_ink"])
        if self.isDown():
            # both states are real tap targets now (2026-09-13 follow-up) -
            # an "OPEN" banner darkens under a press exactly like a filled
            # one always has
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
        is preserved (no `.upper()`) without needing to inspect rendered
        pixels - see the class docstring's 2026-09-07 follow-up."""
        return metrics.elidedText(self._title, Qt.ElideRight, max(0, int(width)))


class JukeboxStripWidget(QFrame):
    """One numbered strip - see the module docstring."""

    #: side ("A"/"B"), track_id - emitted when a filled side's banner is tapped
    sideActivated = Signal(str, int)
    #: this_slot_number - emitted when an *empty* ("OPEN") side's banner is
    #: tapped (2026-09-13 follow-up - see the module docstring). Only the
    #: slot is reported, not which side - `services.jukebox.place_track`
    #: is what actually decides which side a newly-picked song lands on.
    fillRequested = Signal(int)
    #: this_slot_number - emitted when "Organize card…" is chosen from the
    #: card's right-click menu; see the module docstring's 2026-09-07
    #: notes. Kept as `moveRequested` even after the menu label and the
    #: dialog it opens grew a paired genre control (seventh follow-up) -
    #: renaming would only ripple through call sites for no behavioral gain.
    moveRequested = Signal(int)
    #: this_slot_number - emitted when "Remove from jukebox…" is chosen
    #: from the same right-click menu; see the module docstring's
    #: 2026-09-07 "delete this card" follow-up
    removeRequested = Signal(int)
    #: this_slot_number - emitted when "Edit songs…" is chosen from the
    #: same right-click menu (2026-09-13 follow-up - James: "let me right
    #: click on a jukebox card and allow me to edit the songs on the
    #: card"). Unlike `fillRequested`, this covers *either* side, filled or
    #: not - `JukeboxView._on_edit_requested` opens a dialog that shows
    #: both of this slot's current songs and lets either be replaced or
    #: cleared, writing changes through `services.jukebox.set_slot_side`.
    editRequested = Signal(int)

    #: fixed width of the artist-name badge, whatever the artist's name is -
    #: see the module docstring's third 2026-09-07 follow-up for why this
    #: can't just size to its text the way a plain QLabel would by default.
    #:
    #: 2026-09-13 fix: James sent a screenshot of "Steve Miller Band" eliding
    #: to "STEVE MILLER BA…" - an ordinary-length name, not the deliberately
    #: extreme "Bob Seger & The Silver Bullet Band" case the 150px width was
    #: originally sized around. Widened to 198, which is exactly the largest
    #: value that costs nothing: the artist row's own width demand
    #: (label width + the two divider lines' spacing) only starts to exceed
    #: - and therefore drive - the column width past what _ChevronBanner's
    #: sizeHint (210) already reserves once this label passes 198px, so the
    #: whole card's fixed size (still set once via setFixedSize(sizeHint())
    #: below) is completely unaffected. This comfortably fits the vast
    #: majority of real artist billings ("The Rolling Stones," "Bruce
    #: Springsteen," "Earth, Wind & Fire") without truncation; genuinely
    #: long ones still elide, same as before.
    _ARTIST_LABEL_WIDTH = 198

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("JukeboxStripOuter")
        self._slot_number: Optional[int] = None
        self._now_playing_side: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("JukeboxStrip")
        card.setToolTip("Right-click for options (organize or remove this card)")
        outer.addWidget(card)
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)
        card_layout.setSpacing(10)

        self.cover = _CardCover(size=74)
        card_layout.addWidget(self.cover)

        right_col = QVBoxLayout()
        right_col.setSpacing(5)

        self._rows: dict[str, dict] = {}

        top_banner = _ChevronBanner()
        top_banner.clicked.connect(lambda _=False: self._on_key_clicked("A"))
        right_col.addWidget(top_banner)
        self._rows["A"] = {"banner": top_banner, "track_id": None}

        right_col.addWidget(self._build_artist_row())

        bottom_banner = _ChevronBanner()
        bottom_banner.clicked.connect(lambda _=False: self._on_key_clicked("B"))
        right_col.addWidget(bottom_banner)
        self._rows["B"] = {"banner": bottom_banner, "track_id": None}

        card_layout.addLayout(right_col, 1)

        # every child above now has a fixed, content-independent sizeHint
        # (cover: setFixedSize; banners: sizeHint() override; artist_label:
        # setFixedWidth below) - locking to that combined sizeHint here
        # means nothing typed into set_slot() later, and no sibling strip
        # elsewhere in the page's grid being hidden or shown, can ever
        # resize this card again. See the module docstring's third
        # 2026-09-07 follow-up.
        self.setFixedSize(self.sizeHint())

    def _build_artist_row(self) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        left_line = QFrame()
        left_line.setObjectName("JukeboxArtistLine")
        self.artist_label = QLabel("")
        self.artist_label.setObjectName("JukeboxArtistBadge")
        self.artist_label.setAlignment(Qt.AlignCenter)
        self.artist_label.setFixedWidth(self._ARTIST_LABEL_WIDTH)
        right_line = QFrame()
        right_line.setObjectName("JukeboxArtistLine")
        h.addWidget(left_line, 1)
        h.addWidget(self.artist_label)
        h.addWidget(right_line, 1)
        return row

    def _set_artist_text(self, artist_name: str) -> None:
        """Elide by hand rather than letting the label size to its text -
        `artist_label` has a fixed width precisely so a long name (a full
        "Bob Seger & The Silver Bullet Band"-style billing) can't grow this
        card wider than any other on the page."""
        # a few px of slack for the QSS badge's own padding/border (see
        # ui/theme.py's QLabel#JukeboxArtistBadge rule)
        available = self._ARTIST_LABEL_WIDTH - 24
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
            # 2026-09-13 follow-up (see the module docstring): no longer
            # setEnabled(False) - this side is now a real tap target
            # (fillRequested), not a dead greyed-out placeholder.
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
            # an empty ("OPEN") side, now a real tap target (2026-09-13
            # follow-up - see the module docstring) rather than dead: an
            # unused page-filler strip (never set_slot()'d, both sides
            # empty) has no slot number to report, so this stays a no-op
            # for that case only.
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
        """Split out from `contextMenuEvent` so a test can trigger either
        action directly (find it by its text, then `.trigger()`) rather
        than needing to drive `QMenu.exec()`'s real popup event loop. Named
        `_build_context_menu` (not `_build_move_menu`, its original name
        from before "Remove from jukebox…" existed) since it's no longer
        just the move action. The move action's own label reads "Organize
        card…" (not "Move to a different slot…") since 2026-09-07's genre
        chips follow-up folded a genre control into the same dialog.

        "Edit songs…" (2026-09-13 follow-up) sits right after "Organize
        card…" and before the separator: both are "adjust this card in
        place" actions, while "Remove from jukebox…" below the separator
        is the one destructive action on the menu."""
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
