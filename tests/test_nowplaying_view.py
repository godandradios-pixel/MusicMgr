"""Tests for ui/views/nowplaying.py: loading the currently-playing track's
metadata (chart history note, rating, jukebox status, tap-to-jump artist/
release ids), the rating/jukebox controls persisting back to the database,
and the queue list - including the exact "Not Responding" performance
guard the 2026-09-06 fix note describes: `_refresh_queue()` must rebuild
the (potentially huge) queue list only while Now Playing is actually the
page on screen, never on every trackChanged/queueChanged fired from
elsewhere.

Real `QueueItem`s backed by real (dummy-content) files on disk drive these
tests through the actual `PlayerController`, rather than faking its
signals - `_load_current()` only needs `os.path.exists(item.path)` to be
true to fire `trackChanged` synchronously (the async "can't decode this"
error that follows never gets a chance to run without an event-loop spin),
so a plain empty file is enough and no real audio content is needed.
"""

from __future__ import annotations

import datetime as dt

import pytest
from PySide6.QtWidgets import QDialog, QStackedWidget, QWidget
from sqlalchemy import select

from musicmgr.db.models import Chart, ChartEntry, ChartIssue, JukeboxSlot, Track
from musicmgr.services import jukebox as jukebox_svc
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize
from musicmgr.services.player import QueueItem
from musicmgr.ui.theme import COLORS
from musicmgr.ui.views.jukebox import JukeboxPickerDialog
from musicmgr.ui.views.nowplaying import NowPlayingView


@pytest.fixture
def view(ctx):
    return NowPlayingView(ctx)


@pytest.fixture
def current_page(view):
    """Puts `view` in a QStackedWidget as the current page - the state
    `_is_current_page()` checks before `_refresh_queue()` does its
    (potentially expensive) rebuild. See the module's 2026-09-06 "Not
    Responding" fix note."""
    stack = QStackedWidget()
    stack.addWidget(QWidget())  # some other page, index 0
    stack.addWidget(view)       # index 1
    stack.setCurrentWidget(view)
    return stack


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


def make_chart_entry(session, chart_name, track, *, rank, peak_pos=None, chart_date=None) -> ChartEntry:
    chart = session.scalar(select(Chart).where(Chart.name == chart_name))
    if chart is None:
        chart = Chart(name=chart_name, slug=normalize(chart_name).replace(" ", "-"))
        session.add(chart)
        session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=chart_date or dt.date(2000, 1, 1))
    session.add(issue)
    session.flush()
    entry = ChartEntry(
        issue_id=issue.id,
        rank=rank,
        title=track.title,
        artist_name=track.artist_display or "",
        title_key=track.title_key,
        artist_key=normalize(track.artist_display or ""),
        peak_pos=peak_pos,
        track_id=track.id,
    )
    session.add(entry)
    session.flush()
    return entry


def make_queue_item(tmp_path, *, track_id=1, title="Song", artist="Artist", album="Album",
                     duration_ms=180_000, filename=None) -> QueueItem:
    path = tmp_path / (filename or f"{title}.mp3")
    path.write_bytes(b"fake audio bytes")
    return QueueItem(
        track_id=track_id, title=title, artist=artist, album=album,
        path=str(path), duration_ms=duration_ms,
    )


