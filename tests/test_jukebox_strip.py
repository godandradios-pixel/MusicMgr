"""Tests for ui/widgets/jukebox_strip.py: JukeboxStripWidget.

Covers the drag-and-drop reordering added 2026-09-18 (see that module's own
docstring) - `_decode_slot`'s MIME parsing, and the drop side of a drag
(`dragEnterEvent`/`dropEvent`, and the cases that must refuse a drop rather
than accept one). The drag *source* side (`_start_drag`, wired through
`eventFilter` off a mouse press-and-drag on the cover/artist row) calls
`QDrag.exec()`, which opens a real native drag loop that never returns under
a headless/offscreen test run - so that half is exercised by calling
`_start_drag` directly with `QDrag.exec` monkeypatched to a no-op spy
(same "assert it was asked to do the right thing" boundary conftest.py's
own docstring recommends for real OS-level machinery), rather than driving
it through synthetic mouse events.

The existing right-click menu actions already have their own direct-call
test pattern (`_build_context_menu()`, not a real popped-up `QMenu`) - the
drag/drop event handlers here are tested the same way: a constructed
`QDragEnterEvent`/`QDropEvent` handed straight to the widget's event
handler, not a real OS drag. **The `QMimeData` behind each event has to be
kept alive as a local for as long as the event is in use** - PySide6's
drag/drop events hold a raw pointer to it rather than a owning reference,
so letting the `QMimeData` fall out of scope (e.g. building it inside a
helper function that then returns just the event) segfaults the interpreter
the moment `event.mimeData()` is read back. Every test below keeps `mime`
alive in its own local scope for exactly that reason.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt
from PySide6.QtGui import QDrag, QDragEnterEvent, QDropEvent, QMouseEvent

from musicmgr.ui.widgets.jukebox_strip import (
    SLOT_MIME_TYPE,
    JukeboxStripWidget,
    _decode_slot,
)


def make_mime(slot_number) -> QMimeData:
    mime = QMimeData()
    if slot_number is not None:
        mime.setData(SLOT_MIME_TYPE, str(slot_number).encode("utf-8"))
    return mime


def make_strip(slot_number=None) -> JukeboxStripWidget:
    strip = JukeboxStripWidget()
    if slot_number is not None:
        strip.set_slot(
            {
                "slot_number": slot_number,
                "artist_id": 1,
                "artist_name": "Artist",
                "genre": "Rock",
                "side_a": {
                    "track_id": 1,
                    "title": "Song",
                    "duration_ms": 1000,
                    "cover_path": None,
                },
                "side_b": None,
            }
        )
    return strip


class TestDecodeSlot:
    def test_reads_back_the_encoded_slot_number(self, qapp):
        mime = make_mime(7)
        assert _decode_slot(mime) == 7

    def test_missing_format_is_none(self, qapp):
        mime = QMimeData()
        assert _decode_slot(mime) is None

    def test_unparseable_payload_is_none_not_a_crash(self, qapp):
        mime = QMimeData()
        mime.setData(SLOT_MIME_TYPE, b"not-a-number")
        assert _decode_slot(mime) is None


class TestDragEnter:
    def test_accepts_a_drag_from_a_different_slot(self, qapp):
        target = make_strip(slot_number=7)
        mime = make_mime(3)
        event = QDragEnterEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

        target.dragEnterEvent(event)

        assert event.isAccepted()
        assert target.card.property("dropTarget") is True

    def test_refuses_a_card_dropped_onto_itself(self, qapp):
        target = make_strip(slot_number=7)
        mime = make_mime(7)
        event = QDragEnterEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

        target.dragEnterEvent(event)

        assert not event.isAccepted()

    def test_refuses_when_this_strip_has_no_slot_of_its_own(self, qapp):
        # an unused page-filler strip, never set_slot()'d
        target = make_strip(slot_number=None)
        mime = make_mime(3)
        event = QDragEnterEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

        target.dragEnterEvent(event)

        assert not event.isAccepted()

    def test_refuses_a_drag_carrying_no_jukebox_payload(self, qapp):
        target = make_strip(slot_number=7)
        mime = QMimeData()
        event = QDragEnterEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

        target.dragEnterEvent(event)

        assert not event.isAccepted()


class TestDropEvent:
    def test_drop_from_a_different_slot_emits_reorder_requested(self, qapp):
        target = make_strip(slot_number=7)
        received = []
        target.reorderRequested.connect(lambda source, dest: received.append((source, dest)))
        target._set_drop_highlight(True)

        mime = make_mime(3)
        event = QDropEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        target.dropEvent(event)

        assert received == [(3, 7)]
        assert event.isAccepted()
        # the highlight set while hovering is cleared once the drop lands
        assert target.card.property("dropTarget") is False

    def test_dropping_a_card_onto_itself_is_a_silent_no_op(self, qapp):
        target = make_strip(slot_number=7)
        received = []
        target.reorderRequested.connect(lambda source, dest: received.append((source, dest)))

        mime = make_mime(7)
        event = QDropEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        target.dropEvent(event)

        assert received == []
        assert not event.isAccepted()

    def test_dropping_onto_an_unused_filler_strip_is_a_no_op(self, qapp):
        target = make_strip(slot_number=None)
        received = []
        target.reorderRequested.connect(lambda source, dest: received.append((source, dest)))

        mime = make_mime(3)
        event = QDropEvent(QPoint(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        target.dropEvent(event)

        assert received == []
        assert not event.isAccepted()

    def test_drag_leave_clears_the_highlight_without_a_drop(self, qapp):
        target = make_strip(slot_number=7)
        target._set_drop_highlight(True)

        target.dragLeaveEvent(None)

        assert target.card.property("dropTarget") is False


class TestStartDrag:
    def test_encodes_this_cards_own_slot_number_into_the_mime_data(self, qapp, monkeypatch):
        strip = make_strip(slot_number=12)
        captured = {}

        def fake_exec(self, action=Qt.MoveAction):
            captured["mime"] = self.mimeData()
            return Qt.IgnoreAction

        monkeypatch.setattr(QDrag, "exec", fake_exec)

        strip._start_drag()

        assert _decode_slot(captured["mime"]) == 12

    def test_mouse_drag_past_the_threshold_on_the_cover_starts_a_drag(self, qapp, monkeypatch):
        strip = make_strip(slot_number=12)
        calls = []
        monkeypatch.setattr(strip, "_start_drag", lambda: calls.append(True))

        press = QMouseEvent(
            QEvent.MouseButtonPress, QPoint(0, 0), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
        )
        strip.eventFilter(strip.cover, press)

        move = QMouseEvent(
            QEvent.MouseMove, QPoint(50, 50), Qt.NoButton, Qt.LeftButton, Qt.NoModifier
        )
        strip.eventFilter(strip.cover, move)

        assert calls == [True]

    def test_a_small_move_below_the_drag_threshold_does_not_start_a_drag(self, qapp, monkeypatch):
        strip = make_strip(slot_number=12)
        calls = []
        monkeypatch.setattr(strip, "_start_drag", lambda: calls.append(True))

        press = QMouseEvent(
            QEvent.MouseButtonPress, QPoint(0, 0), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
        )
        strip.eventFilter(strip.cover, press)

        move = QMouseEvent(
            QEvent.MouseMove, QPoint(1, 1), Qt.NoButton, Qt.LeftButton, Qt.NoModifier
        )
        strip.eventFilter(strip.cover, move)

        assert calls == []

    def test_an_unused_filler_strip_never_starts_a_drag(self, qapp, monkeypatch):
        strip = make_strip(slot_number=None)
        calls = []
        monkeypatch.setattr(strip, "_start_drag", lambda: calls.append(True))

        press = QMouseEvent(
            QEvent.MouseButtonPress, QPoint(0, 0), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
        )
        strip.eventFilter(strip.cover, press)

        move = QMouseEvent(
            QEvent.MouseMove, QPoint(50, 50), Qt.NoButton, Qt.LeftButton, Qt.NoModifier
        )
        strip.eventFilter(strip.cover, move)

        assert calls == []
