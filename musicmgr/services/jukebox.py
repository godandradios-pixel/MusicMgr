"""The jukebox: a persistent 45-selector favorites board (2026-09-06).

James: "I would like the user to be able to select an artist and add 2 songs
per title strip. This becomes a favorites capability. Any record with 5 star
rating gets added to a title strip." One `JukeboxSlot` (db/models.py) is one
numbered physical "record" behind the glass - a title strip with an A side
and a B side, both by the same artist, exactly like a real 45.

Two things load a track onto the board, and both funnel through the one
`place_track` below rather than duplicating its slot-filling logic:

- **From the artist picker**: `ui/views/jukebox.py`'s "Add to jukebox"
  dialog lets James pick an artist and up to two of their songs (rating
  irrelevant) and calls it once per chosen song.
- **From Title Details' own Jukebox column**: a per-track on/off toggle
  (`services/library.py:toggle_jukebox_membership`) next to that table's
  Rating column - James can flip a single track on or off the board
  without going through the artist picker at all.

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

from ..db.models import JukeboxSlot, Track

#: how many title strips make up one page of the selector - a small enough
#: number that a touch panel can show a full page of strips at readable size
#: without its own scrolling, matching how a real jukebox's mechanical
#: carousel pages through a fixed number of records at a time. 9 (a full
#: 3x3 page - see ui/views/jukebox.py:GRID_COLUMNS) since 2026-09-06.
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

#: what a newly-placed track is filed under when nothing more specific is
#: chosen - `services/library.py:toggle_jukebox_membership` (Title Details'
#: and Now Playing's one-tap jukebox toggle) has no genre picker of its
#: own, so a track added that way lands here and can be refiled later from
#: the Jukebox page's own "Organize card…" dialog (see ui/views/jukebox.py).
#: Must stay in sync with the literal default on `db.models.JukeboxSlot.genre`
#: - that module can't import this constant without a circular import.
DEFAULT_JUKEBOX_GENRE = "Rock"


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
    highest = session.scalar(select(func.max(JukeboxSlot.slot_number))) or 0
    return highest + 1


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
