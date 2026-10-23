"""Video.media_type (2026-10-23): music video vs. movie as its own column,
replacing the old habit of labelling video rows with a made-up "Video"
genre - see db/models.py:Video, services/video_scanner.py:
media_type_for_path / classify_media_types, and the Videos page's tabs
(ui/views/videos.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from sqlalchemy import select

from musicmgr.db import session as db_session
from musicmgr.db.models import Video
from musicmgr.services import video_scanner
from musicmgr.services import videos as vid_svc
from musicmgr.services.scanner import ScanResult

MOVIE, MV = Video.MEDIA_MOVIE, Video.MEDIA_MUSIC_VIDEO


class TestMediaTypeForPath:
    @pytest.mark.parametrize("path, expected", [
        (PureWindowsPath(r"D:\Movies\Rush - R30.mkv"), MOVIE),
        (PureWindowsPath(r"D:\Movies\Michael W. Smith\20 Yr Celebration.mp4"), MOVIE),
        (PureWindowsPath(r"E:\MOVIES\Film.mp4"), MOVIE),
        (PureWindowsPath(r"D:\Videos\Skid Row - 18 And Life.mp4"), MV),
        (PurePosixPath("/home/james/Videos/Rush/2112.mp4"), MV),
        (PurePosixPath("/media/usb/Movies/Concert.mkv"), MOVIE),
        # only a *folder* named Movies counts, not a file name
        (PureWindowsPath(r"D:\Videos\Movies.mp4"), MV),
        (PureWindowsPath(r"D:\Videos\Movies Soundtrack Hits\clip.mp4"), MV),
    ])
    def test_classifies_by_folder(self, path, expected):
        assert video_scanner.media_type_for_path(path) == expected

    def test_a_windows_path_string_read_on_linux(self):
        # library.db carried from PC1 to the Mint PC keeps D:\ paths
        assert video_scanner.media_type_for_path(r"D:\Movies\Film.mp4") == MOVIE
        assert video_scanner.media_type_for_path(r"D:\Videos\Clip.mp4") == MV


def _placeholder(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real video")
    return path


class TestScanner:
    def test_import_sets_media_type(self, session, tmp_path):
        clip = _placeholder(tmp_path / "Videos" / "Band - Clip.mp4")
        film = _placeholder(tmp_path / "Movies" / "Band - Concert.mkv")

        video_scanner.import_video_file(session, clip, ScanResult())
        video_scanner.import_video_file(session, film, ScanResult())

        kinds = {v.title: v.media_type for v in session.scalars(select(Video))}
        assert kinds == {"Clip": MV, "Concert": MOVIE}

    def test_an_unchanged_file_still_gets_its_media_type(self, session, tmp_path):
        film = _placeholder(tmp_path / "Movies" / "Band - Concert.mkv")
        video = video_scanner.import_video_file(session, film, ScanResult())
        video.media_type = MV  # as an older library would have it
        session.flush()

        result = ScanResult()
        video_scanner.import_video_file(session, film, result)

        assert result.unchanged == 1
        assert video.media_type == MOVIE

    def test_classify_media_types_fixes_existing_rows(self, session):
        session.add_all([
            Video(title="A", title_key="a", path=r"D:\Movies\A.mkv"),
            Video(title="B", title_key="b", path=r"D:\Videos\B.mp4"),
            Video(title="C", title_key="c", path="/x/Movies/C.mp4", media_type=MOVIE),
        ])
        session.flush()

        assert video_scanner.classify_media_types(session) == 1
        kinds = {v.title: v.media_type for v in session.scalars(select(Video))}
        assert kinds == {"A": MOVIE, "B": MV, "C": MOVIE}
        assert video_scanner.classify_media_types(session) == 0


class TestMigration:
    def test_an_older_library_gets_the_column_defaulting_to_music_video(self, qapp, tmp_path):
        path = tmp_path / "library.db"
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE videos (id INTEGER PRIMARY KEY, title VARCHAR(500), "
            "title_key VARCHAR(500), artist_display VARCHAR(500), artist_id INTEGER, "
            "year INTEGER, duration_ms INTEGER, path TEXT UNIQUE, size_bytes INTEGER, "
            "mtime FLOAT, codec VARCHAR(40), thumbnail_path TEXT, is_missing BOOLEAN, "
            "play_count INTEGER, last_played_at DATETIME, added_at DATETIME, "
            "last_seen_at DATETIME)")
        con.execute("INSERT INTO videos (title, title_key, path) VALUES ('Old', 'old', 'D:\\Movies\\Old.mkv')")
        con.commit()
        con.close()

        db_session.init_engine(db_path=str(path))
        with db_session.session_scope() as s:
            video = s.scalar(select(Video))
            assert video.media_type == MV  # the column default...
            video_scanner.classify_media_types(s)
            assert video.media_type == MOVIE  # ...until the one-time pass


class TestSearchRows:
    def test_rows_carry_media_type(self, session):
        session.add(Video(title="Film", title_key="film", path="/m/Movies/Film.mp4",
                          media_type=MOVIE))
        session.flush()

        (row,) = vid_svc.list_videos_for_search(session)

        assert row["media_type"] == MOVIE

    def test_added_rank_puts_the_newest_highest(self, session):
        import datetime as dt
        base = dt.datetime(2026, 1, 1)
        session.add_all([
            Video(title="Old", title_key="old", path="/v/old.mp4", added_at=base),
            Video(title="New", title_key="new", path="/v/new.mp4",
                  added_at=base + dt.timedelta(days=5)),
        ])
        session.flush()

        ranks = {r["title"]: r["added_rank"] for r in vid_svc.list_videos_for_search(session)}

        assert ranks["New"] > ranks["Old"]


class TestTitleDetails:
    def test_video_rows_have_no_made_up_genre(self, ctx):
        from musicmgr.ui.views.library import LibraryView

        with ctx.session() as s:
            s.add(Video(title="Concert", title_key="concert", artist_display="Rush",
                        path="/m/Movies/Concert.mkv", media_type=MOVIE))
        view = LibraryView(ctx)
        view.select_mode("details")

        rows = [r for r in view.details_table._all_rows if r.is_video]
        assert len(rows) == 1
        assert rows[0].genre == ""
        assert rows[0].media_type == MOVIE

    def test_searching_movie_finds_movie_rows(self, ctx):
        from musicmgr.ui.widgets.track_details_table import TrackDetailRow, TrackDetailsTable

        table = TrackDetailsTable()
        common = dict(track_id=0, genre="", album_artist="Rush", album="", track_no=None,
                      duration_ms=None, year=None, rating=None, is_video=True)
        table.set_rows([
            TrackDetailRow(title="▶ R30", sort_key="r30", video_id=1, media_type=MOVIE, **common),
            TrackDetailRow(title="▶ 2112", sort_key="2112", video_id=2, media_type=MV, **common),
        ])

        table.set_filter_text("movie")
        assert table.count() == 1

        table.set_filter_text("music video")
        assert table.count() == 1

    def test_the_videos_only_checkbox_is_gone(self, qapp):
        from musicmgr.ui.widgets.track_details_table import TrackDetailsTable

        assert not hasattr(TrackDetailsTable(), "videos_only_checkbox")


def test_videos_is_on_the_sidebar():
    from musicmgr.ui.app import NAV_ITEMS

    keys = [k for k, _ in NAV_ITEMS]
    assert ("videos", "Videos") in NAV_ITEMS
    assert keys.index("details") < keys.index("videos") < keys.index("playlists")
