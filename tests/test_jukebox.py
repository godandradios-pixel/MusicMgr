"""Tests for services/jukebox.py: the persistent 45-selector favorites
board. Two title strips (A/B side pairing, "existing slot before a new
one", genre-scoped pairing), the genre re-file and position-swap controls,
removal (single side vs. whole card), the "NOW PLAYING" code lookup, and
the paging/listing helpers the Jukebox page itself renders from.

Slot numbering, genre scoping and the negative-number swap trick are all
called out explicitly in the module's own docstrings, so those are the
behaviors most worth pinning down here - they're exactly the kind of thing
a future refactor could quietly break without a UI symptom until someone
notices a card paired with the wrong artist or the pager landing on the
wrong page.
"""

from __future__ import annotations

from sqlalchemy import select

from musicmgr.db.models import JukeboxSlot, Track
from musicmgr.services import jukebox as jb
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize


def make_track(session, title, *, artist="Artist", album="Album", **fields) -> Track:
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist)
    release.album_artist_id = artist_obj.id
    track = Track(
        release_id=release.id,
        title=title,
        title_key=normalize(title),
        artist_display=artist,
        **fields,
    )
    session.add(track)
    session.flush()
    return track


class TestPlaceTrack:
    def test_a_brand_new_artist_gets_a_new_slot_on_side_a(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song One", artist="Artist")

        slot = jb.place_track(session, artist.id, track.id)

        assert slot.slot_number == 1
        assert slot.artist_id == artist.id
        assert slot.side_a_track_id == track.id
        assert slot.side_b_track_id is None
        assert slot.genre == jb.DEFAULT_JUKEBOX_GENRE

    def test_a_second_song_by_the_same_artist_fills_side_b_of_the_same_slot(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        first = make_track(session, "Song One", artist="Artist")
        second = make_track(session, "Song Two", artist="Artist")

        slot_a = jb.place_track(session, artist.id, first.id)
        slot_b = jb.place_track(session, artist.id, second.id)

        assert slot_a.slot_number == slot_b.slot_number
        assert slot_b.side_a_track_id == first.id
        assert slot_b.side_b_track_id == second.id

    def test_a_third_song_by_the_same_artist_starts_a_new_slot(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        one = make_track(session, "One", artist="Artist")
        two = make_track(session, "Two", artist="Artist")
        three = make_track(session, "Three", artist="Artist")
        jb.place_track(session, artist.id, one.id)
        jb.place_track(session, artist.id, two.id)

        slot = jb.place_track(session, artist.id, three.id)

        assert slot.slot_number == 2
        assert slot.side_a_track_id == three.id
        assert jb.slot_count(session) == 2

    def test_placing_the_same_track_twice_is_idempotent_not_a_duplicate(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song One", artist="Artist")
        first = jb.place_track(session, artist.id, track.id)

        second = jb.place_track(session, artist.id, track.id)

        assert first.slot_number == second.slot_number
        assert jb.slot_count(session) == 1

    def test_a_different_genre_does_not_reuse_an_open_side_on_another_genres_slot(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        rock_song = make_track(session, "Rock Song", artist="Artist")
        country_song = make_track(session, "Country Song", artist="Artist")
        jb.place_track(session, artist.id, rock_song.id, genre="Rock")

        slot = jb.place_track(session, artist.id, country_song.id, genre="Country")

        # a fresh slot, not sharing the open B side of the Rock slot
        assert slot.slot_number == 2
        assert slot.genre == "Country"
        assert jb.slot_count(session) == 2

    def test_slot_numbers_are_assigned_in_order_across_different_artists(self, session):
        a1 = lib.get_or_create_artist(session, "Artist One")
        a2 = lib.get_or_create_artist(session, "Artist Two")
        t1 = make_track(session, "Song", artist="Artist One")
        t2 = make_track(session, "Song", artist="Artist Two")

        slot1 = jb.place_track(session, a1.id, t1.id)
        slot2 = jb.place_track(session, a2.id, t2.id)

        assert slot1.slot_number == 1
        assert slot2.slot_number == 2

    def test_removing_a_lower_numbered_slot_does_not_free_up_its_number(self, session):
        # place two full (both sides) slots, 1 and 2, then remove the
        # *lower* one - the highest surviving slot_number is still 2, so
        # the next placement correctly continues at 3 rather than reusing 1.
        a1 = lib.get_or_create_artist(session, "Artist One")
        a2 = lib.get_or_create_artist(session, "Artist Two")
        t1 = make_track(session, "One", artist="Artist One")
        t2a = make_track(session, "Two A", artist="Artist Two")
        t2b = make_track(session, "Two B", artist="Artist Two")
        jb.place_track(session, a1.id, t1.id)
        jb.place_track(session, a2.id, t2a.id)
        jb.place_track(session, a2.id, t2b.id)  # fills slot 2's B side
        jb.remove_slot(session, 1)

        new_track = make_track(session, "New", artist="Artist Two")
        slot = jb.place_track(session, a2.id, new_track.id)

        assert slot.slot_number == 3

    def test_removing_the_highest_numbered_slot_does_not_free_up_its_number(self, session):
        # 2026-09-13 fix: `_next_slot_number` used to be
        # `max(existing slot_number) + 1`, computed fresh each time, so
        # removing the *highest* slot (the case exercised here) would make
        # its number available again the moment something new was placed -
        # a real gap from the module's documented "never reused... even
        # after a slot empties out and is removed" promise. The persisted
        # counter in the `settings` table closes it: the retired number 1
        # stays retired even though it was also the board's only slot.
        artist = lib.get_or_create_artist(session, "Artist")
        gone = make_track(session, "Gone", artist="Artist")
        jb.place_track(session, artist.id, gone.id)
        jb.remove_slot(session, 1)

        new_track = make_track(session, "New", artist="Artist")
        slot = jb.place_track(session, artist.id, new_track.id)

        assert slot.slot_number == 2


    def test_an_existing_board_with_no_counter_yet_seeds_from_the_current_max(self, session):
        # simulates an install upgrading into the 2026-09-13 fix: slots
        # already exist (created before the persisted counter existed) but
        # there's no Setting row for it yet. The very first call after the
        # upgrade must not restart numbering from 1 and collide with slot 5.
        artist = lib.get_or_create_artist(session, "Artist")
        legacy_track = make_track(session, "Legacy", artist="Artist")
        session.add(
            JukeboxSlot(slot_number=5, artist_id=artist.id, side_a_track_id=legacy_track.id)
        )
        session.flush()

        new_artist = lib.get_or_create_artist(session, "New Artist")
        new_track = make_track(session, "New", artist="New Artist")
        slot = jb.place_track(session, new_artist.id, new_track.id)

        assert slot.slot_number == 6

    def test_the_counter_keeps_advancing_across_repeated_calls(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        numbers = []
        for i in range(3):
            other = lib.get_or_create_artist(session, f"Artist {i}")
            track = make_track(session, "Song", artist=f"Artist {i}")
            numbers.append(jb.place_track(session, other.id, track.id).slot_number)

        assert numbers == [1, 2, 3]


class TestSetSlotGenre:
    def test_relabels_the_slot_and_keeps_its_number_and_tracks(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id, genre="Rock")

        changed = jb.set_slot_genre(session, slot.slot_number, "Metal")

        assert changed is True
        assert slot.genre == "Metal"
        assert slot.slot_number == 1
        assert slot.side_a_track_id == track.id

    def test_a_nonexistent_slot_number_is_a_no_op(self, session):
        assert jb.set_slot_genre(session, 999, "Metal") is False


class TestSwapSlots:
    def test_swaps_the_slot_numbers_of_two_cards(self, session):
        a1 = lib.get_or_create_artist(session, "Artist One")
        a2 = lib.get_or_create_artist(session, "Artist Two")
        t1 = make_track(session, "Song", artist="Artist One")
        t2 = make_track(session, "Song", artist="Artist Two")
        slot1 = jb.place_track(session, a1.id, t1.id)
        slot2 = jb.place_track(session, a2.id, t2.id)

        swapped = jb.swap_slots(session, slot1.slot_number, slot2.slot_number)

        assert swapped is True
        assert slot1.slot_number == 2
        assert slot2.slot_number == 1
        # nothing else about either card moved
        assert slot1.artist_id == a1.id
        assert slot1.side_a_track_id == t1.id

    def test_the_same_slot_number_picked_twice_is_a_no_op(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id)

        assert jb.swap_slots(session, slot.slot_number, slot.slot_number) is False

    def test_a_nonexistent_slot_number_on_either_side_is_a_no_op(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id)

        assert jb.swap_slots(session, slot.slot_number, 999) is False
        assert jb.swap_slots(session, 999, slot.slot_number) is False
        assert slot.slot_number == 1  # untouched by either failed attempt


def make_slots(session, count, *, genre=jb.DEFAULT_JUKEBOX_GENRE):
    """`count` slots, each a different artist (so `place_track` never pairs
    two songs onto one shared slot - see TestSwapSlots' own two-artist
    setup above for the same reason), in the same `genre` board, returned
    in the order they were created - which is also slot_number order,
    since nothing else has touched the counter yet."""
    slots = []
    for i in range(count):
        artist = lib.get_or_create_artist(session, f"Artist {i}")
        track = make_track(session, f"Song {i}", artist=f"Artist {i}")
        slots.append(jb.place_track(session, artist.id, track.id, genre=genre))
    return slots


class TestReorderSlot:
    """Drag-and-drop reordering (2026-09-18 follow-up) - see the function's
    own docstring for why this exists alongside `swap_slots` rather than
    replacing it: a straight two-way trade doesn't "shift the rest of the
    board over to make room," which is what James asked drag-and-drop to
    do after trying the swap-based version first."""

    def test_moving_the_first_card_onto_the_last_shifts_everyone_else_back_one(self, session):
        a, b, c, d = make_slots(session, 4)
        assert [s.slot_number for s in (a, b, c, d)] == [1, 2, 3, 4]

        assert jb.reorder_slot(session, a.slot_number, d.slot_number) is True

        # a was dragged *forward* past d, so it lands right after d (see
        # reorder_slot's own docstring on target_rank/without_source for
        # why a forward move lands after its target and a backward move
        # lands before it) - everyone in between shifts back one
        assert (b.slot_number, c.slot_number, d.slot_number, a.slot_number) == (1, 2, 3, 4)

    def test_moving_the_last_card_onto_the_first_shifts_everyone_else_forward_one(self, session):
        a, b, c, d = make_slots(session, 4)

        assert jb.reorder_slot(session, d.slot_number, a.slot_number) is True

        # d takes a's old rank (first); everyone else shifts forward one
        assert (d.slot_number, a.slot_number, b.slot_number, c.slot_number) == (1, 2, 3, 4)

    def test_moving_a_card_onto_its_immediate_next_neighbor_is_the_same_as_a_swap(self, session):
        a, b, c = make_slots(session, 3)

        assert jb.reorder_slot(session, a.slot_number, b.slot_number) is True

        assert (b.slot_number, a.slot_number, c.slot_number) == (1, 2, 3)

    def test_moving_a_card_onto_its_immediate_previous_neighbor_is_the_same_as_a_swap(self, session):
        a, b, c = make_slots(session, 3)

        assert jb.reorder_slot(session, b.slot_number, a.slot_number) is True

        assert (b.slot_number, a.slot_number, c.slot_number) == (1, 2, 3)

    def test_cards_outside_the_moved_range_keep_their_number(self, session):
        a, b, c, d, e = make_slots(session, 5)

        # drop c onto e - only c/d/e's ranks are between the old and new
        # position; a and b never move
        assert jb.reorder_slot(session, c.slot_number, e.slot_number) is True

        assert a.slot_number == 1
        assert b.slot_number == 2

    def test_the_same_slot_picked_twice_is_a_no_op(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song")
        slot = jb.place_track(session, artist.id, track.id)

        assert jb.reorder_slot(session, slot.slot_number, slot.slot_number) is False

    def test_a_nonexistent_slot_number_on_either_side_is_a_no_op(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song")
        slot = jb.place_track(session, artist.id, track.id)

        assert jb.reorder_slot(session, slot.slot_number, 999) is False
        assert jb.reorder_slot(session, 999, slot.slot_number) is False
        assert slot.slot_number == 1

    def test_slots_on_different_genre_boards_are_a_no_op(self, session):
        rock = make_slots(session, 2, genre="Rock")
        country = make_slots(session, 1, genre="Country")

        assert jb.reorder_slot(session, rock[0].slot_number, country[0].slot_number) is False
        # untouched by the failed attempt
        assert [s.slot_number for s in rock] == [1, 2]
        assert country[0].slot_number == 3

    def test_the_full_set_of_active_numbers_is_preserved_not_reinvented(self, session):
        slots = make_slots(session, 5)
        before = sorted(s.slot_number for s in slots)

        jb.reorder_slot(session, slots[1].slot_number, slots[4].slot_number)

        after = sorted(s.slot_number for s in slots)
        assert after == before  # same five numbers, just redealt

    def test_nothing_else_about_a_moved_card_changes(self, session):
        a1 = lib.get_or_create_artist(session, "Artist One")
        a2 = lib.get_or_create_artist(session, "Artist Two")
        t1 = make_track(session, "Song", artist="Artist One")
        t2 = make_track(session, "Song", artist="Artist Two")
        slot1 = jb.place_track(session, a1.id, t1.id)
        slot2 = jb.place_track(session, a2.id, t2.id)

        jb.reorder_slot(session, slot1.slot_number, slot2.slot_number)

        assert slot1.artist_id == a1.id
        assert slot1.side_a_track_id == t1.id


class TestRemoveTrack:
    def test_clears_just_that_side_and_leaves_the_other_side_loaded(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        a_side = make_track(session, "A Side", artist="Artist")
        b_side = make_track(session, "B Side", artist="Artist")
        jb.place_track(session, artist.id, a_side.id)
        jb.place_track(session, artist.id, b_side.id)

        jb.remove_track(session, a_side.id)

        slot = session.scalar(select(JukeboxSlot))
        assert slot.side_a_track_id is None
        assert slot.side_b_track_id == b_side.id
        assert jb.slot_count(session) == 1

    def test_removing_the_last_track_on_a_slot_deletes_the_whole_strip(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Only Song", artist="Artist")
        jb.place_track(session, artist.id, track.id)

        jb.remove_track(session, track.id)

        assert jb.slot_count(session) == 0

    def test_a_track_that_is_not_on_the_board_is_a_no_op(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "On The Board", artist="Artist")
        jb.place_track(session, artist.id, track.id)

        jb.remove_track(session, 999_999)  # should not raise

        assert jb.slot_count(session) == 1


class TestSetSlotSide:
    """"Edit songs…" (2026-09-13 follow-up) - James: "let me right click on
    a jukebox card and allow me to edit the songs on the card." Unlike
    `place_track`, `set_slot_side` can overwrite a side that's already
    filled, and can clear one outright with `track_id=None`."""

    def test_fills_an_open_side(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        a_side = make_track(session, "A Side", artist="Artist")
        b_side = make_track(session, "B Side", artist="Artist")
        slot = jb.place_track(session, artist.id, a_side.id)

        changed = jb.set_slot_side(session, slot.slot_number, "B", b_side.id)

        assert changed is True
        assert slot.side_a_track_id == a_side.id
        assert slot.side_b_track_id == b_side.id

    def test_replaces_an_already_filled_side(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        old = make_track(session, "Old Song", artist="Artist")
        new = make_track(session, "New Song", artist="Artist")
        slot = jb.place_track(session, artist.id, old.id)

        changed = jb.set_slot_side(session, slot.slot_number, "A", new.id)

        assert changed is True
        assert slot.side_a_track_id == new.id
        # the bumped track is no longer on the board anywhere
        assert jb.find_code_for_track(session, old.id) is None

    def test_clearing_the_only_side_deletes_the_slot(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Only Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id)

        changed = jb.set_slot_side(session, slot.slot_number, "A", None)

        assert changed is True
        assert jb.slot_count(session) == 0

    def test_clearing_one_side_of_two_leaves_the_slot_with_the_other(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        a_side = make_track(session, "A Side", artist="Artist")
        b_side = make_track(session, "B Side", artist="Artist")
        jb.place_track(session, artist.id, a_side.id)
        slot = jb.place_track(session, artist.id, b_side.id)

        changed = jb.set_slot_side(session, slot.slot_number, "A", None)

        assert changed is True
        assert jb.slot_count(session) == 1
        assert slot.side_a_track_id is None
        assert slot.side_b_track_id == b_side.id

    def test_reassigning_a_side_to_its_own_current_track_is_a_no_op(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id)

        changed = jb.set_slot_side(session, slot.slot_number, "A", track.id)

        assert changed is True
        assert slot.side_a_track_id == track.id
        assert jb.slot_count(session) == 1

    def test_moving_a_track_in_from_a_different_existing_slot(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        elsewhere = make_track(session, "Elsewhere Song", artist="Artist")
        elsewhere_b = make_track(session, "Elsewhere B Side", artist="Artist")
        here = make_track(session, "Here Song", artist="Artist")
        other_slot = jb.place_track(session, artist.id, elsewhere.id)
        # fills other_slot's open B side too, so the next placement below
        # can't be genre-scoped-reused onto it and starts a genuinely
        # separate slot instead (see place_track's own docstring)
        jb.place_track(session, artist.id, elsewhere_b.id)
        slot = jb.place_track(session, artist.id, here.id)
        assert slot.slot_number != other_slot.slot_number

        changed = jb.set_slot_side(session, slot.slot_number, "B", elsewhere.id)

        assert changed is True
        assert slot.side_a_track_id == here.id
        assert slot.side_b_track_id == elsewhere.id
        # its old slot lost that side but still has its other song, so the
        # slot itself survives (only both-sides-empty deletes a slot)
        assert session.get(JukeboxSlot, other_slot.slot_number) is not None
        assert other_slot.side_a_track_id is None
        assert other_slot.side_b_track_id == elsewhere_b.id
        assert jb.slot_count(session) == 2

    def test_swapping_a_and_b_via_two_sequential_calls(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        a_side = make_track(session, "A Side", artist="Artist")
        b_side = make_track(session, "B Side", artist="Artist")
        slot = jb.place_track(session, artist.id, a_side.id)
        jb.set_slot_side(session, slot.slot_number, "B", b_side.id)

        # swap: overwrite B with A's track (A's old value is cleared via
        # the same-slot-other-side branch, B's old value is simply
        # overwritten), then overwrite A with what was originally on B
        jb.set_slot_side(session, slot.slot_number, "B", a_side.id)
        jb.set_slot_side(session, slot.slot_number, "A", b_side.id)

        assert slot.side_a_track_id == b_side.id
        assert slot.side_b_track_id == a_side.id
        assert jb.slot_count(session) == 1

    def test_a_nonexistent_slot_number_is_a_no_op(self, session):
        assert jb.set_slot_side(session, 999, "A", None) is False


class TestRemoveSlot:
    def test_removes_the_whole_card_regardless_of_what_is_loaded(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id)

        removed = jb.remove_slot(session, slot.slot_number)

        assert removed is True
        assert jb.slot_count(session) == 0

    def test_a_nonexistent_slot_number_is_a_no_op(self, session):
        assert jb.remove_slot(session, 999) is False


class TestFindCodeForTrack:
    def test_reports_the_a_side_code(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        jb.place_track(session, artist.id, track.id)

        assert jb.find_code_for_track(session, track.id) == "A1"

    def test_reports_the_b_side_code(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        a_side = make_track(session, "A Side", artist="Artist")
        b_side = make_track(session, "B Side", artist="Artist")
        jb.place_track(session, artist.id, a_side.id)
        jb.place_track(session, artist.id, b_side.id)

        assert jb.find_code_for_track(session, b_side.id) == "B1"

    def test_a_track_not_on_the_board_returns_none(self, session):
        assert jb.find_code_for_track(session, 999_999) is None


class TestBoardTrackIds:
    def test_collects_both_sides_across_every_slot(self, session):
        a1 = lib.get_or_create_artist(session, "Artist One")
        a2 = lib.get_or_create_artist(session, "Artist Two")
        t1 = make_track(session, "One", artist="Artist One")
        t2 = make_track(session, "Two", artist="Artist One")
        t3 = make_track(session, "Three", artist="Artist Two")
        jb.place_track(session, a1.id, t1.id)
        jb.place_track(session, a1.id, t2.id)
        jb.place_track(session, a2.id, t3.id)

        assert jb.board_track_ids(session) == {t1.id, t2.id, t3.id}

    def test_an_empty_board_returns_an_empty_set(self, session):
        assert jb.board_track_ids(session) == set()


class TestSlotAndPageCount:
    def test_slot_count_with_no_genre_counts_everything(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        rock = make_track(session, "Rock Song", artist="Artist")
        country = make_track(session, "Country Song", artist="Artist")
        jb.place_track(session, artist.id, rock.id, genre="Rock")
        jb.place_track(session, artist.id, country.id, genre="Country")

        assert jb.slot_count(session) == 2
        assert jb.slot_count(session, genre="Rock") == 1
        assert jb.slot_count(session, genre="Country") == 1
        assert jb.slot_count(session, genre="Pop") == 0

    def test_page_count_ceil_divides_and_is_never_less_than_one(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        for i in range(10):
            t1 = make_track(session, f"A{i}", artist=f"Artist {i}")
            other_artist = lib.get_or_create_artist(session, f"Artist {i}")
            jb.place_track(session, other_artist.id, t1.id)

        assert jb.slot_count(session) == 10
        assert jb.page_count(session, per_page=9) == 2  # ceil(10/9)

    def test_an_empty_board_still_reports_one_page(self, session):
        assert jb.page_count(session) == 1

    def test_an_empty_genre_page_also_reports_one_page(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        jb.place_track(session, artist.id, track.id, genre="Rock")

        assert jb.page_count(session, genre="Pop") == 1


class TestListSlots:
    def test_orders_by_slot_number(self, session):
        artists = [lib.get_or_create_artist(session, f"Artist {i}") for i in range(3)]
        tracks = [make_track(session, "Song", artist=f"Artist {i}") for i in range(3)]
        for artist, track in zip(artists, tracks):
            jb.place_track(session, artist.id, track.id)

        slots = jb.list_slots(session)

        assert [s.slot_number for s in slots] == [1, 2, 3]

    def test_paginates_with_per_page_and_page_offset(self, session):
        for i in range(5):
            artist = lib.get_or_create_artist(session, f"Artist {i}")
            track = make_track(session, "Song", artist=f"Artist {i}")
            jb.place_track(session, artist.id, track.id)

        first_page = jb.list_slots(session, page=0, per_page=2)
        second_page = jb.list_slots(session, page=1, per_page=2)

        assert [s.slot_number for s in first_page] == [1, 2]
        assert [s.slot_number for s in second_page] == [3, 4]

    def test_filters_by_genre(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        rock = make_track(session, "Rock Song", artist="Artist")
        country = make_track(session, "Country Song", artist="Artist")
        jb.place_track(session, artist.id, rock.id, genre="Rock")
        jb.place_track(session, artist.id, country.id, genre="Country")

        slots = jb.list_slots(session, genre="Country")

        assert len(slots) == 1
        assert slots[0].side_a_track_id == country.id


class TestSlotToDict:
    def test_shape_of_a_fully_loaded_card(self, session):
        artist = lib.get_or_create_artist(session, "The Meridian Set")
        # create the release with its cover first - make_track's own
        # get_or_create_release call below just matches this existing row
        # by (title_key, artist_display) and leaves its cover_path alone,
        # the same "get" path a second real scan of the same album takes.
        lib.get_or_create_release(
            session, "Album", "The Meridian Set", cover_path="/covers/album.jpg"
        )
        a_side = make_track(session, "A Side", artist="The Meridian Set", duration_ms=180_000)
        b_side = make_track(session, "B Side", artist="The Meridian Set", duration_ms=200_000)
        slot = jb.place_track(session, artist.id, a_side.id)
        jb.place_track(session, artist.id, b_side.id)

        row = jb.slot_to_dict(slot)

        assert row["slot_number"] == 1
        assert row["artist_id"] == artist.id
        assert row["artist_name"] == "The Meridian Set"
        assert row["genre"] == jb.DEFAULT_JUKEBOX_GENRE
        assert row["side_a"] == {
            "track_id": a_side.id,
            "title": "A Side",
            "duration_ms": 180_000,
            "cover_path": "/covers/album.jpg",
        }
        assert row["side_b"]["track_id"] == b_side.id

    def test_an_empty_b_side_is_none_not_a_dict(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Only Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id)

        row = jb.slot_to_dict(slot)

        assert row["side_b"] is None

    def test_a_release_with_no_cover_reports_no_cover_path(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "No Cover Song", artist="Artist")

        slot = jb.place_track(session, artist.id, track.id)

        row = jb.slot_to_dict(slot)

        assert row["side_a"]["cover_path"] is None


class TestListSlotRows:
    def test_returns_dict_rows_for_a_page(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        jb.place_track(session, artist.id, track.id)

        rows = jb.list_slot_rows(session)

        assert len(rows) == 1
        assert rows[0]["slot_number"] == 1


class TestGetSlotRow:
    """2026-09-13 follow-up: backs `ui/views/jukebox.py`'s
    `_on_fill_requested` (James: "when I have a jukebox card with one
    song, I would like to click on the card and have the picker allow me
    to select a 2nd song") - a fresh, single-slot lookup by number, rather
    than trusting whatever the strip widget last rendered, since the
    board could have changed between that render and the tap."""

    def test_returns_the_same_shape_as_slot_to_dict(self, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song", artist="Artist")
        slot = jb.place_track(session, artist.id, track.id, genre="Pop")

        row = jb.get_slot_row(session, slot.slot_number)

        assert row == jb.slot_to_dict(slot)

    def test_a_nonexistent_slot_number_returns_none(self, session):
        assert jb.get_slot_row(session, 999) is None


class TestSearchAddableTracks:
    """2026-09-13 follow-up: backs `ui/views/jukebox.py`'s
    `JukeboxPickerDialog` and its two search boxes - James's original ask
    ("I want a standard UI. [a] picker box that allows you to select a
    genre and search by track and/or artist") shipped as one combined
    box, then a same-day follow-up ("I want the picker with two seperate
    search boxes. One for artist and one for track. I'll usually select
    an artist first, and then want to search within that artist for a
    song") split it into the `artist_query`/`track_query` parameters
    tested here, ANDed together rather than either matching on its own.
    Matching itself is deliberately kept in sync with `services/
    library.py:search`'s own `tracks` branch, so most of these tests
    focus on what's actually new here: resolving each match to the artist
    a jukebox slot would file it under (dropping ones that don't resolve
    to one) and the two query parameters combining with AND, not OR."""

    def test_matches_by_track_title_alone(self, session):
        make_track(session, "Moonlight Sonata", artist="Beethoven")

        results = jb.search_addable_tracks(session, track_query="moonlight")

        assert len(results) == 1
        assert results[0]["title"] == "Moonlight Sonata"
        assert results[0]["artist_name"] == "Beethoven"

    def test_matches_by_artist_name_alone(self, session):
        make_track(session, "Song One", artist="The Meridian Set")

        results = jb.search_addable_tracks(session, artist_query="meridian")

        assert len(results) == 1
        assert results[0]["title"] == "Song One"

    def test_artist_and_track_queries_combine_with_and_not_or(self, session):
        """The whole point of the follow-up - James: "I'll usually select
        an artist first, and then want to search within that artist for a
        song." An artist query that matches a *different* track by the
        right artist, plus a track query that matches the *same* title by
        a *different* artist, should each fail to surface a result on
        their own once the other query is also filled in."""
        make_track(session, "Song One", artist="The Meridian Set")
        make_track(session, "Song One", artist="Someone Else")
        make_track(session, "Song Two", artist="The Meridian Set")

        results = jb.search_addable_tracks(
            session, artist_query="meridian", track_query="song one"
        )

        assert len(results) == 1
        assert results[0]["title"] == "Song One"
        assert results[0]["artist_name"] == "The Meridian Set"

    def test_returns_the_resolved_album_artist_id_and_album(self, session):
        artist = lib.get_or_create_artist(session, "The Meridian Set")
        make_track(session, "Song One", artist="The Meridian Set", album="Debut")

        results = jb.search_addable_tracks(session, track_query="song one")

        assert results[0]["artist_id"] == artist.id
        assert results[0]["album"] == "Debut"

    def test_a_track_with_no_resolvable_album_artist_is_dropped(self, session):
        # a release with no album_artist_id set (e.g. pre-dates that
        # column, or a various-artists compilation) - no artist to file a
        # jukebox slot under, matching `album_artist_id_for_track`'s own
        # contract
        release = lib.get_or_create_release(session, "Various Album", "")
        track = Track(
            release_id=release.id,
            title="Orphan Track",
            title_key=normalize("Orphan Track"),
            artist_display="",
        )
        session.add(track)
        session.flush()

        results = jb.search_addable_tracks(session, track_query="orphan")

        assert results == []

    def test_both_queries_blank_returns_nothing(self, session):
        make_track(session, "Song", artist="Artist")

        assert jb.search_addable_tracks(session) == []
        assert jb.search_addable_tracks(session, artist_query="   ", track_query="  ") == []

    def test_no_match_returns_an_empty_list(self, session):
        make_track(session, "Song", artist="Artist")

        assert jb.search_addable_tracks(session, track_query="nonexistent") == []

    def test_respects_the_limit_after_dropping_unresolvable_matches(self, session):
        # 3 resolvable + 2 unresolvable "Song N" matches; limit=2 should
        # still return exactly 2 *results*, not stop after scanning 2 raw
        # rows and coming up short
        for i in range(3):
            make_track(session, f"Song {i}", artist=f"Artist {i}")
        for i in range(3, 5):
            release = lib.get_or_create_release(session, f"Various {i}", "")
            track = Track(
                release_id=release.id,
                title=f"Song {i}",
                title_key=normalize(f"Song {i}"),
                artist_display="",
            )
            session.add(track)
        session.flush()

        results = jb.search_addable_tracks(session, track_query="song", limit=2)

        assert len(results) == 2

    def test_results_are_sorted_by_album_then_title(self, session):
        """2026-09-20 follow-up - James: "on the songs, pick 2 sort the
        result set by album then song title." Tracks inserted in a
        deliberately scrambled order (not alphabetical by title, not
        grouped by album) so a passing result can only come from a real
        sort, not coincidental insertion/title order - same shape as
        `ui/views/jukebox.py`'s own `_load_tracks_for_artist` sort test
        (2026-09-07)."""
        make_track(session, "Zebra", artist="Artist", album="Bravo")
        make_track(session, "Song", artist="Artist", album="Delta")
        make_track(session, "Apple", artist="Artist", album="Bravo")
        make_track(session, "Track", artist="Artist", album="alpha")

        results = jb.search_addable_tracks(session, artist_query="artist")

        assert [(r["album"], r["title"]) for r in results] == [
            ("alpha", "Track"),
            ("Bravo", "Apple"),
            ("Bravo", "Zebra"),
            ("Delta", "Song"),
        ]