class TestOnTrackChanged:
    def test_nothing_playing_resets_everything(self, ctx, view, tmp_path):
        # first put a real track up, then clear it
        item = make_queue_item(tmp_path, track_id=1, title="Was Playing", artist="Someone")
        view._on_track_changed(item)

        view._on_track_changed(None)

        assert view.track_title.text() == "Nothing playing"
        assert view.track_artist.text() == ""
        assert view.track_album.text() == ""
        assert view.chart_note.text() == ""
        assert view.rating_stars.rating() == 0
        assert view.jukebox_toggle.is_on() is False
        assert view._current_track_id is None
        assert view._current_artist_id is None
        assert view._current_release_id is None

    def test_basic_fields_come_from_the_queue_item(self, ctx, view, tmp_path):
        item = make_queue_item(
            tmp_path, track_id=999_999, title="My Song", artist="My Artist", album="My Album"
        )

        view._on_track_changed(item)

        assert view.track_title.text() == "My Song"
        assert view.track_artist.text() == "My Artist"
        assert view.track_album.text() == "My Album"
        assert view._current_track_id == 999_999

    def test_loads_the_tracks_rating_from_the_database(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Rated Song")
            track.rating = 4
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert view.rating_stars.rating() == 4

    def test_a_track_with_no_rating_shows_zero_stars(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Unrated Song")
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert view.rating_stars.rating() == 0

    def test_loads_jukebox_membership_from_the_database(self, ctx, view, tmp_path):
        with ctx.session() as session:
            artist = lib.get_or_create_artist(session, "Jukebox Artist")
            track = make_track(session, "On The Board", artist="Jukebox Artist")
            jukebox_svc.place_track(session, artist.id, track.id)
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert view.jukebox_toggle.is_on() is True

    def test_a_track_not_on_the_jukebox_shows_the_toggle_off(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Not On The Board")
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert view.jukebox_toggle.is_on() is False

    def test_loads_artist_and_release_ids_for_tap_navigation(self, ctx, view, tmp_path):
        with ctx.session() as session:
            artist = lib.get_or_create_artist(session, "Tappable Artist")
            track = make_track(session, "Song", artist="Tappable Artist")
            artist_id, release_id, track_id = artist.id, track.release_id, track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert view._current_artist_id == artist_id
        assert view._current_release_id == release_id

    def test_a_track_with_no_db_row_has_no_navigation_ids_but_does_not_crash(self, ctx, view, tmp_path):
        view._on_track_changed(make_queue_item(tmp_path, track_id=999_999))

        assert view._current_artist_id is None
        assert view._current_release_id is None

    def test_chart_note_reports_the_peak_position_and_weeks_charted(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Charting Song")
            make_chart_entry(session, "Test Chart", track, rank=10, chart_date=dt.date(2000, 1, 1))
            make_chart_entry(session, "Test Chart", track, rank=3, chart_date=dt.date(2000, 1, 8))
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert "Test Chart: peaked at #3" in view.chart_note.text()
        assert "2 week(s) charted" in view.chart_note.text()

    def test_chart_note_prefers_peak_pos_over_rank_when_it_is_better(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Song")
            make_chart_entry(session, "Test Chart", track, rank=10, peak_pos=1)
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert "peaked at #1" in view.chart_note.text()

    def test_play_count_note_is_included_when_the_track_has_been_played(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Played Song", play_count=7)
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert "Played 7 time(s) from this library" in view.chart_note.text()

    def test_a_track_with_no_chart_or_play_history_has_an_empty_chart_note(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Plain Song")
            track_id = track.id

        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        assert view.chart_note.text() == ""


class TestOpenCurrentArtist:
    def test_emits_navigate_and_open_artist_requests(self, ctx, view):
        view._current_artist_id = 42
        navigated = []
        opened = []
        ctx.navigateRequested.connect(navigated.append)
        ctx.openArtistRequested.connect(opened.append)

        view._open_current_artist()

        assert navigated == ["library"]
        assert opened == [42]

    def test_is_a_noop_when_nothing_is_playing(self, ctx, view):
        navigated = []
        ctx.navigateRequested.connect(navigated.append)

        view._open_current_artist()

        assert navigated == []


class TestOpenCurrentRelease:
    def test_emits_navigate_and_open_release_requests(self, ctx, view):
        view._current_release_id = 7
        navigated = []
        opened = []
        ctx.navigateRequested.connect(navigated.append)
        ctx.openReleaseRequested.connect(opened.append)

        view._open_current_release()

        assert navigated == ["library"]
        assert opened == [7]

    def test_is_a_noop_when_nothing_is_playing(self, ctx, view):
        opened = []
        ctx.openReleaseRequested.connect(opened.append)

        view._open_current_release()

        assert opened == []


class TestOnRatingChanged:
    def test_persists_the_new_rating_for_the_currently_playing_track(self, ctx, view, tmp_path):
        with ctx.session() as session:
            track = make_track(session, "Song")
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        view._on_rating_changed(5)

        with ctx.session() as session:
            assert session.get(Track, track_id).rating == 5

    def test_is_a_noop_when_nothing_is_playing(self, ctx, view):
        view._on_rating_changed(5)  # should not raise, nothing to persist to


class TestOnJukeboxToggled:
    """2026-09-13 follow-up: turning the toggle ON now opens
    `ui/views/jukebox.py`'s `JukeboxPickerDialog` (pre-filled and
    pre-checked for the current track - see `_on_jukebox_toggled`'s own
    docstring) instead of silently placing the track under the default
    genre. `JukeboxPickerDialog.exec` is monkeypatched in every "turning
    on" test below to stand in for a person actually working the dialog -
    real `QDialog.exec()` opens a nested event loop that never returns on
    its own in these headless tests."""

    def test_toggling_on_places_the_track_and_updates_the_glyph(self, ctx, view, tmp_path, monkeypatch):
        monkeypatch.setattr(JukeboxPickerDialog, "exec", lambda self: QDialog.Accepted)
        with ctx.session() as session:
            make_track(session, "Song", artist="Toggle Artist")
            track = session.scalar(select(Track))
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))
        assert view.jukebox_toggle.is_on() is False

        view._on_jukebox_toggled()

        assert view.jukebox_toggle.is_on() is True
        with ctx.session() as session:
            assert jukebox_svc.find_code_for_track(session, track_id) is not None

    def test_toggling_on_uses_whatever_genre_was_chosen_in_the_picker(self, ctx, view, tmp_path, monkeypatch):
        """The whole point of the 2026-09-13 follow-up - James: "there is
        no way to select which genre the track should be on" - so this
        pins the dialog's chosen genre actually reaching `place_track`,
        not just the default."""

        def fake_exec(dialog):
            idx = dialog.genre_combo.findData("Pop")
            assert idx >= 0
            dialog.genre_combo.setCurrentIndex(idx)
            return QDialog.Accepted

        monkeypatch.setattr(JukeboxPickerDialog, "exec", fake_exec)
        with ctx.session() as session:
            make_track(session, "Song", artist="Toggle Artist")
            track = session.scalar(select(Track))
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        view._on_jukebox_toggled()

        with ctx.session() as session:
            slot = session.scalar(
                select(JukeboxSlot).where(JukeboxSlot.side_a_track_id == track_id)
            )
            assert slot is not None
            assert slot.genre == "Pop"

    def test_toggling_on_pre_checks_the_current_track_in_the_picker(self, ctx, view, tmp_path, monkeypatch):
        """`check_track` should have already picked the tapped track before
        the dialog is even shown, so a person can tap OK immediately
        without finding and re-checking their own song."""
        seen_picks = []

        def fake_exec(dialog):
            seen_picks.append(dialog.selected_picks())
            return QDialog.Accepted

        monkeypatch.setattr(JukeboxPickerDialog, "exec", fake_exec)
        with ctx.session() as session:
            make_track(session, "Song", artist="Toggle Artist")
            track = session.scalar(select(Track))
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        view._on_jukebox_toggled()

        assert len(seen_picks) == 1
        assert [pick_track_id for pick_track_id, _ in seen_picks[0]] == [track_id]

    def test_cancelling_the_picker_leaves_the_track_off(self, ctx, view, tmp_path, monkeypatch):
        monkeypatch.setattr(JukeboxPickerDialog, "exec", lambda self: QDialog.Rejected)
        with ctx.session() as session:
            make_track(session, "Song", artist="Toggle Artist")
            track = session.scalar(select(Track))
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))

        view._on_jukebox_toggled()

        assert view.jukebox_toggle.is_on() is False
        with ctx.session() as session:
            assert jukebox_svc.find_code_for_track(session, track_id) is None

    def test_toggling_off_removes_the_track_and_updates_the_glyph(self, ctx, view, tmp_path):
        with ctx.session() as session:
            artist = lib.get_or_create_artist(session, "Toggle Artist")
            track = make_track(session, "Song", artist="Toggle Artist")
            jukebox_svc.place_track(session, artist.id, track.id)
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))
        assert view.jukebox_toggle.is_on() is True

        view._on_jukebox_toggled()

        assert view.jukebox_toggle.is_on() is False
        with ctx.session() as session:
            assert jukebox_svc.find_code_for_track(session, track_id) is None

    def test_a_track_with_no_album_artist_notifies_and_leaves_the_glyph_off(self, ctx, view, tmp_path):
        with ctx.session() as session:
            # a release with no album_artist_id set - can't file a jukebox slot
            release = lib.get_or_create_release(session, "No Artist Album", "")
            track = Track(
                release_id=release.id, title="Orphan", title_key="orphan", artist_display=""
            )
            session.add(track)
            session.flush()
            track_id = track.id
        view._on_track_changed(make_queue_item(tmp_path, track_id=track_id))
        notifications = []
        ctx.notified.connect(notifications.append)

        view._on_jukebox_toggled()

        assert view.jukebox_toggle.is_on() is False
        assert notifications and "no album artist" in notifications[0].lower()

    def test_is_a_noop_when_nothing_is_playing(self, ctx, view):
        notifications = []
        ctx.notified.connect(notifications.append)

        view._on_jukebox_toggled()  # should not raise

        assert notifications == []


class TestRefreshQueueGating:
    """The 2026-09-06 "Not Responding" fix: rebuilding the queue list must
    only happen while Now Playing is actually the page on screen."""

    def test_queue_changed_does_not_rebuild_while_another_page_is_current(
        self, ctx, view, current_page, tmp_path
    ):
        current_page.setCurrentIndex(0)  # the *other* page, not view
        item = make_queue_item(tmp_path, title="Song")

        ctx.player.play_tracks([item])  # fires queueChanged -> _refresh_queue

        assert view.queue_list.count() == 0

    def test_refresh_rebuilds_once_this_becomes_the_current_page(
        self, ctx, view, current_page, tmp_path
    ):
        current_page.setCurrentIndex(0)
        item = make_queue_item(tmp_path, title="Song")
        ctx.player.play_tracks([item])
        assert view.queue_list.count() == 0  # not current yet

        current_page.setCurrentWidget(view)
        view.refresh()

        assert view.queue_list.count() == 1

    def test_queue_changed_rebuilds_live_while_already_the_current_page(
        self, ctx, view, current_page, tmp_path
    ):
        current_page.setCurrentWidget(view)  # current from the start

        ctx.player.play_tracks([make_queue_item(tmp_path, title="Song")])

        assert view.queue_list.count() == 1

    def test_with_no_parent_stack_at_all_refresh_queue_is_a_noop(self, ctx, view, tmp_path):
        # view was never added to any QStackedWidget - parentWidget() is
        # None, which _is_current_page() must treat as "not current",
        # not crash on.
        ctx.player.play_tracks([make_queue_item(tmp_path, title="Song")])

        assert view.queue_list.count() == 0


class TestRefreshQueueContent:
    def test_rows_report_title_artist_and_duration(self, ctx, view, current_page, tmp_path):
        item = make_queue_item(tmp_path, title="Song", artist="Artist", duration_ms=125_000)

        ctx.player.play_tracks([item])

        payload = view.queue_list.payload_at(0)
        assert payload["primary"] == "Song"
        assert payload["secondary"] == "Artist"
        assert payload["trail"] == "2:05"
        assert payload["index"] == 0

    def test_the_playing_row_gets_the_note_glyph_bold_and_highlight_color(
        self, ctx, view, current_page, tmp_path
    ):
        one = make_queue_item(tmp_path, title="One", filename="one.mp3")
        two = make_queue_item(tmp_path, title="Two", filename="two.mp3")

        ctx.player.play_tracks([one, two], start=0)

        playing = view.queue_list.payload_at(0)
        other = view.queue_list.payload_at(1)
        assert playing["lead"] == "♪"
        assert playing["bold"] is True
        # 2026-09-13 follow-up: the playing row's highlight moved from the
        # app's old red-orange accent to the walnut-brown palette (see
        # ui/theme.py's #Primary comment) - this test just needs to track
        # whichever color nowplaying.py actually uses now, not pin the old
        # one specifically.
        assert playing["color"] == COLORS["jukebox_key_hi"]
        assert other["lead"] == "2"
        assert other["bold"] is False
        assert other["color"] == COLORS["text"]

    def test_queue_meta_reports_count_and_total_duration(self, ctx, view, current_page, tmp_path):
        one = make_queue_item(tmp_path, title="One", filename="one.mp3", duration_ms=60_000)
        two = make_queue_item(tmp_path, title="Two", filename="two.mp3", duration_ms=90_000)

        ctx.player.play_tracks([one, two])

        assert view.queue_meta.text() == "2 tracks · 2:30"

    def test_an_empty_queue_reports_empty(self, ctx, view, current_page):
        ctx.player.clear_queue()

        assert view.queue_meta.text() == "empty"

    def test_the_playing_row_is_selected_in_the_list(self, ctx, view, current_page, tmp_path):
        one = make_queue_item(tmp_path, title="One", filename="one.mp3")
        two = make_queue_item(tmp_path, title="Two", filename="two.mp3")

        ctx.player.play_tracks([one, two], start=1)

        assert view.queue_list.currentRow() == 1


class TestOnQueueTapped:
    def test_tapping_a_row_jumps_to_its_index(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(ctx.player, "jump_to", lambda idx: calls.append(idx))

        view._on_queue_tapped({"index": 3})

        assert calls == [3]

    def test_no_payload_is_a_noop(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(ctx.player, "jump_to", lambda idx: calls.append(idx))

        view._on_queue_tapped(None)

        assert calls == []

    def test_a_payload_with_no_index_is_a_noop(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(ctx.player, "jump_to", lambda idx: calls.append(idx))

        view._on_queue_tapped({})

        assert calls == []


class TestRemoveSelected:
    def test_removes_the_selected_queue_row(self, ctx, view, current_page, tmp_path):
        one = make_queue_item(tmp_path, title="One", filename="one.mp3")
        two = make_queue_item(tmp_path, title="Two", filename="two.mp3")
        ctx.player.play_tracks([one, two], start=0)
        view.queue_list.setCurrentRow(1)

        view._remove_selected()

        assert [i.title for i in ctx.player.queue] == ["One"]

    def test_nothing_selected_is_a_noop(self, ctx, view, current_page, tmp_path):
        ctx.player.play_tracks([make_queue_item(tmp_path, title="One")])
        view.queue_list.setCurrentRow(-1)

        view._remove_selected()

        assert len(ctx.player.queue) == 1


class TestButtons:
    def test_the_back_button_emits_now_playing_back_requested(self, ctx, view):
        requests = []
        ctx.nowPlayingBackRequested.connect(lambda: requests.append(True))

        view.header.itemAt(0).widget().click()

        assert requests == [True]

    def test_the_clear_queue_button_clears_the_players_queue(self, ctx, view, tmp_path):
        ctx.player.play_tracks([make_queue_item(tmp_path, title="One")])
        assert len(ctx.player.queue) == 1
        clear_button = view.header.itemAt(3).widget()

        clear_button.click()

        assert ctx.player.queue == []
