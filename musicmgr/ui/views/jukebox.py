"""Jukebox: a persistent 45-selector favorites board (2026-09-06).

James: "I want a new sidebar option called jukebox with something like a 45
RPM selector panel... make it feel very authentic... I would like the user
to be able to select an artist and add 2 songs per title strip. This becomes
a favorites capability. Any record with 5 star rating gets added to a title
strip." (The automatic 5-star route described here was removed in a
2026-09-07 follow-up - see services/jukebox.py's module docstring for why -
so this founding quote is kept as-is for history, not as current
behavior.) See services/jukebox.py's module docstring for the full list of
how slots get filled today (this view's own "+ Add to jukebox" button,
Title Details' Jukebox column, and Now Playing's Jukebox toggle - all
three share this view's `JukeboxPickerDialog` as of a 2026-09-13
follow-up) and ui/widgets/jukebox_strip.py for how one title strip is
drawn and played.

Tapping a side's key plays it immediately, the same way a real jukebox
button does (James, when asked whether a tap should just queue the song
silently: "play like a normal jukebox") - `ctx.play_tracks` is the one seam
every other "hit play" action in the app already funnels through, so this
gets Now Playing navigation for free rather than needing its own.

The "NOW PLAYING — A7" header display tracks `ctx.player.trackChanged`
directly rather than only reacting to jukebox taps, so it also lights up
when a 5-star favorite already on the board gets played some other way
(Now Playing's own controls, a playlist, Title Details) - the display
should never lag behind what's actually on the speakers.

2026-09-07 follow-up (James: "is there a way I can move these jukebox
cards around?"): tapping a strip's "⇄" button opens `JukeboxMoveDialog`
- a picker of every other slot currently on the board, by number and
artist - and moving `_on_move_requested` below calls
`services.jukebox.swap_slots` with whichever one was chosen, then
refreshes. Both sides of a strip travel together, same as picking up a
real record.

An actual drag-and-drop version of this shipped first the same day and
was pulled back out once James tried it: there was no way to drag a card
from page 2 onto a target back on page 1, since `self.strips` only ever
holds one page's worth of widgets - there was nothing on screen to drop
onto once the target had paged out of view. Picking a slot *number*
instead of a target *card* doesn't have that problem: `JukeboxMoveDialog`
lists every slot regardless of which page it would show up on, so source
and destination never need to be visible together.

2026-09-07, later same-day follow-up (James: "I would like to right click
on the jukebox card and delete it by removing both songs from that
card"): the same right-click menu gained a "Remove from jukebox…" action
alongside "Move to a different slot…". `_on_remove_requested` below
confirms first (`QMessageBox.question` - this only takes the card off the
board, not the underlying tracks, but there's no undo once it's gone) then
calls `services.jukebox.remove_slot`, which deletes the whole
`JukeboxSlot` regardless of what's loaded on either side.

2026-09-07, a third same-day follow-up - James, looking at the same 3x3
board: "Does this need to be a fixed 3x3? I want the cards to stay the
same size but would like it to expand to 3x4 if the page is high enough."
The grid now builds MAX_ROWS x GRID_COLUMNS strip widgets up front instead
of a fixed nine, and `_rows_that_fit` decides - at every `refresh()` and
again on every resize, via `resizeEvent` - whether the window actually has
room for a fourth row, always falling back to MIN_ROWS when it doesn't.
Card size itself never changes either way, only how many of the
already-fixed-size strips get shown per page, matching the same "cards
don't resize" constraint from the two 2026-09-07 follow-ups above.

2026-09-07, a fourth same-day follow-up: James asked to make this the
app's default startup page (see ui/app.py's `MainWindow.__init__`), and
pointed out the pager ("Page 1 of 3") was eating a whole row of its own
under the grid for not much information - `self.prev_btn`/`page_label`/
`next_btn` moved into the header, just left of "+ Add to jukebox", giving
that row back to the grid (see `__init__` below; `refresh()`'s own
`page_label.setText(...)` call didn't need to change, only where the
label lives). He also asked for the strip key badges ("A7"/"B7") to move
off red, which read as an alert color against this app's own warm brown/
gold branding - see ui/widgets/jukebox_strip.py's docstring for the new
color.

2026-09-07, a fifth same-day follow-up (James: "I would like the jukebox
page to have a chip of 5 genres: Classic Rock, Country, Pop, Hairbands,
Rock. Then have the pages of the cards where you can select the location
and what genre page a track will be organized by"): a row of
`ChipButton`s (`services.jukebox.JUKEBOX_GENRES`) sits between the header
and the grid, exclusive like every other chip row in this app
(cover_grid.py's sort chips, Now Playing's "Up next"/"Lyrics" chips).
Picking a chip is purely a display filter - `refresh()` passes
`genre=self._genre` through to `slot_count`/`page_count`/`list_slot_rows`
- and resets to page 0, since a page number from one genre's board has no
particular meaning on another's. `JukeboxAddDialog` (renamed
`JukeboxPickerDialog` in a 2026-09-13 follow-up - see that class's own
docstring) grew a genre combo
(preselected to whichever chip is active) so a freshly-added card is filed
under the right board from the start; the right-click dialog, renamed
`JukeboxOrganizeDialog` (from `JukeboxMoveDialog`) and reached through the
renamed `_on_organize_requested` (from `_on_move_requested`), grew a
second, independent genre combo alongside its existing slot-location
picker, since re-filing a card's genre and moving its board position are
orthogonal operations (`services.jukebox.set_slot_genre` vs. `swap_slots`)
that this one dialog now exposes together. Unlike the old move-only
dialog, the Organize dialog's OK button is never disabled for having no
other slot to swap with - reassigning just the genre is a complete,
valid action even on a single-card board, so the "No other slot to move
this to yet" early bailout is gone.

2026-09-07, a sixth same-day follow-up - James: "great job on the Genre
on the jukebox option. Please add 2 more chips: Christian, 80's. Then
reorder them from left to right: Country, Christian, Classic Rock, Rock,
80's, Hairbands, Pop. And then make the pills not red but the brown
color." `services.jukebox.JUKEBOX_GENRES` grew from five entries to seven
and its tuple order changed to match - the chip row here builds one
`ChipButton` per entry in that same order, so reordering the tuple is the
entire fix for the on-screen left-to-right order, and every genre combo
box (`JukeboxAddDialog`/`JukeboxPickerDialog`, `JukeboxOrganizeDialog`)
picks up the new order
and members for free, being built the same way. Each chip's object name
is overridden from `ChipButton`'s own default ("Chip") to "ChipWarm"
right after construction, so `ui/theme.py`'s new `#ChipWarm:checked` rule
(walnut-brown, reusing the jukebox key palette) applies only to these
genre chips - every other chip row in the app (the ones named in the
paragraph above) keeps the ordinary red `#Chip:checked` look.

2026-09-08 follow-up - James: "add 3 more genres to the jukebox: Metal,
R&B, Hip/Hop." `services.jukebox.JUKEBOX_GENRES` grew from seven entries
to ten, appended after Pop rather than interleaved into the existing
order (unlike the sixth follow-up above, this one didn't ask for a
reorder). Nothing else here changed - same ChipButton-per-entry loop,
same ChipWarm styling, same genre combo boxes built off the same tuple.

2026-09-13 follow-up - James: "the cards don't need to be a forced 3 cols
by 4 rows. It should adjust as the window in the app adjusts." Column
count used to be the fixed `GRID_COLUMNS = 3` constant, with only the row
count (`_rows_that_fit`) responding to the window's actual size - an
asymmetry left over from the fourth same-day 2026-09-07 follow-up above,
which only ever asked "does this need to be a fixed 3x3," not "does this
need to be a fixed *3-wide*." `GRID_COLUMNS` is gone, replaced by
`MIN_COLUMNS`/`MAX_COLUMNS` and a `_cols_that_fit()` that mirrors
`_rows_that_fit()` exactly - same "measure grid_host, divide by one fixed
strip size plus spacing, walk down from the max" logic, just against
`grid_host.width()` and the strips' width instead of height. The strip
pool is now built MAX_ROWS x MAX_COLUMNS up front (bigger than before,
but still trivial - see the third same-day 2026-09-07 follow-up for why
pre-building beats constructing strips on demand), and `_arrange_grid()`
re-places however many of them the current page needs at their new
(row, col) whenever either dimension changes - `QGridLayout` has to be
told every cell explicitly, so a widget's spot from the last arrangement
doesn't just carry over the way its fixed size already does. Card size
itself still never changes, same as every resizing-related follow-up
above - only how many fit, in either direction now instead of just one.

2026-09-13, later same-day follow-up (James: "close the gap of the space
from the left sidebar menu options and the cards. too much empty space"):
`BaseView`'s shared 20px left content margin (see ui/views/base.py) reads
as a much wider band than that once the nav rail's own fixed width is
added on top of it, and on this page - unlike, say, Library's own list
view - there's no other visual element (a search box, a filter row)
between the rail and the first column to make that band read as
intentional spacing rather than empty space. `JukeboxView.__init__`
narrows just its own `self._root` left margin to 6px right after calling
`super().__init__`, rather than changing `BaseView`'s default and
affecting every other page that didn't ask for this.

Same day, next follow-up (James, after narrowing `_root`'s margin above
still left "too much space" at the smallest window size): the remaining
gap wasn't `_root` at all - `self.grid`, the `QGridLayout` the cards sit
in, carries its own separate ~9px default contents margin on every side,
independent of whatever margin its parent layout already applies. Zeroed
in `__init__` alongside the other `self.grid.set*` calls, next to
`_GridHost` (see that class's own docstring for the *other* Qt
"minimum size" gotcha this same grid ran into).

Same day, third follow-up (James: "still too much space" - this time
confirmed after a full app restart, ruling out a stale running process
as the cause): with both of the above already zeroed, the only thing
left between the rail and the cards was `JukeboxStripWidget`'s own 4px
outer frame margin (see ui/widgets/jukebox_strip.py) - real, but small.
James still wanted it tighter, so `_root`'s left margin went from 6px
to 0 in `JukeboxView.__init__`; that 4px card-frame inset is what's left
today, and it's shared by every card on every side (it's also most of
the visible gap *between* columns, on top of the grid's own 16px
horizontal spacing), not something to strip out just for the leftmost
column without changing how every card looks against its neighbors.

2026-09-13, a later follow-up (James: "I want a better UI for the
Jukebox 'picker'... I want a standard UI. [a] picker box that allows you
to select a genre and search by track and/or artist"): `JukeboxAddDialog`
was reworked into `JukeboxPickerDialog` (see that class's own docstring)
- a single dialog, used both from this view's own "+ Add to jukebox"
button and, new as of this follow-up, from a track-level Jukebox toggle
turning ON (Title Details' column, Now Playing's toggle - see those two
views' `_on_jukebox_toggle_requested`/`_on_jukebox_toggled`). Until now
those two toggles had no genre picker of their own at all and silently
filed every track under the default "Rock" page
(`services/jukebox.py:DEFAULT_JUKEBOX_GENRE`) - James confirmed the
toggle should open this same full picker dialog, pre-selected to
whichever track was actually tapped, rather than a smaller
genre-only popup of its own.

Same day, a follow-up to the follow-up (James, looking at the picker's
one combined "Search by track or artist" box: "I want the picker with
two seperate search boxes. One for artist and one for track. I'll
usually select an artist first, and then want to search within that
artist for a song"): `JukeboxPickerDialog`'s single `search_box` became
two - `artist_search` and `track_search` - ANDed together rather than
either matching on its own the way the one combined box did, so typing
an artist narrows the board and typing a track further narrows *within*
whatever the artist box already matched. See the class's own docstring
and `services/jukebox.py:search_addable_tracks` for how the two combine.

Same day, a third follow-up (James, looking at a one-song card's greyed
"OPEN" second banner: "when I have a jukebox card with one song, I would
like to click on the card and have the picker allow me to select a 2nd
song"): that banner is a real tap target now - see
`ui/widgets/jukebox_strip.py`'s own module docstring for the widget-side
half of this - and `_on_fill_requested` below opens the very same
`JukeboxPickerDialog`, pre-filled with the card's own artist and genre
and capped at one pick, rather than a second, purpose-built dialog just
for this one entry point.

Same day, a fourth follow-up (James: "let me right click on a jukebox
card and allow me to edit the songs on the card"): a new "Edit songs…"
entry on the card's right-click menu (see `ui/widgets/jukebox_strip.py`'s
own module docstring for the widget-side half) opens the new
`JukeboxEditSongsDialog` below, which shows both of the slot's sides -
filled or not - with Change…/Clear controls per side. "Change…" reuses
`JukeboxPickerDialog` yet again as a nested dialog (`show_genre=False`,
`max_picks=1`); the outer dialog only tracks a plan until it's accepted,
and `_on_edit_requested` applies it in one pass via the new
`services/jukebox.py:set_slot_side` - the first place any of the three
jukebox entry points can *replace* an already-filled side, not just fill
an open one.

2026-09-18 follow-up - James asked for drag-and-drop card reordering
again, after asking for it pulled back out the first time (see this
docstring's own 2026-09-07 note above): the reason it didn't work out
then - a card on page 2 had nowhere on-screen to be dropped onto a target
still sitting on page 1 - gets solved this time instead of sidestepped.
`JukeboxStripWidget` is now a drag source and drop target on its own (see
its own module docstring); dropping one card onto another emits
`reorderRequested(source_slot, target_slot)`, wired here to
`_on_reorder_requested`. Both stay on the board side-by-side: a drag only
ever reorders within the genre page currently showing (there's nothing
else visible to drop onto), so "Organize card…" is still how a card gets
moved across genre boards.

Same day, a follow-up: `_on_reorder_requested` first called
`services.jukebox.swap_slots` - the exact same two-way trade
`_on_organize_requested`'s dialog already makes, just reached by a drag
instead of picking a slot number from a dropdown. James, after trying it:
"If the page is full, it doesn't seem to allow the drop and have it
automatically shift the last one to the next page." A swap was never
actually refusing anything - every card on a full page already has a
slot_number, so any of them is a valid drop target - but trading exactly
two cards' positions isn't the same as inserting the dragged card where
it was dropped and having everything after it shift down to make room,
which is what "shift the last one to the next page" was asking for.
`_on_reorder_requested` now calls `services.jukebox.reorder_slot` instead
- see that function's own docstring for how the rank-based reassignment
works. Nothing here had to change to make an overflowing card land on the
next page: `refresh()` re-renders whichever `per_page` cards now fall on
the current page in slot_number order, exactly like it already does for
any other change to the board.

What actually closes the old page-1/page-2 gap is `prev_btn`/`next_btn`:
both get `setAcceptDrops(True)` and an installed event filter here, so
hovering a dragged card over either arrow starts `_page_flip_timer`
(`PAGE_FLIP_HOVER_MS`); if the hover is still there when it fires,
`_flip_page_during_drag` calls the same `_change_page`/`refresh()` the
arrow's own click handler uses and restarts the timer, so holding a card
over "›" keeps walking forward one page at a time for as long as there's
another page in that direction (`next_btn`/`prev_btn.isEnabled()` is the
same bound the visible arrows already respect). Qt's `QDrag.exec()` runs
its own nested event loop while a drag is active, which is what lets this
timer keep firing - and `refresh()` keep repainting the grid underneath
the cursor - without the drag itself being interrupted. Neither arrow is
ever a real *drop* target for a card (`QEvent.Drop` is explicitly
ignored in the filter below) - releasing over one just ends the drag with
nothing swapped, same as releasing over any other empty space.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...db.models import Track
from ...services import jukebox as jkb_svc
from ..context import AppContext
from ..widgets.common import ChipButton, EmptyState, TouchButton, dim_label
from ..widgets.jukebox_strip import SLOT_MIME_TYPE, JukeboxStripWidget
from .base import BaseView

#: 3-6 columns and 3-4 rows, both decided at runtime from how much space
#: `grid_host` actually has - a full page readable at arm's length on a
#: touch panel without its own scrolling (James, 2026-09-06: "make the
#: jukebox 3 columns instead of 2"; 2026-09-07 follow-up: "Does this need
#: to be a fixed 3x3? ... would like it to expand to 3x4 if the page is
#: high enough"; 2026-09-13 follow-up: "the cards don't need to be a
#: forced 3 cols by 4 rows. It should adjust as the window in the app
#: adjusts"). `JukeboxView._rows_that_fit`/`_cols_that_fit` do the actual
#: measuring - MIN_ROWS/MIN_COLUMNS is what the board always shows even
#: when there's no room for more, MAX_ROWS/MAX_COLUMNS is the most it will
#: ever show (and how big a strip-widget pool gets built up front - see
#: `__init__`). services/jukebox.py's SLOTS_PER_PAGE (9) stays put as that
#: module's own documented default for any other caller; this view always
#: passes its own dynamically computed per_page instead of relying on it.
MIN_ROWS = 3
MAX_ROWS = 4
MIN_COLUMNS = 3
MAX_COLUMNS = 6

#: a physical 45 only has two sides - the picker refuses a third pick rather
#: than silently bumping one back off, so the person always sees exactly
#: what they're about to add.
MAX_PICKS = 2

#: how long a card has to hover over a page-arrow button, mid-drag, before
#: that direction's page flips underneath it (2026-09-18 follow-up - see
#: the module docstring's drag-and-drop note). Long enough that passing
#: over the arrow on the way to a normal in-page drop doesn't flip a page
#: by accident; short enough that walking a card several pages over by
#: holding it there doesn't feel like waiting.
PAGE_FLIP_HOVER_MS = 600


class JukeboxPickerDialog(QDialog):
    """The standard "Add to jukebox" picker (2026-09-13 follow-up) - James:
    "I want a better UI for the Jukebox 'picker'... The problem with the
    tracks is there is no way to select which genre the track should be
    on. I want a standard UI. [a] picker box that allows you to select a
    genre and search by track and/or artist." One dialog, used from both
    of the app's two places that add a track to the board:

    - The Jukebox page's own "+ Add to jukebox" button
      (`JukeboxView._open_add_dialog`).
    - A track-level Jukebox toggle turning ON - Title Details' own column
      (`ui/views/library.py`'s `_on_jukebox_toggle_requested`) and Now
      Playing's toggle (`ui/views/nowplaying.py`'s `_on_jukebox_toggled`).
      Neither of those had a genre picker of its own before this - a
      track added that way silently landed on the default "Rock" page
      every time (see `services/library.py:toggle_jukebox_membership`,
      which no longer calls `place_track` itself for the "turning on"
      case - see those two views for the replacement flow). Opened from
      here, `initial_artist_query`/`initial_track_query`/`check_track`
      pre-fill and pre-check the actual track that was tapped, so the
      person just picks a genre and taps OK rather than re-finding the
      song they already clicked on.

    Replaces the previous "Add to jukebox" dialog's artist-first flow
    (pick one artist from a dropdown, then check that artist's tracks one
    artist at a time via `list_tracks_for_album_artist`) with two live
    search boxes, `artist_search` and `track_search`, ANDed together
    (`search_tracks`, backed by `services/jukebox.py:search_addable_tracks`)
    - James confirmed replacing the dropdown outright rather than keeping
    it alongside search, and a same-day follow-up split what started as
    one combined "track or artist" box into these two separate ones:
    "I'll usually select an artist first, and then want to search within
    that artist for a song" - typing into `artist_search` narrows the
    board down to that artist, and `track_search` narrows further within
    whatever `artist_search` currently matches, rather than either field
    needing to name the same track/artist pair on its own the way the one
    combined box did. `genre_combo` and the checkbox-list mechanics
    (capped at `max_picks` - the module `MAX_PICKS` by default, overridden
    to 1 by `JukeboxView._on_fill_requested` when only one card side is
    actually open - a real `QCheckBox` per row so a tap anywhere on the
    label toggles it, see the 2026-09-07 fix this inherits unchanged) are
    otherwise the same as before.

    Picks persist across searches: `self._picks` (a `{track_id:
    artist_id}` dict) is the source of truth for what's selected, not
    whatever happens to be checked in the currently-visible results list
    - typing into either search box re-renders the list from scratch
    (`_render_results`), so a track picked under one search stays picked
    (and, if it reappears under a later search, shows checked again) even
    while it's scrolled out of view. Search can span multiple artists in
    one go now (unlike the old artist-first flow), so picks are tracked
    as `(track_id, artist_id)` pairs rather than one shared artist id plus
    a bare list of track ids - `selected_picks()` replaces the old
    `selected_artist_id()`/`selected_track_ids()` pair for exactly that
    reason.

    2026-09-13, fourth follow-up - reused as the nested "pick a
    replacement" step inside `JukeboxEditSongsDialog`'s "Change…" button
    (James: "let me right click on a jukebox card and allow me to edit the
    songs on the card"). Two small additions support that reuse without
    touching either existing call site's behavior: `show_genre=False`
    hides `genre_combo` and its label entirely, since editing a slot's
    songs never touches its genre; and `selected_rows()` returns each
    pick's title alongside its ids (`self._pick_titles`, populated in
    `_render_results`/`_on_checkbox_toggled`/`check_track` right alongside
    `self._picks`) because the edit dialog needs the picked title to show
    in its own side-by-side summary, which plain `selected_picks()`
    doesn't carry.

    2026-09-18 follow-up - James, on a large library: "when I go back to
    enter another track, it seems I can enter one letter but then it
    bounces back to the artist." Both search boxes used to call
    `search_tracks` straight from `textChanged`, so every keystroke ran a
    real query against the whole library - long enough on a big collection
    to visibly freeze the dialog, which is exactly the kind of pause that
    gets a person to re-click the field they think stopped responding; a
    stray click made mid-freeze doesn't get delivered until the query
    finally returns, and it's *that* click - not anything this dialog does
    on purpose - that was moving focus. `_search_timer` now debounces
    `_run_search` by 220ms of no further typing (`_schedule_search`,
    mirroring `ui/views/search.py`'s own `SearchView._schedule`/
    `_run_search`), so a query only fires once someone's actually paused.
    `_render_results` also now disposes of each search's checkbox widgets
    before clearing the list, rather than leaking them as invisible
    orphans of `track_list`'s viewport - the more searches a session ran,
    the larger and slower `track_list` silently got, which only made the
    freeze-and-mis-click above worse the longer the dialog stayed open.
    """

    def __init__(
        self,
        parent,
        search_tracks: Callable[[str, str], list[dict]],
        genres: Sequence[str] = (),
        default_genre: Optional[str] = None,
        initial_artist_query: str = "",
        initial_track_query: str = "",
        max_picks: int = MAX_PICKS,
        title: str = "Add to jukebox",
        show_genre: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        # Widened from the original 460x560 (2026-09-20 follow-up) - James:
        # "expand the Add to jukebox window so I can see more text for the
        # songs and their albums." Each row's checkbox label is a single
        # unwrapped line ("Title — Artist (Album)"), so a narrow dialog just
        # clipped long titles/albums behind track_list's horizontal
        # scrollbar rather than shrinking the text - widening the dialog
        # itself is what actually shows more of that line. `resize` (not
        # just a taller `setMinimumSize` floor) sets the size the dialog
        # actually opens at; it stays user-resizable beyond this in either
        # direction since nothing here caps it with setMaximumSize.
        self.setMinimumSize(460, 560)
        self.resize(820, 640)
        #: `(artist_query, track_query) -> results` - see the class
        #: docstring for how the two boxes below combine (ANDed, not
        #: either/or the way the single box this replaced worked).
        self._search_tracks = search_tracks
        #: overridable per call site (2026-09-13 follow-up) - defaults to
        #: the module `MAX_PICKS` (a fresh card's two open sides), but
        #: `JukeboxView._on_fill_requested` passes 1 when there's only one
        #: open side left to fill on an existing card.
        self._max_picks = max_picks
        #: {track_id: artist_id} - see class docstring; insertion order is
        #: preserved (plain dict, py3.7+) though nothing currently relies
        #: on that ordering.
        self._picks: dict[int, int] = {}
        #: {track_id: title} - kept alongside `self._picks` (2026-09-13,
        #: fourth follow-up) purely for `selected_rows()`; unused by either
        #: of the original two call sites, which only ever read
        #: `selected_picks()`.
        self._pick_titles: dict[int, str] = {}
        #: debounces `_run_search` off of raw keystrokes (2026-09-18
        #: follow-up) - James: "when I go back to enter another track, it
        #: seems I can enter one letter but then it bounces back to the
        #: artist." `_search_tracks` runs a real, synchronous query
        #: against the whole library (no index-friendly prefix on either
        #: `ilike`/`like` pattern - see `search_addable_tracks`'s own
        #: docstring), and this dialog used to call it on every single
        #: keystroke in either box - fine on a small library, but on a
        #: large one each keystroke could visibly freeze the dialog for a
        #: few hundred ms. A frozen dialog is exactly what makes a person
        #: re-click the field they think lost focus - that stray click
        #: just queues up behind the frozen keystroke and lands the moment
        #: the query finally returns, which is what actually moves focus;
        #: nothing here was deliberately sending it to `artist_search`.
        #: Same 220ms-after-the-last-keystroke debounce `ui/views/
        #: search.py`'s own `SearchView` already uses for the same reason
        #: - `_schedule_search`/`_run_search` mirror its `_schedule`/
        #: `_run_search` naming for that reason.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(220)
        self._search_timer.timeout.connect(self._run_search)

        layout = QVBoxLayout(self)

        layout.addWidget(dim_label("Search by artist"))
        self.artist_search = QLineEdit()
        self.artist_search.setPlaceholderText("Artist name…")
        self.artist_search.textChanged.connect(self._schedule_search)
        layout.addWidget(self.artist_search)

        layout.addWidget(dim_label("Search by track"))
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Track name…")
        self.track_search.textChanged.connect(self._schedule_search)
        layout.addWidget(self.track_search)

        self.genre_label = dim_label("Genre page")
        layout.addWidget(self.genre_label)
        self.genre_combo = QComboBox()
        for genre in genres:
            self.genre_combo.addItem(genre, genre)
        if default_genre is not None:
            idx = self.genre_combo.findData(default_genre)
            if idx >= 0:
                self.genre_combo.setCurrentIndex(idx)
        layout.addWidget(self.genre_combo)
        if not show_genre:
            # 2026-09-13, fourth follow-up (see class docstring): editing a
            # slot's songs never touches its genre, so the nested picker
            # inside `JukeboxEditSongsDialog` hides this control entirely
            # rather than showing a genre choice that's simply ignored.
            self.genre_label.hide()
            self.genre_combo.hide()

        layout.addWidget(dim_label(f"Songs (pick up to {self._max_picks})"))
        self.track_list = QListWidget()
        self.track_list.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(self.track_list, 1)

        self.hint = dim_label("")
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_hint()
        if initial_artist_query or initial_track_query:
            # blocked while both boxes are filled in, then searched once at
            # the end - filling them one at a time without blocking would
            # fire an extra, immediately-superseded search on the first
            # setText() alone (e.g. artist-only, before the track query
            # that narrows it is in place)
            self.artist_search.blockSignals(True)
            self.track_search.blockSignals(True)
            self.artist_search.setText(initial_artist_query)
            self.track_search.setText(initial_track_query)
            self.artist_search.blockSignals(False)
            self.track_search.blockSignals(False)
            # a pre-filled query (from a track-level Jukebox toggle - see
            # the class docstring) searches immediately rather than
            # waiting out the debounce below, the same way `SearchView.
            # refresh()` runs its own `_run_search()` straight away for a
            # query that's already sitting in the box.
            self._run_search()

    def _schedule_search(self, _text: str = "") -> None:
        """`textChanged` handler for both search boxes (2026-09-18
        follow-up - see `self._search_timer`'s own comment). Restarts the
        debounce timer on every keystroke rather than searching directly;
        `_run_search` is what actually queries and re-renders, once
        typing has paused for `_search_timer`'s interval."""
        self._search_timer.start()

    def _run_search(self) -> None:
        artist_query = self.artist_search.text().strip()
        track_query = self.track_search.text().strip()
        results = (
            self._search_tracks(artist_query, track_query)
            if (artist_query or track_query)
            else []
        )
        self._render_results(results)

    def _render_results(self, results: list[dict]) -> None:
        # QListWidget.clear() deletes the QListWidgetItems but - a known
        # Qt quirk - leaves any widget set via setItemWidget() behind as
        # an orphaned, invisible child of the list's viewport rather than
        # deleting it too. Every search used to leak that render's whole
        # batch of checkboxes this way; explicitly detaching and disposing
        # of each one first is the documented way to avoid it (Qt's own
        # QListWidget::removeItemWidget() docs: "the ownership of the
        # widget is passed to the caller").
        for row_index in range(self.track_list.count()):
            item = self.track_list.item(row_index)
            widget = self.track_list.itemWidget(item)
            if widget is not None:
                self.track_list.removeItemWidget(item)
                widget.deleteLater()
        self.track_list.clear()
        for row in results:
            track_id = row["track_id"]
            item = QListWidgetItem()
            item.setData(Qt.UserRole, track_id)
            # tall enough that the checkbox's own click area (its label +
            # indicator, not the bare row) is a real touch target - see the
            # 2026-09-07 fix note in the class docstring
            item.setSizeHint(QSize(0, TOUCH["row_height"]))
            label = f"{row['title']} — {row['artist_name']}"
            if row.get("album"):
                label += f" ({row['album']})"
            checkbox = QCheckBox(label)
            checkbox.setProperty("track_id", track_id)
            checkbox.setProperty("artist_id", row["artist_id"])
            checkbox.setProperty("title", row["title"])
            # reflects a pick made under an earlier search term - see the
            # class docstring's "picks persist across searches" note
            checkbox.setChecked(track_id in self._picks)
            checkbox.toggled.connect(self._on_checkbox_toggled)
            self.track_list.addItem(item)
            self.track_list.setItemWidget(item, checkbox)
        self._update_hint()

    def _on_checkbox_toggled(self, checked: bool) -> None:
        box = self.sender()
        track_id = box.property("track_id")
        artist_id = box.property("artist_id")
        title = box.property("title")
        if checked:
            if len(self._picks) >= self._max_picks and track_id not in self._picks:
                # last one over the limit - put it back rather than silently
                # bumping an earlier pick, so the person's own two choices
                # stick (2026-09-07 fix, carried over unchanged)
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
                return
            self._picks[track_id] = artist_id
            self._pick_titles[track_id] = title or ""
        else:
            self._picks.pop(track_id, None)
            self._pick_titles.pop(track_id, None)
        self._update_hint()

    def _sync_checkbox_for(self, track_id: int) -> None:
        """Checks `track_id`'s row if it's currently in the visible results
        list - used by `check_track` below, which updates `self._picks`
        (the real source of truth) regardless of whether that row happens
        to be on screen right now."""
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            if item.data(Qt.UserRole) == track_id:
                checkbox = self.track_list.itemWidget(item)
                if checkbox is not None and not checkbox.isChecked():
                    checkbox.setChecked(True)
                return

    def check_track(self, track_id: int, artist_id: int, title: str = "") -> None:
        """Pre-selects one track - used when this dialog is opened from a
        track-level Jukebox toggle (see the class docstring) so the track
        that was actually tapped starts out already picked, rather than
        the person having to find and re-check it themselves after typing
        a search for it. Call after construction (typically right after
        `initial_artist_query`/`initial_track_query` have already narrowed
        the results down to that exact track, so its row is visible to
        show as checked). `title` is optional (2026-09-13, fourth
        follow-up) - only `selected_rows()` callers need it; the two
        original call sites don't pass it and don't need to."""
        self._picks[track_id] = artist_id
        if title:
            self._pick_titles[track_id] = title
        self._sync_checkbox_for(track_id)
        self._update_hint()

    def _update_hint(self) -> None:
        count = len(self._picks)
        self.hint.setText(f"{count} of {self._max_picks} selected")
        self.ok_button.setEnabled(count > 0)

    def selected_picks(self) -> list[tuple[int, int]]:
        """Every current pick as `(track_id, artist_id)` pairs - one
        `place_track` call per pair is what both call sites do with this
        (see `JukeboxView._open_add_dialog`)."""
        return list(self._picks.items())

    def selected_genre(self) -> Optional[str]:
        return self.genre_combo.currentData()

    def selected_rows(self) -> list[dict]:
        """Every current pick as `{"track_id", "artist_id", "title"}`
        dicts (2026-09-13, fourth follow-up) - like `selected_picks()` but
        carrying the title too, for `JukeboxEditSongsDialog`'s own summary
        of what was just picked. `selected_picks()` is left as-is for the
        two original call sites, neither of which needs a title."""
        return [
            {"track_id": tid, "artist_id": aid, "title": self._pick_titles.get(tid, "")}
            for tid, aid in self._picks.items()
        ]


class JukeboxEditSongsDialog(QDialog):
    """"Edit songs…" (2026-09-13 follow-up) - James: "let me right click on
    a jukebox card and allow me to edit the songs on the card." Shows this
    slot's two sides, each with its current song title (or "— empty —")
    and a Change…/Clear pair of buttons. "Change…" opens a nested
    `JukeboxPickerDialog` (search by artist/track, capped at one pick,
    `show_genre=False` since this never touches the slot's genre) scoped
    by default to the slot's own artist, exactly like the picker
    `JukeboxView._on_fill_requested` already opens for an *open* side -
    this dialog is what extends that same picking flow to an already
    filled side, and to both sides at once rather than just whichever one
    happens to be open.

    Only tracks a plan (`self._plan`) while open - `{"A": (track_id_or_
    None, title), ...}`, one entry per side actually touched or cleared;
    a side left alone entirely doesn't appear in `plan()`'s result at all,
    so `JukeboxView._on_edit_requested` knows to leave it exactly as-is
    rather than re-writing it to its own current value. All actual writes
    happen there, via `services.jukebox.set_slot_side`, once, after this
    dialog is accepted - the same "widget/dialog only reports the intent,
    the view owns the database" split every other card action already
    follows (`sideActivated`, `moveRequested`, `removeRequested`,
    `fillRequested`)."""

    #: sentinel meaning "this side hasn't been touched" - distinct from a
    #: real planned value of `(None, "")` (an explicit Clear).
    _UNCHANGED = object()

    def __init__(self, parent, row: dict, search_tracks: Callable[[str, str], list[dict]]) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit songs — Slot {row['slot_number']}")
        self._row = row
        self._search_tracks = search_tracks
        self._plan: dict[str, object] = {"A": self._UNCHANGED, "B": self._UNCHANGED}
        self._labels: dict[str, QLabel] = {}
        self._clear_buttons: dict[str, TouchButton] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(dim_label(row.get("artist_name", "")))

        for side in ("A", "B"):
            row_widget = QWidget()
            h = QHBoxLayout(row_widget)
            h.setContentsMargins(0, 0, 0, 0)
            label = QLabel()
            self._labels[side] = label
            h.addWidget(label, 1)
            change_btn = TouchButton("Change…")
            change_btn.clicked.connect(lambda _=False, s=side: self._on_change_clicked(s))
            h.addWidget(change_btn)
            clear_btn = TouchButton("Clear")
            clear_btn.clicked.connect(lambda _=False, s=side: self._on_clear_clicked(s))
            self._clear_buttons[side] = clear_btn
            h.addWidget(clear_btn)
            layout.addWidget(row_widget)
            self._refresh_label(side)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _current(self, side: str) -> tuple[Optional[int], str]:
        """`(track_id_or_None, title)` for `side` - whatever's planned so
        far if this side has already been touched this session, otherwise
        the slot's actual current value from `row` (as `get_slot_row`
        shaped it: `row["side_a"]`/`row["side_b"]`, each `None` or a dict
        with a `"title"` key)."""
        plan_value = self._plan[side]
        if plan_value is not self._UNCHANGED:
            return plan_value
        data = self._row.get("side_a" if side == "A" else "side_b")
        if data is None:
            return None, ""
        return data["track_id"], data["title"]

    def _refresh_label(self, side: str) -> None:
        track_id, title = self._current(side)
        self._labels[side].setText(f"Side {side}: {title}" if title else f"Side {side}: — empty —")
        self._clear_buttons[side].setEnabled(track_id is not None)

    def _on_change_clicked(self, side: str) -> None:
        dialog = JukeboxPickerDialog(
            self,
            self._search_tracks,
            initial_artist_query=self._row.get("artist_name", ""),
            max_picks=1,
            title=f"Choose a song for side {side}",
            show_genre=False,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        rows = dialog.selected_rows()
        if not rows:
            return
        picked = rows[0]
        self._plan[side] = (picked["track_id"], picked["title"])
        self._refresh_label(side)

    def _on_clear_clicked(self, side: str) -> None:
        self._plan[side] = (None, "")
        self._refresh_label(side)

    def plan(self) -> dict[str, tuple[Optional[int], str]]:
        """`{"A": (track_id_or_None, title), ...}` - only sides actually
        touched (changed or cleared) this session; see the class
        docstring for why an untouched side is left out entirely rather
        than included at its current value."""
        return {side: value for side, value in self._plan.items() if value is not self._UNCHANGED}


class JukeboxOrganizeDialog(QDialog):
    """"Organize card…" (2026-09-07 follow-up; renamed from
    `JukeboxMoveDialog` the same day genre chips were added) - combines the
    original "move to slot #" picker with a second, independent genre
    combo, since re-filing a card's genre chip and moving its board
    position are orthogonal operations (`services.jukebox.set_slot_genre`
    vs. `swap_slots`) that James asked to control from the one card menu
    ("select the location and what genre page a track will be organized
    by").

    The location picker lists every *other* slot on the board that shares
    the genre currently selected just above it, by number and artist,
    rather than asking for a bare number to type in: slot numbers are
    assigned once and never reused or renumbered even after a slot is
    removed (see services/jukebox.py:JukeboxSlot), so an in-use library can
    easily have gaps - a plain 1..N spinner would happily suggest numbers
    that don't exist. `slots` is a list of (slot_number, artist_name,
    genre) triples for every slot on the whole board, not just the current
    page - the genre combo starts on the card's current genre (the page
    the "Organize card…" menu was opened from) but changing it re-filters
    the location list live to that genre's own slots instead, since
    picking a target slot on a board you're not filing this card onto
    would be confusing (2026-09-07, same-day follow-up - James: "I only
    want to be able to see [the currently selected genre] on my slots to
    move dropdown"). The entire point of this half of the dialog over the
    drag-and-drop it originally replaced is that source and target never
    need to be visible together. A leading "Stay in place" entry
    (`itemData=None`) means the location half is optional now that
    genre-only reassignment is a complete action on its own - OK is never
    disabled here, unlike the old move-only dialog, which refused to open
    with nothing else on the board to swap with.
    """

    def __init__(
        self,
        parent,
        current_slot: int,
        slots: list[tuple[int, str, str]],
        current_genre: str,
        genres: Sequence[str] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Organize card")
        self._current_slot = current_slot
        self._slots = slots

        layout = QVBoxLayout(self)
        layout.addWidget(dim_label(f"Slot {current_slot} — genre page"))

        self.genre_combo = QComboBox()
        for genre in genres:
            self.genre_combo.addItem(genre, genre)
        idx = self.genre_combo.findData(current_genre)
        if idx >= 0:
            self.genre_combo.setCurrentIndex(idx)
        self.genre_combo.currentIndexChanged.connect(
            lambda _idx: self._refresh_slot_options(self.genre_combo.currentData())
        )
        layout.addWidget(self.genre_combo)

        layout.addWidget(dim_label("Move to a different slot (optional)"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)
        self._refresh_slot_options(current_genre)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_slot_options(self, genre: str) -> None:
        """Rebuilds the location list from scratch for `genre` - called once
        up front for the card's starting genre, and again every time the
        genre combo above changes, so the two controls never disagree about
        which board a target slot belongs to."""
        self.combo.clear()
        self.combo.addItem("Stay in place", None)
        for number, artist_name, slot_genre in self._slots:
            if number == self._current_slot or slot_genre != genre:
                continue
            label = f"Slot {number} — {artist_name}" if artist_name else f"Slot {number}"
            self.combo.addItem(label, number)

    def target_slot(self) -> Optional[int]:
        return self.combo.currentData()

    def selected_genre(self) -> str:
        return self.genre_combo.currentData()


class _GridHost(QWidget):
    """The plain container `JukeboxView.grid` lives in - overrides
    `minimumSizeHint()` to always report zero rather than the default Qt
    behavior of asking its layout, which would otherwise report however
    much space the *currently placed* fixed-size strips need.

    2026-09-13 fix (James: "the cards don't need to be a forced 3 cols by
    4 rows. It should adjust as the window in the app adjusts" - see the
    module docstring): with `self.grid.setSizeConstraint(QLayout.
    SetNoConstraint)` alone, growing to a wide arrangement (say
    MAX_COLUMNS=6 on a maximized window) still locked the *whole
    application window* at that width and it could never be narrowed
    again - SetNoConstraint only stops the grid layout from forcing this
    widget's own `minimumSize()` up, but `minimumSizeHint()` (a different,
    unaffected method that a *parent* layout consults - here, `BaseView`'s
    own `self._root`) still delegates straight to the grid layout's
    computed minimum regardless of that flag, so the oversized hint
    propagated up through this view and pinned the whole window's minimum
    width right along with it. A plain QWidget has no such override
    available from outside, hence this tiny subclass - the actual "how
    many strips fit" decision already lives entirely in `_rows_that_fit`/
    `_cols_that_fit`, which is what should decide the size, not whatever
    was left over from the last arrangement."""

    def minimumSizeHint(self) -> QSize:  # noqa: D102 - Qt override
        return QSize(0, 0)


class JukeboxView(BaseView):
    title_text = "Jukebox"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        # 2026-09-13 follow-up (James: "close the gap of the space from the
        # left sidebar menu options and the cards. too much empty space"):
        # `BaseView.__init__` sets a 20px left margin on `self._root` that
        # every view shares (library, playlists, charts, settings, etc.),
        # so it isn't something to change globally just because this one
        # page's grid makes the gap more noticeable. Narrowed here, after
        # `super().__init__` already built `self._root`, rather than
        # touching `BaseView`'s own default.
        #
        # First narrowed to 6px, paired with zeroing `self.grid`'s own
        # separate default margin below - James confirmed after a real
        # app restart that the two together got the gap down to just this
        # view's `JukeboxStripWidget` cards' own 4px outer frame (see
        # ui/widgets/jukebox_strip.py's `outer.setContentsMargins`), but
        # still asked for it tighter still. Taken all the way to 0 here -
        # that remaining 4px card frame inset is the only thing left
        # between the rail and the cards now, and it's shared by every
        # side of every card (including the row spacing between columns),
        # not something worth special-casing away just for the leftmost
        # one.
        _left, top, right, bottom = self._root.getContentsMargins()
        self._root.setContentsMargins(0, top, right, bottom)
        self._page = 0
        # settled by the first refresh()/resize measurement - MIN_ROWS/
        # MIN_COLUMNS are just the starting guess so `resizeEvent`'s "did
        # this actually change" check has something to compare against
        # before that.
        self._current_rows = MIN_ROWS
        self._current_cols = MIN_COLUMNS
        # which genre chip is active - see the module docstring's fifth
        # same-day 2026-09-07 follow-up. Starts on DEFAULT_JUKEBOX_GENRE
        # ("Rock") rather than the first chip in the row, so a track filed
        # with no genre picker of its own (Title Details'/Now Playing's
        # one-tap toggle, or a pre-existing card from before this follow-up
        # shipped) shows up on whichever page is already open at launch.
        self._genre = jkb_svc.DEFAULT_JUKEBOX_GENRE

        # drag-and-drop page-flip (2026-09-18 follow-up - see the module
        # docstring): fires _flip_page_during_drag once a dragged card has
        # hovered a page arrow for PAGE_FLIP_HOVER_MS; _page_flip_delta is
        # which direction that hover was over (-1/prev_btn, +1/next_btn),
        # set fresh on every DragEnter in eventFilter below.
        self._page_flip_timer = QTimer(self)
        self._page_flip_timer.setSingleShot(True)
        self._page_flip_timer.timeout.connect(self._flip_page_during_drag)
        self._page_flip_delta = 0

        self.now_playing_label = QLabel("NOW PLAYING — —")
        self.now_playing_label.setObjectName("JukeboxNowPlayingText")
        self.header.addWidget(self.now_playing_label)
        self.header.addStretch(1)

        # the pager (2026-09-07 follow-up, James: "Now at the bottom of the
        # jukebox page there is a page 1 of 3 which takes up a whole row of
        # space. Recommend we move that to the left of the top + Add to
        # jukebox button") - used to be its own centered row under the grid
        # (see refresh()'s page_label.setText below, unchanged); moving it
        # into the header next to "+ Add to jukebox" gives that whole row
        # back to the grid instead.
        # "JukeboxPageArrow", not the shared "RowPageArrow" every other
        # pager in the app uses (cover_grid.py's own horizontal rows) -
        # 2026-09-18 follow-up, James: "let's make those < and > bigger."
        # A dedicated object name (see ui/theme.py's own note on it) keeps
        # this page's arrows resizable on their own without also growing
        # Library's pager, the same "give it its own style" fix
        # GatefoldCoverflow's #CoverflowNavArrow already set the precedent
        # for (2026-09-16 follow-up - see that widget's own docstring).
        self.prev_btn = TouchButton("‹")
        self.prev_btn.setObjectName("JukeboxPageArrow")
        self.prev_btn.clicked.connect(lambda: self._change_page(-1))
        self.page_label = dim_label("")
        self.next_btn = TouchButton("›")
        self.next_btn.setObjectName("JukeboxPageArrow")
        self.next_btn.clicked.connect(lambda: self._change_page(1))
        # 2026-09-18 follow-up: lets a dragged jukebox card flip the page
        # by hovering over either arrow - see the module docstring and
        # eventFilter/_flip_page_during_drag below.
        self.prev_btn.setAcceptDrops(True)
        self.next_btn.setAcceptDrops(True)
        self.prev_btn.installEventFilter(self)
        self.next_btn.installEventFilter(self)
        self.header.addWidget(self.prev_btn)
        self.header.addWidget(self.page_label)
        self.header.addWidget(self.next_btn)
        self.header.addSpacing(20)

        add_btn = TouchButton("+ Add to jukebox", primary=True)
        # 2026-09-07 follow-up - James: "let's also change the red color on
        # the Now Playing, Add to Jukebox and the Play button at the
        # bottom to the same MusicMgr color palett[e]." Overriding the
        # object name TouchButton(primary=True) just set swaps this one
        # button onto ui/theme.py's #PrimaryWarm (brown) rule instead of
        # the ordinary #Primary (red-orange) every other "primary" button
        # in the app still uses - see theme.py's COLORS comment for why
        # the shared accent token itself wasn't retinted.
        add_btn.setObjectName("PrimaryWarm")
        add_btn.clicked.connect(self._open_add_dialog)
        self.header.addWidget(add_btn)

        # genre chips (2026-09-07 follow-up) - exclusive like every other
        # chip row in this app (cover_grid.py's sort chips, Now Playing's
        # "Up next"/"Lyrics"); which board is showing, not a search filter.
        # Built in `JUKEBOX_GENRES` order, which is exactly the left-to-
        # right order James asked for (sixth same-day follow-up) - no
        # separate ordering logic needed here.
        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        self._genre_chips = QButtonGroup(self)
        self._genre_chips.setExclusive(True)
        for genre in jkb_svc.JUKEBOX_GENRES:
            chip = ChipButton(genre)
            # "make the pills not red but the brown color" (sixth same-day
            # follow-up) - overrides ChipButton's own "Chip" object name so
            # only these genre chips pick up ui/theme.py's brown
            # #ChipWarm:checked rule; every other chip row in the app keeps
            # the ordinary red #Chip:checked look.
            chip.setObjectName("ChipWarm")
            chip.setProperty("genre", genre)
            chip.setChecked(genre == self._genre)
            self._genre_chips.addButton(chip)
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        self._genre_chips.buttonClicked.connect(self._on_genre_chip_clicked)
        self.body().addLayout(chip_row)

        # "Rate a track 5 stars..." dropped from this hint 2026-09-07 - the
        # automatic 5-star route it described was removed the same day
        # (see services/jukebox.py's module docstring: James, "I don't
        # like using my 5 stars to get a track on the Jukebox cards") and
        # the leftover copy kept advertising a way in that no longer
        # existed. "+ Add to jukebox" is the only route this empty state
        # needs to point at now.
        self.empty_state = EmptyState(
            "No favorites yet",
            "Tap “+ Add to jukebox” to load one.",
        )
        self.body().addWidget(self.empty_state)

        # _GridHost (see its own docstring) plus SetNoConstraint together
        # stop this container's size from ever being dictated by however
        # many strips happen to be placed in it right now - see _GridHost's
        # docstring for why both are needed. Without them, growing past the
        # old fixed 3 columns (2026-09-13 follow-up - see the module
        # docstring) would permanently widen the whole application window
        # the first time a wide screen fit more of them, with no way back.
        self.grid_host = _GridHost()
        self.grid = QGridLayout(self.grid_host)
        # vertical spacing tightened from 16 to 8 (2026-09-13 follow-up -
        # see the module docstring's note on _rows_that_fit) - James
        # reported the 4th row never showing even fully maximized; logging
        # the real numbers showed _rows_that_fit was measuring correctly
        # all along; a 4th row needs 4 strips + 3 gaps of vertical room,
        # and on his display that came out just 5px short of what
        # maximized actually offers (14px short of what his smallest
        # window offered) - nothing to do with resize timing after all.
        # Horizontal spacing (between columns) stays 16 - only the row gap
        # was the tight dimension.
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(8)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        # 2026-09-13, later same-day follow-up (James, after the left-margin
        # narrowing below still left "too much space between the menu
        # options and the first row of cards" at the smallest window size):
        # `QGridLayout` carries its own default ~9px contents margin on
        # every side, on top of whatever margin `self._root` (this view's
        # outer `QVBoxLayout`, set in `__init__` below) already has - that
        # hidden second margin, not `_root`'s, turned out to be most of
        # what was left of the gap once `_root`'s own left margin had
        # already been narrowed. Zeroed here since the outer margin already
        # controls how far the grid sits from the rail; nothing else in
        # this layout depends on the grid having its own interior margin.
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.body().addWidget(self.grid_host, 1)

        # built MAX_ROWS x MAX_COLUMNS up front regardless of what actually
        # fits right now (2026-09-13 follow-up widened this from MAX_ROWS x
        # 3 - see the module docstring) - a strip is cheap and this avoids
        # ever constructing one on demand mid-resize (see the third
        # same-day 2026-09-07 follow-up for why that matters). None of them
        # are placed into `self.grid` yet - `_arrange_grid()` does that
        # once the actual column count for the current size is known.
        self.strips: list[JukeboxStripWidget] = []
        for _ in range(MAX_ROWS * MAX_COLUMNS):
            strip = JukeboxStripWidget()
            strip.sideActivated.connect(self._on_side_activated)
            strip.fillRequested.connect(self._on_fill_requested)
            strip.editRequested.connect(self._on_edit_requested)
            strip.moveRequested.connect(self._on_organize_requested)
            strip.removeRequested.connect(self._on_remove_requested)
            strip.reorderRequested.connect(self._on_reorder_requested)
            self.strips.append(strip)

        self.ctx.player.trackChanged.connect(self._on_track_changed)

    # -- populating -------------------------------------------------------

    def _rows_that_fit(self) -> int:
        """How many rows of fixed-size strips actually fit in the space
        `grid_host` has right now. `grid_host` carries the only stretch
        factor in this view's top-level layout (see `BaseView.body()`), so
        its height already reflects whatever room is left over after the
        header and pager - independent of how many strips happen to be
        visible inside it at the moment - which is what makes measuring it
        here safe rather than circular. Never returns below MIN_ROWS: a
        short (or not-yet-shown) window still gets a full page of MIN_ROWS
        rows at their fixed size, same as before this follow-up, rather
        than shrinking the cards or the row count any further."""
        strip_height = self.strips[0].sizeHint().height()
        spacing = self.grid.verticalSpacing()
        available = self.grid_host.height()
        for rows in range(MAX_ROWS, MIN_ROWS, -1):
            needed = rows * strip_height + (rows - 1) * spacing
            if available >= needed:
                return rows
        return MIN_ROWS

    def _cols_that_fit(self) -> int:
        """`_rows_that_fit`'s exact same measure-and-walk-down logic against
        `grid_host.width()` instead of height (2026-09-13 follow-up - see
        the module docstring). Never returns below MIN_COLUMNS, for the
        same reason `_rows_that_fit` never returns below MIN_ROWS."""
        strip_width = self.strips[0].sizeHint().width()
        spacing = self.grid.horizontalSpacing()
        available = self.grid_host.width()
        for cols in range(MAX_COLUMNS, MIN_COLUMNS, -1):
            needed = cols * strip_width + (cols - 1) * spacing
            if available >= needed:
                return cols
        return MIN_COLUMNS

    def _arrange_grid(self) -> None:
        """Places however many strips the current page size
        (`_current_rows` x `_current_cols`) needs into `self.grid` at their
        (row, col) for that column count, in reading order. `QGridLayout`
        has to be told every cell explicitly - unlike a strip's fixed size,
        which is set once and never revisited, a strip's *position* is only
        ever correct for the column count it was placed under, so this has
        to run again every time either dimension changes, not just once in
        `__init__` (2026-09-13 follow-up - see the module docstring).
        Removing every strip first (rather than tracking what moved) is the
        same "clear and rebuild" idiom other widgets in this app use for
        their own layout changes (e.g. `VideoTable._rebuild`); with only a
        few dozen strips in the pool this is not worth optimizing further."""
        for strip in self.strips:
            self.grid.removeWidget(strip)
        per_page = self._current_cols * self._current_rows
        for i, strip in enumerate(self.strips[:per_page]):
            self.grid.addWidget(strip, i // self._current_cols, i % self._current_cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Qt may not have finished re-laying-out grid_host to its new size
        # by the time this handler runs - layout updates for a resized
        # widget's children can be posted rather than applied synchronously
        # - so a zero-delay singleShot defers the actual measurement to the
        # next trip through the event loop, after Qt has settled the new
        # geometry. Same "recompute, only act if it changed" idiom
        # ui/widgets/album_tracks.py's TrackListWidget.resizeEvent already
        # uses for width instead of height.
        #
        # 2026-09-13 follow-up - James reported the board settling one row
        # short after maximizing. First suspected as this exact kind of
        # measure-too-early race and "fixed" with a second, later-delayed
        # recheck below - temporary diagnostic logging then showed that
        # guess was wrong: _rows_that_fit was measuring correctly the whole
        # time, and the window genuinely didn't have the ~576px of height a
        # 4th row needs (it had 571, maximized) - a spacing issue, fixed in
        # __init__ by tightening the grid's vertical spacing instead (see
        # its own note there). The second recheck stays anyway - it's a
        # no-op whenever the first one already got it right, so it costs
        # nothing, and some future, much bigger jump could yet need it.
        QTimer.singleShot(0, self._apply_grid_for_current_size)
        QTimer.singleShot(200, self._apply_grid_for_current_size)

    def _apply_grid_for_current_size(self) -> None:
        """Renamed from `_apply_rows_for_current_size` (2026-09-13 follow-up
        - see the module docstring) now that both dimensions respond to a
        resize, not just rows."""
        rows = self._rows_that_fit()
        cols = self._cols_that_fit()
        if rows == self._current_rows and cols == self._current_cols:
            return
        self._current_rows = rows
        self._current_cols = cols
        self.refresh()

    def _on_genre_chip_clicked(self, button) -> None:
        genre = button.property("genre")
        if genre == self._genre:
            return
        self._genre = genre
        self._page = 0
        self.refresh()

    def refresh(self) -> None:
        self._current_rows = self._rows_that_fit()
        self._current_cols = self._cols_that_fit()
        self._arrange_grid()
        per_page = self._current_cols * self._current_rows
        with self.ctx.session() as session:
            total = jkb_svc.slot_count(session, genre=self._genre)
            total_pages = jkb_svc.page_count(session, per_page=per_page, genre=self._genre)
            self._page = max(0, min(self._page, total_pages - 1))
            rows = jkb_svc.list_slot_rows(
                session, page=self._page, per_page=per_page, genre=self._genre
            )
            now_playing_code = self._resolve_now_playing_code(session)

        self.empty_state.setVisible(total == 0)
        self.grid_host.setVisible(total > 0)

        for i, strip in enumerate(self.strips):
            if i < len(rows):
                strip.set_slot(rows[i])
                strip.set_now_playing(_side_if_matching(now_playing_code, rows[i]["slot_number"]))
                strip.show()
            else:
                strip.clear()
                strip.hide()

        self.page_label.setText(f"Page {self._page + 1} of {total_pages}")
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page < total_pages - 1)
        self.now_playing_label.setText(
            f"NOW PLAYING — {now_playing_code}" if now_playing_code else "NOW PLAYING — —"
        )

    def _resolve_now_playing_code(self, session) -> Optional[str]:
        current = self.ctx.player.current
        if current is None:
            return None
        return jkb_svc.find_code_for_track(session, current.track_id)

    def _on_track_changed(self, item) -> None:
        self.refresh()

    def _change_page(self, delta: int) -> None:
        self._page += delta
        self.refresh()

    # -- drag-and-drop page-flip (2026-09-18 follow-up) ----------------------

    def eventFilter(self, obj, event):  # noqa: D102 - Qt override
        """Watches `prev_btn`/`next_btn` (installed on them in `__init__`)
        for a jukebox card hovering during a drag - see the module
        docstring. Every branch here returns True: neither arrow has any
        other event-filtered behavior of its own to fall through to, and
        swallowing a non-jukebox drag's events here (rather than letting
        Qt fall back to `QWidget`'s defaults) keeps an unrelated drag from
        ever landing on a page arrow as if it were a real drop target."""
        if obj is self.prev_btn or obj is self.next_btn:
            etype = event.type()
            if etype == QEvent.DragEnter:
                if event.mimeData().hasFormat(SLOT_MIME_TYPE):
                    event.acceptProposedAction()
                    self._page_flip_delta = -1 if obj is self.prev_btn else 1
                    self._maybe_start_page_flip_timer()
                else:
                    event.ignore()
                return True
            if etype == QEvent.DragMove:
                if event.mimeData().hasFormat(SLOT_MIME_TYPE):
                    event.acceptProposedAction()
                else:
                    event.ignore()
                return True
            if etype == QEvent.DragLeave:
                self._page_flip_timer.stop()
                return True
            if etype == QEvent.Drop:
                # neither arrow is a real drop target for a card - see the
                # module docstring's closing paragraph.
                event.ignore()
                self._page_flip_timer.stop()
                return True
        return super().eventFilter(obj, event)

    def _maybe_start_page_flip_timer(self) -> None:
        can_flip = (
            self._page_flip_delta < 0 and self.prev_btn.isEnabled()
        ) or (self._page_flip_delta > 0 and self.next_btn.isEnabled())
        if can_flip:
            self._page_flip_timer.start(PAGE_FLIP_HOVER_MS)

    def _flip_page_during_drag(self) -> None:
        self._change_page(self._page_flip_delta)
        self._maybe_start_page_flip_timer()

    # -- playing ------------------------------------------------------------

    def _on_side_activated(self, side: str, track_id: int) -> None:
        # play_tracks() must be called while the session is still open -
        # QueueItem.from_track() lazily reads track.release/primary_file,
        # which raises DetachedInstanceError (silently, from inside this
        # Qt slot) once the session that loaded `track` has closed. Fixed
        # 2026-09-06 after James reported a key tap not starting playback.
        with self.ctx.session() as session:
            track = session.get(Track, track_id)
            tracks = [track] if track is not None else []
            if tracks:
                self.ctx.play_tracks(tracks, source="jukebox")

    def _on_fill_requested(self, slot_number: int) -> None:
        """A tap on a one-song card's empty "OPEN" banner (2026-09-13
        follow-up - James: "when I have a jukebox card with one song, I
        would like to click on the card and have the picker allow me to
        select a 2nd song"). Looks the slot up fresh
        (`jkb_svc.get_slot_row`) rather than trusting whatever the strip
        widget last rendered, since the board could have changed between
        that render and the tap (another add, a removal, a re-file to a
        different genre); a slot that's vanished in the meantime is a
        silent no-op - the very next `refresh()` (triggered elsewhere, by
        whatever actually changed the board) will already show its
        current, correct state.

        Opens the same `JukeboxPickerDialog` the page's own "+ Add to
        jukebox" button uses, pre-filled with this card's own artist and
        genre - a search starting from "this card" rather than blank -
        and capped at one pick (`max_picks=1`): unlike a fresh "+ Add to
        jukebox" placement, there's exactly one open side here to fill,
        not two, and a second pick would have nowhere on *this* card to
        land (`services/jukebox.py:place_track` would just start a new
        card for it, defeating the point of tapping this one)."""
        with self.ctx.session() as session:
            row = jkb_svc.get_slot_row(session, slot_number)
        if row is None:
            return
        dialog = JukeboxPickerDialog(
            self,
            self._search_addable_tracks,
            genres=jkb_svc.JUKEBOX_GENRES,
            default_genre=row["genre"],
            initial_artist_query=row.get("artist_name", ""),
            max_picks=1,
            title="Add a second song",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        picks = dialog.selected_picks()
        genre = dialog.selected_genre() or row["genre"]
        if not picks:
            return
        with self.ctx.session() as session:
            for track_id, artist_id in picks:
                jkb_svc.place_track(session, artist_id, track_id, genre=genre)
        self.ctx.notify("Added to the jukebox")
        self.refresh()

    def _on_edit_requested(self, slot_number: int) -> None:
        """"Edit songs…" (2026-09-13 follow-up) - James: "let me right
        click on a jukebox card and allow me to edit the songs on the
        card." Same fresh-lookup-and-silently-bail pattern as
        `_on_fill_requested` above if the slot's vanished by the time this
        fires. `JukeboxEditSongsDialog.plan()` only reports sides that
        were actually touched, so an empty plan (dialog accepted with no
        changes made, or cancelled) is a no-op - nothing to write, no
        `refresh()` needed."""
        with self.ctx.session() as session:
            row = jkb_svc.get_slot_row(session, slot_number)
        if row is None:
            return
        dialog = JukeboxEditSongsDialog(self, row, self._search_addable_tracks)
        if dialog.exec() != QDialog.Accepted:
            return
        plan = dialog.plan()
        if not plan:
            return
        with self.ctx.session() as session:
            for side, (track_id, _title) in plan.items():
                jkb_svc.set_slot_side(session, slot_number, side, track_id)
        self.ctx.notify("Jukebox card updated")
        self.refresh()

    def _on_organize_requested(self, slot_number: int) -> None:
        """"Organize card…" (2026-09-07 follow-up; renamed from
        `_on_move_requested` the same day genre chips were added) - fetches
        every slot on the whole board (with its genre) so the dialog can
        filter the location list down to whichever genre is selected in its
        own genre combo, live, as James asked when he found the list showing
        every board's cards mixed together ("I only want to be able to see
        [the currently selected genre] on my slots to move dropdown") -
        `JukeboxOrganizeDialog._refresh_slot_options` does the actual
        filtering; this just hands it the full unfiltered set once so
        changing the genre combo doesn't need another DB round-trip.
        Swapping board position itself is still a pure `swap_slots`
        operation unaffected by genre. Unlike the old move-only handler,
        there's no "only one slot on the board" early bailout any more -
        reassigning just the genre chip is a complete, valid action even
        when there's nothing else to swap with."""
        with self.ctx.session() as session:
            total = jkb_svc.slot_count(session)
            rows = jkb_svc.list_slot_rows(session, page=0, per_page=max(total, 1))
        slots = [(r["slot_number"], r["artist_name"], r["genre"]) for r in rows]
        current = next((r for r in rows if r["slot_number"] == slot_number), None)
        current_genre = current["genre"] if current is not None else self._genre

        dialog = JukeboxOrganizeDialog(
            self, slot_number, slots, current_genre, genres=jkb_svc.JUKEBOX_GENRES
        )
        if dialog.exec() != QDialog.Accepted:
            return
        target = dialog.target_slot()
        new_genre = dialog.selected_genre()

        with self.ctx.session() as session:
            if new_genre != current_genre:
                jkb_svc.set_slot_genre(session, slot_number, new_genre)
            if target is not None:
                jkb_svc.swap_slots(session, slot_number, target)
        self.refresh()

    def _on_reorder_requested(self, source_slot: int, target_slot: int) -> None:
        """A card dropped onto a different card (2026-09-18 follow-up - see
        the module docstring). Unlike `_on_organize_requested`'s dialog,
        which does a pure two-way `swap_slots`, this calls
        `services.jukebox.reorder_slot`: the dragged card takes the
        target's rank and every card from there on shifts over by one -
        real list reordering, not a swap - which is also what makes a card
        landing on the next page "just happen" rather than needing
        anything special here: `refresh()` re-renders whichever `per_page`
        cards now fall on the current page, in the new slot_number order,
        same as any other change to the board. `reorder_slot` returning
        False (the board changed out from under the drag, or - shouldn't
        happen given a drop only ever lands on a currently-visible, same-
        genre card - a genre mismatch) is a silent no-op, matching every
        other "board changed while a gesture was in flight" guard on this
        view."""
        with self.ctx.session() as session:
            reordered = jkb_svc.reorder_slot(session, source_slot, target_slot)
        if reordered:
            self.refresh()

    def _on_remove_requested(self, slot_number: int) -> None:
        """James, 2026-09-07: "I would like to right click on the jukebox
        card and delete it by removing both songs from that card." A
        confirmation prompt first since this is destructive to the board
        (though not to the library - see services.jukebox.remove_slot's
        docstring), matching the confirm-before-delete pattern playlists
        and folders already use elsewhere in the app."""
        confirm = QMessageBox.question(
            self,
            "Remove from jukebox",
            "Remove this card from the jukebox? Both songs stay in your "
            "library and can be added back later.",
        )
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            jkb_svc.remove_slot(session, slot_number)
        self.refresh()

    # -- manual add ---------------------------------------------------------

    def _open_add_dialog(self) -> None:
        dialog = JukeboxPickerDialog(
            self,
            self._search_addable_tracks,
            genres=jkb_svc.JUKEBOX_GENRES,
            default_genre=self._genre,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        picks = dialog.selected_picks()
        genre = dialog.selected_genre() or jkb_svc.DEFAULT_JUKEBOX_GENRE
        if not picks:
            return
        with self.ctx.session() as session:
            for track_id, artist_id in picks:
                jkb_svc.place_track(session, artist_id, track_id, genre=genre)
        self.ctx.notify(
            f"Added {len(picks)} song{'s' if len(picks) != 1 else ''} to the jukebox"
        )
        self.refresh()

    def _search_addable_tracks(self, artist_query: str, track_query: str) -> list[dict]:
        """`JukeboxPickerDialog`'s `search_tracks` callback - straight to
        `services/jukebox.py:search_addable_tracks`, debounced by the
        dialog's own `_search_timer` (2026-09-18 follow-up) rather than
        firing on every keystroke, and still nothing like the old
        artist-first dialog's "load one artist's whole discography up
        front" approach."""
        with self.ctx.session() as session:
            return jkb_svc.search_addable_tracks(session, artist_query, track_query)


def _side_if_matching(code: Optional[str], slot_number: int) -> Optional[str]:
    """"A7" matches slot 7's A side; anything else (a different slot, or no
    code at all because nothing playing is on the board) matches neither."""
    if not code or code[1:] != str(slot_number):
        return None
    return code[0]
