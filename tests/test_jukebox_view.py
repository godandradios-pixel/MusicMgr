"""Tests for ui/views/jukebox.py: JukeboxView and JukeboxPickerDialog.

Scoped to the 2026-09-18 drag-and-drop follow-up - see that module's own
docstring. `_on_reorder_requested` is a thin wrapper around
`services.jukebox.swap_slots` plus a `refresh()`, so it's tested the same
way `_on_organize_requested`'s own swap already would be. The page-flip
machinery (`eventFilter` on `prev_btn`/`next_btn`, `_page_flip_timer`,
`_flip_page_during_drag`) is what's actually new here - a real drag never
runs under a headless test (see test_jukebox_strip.py's own docstring for
why), so it's driven the same way this view's siblings drive their own Qt
event plumbing: real `QDragEnterEvent`/synthetic event objects handed
straight to `eventFilter`, and the hover timer fired directly rather than
waited on, to keep the suite fast and deterministic.

`TestJukeboxPickerDialog` covers the same day's second follow-up - James,
after picking one song and going back to search for another: "it seems I
can enter one letter but then it bounces back to the artist." Like
`test_search_view.py`'s own `SearchView` tests, the 220ms debounce timer
itself isn't exercised here (spinning a real Qt event loop for it would
make the suite slow and timing-flaky for no real coverage gain) -
`_schedule_search` is only checked to start the timer without querying
immediately, and `_run_search` (what the timer actually fires) is called
directly to test the query/render behavior itself.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QCheckBox

from musicmgr.services import jukebox as jb
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize
from musicmgr.db.models import Track
from musicmgr.ui.views.jukebox import JukeboxPickerDialog, JukeboxView
from musicmgr.ui.widgets.jukebox_strip import SLOT_MIME_TYPE


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


def make_mime(slot_number: int) -> QMimeData:
    mime = QMimeData()
    mime.setData(SLOT_MIME_TYPE, str(slot_number).encode("utf-8"))
    return mime


class TestOnReorderRequested:
    def test_reorders_the_two_slots_and_refreshes(self, ctx, session):
        # reorder_slot's own tests (test_jukebox.py) cover the full
        # rank-shifting behavior in detail - this just confirms the view
        # wires the signal through to that call rather than the old
        # swap_slots, and refreshes afterward.
        a1 = lib.get_or_create_artist(session, "Artist One")
        a2 = lib.get_or_create_artist(session, "Artist Two")
        t1 = make_track(session, "Song", artist="Artist One")
        t2 = make_track(session, "Song", artist="Artist Two")
        slot1 = jb.place_track(session, a1.id, t1.id)
        slot2 = jb.place_track(session, a2.id, t2.id)
        session.flush()

        view = JukeboxView(ctx)
        view._on_reorder_requested(slot1.slot_number, slot2.slot_number)

        with ctx.session() as s:
            row1 = jb.get_slot_row(s, 2)  # slot1 now carries what was slot 2's number
            assert row1["artist_name"] == "Artist One"

    def test_a_vanished_slot_is_a_silent_no_op(self, ctx, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song")
        slot = jb.place_track(session, artist.id, track.id)
        session.flush()

        view = JukeboxView(ctx)
        # 999 doesn't exist - reorder_slot returns False, refresh() must not blow up
        view._on_reorder_requested(slot.slot_number, 999)

        with ctx.session() as s:
            assert jb.get_slot_row(s, slot.slot_number) is not None

    def test_dragging_a_card_from_a_later_page_onto_an_earlier_one_bumps_the_last_card_over(
        self, ctx, session, monkeypatch
    ):
        """The actual scenario James reported: drop a card from a later
        page onto a target on a fuller, earlier page, and the page you
        dropped onto should still only show `per_page` cards - whichever
        one that displaces spills onto the next page. Nothing about this
        is page-aware code in `_on_reorder_requested`/`reorder_slot` -
        it's just what falls out of re-rendering `per_page` slots in
        slot_number order after the ranks shift (see reorder_slot's own
        docstring)."""
        view = JukeboxView(ctx)
        monkeypatch.setattr(view, "_rows_that_fit", lambda: 1)
        monkeypatch.setattr(view, "_cols_that_fit", lambda: 1)  # 1 card/page

        slots = []
        for i in range(3):
            artist = lib.get_or_create_artist(session, f"Artist {i}")
            track = make_track(session, f"Song {i}", artist=f"Artist {i}")
            slots.append(jb.place_track(session, artist.id, track.id))
        session.flush()
        first, second, third = slots
        view.refresh()

        # drag `third` (currently page 3's only card) onto `first`
        # (currently page 1's only card) - third should land right before
        # first (a backward move - see reorder_slot's docstring), pushing
        # first and second each back one page
        view._on_reorder_requested(third.slot_number, first.slot_number)

        with ctx.session() as s:
            page1 = jb.list_slot_rows(s, page=0, per_page=1)
            page2 = jb.list_slot_rows(s, page=1, per_page=1)
            page3 = jb.list_slot_rows(s, page=2, per_page=1)

        assert page1[0]["artist_name"] == "Artist 2"  # third, moved to the front
        assert page2[0]["artist_name"] == "Artist 0"  # first, bumped to page 2
        assert page3[0]["artist_name"] == "Artist 1"  # second, bumped to page 3


class TestPageFlipDuringDrag:
    def _drag_enter(self, view, button, slot_number=3):
        mime = make_mime(slot_number)
        event = QDragEnterEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        handled = view.eventFilter(button, event)
        return handled, event

    def test_hovering_next_arrow_starts_the_flip_timer_when_enabled(self, ctx, session):
        view = JukeboxView(ctx)
        view.next_btn.setEnabled(True)

        handled, event = self._drag_enter(view, view.next_btn)

        assert handled is True
        assert event.isAccepted()
        assert view._page_flip_delta == 1
        assert view._page_flip_timer.isActive()

    def test_hovering_prev_arrow_when_disabled_does_not_start_the_timer(self, ctx, session):
        view = JukeboxView(ctx)
        view.prev_btn.setEnabled(False)  # already on page 1 - nothing to flip back to

        handled, event = self._drag_enter(view, view.prev_btn)

        assert handled is True
        assert event.isAccepted()  # still a valid jukebox drag, just no page to flip to
        assert not view._page_flip_timer.isActive()

    def test_drag_leave_stops_a_pending_flip(self, ctx, session):
        view = JukeboxView(ctx)
        view.next_btn.setEnabled(True)
        self._drag_enter(view, view.next_btn)
        assert view._page_flip_timer.isActive()

        view.eventFilter(view.next_btn, QEvent(QEvent.DragLeave))

        assert not view._page_flip_timer.isActive()

    def test_a_drop_on_an_arrow_is_ignored_and_swaps_nothing(self, ctx, session):
        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Song")
        jb.place_track(session, artist.id, track.id)
        session.flush()

        view = JukeboxView(ctx)
        view.next_btn.setEnabled(True)
        mime = make_mime(1)
        event = QDropEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

        handled = view.eventFilter(view.next_btn, event)

        assert handled is True
        assert not event.isAccepted()

    def test_flip_page_during_drag_advances_the_page_and_keeps_paging(self, ctx, session, monkeypatch):
        # `refresh()` recomputes _current_rows/_current_cols from real
        # widget geometry (_rows_that_fit/_cols_that_fit) every time it
        # runs, which under an offscreen/unsized test window always falls
        # back to MIN_ROWS x MIN_COLUMNS (9 slots/page) - stubbed down to a
        # 1-slot page here so three slots make three distinct pages
        # without needing dozens of fixture rows to prove the same point.
        view = JukeboxView(ctx)
        monkeypatch.setattr(view, "_rows_that_fit", lambda: 1)
        monkeypatch.setattr(view, "_cols_that_fit", lambda: 1)

        # three *different* artists - place_track pairs same-artist songs
        # onto one shared slot before starting a new one, so reusing one
        # artist here would only ever produce two slots (one full A/B
        # pair, one single), not the three separate pages this test wants.
        for i in range(3):
            artist = lib.get_or_create_artist(session, f"Artist {i}")
            track = make_track(session, f"Song {i}", artist=f"Artist {i}")
            jb.place_track(session, artist.id, track.id)
        session.flush()
        view.refresh()
        assert view._page == 0

        view._page_flip_delta = 1
        view._flip_page_during_drag()

        assert view._page == 1
        # one more page ahead (page 2 of 3) - the timer restarts to keep paging
        assert view._page_flip_timer.isActive()

    def test_flip_page_during_drag_stops_restarting_at_the_last_page(self, ctx, session, monkeypatch):
        view = JukeboxView(ctx)
        monkeypatch.setattr(view, "_rows_that_fit", lambda: 1)
        monkeypatch.setattr(view, "_cols_that_fit", lambda: 1)

        artist = lib.get_or_create_artist(session, "Artist")
        track = make_track(session, "Only Song")
        jb.place_track(session, artist.id, track.id)
        session.flush()
        view.refresh()  # 1 slot, 1 slot/page -> a single page

        view._page_flip_delta = 1
        view._flip_page_during_drag()

        assert view._page == 0  # nothing to flip to - stayed put
        assert not view._page_flip_timer.isActive()


class TestJukeboxPickerDialog:
    """`JukeboxPickerDialog`'s own search boxes (2026-09-18 follow-up) -
    see the module docstring for James's report and why. A plain stub
    `search_tracks` callable stands in for `JukeboxView._search_addable_
    tracks` here - nothing under test touches the database, so there's no
    need for the `ctx`/`session` fixtures the rest of this file uses."""

    @staticmethod
    def _catalog_search(rows):
        def search_tracks(artist_query: str, track_query: str):
            aq, tq = artist_query.lower(), track_query.lower()
            return [
                row
                for row in rows
                if (not aq or aq in row["artist_name"].lower())
                and (not tq or tq in row["title"].lower())
            ]

        return search_tracks

    def test_typing_schedules_a_search_without_running_it_immediately(self, qapp):
        dialog = JukeboxPickerDialog(None, self._catalog_search([]), genres=["Rock"])

        dialog.artist_search.setText("Whitney")  # textChanged -> _schedule_search

        assert dialog._search_timer.isActive()
        # the timer hasn't fired yet (no event loop spin) - _run_search only
        # actually queries once it does, so nothing's rendered yet.
        assert dialog.track_list.count() == 0

    def test_run_search_ands_the_artist_and_track_queries(self, qapp):
        rows = [
            {"track_id": 1, "title": "Dear John Letter", "artist_name": "Whitney Houston", "artist_id": 10, "album": ""},
            {"track_id": 2, "title": "Greatest Love of All", "artist_name": "Whitney Houston", "artist_id": 10, "album": ""},
            {"track_id": 3, "title": "Dear Prudence", "artist_name": "The Beatles", "artist_id": 20, "album": ""},
        ]
        dialog = JukeboxPickerDialog(None, self._catalog_search(rows), genres=["Rock"])
        dialog.artist_search.setText("Whitney")
        dialog.track_search.setText("Dear")

        dialog._run_search()  # what the debounce timer fires once typing pauses

        assert dialog.track_list.count() == 1

    def test_rerendering_disposes_of_the_previous_renders_checkbox_widgets(self, qapp):
        """The bug's actual mechanism (see the module docstring):
        `_render_results` used to leak every search's checkboxes as
        orphaned children of `track_list`'s viewport instead of disposing
        of them, so `track_list` only ever grew, session-long, and got
        slower to lay out with every keystroke - compounding the freeze
        that led to the reported mis-click."""
        rows = [
            {"track_id": i, "title": f"Song {i}", "artist_name": "Artist", "artist_id": 1, "album": ""}
            for i in range(5)
        ]
        dialog = JukeboxPickerDialog(None, self._catalog_search(rows), genres=["Rock"])

        dialog.artist_search.setText("Artist")
        dialog._run_search()
        assert len(dialog.track_list.findChildren(QCheckBox)) == 5

        # a second, narrower search's re-render shouldn't leave the first
        # search's checkboxes behind - `_render_results` schedules their
        # disposal with `deleteLater()` (the documented way to drop a
        # widget handed over by `removeItemWidget()`), which only actually
        # runs once the event loop gets a turn - `sendPostedEvents` forces
        # that turn here rather than requiring a real `QTest.qWait()`, the
        # same way the running app's own event loop would reclaim them
        # between keystrokes.
        dialog.track_search.setText("Song 1")
        dialog._run_search()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert dialog.track_list.count() == 1
        assert len(dialog.track_list.findChildren(QCheckBox)) == 1

    def test_a_pick_survives_a_debounced_rerender(self, qapp):
        """Picks persist across searches (see the class's own docstring) -
        confirming that still holds now that render is reached via
        `_run_search` instead of the old `_on_search_changed`."""
        rows = [
            {"track_id": 1, "title": "Dear John Letter", "artist_name": "Whitney Houston", "artist_id": 10, "album": ""},
        ]
        dialog = JukeboxPickerDialog(None, self._catalog_search(rows), genres=["Rock"])
        dialog.artist_search.setText("Whitney")
        dialog._run_search()
        checkbox = dialog.track_list.itemWidget(dialog.track_list.item(0))
        checkbox.setChecked(True)
        assert dialog._picks == {1: 10}

        # re-run the same search (as the debounce timer would after any
        # further keystroke) - the pick should still show checked
        dialog._run_search()
        checkbox = dialog.track_list.itemWidget(dialog.track_list.item(0))
        assert checkbox.isChecked()
        assert dialog._picks == {1: 10}
