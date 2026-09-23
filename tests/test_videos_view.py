"""Tests for ui/views/videos.py: the videos table view - loading rows into
VideoTable, the "only load once, then wait for an explicit change signal"
refresh contract, opening a video into the embedded player pane (missing
file / missing DB row guards included), and the collapsed-search-tile
"focus this artist's group" jump.

`VideoPlayerPanel.play` isn't exercised for real here - it drives an actual
QMediaPlayer/VideoController, which needs a real playable video file and
buys this suite nothing over confirming *what* VideosView asks it to play.
`player_panel.play` is monkeypatched to a spy instead, the same "assert it
was asked to do the right thing" boundary conftest.py's own docstring
recommends for real playback/threading machinery.
"""

from __future__ import annotations

import pytest

from musicmgr.db.models import Video
from musicmgr.ui.views.videos import PANE_PLAYER, PANE_TABLE, VideosView


@pytest.fixture
def view(ctx):
    return VideosView(ctx)


def make_video(session, title, *, artist=None, path=None, **fields) -> Video:
    video = Video(
        title=title,
        title_key=title.lower(),
        artist_display=artist,
        path=path or f"/videos/{title}.mp4",
        **fields,
    )
    session.add(video)
    session.flush()
    return video


class TestRefresh:
    def test_first_refresh_loads_videos(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "One")

        view.refresh()

        assert view.table.count() == 1
        assert view._loaded is True

    def test_a_second_refresh_does_not_reload(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "One")
        view.refresh()

        with ctx.session() as session:
            make_video(session, "Two")
        view.refresh()  # _loaded is already True - must not pick up "Two"

        assert view.table.count() == 1

    def test_videos_changed_forces_a_reload(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "One")
        view.refresh()

        with ctx.session() as session:
            make_video(session, "Two")
        ctx.videosChanged.emit()

        assert view.table.count() == 2
        assert view._loaded is True


class TestLoadVideos:
    def test_row_fields_are_carried_across_from_the_model(self, ctx, view):
        with ctx.session() as session:
            make_video(
                session, "My Video", artist="Some Artist", year=2001, duration_ms=90_000
            )

        view.refresh()

        rows = view.table._rows
        assert len(rows) == 1
        row = rows[0]
        assert row.title == "My Video"
        assert row.artist == "Some Artist"
        assert row.year == 2001
        assert row.duration_ms == 90_000
        assert row.sort_key == "my video"

    def test_a_video_with_no_artist_display_gets_an_empty_string_not_none(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "Orphan Video", artist=None)

        view.refresh()

        assert view.table._rows[0].artist == ""

    def test_stats_label_reports_count_and_total_duration(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "One", duration_ms=60_000)
            make_video(session, "Two", duration_ms=120_000)

        view.refresh()

        assert view.stats.text() == "2 videos · 3:00"

    def test_stats_label_is_singular_for_exactly_one_video(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "One", duration_ms=60_000)

        view.refresh()

        assert view.stats.text() == "1 video · 1:00"

    def test_an_empty_library_reports_zero_videos(self, ctx, view):
        view.refresh()

        assert view.stats.text() == "0 videos · --:--"

    def test_videos_with_no_duration_do_not_count_toward_the_total(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "Known", duration_ms=60_000)
            make_video(session, "Unknown Duration", duration_ms=None)

        view.refresh()

        assert view.stats.text() == "2 videos · 1:00"


