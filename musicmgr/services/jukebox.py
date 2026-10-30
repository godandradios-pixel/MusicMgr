"""The jukebox: a persistent 45-selector favorites board.

One `JukeboxSlot` (db/models.py) is one numbered physical "record" behind
the glass - a title strip with an A side and a B side, both by the same
artist, exactly like a real 45.

Three places load a track onto the board, all funnelling through
`place_track` below and all using `ui/views/jukebox.py:JukeboxPickerDialog`
to choose what gets added and to which genre:

- **The Jukebox page's "+ Add to jukebox" button** opens the picker
  directly: search by track and/or artist (`search_addable_tracks`), pick
  up to two songs and a genre, and it calls `place_track` once per song.
- **Title Details' Jukebox column** and **Now Playing's Jukebox toggle**:
  turning either ON opens the same picker, pre-filled for the tapped track
  (`ui/views/library.py:LibraryView._on_jukebox_toggle_requested`,
  `ui/views/nowplaying.py:NowPlayingView._on_jukebox_toggled`). Turning
  either OFF is a direct `remove_track` call with no dialog.

Star ratings and jukebox membership are fully independent: a rating change
never adds or removes a card.

`place_track` always tries to fill an open A or B side on one of that
artist's *existing* slots (on the same genre board) before starting a new
numbered strip, so two songs by the same artist pair up on one record.
Slot numbers are assigned once, in order, and never reused or renumbered
even after a slot is removed: `_next_slot_number` advances a persisted
high-water mark in the generic `Setting` table (`_NEXT_SLOT_NUMBER_KEY`)
rather than deriving `max + 1` from surviving rows, which would recycle the
highest number once its slot was deleted (see tests/test_jukebox.py).

Every slot carries a `genre` tag (`JukeboxSlot.genre`) - a
board-organization label, not derived from the track's tagged Genre(s),
since boards like "Hairbands" don't match real genre metadata.
`set_slot_genre` re-files a card under a different chip independently of
its board position (`swap_slots`/`reorder_slot` handle position; both are
separate controls in `ui/views/jukebox.py`'s "Organize card…" dialog).

The chip list is per-install, user-editable and persisted:
`get_jukebox_genres` reads it (seeding from `_DEFAULT_JUKEBOX_GENRES` on a
fresh database) and `add_genre`/`rename_genre`/`move_genre`/`delete_genre`
mutate it. It is stored as one JSON list under a single `Setting` row
(`_GENRES_SETTING_KEY`) - an ordered list of short strings doesn't need its
own table.

`rename_genre`/`delete_genre` also sweep `JukeboxSlot.genre` for cards
tagged with the old name, since a genre chip is a live filter
(`list_slots`/`slot_count`/etc. take `genre=`) and a card under a tag with
no chip would be invisible on every page. `delete_genre` reassigns orphaned
cards to `DEFAULT_JUKEBOX_GENRE` if still listed, else to the first genre,
and refuses to delete the last genre (the view always needs a chip, and
`place_track` always needs somewhere to file a new card). Both reject a
case-insensitive name collision rather than silently merging two genres'
cards onto one chip.
"""

from __future__ import annotations

import json
from typing import Optional, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from ..db.models import JukeboxSlot, Release, Setting, Track
from . import artwork_names
from .search_text import fold_ilike
from .matching import normalize

#: how many title strips make up one page of the selector (a full 3x3
#: page). JukeboxView sizes its pages to the window instead
#: (`JukeboxView._rows_that_fit`/`_cols_that_fit`), so this is only the
#: default for other callers.
SLOTS_PER_PAGE = 9

#: seed for `get_jukebox_genres` on a fresh database; not read directly
#: anywhere else. Kept in this order so existing installs upgrading to the
#: persisted list see the same chips in the same order.
_DEFAULT_JUKEBOX_GENRES = (
    "Country", "Christian", "Classic Rock", "Rock", "80's", "Hairbands", "Pop",
    "Metal", "R&B", "Hip/Hop",
)

