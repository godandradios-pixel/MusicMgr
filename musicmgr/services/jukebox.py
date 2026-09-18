"""The jukebox: a persistent 45-selector favorites board (2026-09-06).

James: "I would like the user to be able to select an artist and add 2 songs
per title strip. This becomes a favorites capability. Any record with 5 star
rating gets added to a title strip." One `JukeboxSlot` (db/models.py) is one
numbered physical "record" behind the glass - a title strip with an A side
and a B side, both by the same artist, exactly like a real 45.

Three places load a track onto the board, and all three funnel through the
one `place_track` below rather than duplicating its slot-filling logic -
since a 2026-09-13 follow-up, all three also share the same
`ui/views/jukebox.py:JukeboxPickerDialog` for actually choosing what gets
added and to which genre:

- **The Jukebox page's own "+ Add to jukebox" button** opens the picker
  directly - James searches by track and/or artist (`search_addable_tracks`
  below), picks up to two songs and a genre, and it calls `place_track`
  once per chosen song.
- **Title Details' own Jukebox column** and **Now Playing's Jukebox
  toggle**: turning either ON opens the same picker dialog, pre-filled and
  pre-checked for the specific track that was tapped
  (`ui/views/library.py:LibraryView._on_jukebox_toggle_requested`,
  `ui/views/nowplaying.py:NowPlayingView._on_jukebox_toggled`) - James can
  still just pick a genre and tap OK without searching for anything.
  Turning either OFF is a direct, immediate `remove_track` call with no
  dialog at all.

Until the 2026-09-13 follow-up above, the two toggles didn't open a
dialog when turning on at all - they called
`services/library.py:toggle_jukebox_membership` (since removed), which
placed the track under `DEFAULT_JUKEBOX_GENRE` unconditionally. James:
"I want a better UI for the Jukebox 'picker'... The problem with the
tracks is there is no way to select which genre the track should be on."

Until a 2026-09-07 follow-up, a third route existed too:
`services/library.py:set_track_rating` used to call `place_track`
automatically the moment a track crossed *up* to a 5-star rating (and
`remove_track` the moment one crossed back down), unprompted. James:
"I don't like using my 5 stars to get a track on the Jukebox cards" -
rating and jukebox membership are now fully independent; a track's star
rating never touches the board either way any more, in either direction.
Existing slots that were originally loaded that way are untouched by this
change - only future rating edits stopped moving them.

Whichever route a song arrives by, `place_track` always tries to fill an
open A or B side on one of that artist's *existing* slots before starting a
new numbered strip - so two 5-star songs by the same artist end up paired
on one physical record instead of on two separate ones, matching a real
jukebox where you'd load B-sides onto existing 45s before reaching for a
fresh one. Slot numbers are assigned once, in order, and never reused or
renumbered even after a slot empties out and is removed.

2026-09-13 fix: that "never reused" promise was only actually true for
non-highest slots. `_next_slot_number` used to compute
`max(existing slot_number) + 1` fresh each time, so removing the
*highest*-numbered card (deleting the newest slot, or clearing a
single-slot board down to nothing) silently freed its number back up for
the very next placement - tests written against the documented contract
caught the gap (see tests/test_jukebox.py). `_next_slot_number` now reads
and advances a persisted high-water mark in the generic `Setting` table
(key `_NEXT_SLOT_NUMBER_KEY`) instead of deriving it from whichever rows
happen to still exist, so a number stays retired for good once handed
out, matching what this docstring always promised. Existing installs seed
that counter from the current on-disk max the first time it's read, so
numbers already in use are never disturbed - only future allocations are
protected from here on.

2026-09-07 follow-up: James asked for genre chips on the Jukebox page
("I would like the jukebox page to have a chip of 5 genres: Classic Rock,
Country, Pop, Hairbands, Rock. Then have the pages of the cards where you
can select the location and what genre page a track will be organized
by"). Every slot now carries a `genre` tag (`JukeboxSlot.genre`) - purely a
board-organization label, not derived from the track's own tagged
Genre(s), since "Hairbands" doesn't correspond to real genre metadata any
file actually carries. `place_track` files a new slot under a given genre
(defaulting to `DEFAULT_JUKEBOX_GENRE` for the two callers above, neither
of which has a genre picker of its own) and only pairs a song onto an
*existing* slot when that slot is already on the same genre board - an
artist's Country single won't silently fill a B-side slot on their
existing Classic-Rock-tagged 45. `set_slot_genre` lets a card already on
the board be re-filed under a different chip independently of its board
position (`swap_slots` handles position; the two are orthogonal and
exposed as two independent controls in `ui/views/jukebox.py`'s "Organize
card…" dialog).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import JukeboxSlot, Release, Setting, Track
from .matching import normalize

#: how many title strips make up one page of the selector - a small enough
#: number that a touch panel can show a full page of strips at readable size
#: without its own scrolling, matching how a real jukebox's mechanical
#: carousel pages through a fixed number of records at a time. 9 (a full
#: 3x3 page) since 2026-09-06. JukeboxView itself no longer has a fixed
#: page size (2026-09-13 follow-up - see its own module docstring: page
#: size now adapts to the actual window, via
#: `JukeboxView._rows_that_fit`/`_cols_that_fit`), so this is only what any
#: *other* caller gets by default.
SLOTS_PER_PAGE = 9

#: the fixed board categories James wants as chips on the Jukebox page
#: (2026-09-07 follow-up: "I would like the jukebox page to have a chip of
#: 5 genres: Classic Rock, Country, Pop, Hairbands, Rock") - a card's genre
#: is purely this board-organization tag (`JukeboxSlot.genre`), not derived
#: from the track's own tagged Genre(s) - "Hairbands" doesn't correspond to
#: real genre metadata any file actually carries. Grew to seven the same
#: day (James: "add 2 more chips: Christian, 80's. Then reorder them...
#: Country, Christian, Classic Rock, Rock, 80's, Hairbands, Pop") - the
#: tuple's order here is exactly the left-to-right chip order on the page
#: (`ui/views/jukebox.py` builds one `ChipButton` per entry, in order) and
#: the order every genre combo box lists them in too. Grew to ten on
#: 2026-09-08 (James: "add 3 more genres to the jukebox: Metal, R&B,
#: Hip/Hop") - appended, not interleaved into the earlier reorder, since
#: this follow-up asked only to add chips, not to reorder existing ones.
JUKEBOX_GENRES = (
    "Country", "Christian", "Classic Rock", "Rock", "80's", "Hairbands", "Pop",
    "Metal", "R&B", "Hip/Hop",
)

#: what a newly-placed track is filed under when the picker dialog's own
#: `genre_combo` doesn't resolve to anything (an empty `genres` sequence,
#: or - defensively - a `None` from `currentData()`); also
#: `JukeboxPickerDialog`'s own preselected default genre whenever the
#: caller doesn't hand it a more specific one to preselect. Until a
#: 2026-09-13 follow-up, this was also the *unconditional* genre for a
#: track added via Title Details' or Now Playing's jukebox toggle - see
#: the module docstring - since neither had a genre picker of its own back
#: then; both now open the real picker dialog instead, so a track added
#: through them can land anywhere, same as one added from the Jukebox
#: page's own button. A track that does land here can always be refiled
#: later from the Jukebox page's own "Organize card…" dialog (see
#: ui/views/jukebox.py). Must stay in sync with the literal default on
#: `db.models.JukeboxSlot.genre` - that module can't import this constant
#: without a circular import.
DEFAULT_JUKEBOX_GENRE = "Rock"

#: `Setting.key` holding the next never-before-used slot number - see the
#: 2026-09-13 fix note in the module docstring. A plain int stored as text,
#: like every other row in the generic `settings` table.
_NEXT_SLOT_NUMBER_KEY = "jukebox_next_slot_number"


def _find_slot_for_track(session: Session, track_id: int) -> Optional[JukeboxSlot]:
    return session.scalar(
        select(JukeboxSlot).where(
            or_(
                JukeboxSlot.side_a_track_id == track_id,
                JukeboxSlot.side_b_track_id == track_id,
            )
        )
    )


def _next_slot_number(session: Session) -> int:
    """The next never-before-used slot number, advancing a persisted
    high-water mark rather than `max(existing slot_number) + 1` - see the
    2026-09-13 fix note in the module docstring for why that used to let a
    number come back into circulation once the highest slot was removed.

    On first use - no counter row yet, e.g. an existing install upgrading
    into this fix - the counter is seeded from whatever the highest
    slot_number *currently on the board* is, so numbers already assigned
    are left exactly where they are; only allocations from here on are
    protected."""
    setting = session.get(Setting, _NEXT_SLOT_NUMBER_KEY)
    if setting is None:
        highest = session.scalar(select(func.max(JukeboxSlot.slot_number))) or 0
        setting = Setting(key=_NEXT_SLOT_NUMBER_KEY, value=str(highest + 1))
        session.add(setting)
    next_number = int(setting.value)
    setting.value = str(next_number + 1)
    session.flush()
    return next_number


def place_track(
    session: Session,
    artist_id: int,
    track_id: int,
    genre: str = DEFAULT_JUKEBOX_GENRE,
) -> JukeboxSlot:
    """Load one song onto the board - see the module docstring for the two
    callers. Idempotent: a track already sitting on some slot (either side)
    is left exactly where it is rather than duplicated onto a second one, so
    both callers can re-run freely - a rating re-saved as 5, the same song
    picked twice in the picker - without piling up duplicate strips (note
    this means `genre` is ignored for a track that's already placed; use
    `set_slot_genre` to re-file an existing card instead).

    `genre` scopes both halves of the "existing slot" search: an open A/B
    side is only reused on one of this artist's slots that's already tagged
    with this same genre, and a brand-new slot is created under it. This
    keeps each genre's board self-contained - pairing two songs by the same
    artist only happens within the same genre page, not across boards."""
    existing = _find_slot_for_track(session, track_id)
    if existing is not None:
        return existing

    open_slot = session.scalar(
        select(JukeboxSlot)
        .where(
            JukeboxSlot.artist_id == artist_id,
            JukeboxSlot.genre == genre,
            or_(
                JukeboxSlot.side_a_track_id.is_(None),
                JukeboxSlot.side_b_track_id.is_(None),
            ),
        )
        .order_by(JukeboxSlot.slot_number)
    )
    if open_slot is not None:
        if open_slot.side_a_track_id is None:
            open_slot.side_a_track_id = track_id
        else:
            open_slot.side_b_track_id = track_id
        session.flush()
        return open_slot

    slot = JukeboxSlot(
        slot_number=_next_slot_number(session),
        artist_id=artist_id,
        side_a_track_id=track_id,
        genre=genre,
    )
    session.add(slot)
    session.flush()
    return slot


def set_slot_genre(session: Session, slot_number: int, genre: str) -> bool:
    """Re-files an existing card under a different genre chip (2026-09-07
    follow-up "Organize card…" dialog, ui/views/jukebox.py) - purely
    relabels `JukeboxSlot.genre`; the slot keeps its same number (so its
    "A7"/"B7" code never changes) and both sides stay exactly as loaded.
    Independent of `swap_slots` - moving board position and changing genre
    are two separate controls in the same dialog, not entangled together.

    Returns False as a no-op if the slot number doesn't exist any more
    (e.g. the board changed while the dialog was open), True once committed
    via `session.flush()`."""
    slot = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == slot_number)
    )
    if slot is None:
        return False
    slot.genre = genre
    session.flush()
    return True


def swap_slots(session: Session, slot_number_a: int, slot_number_b: int) -> bool:
    """Swap the board positions of two title strips - the manual reorder
    James asked for (2026-09-07 follow-up: "is there a way I can move
    these jukebox cards around?"). Called from `ui/views/jukebox.py`'s
    "Organize card…" dialog (`JukeboxOrganizeDialog`/`_on_organize_requested`,
    renamed the same day the genre chips were added, when the dialog picked
    up a second, independent genre control alongside this one) -
    an earlier drag-and-drop version called this same function but got
    pulled the same day once James found he couldn't drag a card across a
    page boundary; the function itself didn't need to change; only what
    calls it did. Both sides of a strip move together as one unit, the
    same way picking up a real 45 moves its A and B side at once - there's
    no way to reorder just one side.

    `slot_number` carries a UNIQUE constraint (see JukeboxSlot's
    docstring), so the two rows can't simply be assigned each other's
    number directly: SQLite checks uniqueness immediately per statement,
    not at transaction end, so setting the first row to the second row's
    number would collide with that still-unswapped second row. Routing
    through a temporary number - always negative, since real slot numbers
    only ever count up from 1 - sidesteps that without touching anything
    else about either slot (artist, tracks, created_at all stay put).

    Returns False as a no-op for the same slot picked twice or if either
    slot number doesn't exist (e.g. the board changed while the picker was
    open); True once the swap is committed via `session.flush()`.
    """
    if slot_number_a == slot_number_b:
        return False
    slot_a = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == slot_number_a)
    )
    slot_b = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == slot_number_b)
    )
    if slot_a is None or slot_b is None:
        return False
    slot_a.slot_number = -1
    session.flush()
    slot_b.slot_number = slot_number_a
    session.flush()
    slot_a.slot_number = slot_number_b
    session.flush()
    return True


def reorder_slot(session: Session, source_slot_number: int, target_slot_number: int) -> bool:
    """Drag-and-drop reordering (2026-09-18 follow-up, `ui/widgets/
    jukebox_strip.py:reorderRequested`) - unlike `swap_slots` above, which
    trades exactly two cards' positions and leaves every other slot
    untouched (still what "Organize card…" does, unchanged), this inserts
    the dragged card at the target's rank and shifts every card from there
    on over by one - the same "list reordering" an ordinary playlist drag
    does, not an operator physically swapping two 45s. James, after trying
    the swap-based version of the drag: "If the page is full, it doesn't
    seem to allow the drop and have it automatically shift the last one to
    the next page." A card landing on the next page isn't anything this
    function (or the view) arranges specially - `list_slots`/
    `list_slot_rows` already render exactly `per_page` cards per page in
    slot_number order, so whichever card ends up ranked past the end of a
    page is already on the next page the moment its rank here changes.

    Reassigns `slot_number` *by rank*, not by inventing new numbers: both
    slots have to share a genre (a drag today only ever happens between
    cards visible on the same page, so this should never actually fire
    cross-genre in practice - checked here rather than assumed, same
    defensive spirit as everything else in this module that can no-op
    rather than trust the caller). Every slot on that genre's board is
    pulled in ascending slot_number order; the dragged slot is spliced out
    and reinserted at the target's *original* rank (`target_rank`, read
    before the splice - see the comment on it below for why the post-
    splice rank would give the wrong answer half the time), the same
    "pop from index i, insert at index j" most drag-reorder lists use.
    That single rule ends up direction-dependent in a way that reads as
    natural once dropped, even though it isn't spelled out anywhere on the
    card itself: dragging a card *forward* past its target lands it
    immediately *after* the target (everything that was between them
    shifts back to fill the gap it left), while dragging one *backward*
    onto a target lands it immediately *before* the target. Dropping a
    card on its immediate neighbor - either direction - is a plain
    two-item swap, same as `swap_slots` would give: see
    `TestReorderSlot.test_moving_a_card_onto_its_immediate_next_neighbor_
    is_the_same_as_a_swap`/`..._previous_neighbor_...` in
    tests/test_jukebox.py. The *same set* of
    already-active numbers is then redealt across the new rank order - no
    number is invented and no retired number (one freed by a slot that
    was fully removed - see `_next_slot_number`) is ever reused; only
    numbers already live on this genre's board change hands, the same
    thing a plain two-way `swap_slots` already does, just generalized
    past two.

    Routed through temporary negative placeholders first, the same trick
    `swap_slots` uses and for the same reason: SQLite checks the unique
    constraint on `slot_number` per statement, not at transaction end, so
    writing final numbers directly could collide with a row that hasn't
    been reassigned yet. Every slot on the board gets touched here rather
    than only the ones whose number actually changes - genre boards are
    small (the same "fetch everything, filter in memory" trade-off
    `_on_organize_requested` already makes for the whole board), so the
    extra writes aren't worth the bookkeeping to avoid.

    Returns False as a no-op for the same slot picked twice, either slot
    number no longer existing, or the two slots not sharing a genre; True
    once committed via `session.flush()`."""
    if source_slot_number == target_slot_number:
        return False
    source = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == source_slot_number)
    )
    target = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == target_slot_number)
    )
    if source is None or target is None or source.genre != target.genre:
        return False

    ordered = list(
        session.scalars(
            select(JukeboxSlot)
            .where(JukeboxSlot.genre == source.genre)
            .order_by(JukeboxSlot.slot_number)
        )
    )
    numbers = [slot.slot_number for slot in ordered]  # already ascending

    # target's rank *before* source is removed - splicing source back in at
    # this same index is standard "move array item to index N" semantics:
    # dragging forward (source was earlier than target) lands source right
    # after target, since removing source shifts everything from target on
    # back by one before the insert; dragging backward (source was later)
    # lands source right before target, since target's own index isn't
    # touched by removing something that came after it. Using the
    # *post-removal* index of target here instead (i.e. looking it up in
    # `without_source`) would only get the backward case right - the
    # forward case would insert source one slot too early every time,
    # which is exactly the bug that made "drop card A onto its very next
    # neighbor B" a silent no-op during development (A was already
    # sitting where that math would place it).
    target_rank = ordered.index(target)
    without_source = [slot for slot in ordered if slot is not source]
    without_source.insert(target_rank, source)
    new_order = without_source

    for i, slot in enumerate(new_order):
        slot.slot_number = -(i + 1)
    session.flush()
    for slot, number in zip(new_order, numbers):
        slot.slot_number = number
    session.flush()
    return True


def remove_track(session: Session, track_id: int) -> None:
    """Pull a track off whichever slot it's on - a 5-star rating lifted, or
    the track deleted outright. Clears just that side; once both sides are
    empty the whole strip is deleted rather than left behind as a
    permanently blank slot number, the same way an operator pulls the empty
    sleeve rather than leaving it loaded with nothing playable."""
    slot = _find_slot_for_track(session, track_id)
    if slot is None:
        return
    if slot.side_a_track_id == track_id:
        slot.side_a_track_id = None
    if slot.side_b_track_id == track_id:
        slot.side_b_track_id = None
    if slot.side_a_track_id is None and slot.side_b_track_id is None:
        session.delete(slot)
    session.flush()


def set_slot_side(
    session: Session, slot_number: int, side: str, track_id: Optional[int]
) -> bool:
    """"Edit songs…" (2026-09-13 follow-up) - James: "let me right click on
    a jukebox card and allow me to edit the songs on the card." Overwrites
    one side of an *existing* slot with a new track (or with None, to clear
    that side outright) - unlike `place_track`, which only ever fills an
    open side and never disturbs one that's already loaded.

    Returns False as a no-op if `slot_number` doesn't exist any more (the
    board changed while the edit dialog was open); True once the change is
    committed via `session.flush()`, including the no-op-but-successful
    case where `track_id` already matches what's currently on that side.

    Three things make this trickier than a plain attribute assignment:

    * Reassigning a side to the exact track it already holds is a no-op -
      short-circuit before touching anything else, since routing it through
      the dedup logic below would incorrectly clear the very value being
      "set".
    * `track_id` might already be loaded somewhere else on the board (a
      different slot, or - see below - this same slot's *other* side).
      Every track can only ever occupy one code, so the old location has to
      be cleared first. When that old location is this same slot's other
      side, it's cleared directly rather than via `remove_track`: calling
      `remove_track` first would see both of this slot's sides still
      holding old values (the side being set here hasn't been overwritten
      yet), find neither empty, and leave the slot alone - which is fine -
      but if it were instead a slot where clearing that other side would
      leave both sides empty, `remove_track` would delete the row itself
      before this function gets a chance to write the new value back onto
      the now-deleted ORM object. Clearing the field directly sidesteps
      that ordering hazard entirely, for both cases at once.
    * After the new value is written, this side and the other side might
      both now be empty (`track_id=None` clearing the last remaining
      song) - same "delete the empty strip" rule `remove_track` already
      follows.

    Calling this twice in a row - once per side - is how the view
    implements an A/B "swap": the first call moves side A's track onto
    itself as a same-slot-other-side dedup of what's about to land on side
    B, and the second call writes the rest through. See
    `TestSetSlotSide.test_swap_sides` for the full trace."""
    slot = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == slot_number)
    )
    if slot is None:
        return False
    current = slot.side_a_track_id if side == "A" else slot.side_b_track_id
    if track_id == current:
        return True
    if track_id is not None:
        existing = _find_slot_for_track(session, track_id)
        if existing is not None:
            if existing.slot_number == slot_number:
                if existing.side_a_track_id == track_id:
                    existing.side_a_track_id = None
                else:
                    existing.side_b_track_id = None
            else:
                remove_track(session, track_id)
    if side == "A":
        slot.side_a_track_id = track_id
    else:
        slot.side_b_track_id = track_id
    if slot.side_a_track_id is None and slot.side_b_track_id is None:
        session.delete(slot)
    session.flush()
    return True


def remove_slot(session: Session, slot_number: int) -> bool:
    """Take a whole title strip off the board - both sides at once,
    whatever's loaded on either one. James, 2026-09-07: "I would like to
    right click on the jukebox card and delete it by removing both songs
    from that card." Unlike `remove_track`, this doesn't care which tracks
    (if any) are actually loaded - it always deletes the `JukeboxSlot` row
    outright, the same end state `remove_track` reaches on its own once
    both sides happen to end up empty. Only removes the slot's membership
    on the board; the underlying tracks and their files are untouched; a
    5-star rating on either song, if it has one, is untouched too (a
    manually-removed card doesn't silently re-add itself on the next
    rating change, but nor does removing it here strip the rating - the
    two are independent, same as they already were for `remove_track`).

    Returns False as a no-op if the slot number doesn't exist any more
    (e.g. the board changed while a confirmation dialog was open), True
    once the removal is committed via `session.flush()`."""
    slot = session.scalar(
        select(JukeboxSlot).where(JukeboxSlot.slot_number == slot_number)
    )
    if slot is None:
        return False
    session.delete(slot)
    session.flush()
    return True


def find_code_for_track(session: Session, track_id: int) -> Optional[str]:
    """The "A7"/"B7"-style code for wherever a track is loaded on the board,
    or None if it isn't on the jukebox at all - what JukeboxView's "NOW
    PLAYING" display reads on every track change, whether or not the
    jukebox page happens to be the one on screen at the time."""
    slot = _find_slot_for_track(session, track_id)
    if slot is None:
        return None
    side = "A" if slot.side_a_track_id == track_id else "B"
    return f"{side}{slot.slot_number}"


def board_track_ids(session: Session) -> set[int]:
    """Every track_id currently loaded on any slot, either side - what Title
    Details' own Jukebox column batch-checks membership against
    (`services/library.py:list_track_details`) rather than a per-row query.
    Same "one query up front, not N" rule that table already had to learn
    once before, the hard way, for row height/genre joins at real-library
    scale (tens of thousands of rows) - not repeating that mistake here."""
    rows = session.execute(
        select(JukeboxSlot.side_a_track_id, JukeboxSlot.side_b_track_id)
    )
    ids: set[int] = set()
    for a, b in rows:
        if a is not None:
            ids.add(a)
        if b is not None:
            ids.add(b)
    return ids


def slot_count(session: Session, genre: Optional[str] = None) -> int:
    """Total slots on the board, or just the ones tagged with `genre` when
    given - what the Jukebox page's pager counts against once a genre chip
    is selected (2026-09-07 follow-up)."""
    stmt = select(func.count(JukeboxSlot.id))
    if genre is not None:
        stmt = stmt.where(JukeboxSlot.genre == genre)
    return session.scalar(stmt) or 0


def page_count(
    session: Session, per_page: int = SLOTS_PER_PAGE, genre: Optional[str] = None
) -> int:
    """Always at least 1, so an empty board (or an empty genre page) still
    shows one (empty) page rather than the pager having nothing to display
    at all."""
    total = slot_count(session, genre=genre)
    return max(1, -(-total // per_page))  # ceil division


def list_slots(
    session: Session,
    page: int = 0,
    per_page: int = SLOTS_PER_PAGE,
    genre: Optional[str] = None,
) -> list[JukeboxSlot]:
    """One page of slots in slot-number order - the order they were first
    loaded, matching how a real jukebox's carousel pages through its records
    in position order rather than shuffling them. Restricted to slots tagged
    with `genre` when given, so each genre chip pages through only its own
    cards (2026-09-07 follow-up)."""
    stmt = (
        select(JukeboxSlot)
        .options(
            selectinload(JukeboxSlot.artist),
            selectinload(JukeboxSlot.side_a_track).selectinload(Track.release),
            selectinload(JukeboxSlot.side_b_track).selectinload(Track.release),
        )
        .order_by(JukeboxSlot.slot_number)
        .offset(page * per_page)
        .limit(per_page)
    )
    if genre is not None:
        stmt = stmt.where(JukeboxSlot.genre == genre)
    return list(session.scalars(stmt))


def slot_to_dict(slot: JukeboxSlot) -> dict:
    """Plain-dict projection for the UI layer - JukeboxStripWidget and its
    tests only ever need these fields, never a live ORM object (the same
    reasoning `list_track_details` gives for its own dict rows)."""

    def side(track: Optional[Track]) -> Optional[dict]:
        if track is None:
            return None
        return {
            "track_id": track.id,
            "title": track.title,
            "duration_ms": track.duration_ms,
            "cover_path": track.release.cover_path if track.release else None,
        }

    return {
        "slot_number": slot.slot_number,
        "artist_id": slot.artist_id,
        "artist_name": slot.artist.name if slot.artist else "",
        "genre": slot.genre,
        "side_a": side(slot.side_a_track),
        "side_b": side(slot.side_b_track),
    }


def list_slot_rows(
    session: Session,
    page: int = 0,
    per_page: int = SLOTS_PER_PAGE,
    genre: Optional[str] = None,
) -> list[dict]:
    """`list_slots` plus `slot_to_dict` in one call - what JukeboxView
    actually wants for a page. `genre` filters same as `list_slots`."""
    return [slot_to_dict(s) for s in list_slots(session, page, per_page, genre=genre)]


def get_slot_row(session: Session, slot_number: int) -> Optional[dict]:
    """One slot's `slot_to_dict` row, by number - not a page (2026-09-13
    follow-up, `ui/views/jukebox.py`'s `_on_fill_requested`, wired to
    `ui/widgets/jukebox_strip.py`'s new `fillRequested` signal). Tapping a
    one-song card's empty "OPEN" banner needs this exact card's own
    artist and genre fresh from the database - not whatever `refresh()`
    last rendered into the strip - since the board could have changed
    between that render and the tap (another add, a removal, a genre
    re-file). Returns None if the slot number no longer exists, same
    "board changed underneath the click" case `set_slot_genre`/
    `swap_slots` already guard against with their own `bool` returns."""
    slot = session.scalar(
        select(JukeboxSlot)
        .options(
            selectinload(JukeboxSlot.artist),
            selectinload(JukeboxSlot.side_a_track).selectinload(Track.release),
            selectinload(JukeboxSlot.side_b_track).selectinload(Track.release),
        )
        .where(JukeboxSlot.slot_number == slot_number)
    )
    return slot_to_dict(slot) if slot is not None else None


def search_addable_tracks(
    session: Session,
    artist_query: str = "",
    track_query: str = "",
    limit: int = 50,
) -> list[dict]:
    """Search-by-artist-and-track for the Jukebox picker dialog
    (2026-09-13 follow-up, `ui/views/jukebox.py`'s `JukeboxPickerDialog`).
    James's original ask ("I want a standard UI. [a] picker box that
    allows you to select a genre and search by track and/or artist")
    shipped as one combined search box matching on either; a same-day
    follow-up split it into the two separate `artist_query`/`track_query`
    parameters here, ANDed together rather than OR'd - James: "I'll
    usually select an artist first, and then want to search within that
    artist for a song." Either can be blank on its own (an artist-only or
    track-only search still narrows the board), but both blank returns
    nothing rather than the whole library. Matching itself is deliberately
    kept in sync with `services/library.py:search`'s `tracks` branch
    (`title_key.like` / `artist_display.ilike`) rather than reinventing
    the matching rule a second time.

    Every match is resolved up front to the artist a jukebox slot would
    actually file it under - the same `Release.album_artist_id` rule
    `services/library.py:album_artist_id_for_track` uses (inlined here
    rather than imported, since `library.py` already imports this module
    the other way round). A match with no resolvable album artist - a
    various-artists compilation track, or a release that predates the
    Album Artist column and hasn't been backfilled - is dropped rather
    than shown and then failing to add when picked.

    Returns plain dicts, not ORM rows (the dialog only ever reads these
    four fields, and the session may close before the dialog does):
    `track_id`, `title`, `artist_name` (the resolved *album* artist's
    name, which is what a jukebox card will actually show - not the
    track's own possibly-different `artist_display`), `artist_id`, and
    `album` (the release title, to tell apart same-named tracks in the
    results list). Capped at `limit` *results* - matches with no
    resolvable artist don't count against it, so more rows than `limit`
    may be scanned to fill it."""
    artist_query = artist_query.strip()
    track_query = track_query.strip()
    if not artist_query and not track_query:
        return []
    stmt = (
        select(Track)
        .options(selectinload(Track.release).selectinload(Release.album_artist))
        .order_by(Track.title)
        .limit(limit * 3)
    )
    if artist_query:
        stmt = stmt.where(Track.artist_display.ilike(f"%{artist_query}%"))
    if track_query:
        stmt = stmt.where(Track.title_key.like(f"%{normalize(track_query)}%"))
    candidates = session.scalars(stmt).unique()

    results: list[dict] = []
    for track in candidates:
        if len(results) >= limit:
            break
        release = track.release
        artist_id = release.album_artist_id if release is not None else None
        if artist_id is None:
            continue
        artist = release.album_artist
        results.append(
            {
                "track_id": track.id,
                "title": track.title,
                "artist_name": artist.name if artist is not None else "",
                "artist_id": artist_id,
                "album": release.title if release is not None else "",
            }
        )
    return results