class TestOpenVideo:
    def test_playing_an_existing_file_switches_to_the_player_pane(self, ctx, view, tmp_path, monkeypatch):
        real_file = tmp_path / "clip.mp4"
        real_file.write_bytes(b"fake video bytes")
        with ctx.session() as session:
            video = make_video(
                session, "Playable", artist="Some Artist", path=str(real_file)
            )
            video_id = video.id

        calls = []
        monkeypatch.setattr(
            view.player_panel, "play", lambda *a: calls.append(a)
        )

        view._open_video(video_id)

        assert view.panes.currentIndex() == PANE_PLAYER
        assert calls == [(video_id, str(real_file), "Playable", "Some Artist")]

    def test_a_video_flagged_missing_notifies_and_stays_on_the_table(
        self, ctx, view, tmp_path, monkeypatch
    ):
        real_file = tmp_path / "clip.mp4"
        real_file.write_bytes(b"fake video bytes")  # exists on disk...
        with ctx.session() as session:
            video = make_video(session, "Flagged Missing", path=str(real_file), is_missing=True)
            video_id = video.id  # ...but the db says it's missing anyway

        calls = []
        monkeypatch.setattr(view.player_panel, "play", lambda *a: calls.append(a))
        notifications = []
        ctx.notified.connect(notifications.append)

        view._open_video(video_id)

        assert calls == []
        assert view.panes.currentIndex() == PANE_TABLE
        assert notifications and "missing" in notifications[0].lower()

    def test_a_video_whose_file_no_longer_exists_on_disk_notifies(
        self, ctx, view, tmp_path, monkeypatch
    ):
        gone_path = str(tmp_path / "does-not-exist.mp4")
        with ctx.session() as session:
            video = make_video(session, "Gone", path=gone_path, is_missing=False)
            video_id = video.id

        calls = []
        monkeypatch.setattr(view.player_panel, "play", lambda *a: calls.append(a))
        notifications = []
        ctx.notified.connect(notifications.append)

        view._open_video(video_id)

        assert calls == []
        assert view.panes.currentIndex() == PANE_TABLE
        assert notifications and "missing" in notifications[0].lower()

    def test_an_unknown_video_id_is_a_no_op(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(view.player_panel, "play", lambda *a: calls.append(a))
        notifications = []
        ctx.notified.connect(notifications.append)

        view._open_video(999_999)

        assert calls == []
        assert notifications == []
        assert view.panes.currentIndex() == PANE_TABLE


class TestBrowseChrome:
    """2026-09-23: the page title, stats, search row and A-Z jump bar all
    hide while the player pane is up, so the video gets that height (see
    VideosView._set_browse_chrome_visible).

    isHidden(), not isVisible(), throughout - same reason as
    TestJumpBar's own note: nothing here is ever shown in a real window.
    """

    def _play_something(self, ctx, view, tmp_path, monkeypatch):
        real_file = tmp_path / "clip.mp4"
        real_file.write_bytes(b"fake video bytes")
        with ctx.session() as session:
            # enough videos that the jump bar would otherwise be showing,
            # so hiding it is actually being asserted
            for i in range(10):
                make_video(session, f"{chr(65 + i)} Song")
            video = make_video(session, "Playable", path=str(real_file))
            video_id = video.id
        view.refresh()
        monkeypatch.setattr(view.player_panel, "play", lambda *a: None)
        view._open_video(video_id)

    def test_chrome_is_visible_on_the_table_pane(self, ctx, view):
        with ctx.session() as session:
            for i in range(10):
                make_video(session, f"{chr(65 + i)} Song")

        view.refresh()

        assert view.title.isHidden() is False
        assert view.stats.isHidden() is False
        assert view.search_row.isHidden() is False
        assert view.jump_bar.isHidden() is False

    def test_opening_a_video_hides_all_of_it(self, ctx, view, tmp_path, monkeypatch):
        self._play_something(ctx, view, tmp_path, monkeypatch)

        assert view.panes.currentIndex() == PANE_PLAYER
        assert view.title.isHidden() is True
        assert view.stats.isHidden() is True
        assert view.search_row.isHidden() is True
        assert view.jump_bar.isHidden() is True

    def test_focusing_an_artist_brings_it_all_back(self, ctx, view, tmp_path, monkeypatch):
        self._play_something(ctx, view, tmp_path, monkeypatch)

        view._focus_artist("A Song")

        assert view.panes.currentIndex() == PANE_TABLE
        assert view.title.isHidden() is False
        assert view.stats.isHidden() is False
        assert view.search_row.isHidden() is False
        assert view.jump_bar.isHidden() is False

    def test_a_short_list_keeps_its_jump_bar_hidden_on_the_way_back(
        self, ctx, view, tmp_path, monkeypatch
    ):
        """Restoring the chrome must not override the jump bar's own
        "too few letters to bother" rule (_refresh_jump_bar)."""
        real_file = tmp_path / "clip.mp4"
        real_file.write_bytes(b"fake video bytes")
        with ctx.session() as session:
            video = make_video(session, "Only One", artist="Solo", path=str(real_file))
            video_id = video.id
        view.refresh()
        monkeypatch.setattr(view.player_panel, "play", lambda *a: None)
        view._open_video(video_id)

        view._focus_artist("Solo")

        assert view.search_row.isHidden() is False
        assert view.jump_bar.isHidden() is True


class TestFocusArtist:
    def test_switches_to_the_table_pane_and_focuses_the_artist_group(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            make_video(session, "Song", artist="Target Artist")
        view.refresh()
        # land on the player pane first, as if a video had just been opened
        view.panes.setCurrentIndex(PANE_PLAYER)

        calls = []
        monkeypatch.setattr(view.table, "focus_artist", lambda artist: calls.append(artist))

        view._focus_artist("Target Artist")

        assert view.panes.currentIndex() == PANE_TABLE
        assert calls == ["Target Artist"]


class TestSearch:
    def test_typing_in_the_search_box_filters_the_table_by_title_or_artist(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "Take On Me", artist="a-ha")
            make_video(session, "Chiquitita", artist="ABBA")
        view.refresh()

        view.search_box.setText("abba")

        assert view.table.leaf_titles() == ["Chiquitita"]

        view.search_box.clear()

        assert sorted(view.table.leaf_titles()) == ["Chiquitita", "Take On Me"]


class TestJumpBar:
    def test_tapping_a_letter_types_it_into_the_search_box(self, ctx, view):
        with ctx.session() as session:
            for i in range(10):
                make_video(session, f"{chr(65 + i)} Song")
        view.refresh()

        view._on_letter_typed("C")

        assert view.search_box.text() == "C"

    def test_hash_is_never_typed(self, ctx, view):
        view._on_letter_typed("#")

        assert view.search_box.text() == ""

    def test_tapping_a_letter_scrolls_the_table_to_it(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            for i in range(10):
                make_video(session, f"{chr(65 + i)} Song")
        view.refresh()

        calls = []
        monkeypatch.setattr(view.table, "scroll_to_letter", calls.append)

        view._on_letter_key("C")

        assert calls == ["C"]

    def test_jump_bar_hides_for_a_short_list(self, ctx, view):
        with ctx.session() as session:
            make_video(session, "One")

        view.refresh()

        # isHidden(), not isVisible() - this view is never actually shown
        # in a window in this headless test, so isVisible() is always False
        # regardless of setVisible() - isHidden() reflects the explicit
        # flag setVisible() actually sets, independent of on-screen state
        assert view.jump_bar.isHidden() is True

    def test_jump_bar_shows_and_updates_for_a_long_list(self, ctx, view):
        with ctx.session() as session:
            for i in range(10):
                make_video(session, f"{chr(65 + i)} Song")

        view.refresh()

        assert view.jump_bar.isHidden() is False
