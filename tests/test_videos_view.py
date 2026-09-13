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
