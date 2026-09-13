"""Tests for services/videos.py: the video library's read queries and the
watch-count/play-history bump - the video-side counterpart to
services/library.py, but much smaller since Video is a flat table with no
get-or-create discography to build.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from musicmgr.db.models import Video
from musicmgr.services import library as lib
from musicmgr.services import videos as vid_svc


def make_video(session, title, *, artist=None, artist_display=None, **fields) -> Video:
    artist_id = None
    if artist is not None:
        artist_id = lib.get_or_create_artist(session, artist).id
    video = Video(
        title=title,
        title_key=title.lower(),
        artist_id=artist_id,
        artist_display=artist_display if artist_display is not None else artist,
        path=f"/videos/{title}.mp4",
        **fields,
    )
    session.add(video)
    session.flush()
    return video


class TestListVideos:
    def test_orders_by_title_case_insensitively(self, session):
        make_video(session, "zebra")
        make_video(session, "Apple")
        make_video(session, "banana")

        titles = [v.title for v in vid_svc.list_videos(session)]

        assert titles == ["Apple", "banana", "zebra"]

    def test_filters_by_artist_id(self, session):
        rush = lib.get_or_create_artist(session, "Rush")
        other = lib.get_or_create_artist(session, "Someone Else")
        make_video(session, "Tom Sawyer", artist="Rush")
        make_video(session, "Unrelated Clip", artist="Someone Else")

        titles = [v.title for v in vid_svc.list_videos(session, artist_id=rush.id)]

        assert titles == ["Tom Sawyer"]
        assert other.id != rush.id  # sanity: the two artists are distinct rows


class TestListVideosForSearch:
    def test_prefers_the_linked_artists_current_name(self, session):
        artist = lib.get_or_create_artist(session, "Original Name")
        make_video(session, "Some Clip", artist="Original Name")
        artist.name = "Renamed Artist"  # a rename after the video was scanned
        session.flush()

        rows = vid_svc.list_videos_for_search(session)

        assert rows[0]["artist_name"] == "Renamed Artist"

    def test_falls_back_to_the_denormalized_artist_display_with_no_linked_artist(self, session):
        make_video(session, "Orphan Clip", artist=None, artist_display="Some Old Credit")

        rows = vid_svc.list_videos_for_search(session)

        assert rows[0]["artist_name"] == "Some Old Credit"

    def test_returns_the_expected_fields_and_sort_key(self, session):
        make_video(session, "My Video", artist="Some Artist", year=1999, duration_ms=123_000)

        rows = vid_svc.list_videos_for_search(session)

        row = rows[0]
        assert row["title"] == "My Video"
        assert row["year"] == 1999
        assert row["duration_ms"] == 123_000
        assert row["sort_key"] == "my video"

    def test_orders_by_title_case_insensitively(self, session):
        make_video(session, "zebra", artist="A")
        make_video(session, "Apple", artist="A")

        titles = [r["title"] for r in vid_svc.list_videos_for_search(session)]

        assert titles == ["Apple", "zebra"]


class TestVideoCounts:
    def test_empty_library_is_all_zero(self, session):
        assert vid_svc.video_counts(session) == {
            "videos": 0, "video_missing": 0, "video_total_ms": 0,
        }

    def test_counts_reflect_what_is_in_the_database(self, session):
        make_video(session, "One", duration_ms=100_000)
        make_video(session, "Two", duration_ms=200_000, is_missing=True)

        counts = vid_svc.video_counts(session)

        assert counts["videos"] == 2
        assert counts["video_missing"] == 1
        assert counts["video_total_ms"] == 300_000


class TestRecordWatch:
    def test_increments_play_count_and_sets_last_played_at(self, session):
        video = make_video(session, "Watched Clip")
        assert video.play_count == 0
        assert video.last_played_at is None

        vid_svc.record_watch(session, video.id)

        assert video.play_count == 1
        assert video.last_played_at is not None

    def test_repeated_watches_keep_incrementing(self, session):
        video = make_video(session, "Watched Clip")

        vid_svc.record_watch(session, video.id)
        vid_svc.record_watch(session, video.id)
        vid_svc.record_watch(session, video.id)

        assert video.play_count == 3

    def test_an_unknown_video_id_is_a_no_op(self, session):
        # should not raise
        vid_svc.record_watch(session, 999_999)