#: `Setting.key` holding the persisted, user-editable genre list, as JSON
#: (see the module docstring).
_GENRES_SETTING_KEY = "jukebox_genres"

#: what a newly-placed track is filed under when the picker's `genre_combo`
#: doesn't resolve to anything (empty `genres`, or defensively a `None`
#: from `currentData()`); also `JukeboxPickerDialog`'s default preselected
#: genre. Must stay in sync with the literal default on
#: `db.models.JukeboxSlot.genre` - that module can't import this constant
#: without a circular import.
DEFAULT_JUKEBOX_GENRE = "Rock"

#: `Setting.key` holding the next never-before-used slot number (see the
#: module docstring). A plain int stored as text, like every other row in
#: the generic `settings` table.
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
    high-water mark rather than `max(existing slot_number) + 1` (which
    would reuse the number of a removed highest slot).

    On first use (no counter row yet) the counter is seeded from the
    highest slot_number currently on the board, so numbers already
    assigned are left where they are."""
    setting = session.get(Setting, _NEXT_SLOT_NUMBER_KEY)
    if setting is None:
        highest = session.scalar(select(func.max(JukeboxSlot.slot_number))) or 0
        setting = Setting(key=_NEXT_SLOT_NUMBER_KEY, value=str(highest + 1))
        session.add(setting)
    next_number = int(setting.value)
    setting.value = str(next_number + 1)
    session.flush()
    return next_number


def get_jukebox_genres(session: Session) -> tuple[str, ...]:
    """The persisted, left-to-right order of genre chips - what
    `ui/views/jukebox.py`'s chip row and every genre combo box build
    themselves from. Seeded from `_DEFAULT_JUKEBOX_GENRES` the first time
    it's called against a given database.

    A stored value that fails to parse as a JSON list falls back to the
    default tuple rather than raising - a corrupted setting shouldn't be
    able to take the whole Jukebox page down."""
    setting = session.get(Setting, _GENRES_SETTING_KEY)
    if setting is None:
        setting = Setting(
            key=_GENRES_SETTING_KEY, value=json.dumps(list(_DEFAULT_JUKEBOX_GENRES))
        )
        session.add(setting)
        session.flush()
    try:
        genres = json.loads(setting.value)
        if not isinstance(genres, list) or not all(isinstance(g, str) for g in genres):
            raise ValueError("not a list of strings")
    except (TypeError, ValueError):
        genres = list(_DEFAULT_JUKEBOX_GENRES)
    return tuple(genres)


def _save_jukebox_genres(session: Session, genres: Sequence[str]) -> None:
    """Write the ordered genre list back out - shared by `add_genre`/
    `rename_genre`/`delete_genre`, which read the list via
    `get_jukebox_genres` (creating the `Setting` row if needed), mutate a
    copy, and hand it back here."""
    setting = session.get(Setting, _GENRES_SETTING_KEY)
    value = json.dumps(list(genres))
    if setting is None:
        session.add(Setting(key=_GENRES_SETTING_KEY, value=value))  # pragma: no cover - defensive
    else:
        setting.value = value
    session.flush()


def add_genre(session: Session, name: str) -> bool:
    """Appends a new chip to the end of the row. Blank names are rejected,
    as is a name already on the board (compared case-insensitively, so
    "rock" can't sit next to "Rock"). Returns False as a no-op for either
    rejection, True once the new list is flushed."""
    name = name.strip()
    if not name:
        return False
    genres = list(get_jukebox_genres(session))
    if any(existing.lower() == name.lower() for existing in genres):
        return False
    genres.append(name)
    _save_jukebox_genres(session, genres)
    return True


def rename_genre(session: Session, old_name: str, new_name: str) -> bool:
    """Relabels one chip in place (its position doesn't change) and every
    `JukeboxSlot.genre` filed under the old name, in the same bulk
    `UPDATE`, so no card is stranded under a tag no chip shows (see the
    module docstring). The long-press rename gesture lives in
    `ui/views/jukebox.py`.

    Returns False as a no-op if `old_name` isn't a current genre (the chip
    row changed underneath the gesture), `new_name` is blank, or it
    collides case-insensitively with a *different* existing genre.
    Renaming a genre to its own name is a no-op that returns True."""
    new_name = new_name.strip()
    if not new_name:
        return False
    genres = list(get_jukebox_genres(session))
    if old_name not in genres:
        return False
    if new_name == old_name:
        return True
    if any(existing.lower() == new_name.lower() for existing in genres if existing != old_name):
        return False
    genres[genres.index(old_name)] = new_name
    _save_jukebox_genres(session, genres)
    session.execute(
        update(JukeboxSlot).where(JukeboxSlot.genre == old_name).values(genre=new_name)
    )
    session.flush()
    return True


def move_genre(session: Session, name: str, new_index: int) -> bool:
    """Moves one chip to a new position in the row - the write behind the
    drag gesture in `ui/views/jukebox.py` (`_GenreChip`). `new_index` is
    the chip's position in the *resulting* list, clamped to the row's
    bounds. Only the order changes; no `JukeboxSlot.genre` is touched.

    Returns False as a no-op if `name` isn't a current genre (the row
    changed underneath the drag) or the chip would land where it already
    is; True once the reordered list is flushed."""
    genres = list(get_jukebox_genres(session))
    if name not in genres:
        return False
    old_index = genres.index(name)
    new_index = max(0, min(len(genres) - 1, new_index))
    if new_index == old_index:
        return False
    genres.pop(old_index)
    genres.insert(new_index, name)
    _save_jukebox_genres(session, genres)
    return True


def delete_genre(session: Session, name: str) -> bool:
    """Removes one chip outright. Every card filed under it is reassigned
    rather than stranded (see the module docstring): to
    `DEFAULT_JUKEBOX_GENRE` if that's still one of the remaining genres,
    otherwise to the new first genre. The last remaining genre can't be
    deleted (`ui/views/jukebox.py` always needs a chip to show, and
    `place_track` always needs somewhere to file a new card).

    Returns False as a no-op if `name` isn't a genre that currently exists
    or is the only one left; True once both the shortened list and the
    reassigned slots are committed via `session.flush()`."""
    genres = list(get_jukebox_genres(session))
    if name not in genres or len(genres) <= 1:
        return False
    genres.remove(name)
    fallback = DEFAULT_JUKEBOX_GENRE if DEFAULT_JUKEBOX_GENRE in genres else genres[0]
    _save_jukebox_genres(session, genres)
    session.execute(
        update(JukeboxSlot).where(JukeboxSlot.genre == name).values(genre=fallback)
    )
    session.flush()
    return True


def place_track(
    session: Session,
    artist_id: int,
    track_id: int,
    genre: str = DEFAULT_JUKEBOX_GENRE,
) -> JukeboxSlot:
    """Load one song onto the board - see the module docstring for the
    callers. Idempotent: a track already on some slot (either side) is left
    where it is rather than duplicated, so callers can re-run freely (note
    this means `genre` is ignored for a track that's already placed; use
    `set_slot_genre` to re-file an existing card instead).

    `genre` scopes both halves of the "existing slot" search: an open A/B
    side is only reused on one of this artist's slots already tagged with
    this genre, and a brand-new slot is created under it, so pairing only
    happens within the same genre board."""
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
    """Re-files an existing card under a different genre chip (the
    "Organize card…" dialog, ui/views/jukebox.py) - purely relabels
    `JukeboxSlot.genre`; the slot keeps its number (so its "A7"/"B7" code
    never changes) and both sides stay as loaded. Independent of
    `swap_slots`.

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
    """Swap the board positions of two title strips - called from
    `ui/views/jukebox.py`'s "Organize card…" dialog
    (`JukeboxOrganizeDialog`/`_on_organize_requested`). Both sides of a
    strip move together as one unit, like picking up a real 45 - there's
    no way to reorder just one side.

    `slot_number` carries a UNIQUE constraint (see JukeboxSlot's
    docstring), and SQLite checks uniqueness per statement, not at
    transaction end, so assigning the first row the second row's number
    directly would collide. Routing through a temporary number - always
    negative, since real slot numbers count up from 1 - sidesteps that
    without touching anything else about either slot.

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
    """Drag-and-drop reordering (`ui/widgets/jukebox_strip.py:
    reorderRequested`). Unlike `swap_slots`, which trades exactly two
    cards' positions (still what "Organize card…" does), this inserts the
    dragged card at the target's rank and shifts every card from there on
    by one, like an ordinary playlist drag. A card pushed past the end of
    a page lands on the next page automatically, since `list_slots`/
    `list_slot_rows` render `per_page` cards per page in slot_number order.

    Both slots must share a genre (checked rather than assumed). Every
    slot on that genre's board is pulled in slot_number order; the dragged
    slot is spliced out and reinserted at the target's *original* rank
    (see the comment on `target_rank` below). So dragging forward lands a
    card just *after* the target, dragging backward just *before* it, and
    dropping onto an immediate neighbor is a plain two-item swap (see
    `TestReorderSlot` in tests/test_jukebox.py). The *same set* of
    already-active numbers is then redealt across the new order - no number
    is invented and no retired number (see `_next_slot_number`) is reused.

    Routed through temporary negative placeholders first, as in
    `swap_slots`, because SQLite checks the unique constraint on
    `slot_number` per statement. Every slot on the board is rewritten
    rather than only the changed ones - genre boards are small, so the
    extra writes aren't worth the bookkeeping.

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

    # target's rank *before* source is removed: splicing source back in at
    # this index lands it right after target when dragging forward (removing
    # source shifts target back by one) and right before target when
    # dragging backward. Using target's *post-removal* index would insert
    # one slot too early in the forward case, making "drop A onto its next
    # neighbor B" a silent no-op.
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
    """Pull a track off whichever slot it's on (e.g. the track was
    deleted). Clears just that side; once both sides are empty the whole
    strip is deleted rather than left as a permanently blank slot number."""
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
    """"Edit songs…" from a card's context menu. Overwrites one side of an
    *existing* slot with a new track (or with None, to clear that side) -
    unlike `place_track`, which only fills an open side.

    Returns False as a no-op if `slot_number` doesn't exist any more (the
    board changed while the edit dialog was open); True once the change is
    committed via `session.flush()`, including when `track_id` already
    matches what's on that side.

    Three things make this trickier than a plain attribute assignment:

    * Reassigning a side to the track it already holds short-circuits
      first, since the dedup logic below would otherwise clear the very
      value being "set".
    * `track_id` might already be loaded elsewhere on the board (another
      slot, or this slot's *other* side), and a track can only occupy one
      code, so the old location is cleared first. When it's this slot's
      other side, it's cleared directly rather than via `remove_track`,
      which could delete this slot's row (if both sides ended up empty)
      before the new value is written back onto the ORM object.
    * After the write, both sides might be empty (`track_id=None` clearing
      the last song) - same "delete the empty strip" rule as
      `remove_track`.

    Calling this twice in a row - once per side - is how the view
    implements an A/B "swap". See `TestSetSlotSide.test_swap_sides` for
    the full trace."""
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
    whatever's loaded. Unlike `remove_track`, this always deletes the
    `JukeboxSlot` row outright. Only board membership changes; the tracks,
    their files and their ratings are untouched.

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
    Details' Jukebox column batch-checks membership against
    (`services/library.py:list_track_details`) with one query up front
    rather than a per-row query at real-library scale."""
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
    given - what the Jukebox page's pager counts against."""
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
    loaded, like a real jukebox's carousel. Restricted to slots tagged with
    `genre` when given, so each genre chip pages through only its own
    cards."""
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
            # each side its own label when it has one
            "cover_path": artwork_names.side_cover(
                track.release.cover_path if track.release else None, track.position),
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
    """One slot's `slot_to_dict` row, by number - for `ui/views/jukebox.py`'s
    `_on_fill_requested` (the strip's `fillRequested` signal). Tapping a
    one-song card's empty "OPEN" banner needs the card's artist and genre
    fresh from the database, since the board may have changed since the
    last render. Returns None if the slot number no longer exists."""
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
    """Search by artist and track for the Jukebox picker dialog
    (`ui/views/jukebox.py:JukeboxPickerDialog`). `artist_query` and
    `track_query` are ANDed together, so a user can pick an artist and then
    search within that artist for a song. Either can be blank, but both
    blank returns nothing rather than the whole library. Matching is kept
    in sync with `services/library.py:search`'s `tracks` branch
    (`title_key.like` / `artist_display.ilike`).

    Every match is resolved to the artist a jukebox slot would file it
    under - the same `Release.album_artist_id` rule as
    `services/library.py:album_artist_id_for_track` (inlined, since
    `library.py` imports this module). A match with no resolvable album
    artist (a various-artists compilation, or an un-backfilled release) is
    dropped rather than shown and then failing to add.

    Returns plain dicts, not ORM rows (the session may close before the
    dialog does): `track_id`, `title`, `artist_name` (the resolved *album*
    artist, which is what a card shows), `artist_id`, and `album`. Capped
    at `limit` *results* - unresolvable matches don't count, so more rows
    may be scanned. The returned list is sorted case-insensitively by
    `(album, title)` so each album's matches cluster together."""
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
        stmt = stmt.where(fold_ilike(Track.artist_display, f"%{artist_query}%"))
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
    # re-sorted after capping at `limit`, not in the SQL `order_by` above,
    # which still governs which raw rows get scanned first.
    results.sort(key=lambda r: (r["album"].lower(), r["title"].lower()))
    return results


def genre_tracks(session: Session, genre: Optional[str]) -> list[Track]:
    """Every song on one genre board's cards (all pages), in card order,
    A side then B side - what the Jukebox page's "Play all" / "Shuffle"
    play. `genre=None` means every card."""
    stmt = (
        select(JukeboxSlot)
        .options(
            selectinload(JukeboxSlot.side_a_track).selectinload(Track.files),
            selectinload(JukeboxSlot.side_a_track).selectinload(Track.release),
            selectinload(JukeboxSlot.side_b_track).selectinload(Track.files),
            selectinload(JukeboxSlot.side_b_track).selectinload(Track.release),
        )
        .order_by(JukeboxSlot.slot_number)
    )
    if genre is not None:
        stmt = stmt.where(JukeboxSlot.genre == genre)
    tracks: list[Track] = []
    for slot in session.scalars(stmt):
        for track in (slot.side_a_track, slot.side_b_track):
            if track is not None:
                tracks.append(track)
    return tracks


def slot_position(session: Session, genre: Optional[str], track_id: int) -> Optional[int]:
    """0-based position, in card order, of the card holding `track_id` on
    `genre`'s board (either side) - or None if it isn't on that board. The
    Jukebox page divides this by cards-per-page to flip to the playing
    song's page."""
    stmt = select(JukeboxSlot.side_a_track_id, JukeboxSlot.side_b_track_id).order_by(
        JukeboxSlot.slot_number)
    if genre is not None:
        stmt = stmt.where(JukeboxSlot.genre == genre)
    for index, (a, b) in enumerate(session.execute(stmt)):
        if track_id in (a, b):
            return index
    return None
