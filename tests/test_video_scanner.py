"""Tests for services/video_scanner.py: the folder walk, the tagless
"Artist - Title" / "Artist/Title" filename-convention parser (there is no
tag-reading equivalent to mutagen's ID3/Vorbis support for arbitrary video
containers), best-effort duration reading, and end-to-end scan bookkeeping.

Most fixtures here are tiny placeholder files with a real video extension
but no real container bytes - `import_video_file` never actually needs
readable video data (only `path.stat()` and the filename/folder), and
`read_video_duration` is documented to swallow any read failure and return
None, so a placeholder file is enough to exercise everything except real
duration reading. For that one case, `make_real_mp4` shells out to ffmpeg
for an actual short, silent, playable MP4 - skipped if ffmpeg isn't on
PATH, the same guard this project's own test_spectrum.py uses for the same
reason (a real encoder isn't guaranteed to be on every dev machine).
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import Artist, Video, WatchedFolder
from musicmgr.services import video_scanner

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def make_placeholder_video(path: Path, content: bytes = b"not a real video") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def make_real_mp4(path: Path, seconds: float = 1.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=32x32:d={seconds}",
            "-t", str(seconds), "-pix_fmt", "yuv420p", "-an", str(path),
        ],
        check=True, capture_output=True,
    )


class TestIterVideoFiles:
    def test_finds_only_recognized_video_extensions(self, tmp_path):
        make_placeholder_video(tmp_path / "clip.mp4")
        (tmp_path / "cover.jpg").write_bytes(b"not a video")
        (tmp_path / "notes.txt").write_text("not a video either")

        found = list(video_scanner.iter_video_files(tmp_path))

        assert found == [tmp_path / "clip.mp4"]

    def test_skips_dotfiles_and_dot_directories(self, tmp_path):
        make_placeholder_video(tmp_path / "visible.mp4")
        make_placeholder_video(tmp_path / ".hidden.mp4")
        make_placeholder_video(tmp_path / ".git" / "ghost.mp4")

        found = list(video_scanner.iter_video_files(tmp_path))

        assert found == [tmp_path / "visible.mp4"]

    def test_a_name_starting_with_a_dot_and_a_digit_is_found(self, tmp_path):
        # 2026-10-25: D:\\Videos\\.38 Special - Caught Up in You.mp4 was never imported
        video = tmp_path / ".38 Special - Caught Up in You.mp4"
        make_placeholder_video(video)

        assert list(video_scanner.iter_video_files(tmp_path)) == [video]

    def test_parses_the_artist_of_a_dot_and_digit_name(self):
        title, artist = video_scanner._parse_video_title_artist(
            Path("/videos/.38 Special - Hold On Loosely (Live).mp4"))

        assert artist == ".38 Special"
        assert title == "Hold On Loosely (Live)"

    def test_recurses_into_subfolders(self, tmp_path):
        nested = tmp_path / "Artist" / "Some Video.mkv"
        make_placeholder_video(nested)

        found = list(video_scanner.iter_video_files(tmp_path))

        assert found == [nested]


class TestParseVideoTitleArtist:
    def test_artist_dash_title_convention(self):
        path = Path("/videos/The Meridian Set - Slow Repeater.mp4")

        title, artist = video_scanner._parse_video_title_artist(path)

        assert title == "Slow Repeater"
        assert artist == "The Meridian Set"

    def test_plain_title_inside_an_artist_folder(self):
        path = Path("/videos/Dana Voss/Live at the Grand.mp4")

        title, artist = video_scanner._parse_video_title_artist(path)

        assert title == "Live at the Grand"
        assert artist == "Dana Voss"

    def test_a_dash_with_no_real_title_falls_back_to_the_folder(self):
        # "Artist - " with nothing after the dash: partition succeeds but
        # the title half is empty after stripping, so this must not be
        # treated as a real "Artist - Title" match.
        path = Path("/videos/Some Band/Some Band - .mp4")

        title, artist = video_scanner._parse_video_title_artist(path)

        assert artist == "Some Band"  # from the folder, not the empty dash-split
        assert title == "Some Band -"

    def test_no_folder_and_no_dash_falls_back_to_unknown_artist(self):
        path = Path("Standalone Clip.mp4")

        title, artist = video_scanner._parse_video_title_artist(path)

        assert title == "Standalone Clip"
        assert artist == "Unknown Artist"

    def test_a_whitespace_only_stem_falls_back_to_untitled(self):
        # a leading-dot filename like ".mp4" has no separate stem in
        # pathlib (the whole name IS the stem), so this uses a real,
        # if unusual, whitespace-only filename to reach the actual
        # `stem.strip() or "Untitled"` fallback.
        path = Path("/videos/Some Artist/   .mp4")

        title, artist = video_scanner._parse_video_title_artist(path)

        assert title == "Untitled"
        assert artist == "Some Artist"


class TestReadVideoDuration:
    def test_an_unreadable_file_returns_none_without_raising(self, tmp_path):
        path = tmp_path / "garbage.mkv"
        make_placeholder_video(path, content=b"definitely not a real video container")

        assert video_scanner.read_video_duration(path) is None

    @pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not available to encode a real fixture")
    def test_reads_duration_from_a_real_mp4(self, tmp_path):
        path = tmp_path / "real.mp4"
        make_real_mp4(path, seconds=1.5)

        duration = video_scanner.read_video_duration(path)

        assert duration is not None
        assert 1300 <= duration <= 1700  # ~1500ms, some encoder slack


class TestImportVideoFile:
    def test_adds_a_new_video_with_title_and_artist_from_the_path(self, session, tmp_path):
        path = tmp_path / "The Meridian Set" / "Odd Hours.mp4"
        make_placeholder_video(path)
        result = video_scanner.ScanResult()

        video = video_scanner.import_video_file(session, path, result)

        assert result.added == 1
        assert video.title == "Odd Hours"
        assert video.artist_display == "The Meridian Set"
        artist = session.get(Artist, video.artist_id)
        assert artist.name == "The Meridian Set"

    def test_rescanning_an_unchanged_file_does_not_duplicate_it(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        result = video_scanner.ScanResult()
        video_scanner.import_video_file(session, path, result)

        video_scanner.import_video_file(session, path, result)

        assert result.added == 1
        assert result.unchanged == 1
        assert session.scalar(select(func.count(Video.id))) == 1

    def test_a_changed_file_updates_the_same_row_instead_of_adding_a_new_one(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path, content=b"version one")
        result = video_scanner.ScanResult()
        video_scanner.import_video_file(session, path, result)

        make_placeholder_video(path, content=b"a longer version two, different size")
        video_scanner.import_video_file(session, path, result)

        assert result.added == 1
        assert result.updated == 1
        assert session.scalar(select(func.count(Video.id))) == 1

    def test_two_videos_by_the_same_artist_folder_share_one_artist_row(self, session, tmp_path):
        result = video_scanner.ScanResult()
        v1 = video_scanner.import_video_file(
            session, self._make(tmp_path, "Shared Artist", "First.mp4"), result
        )
        v2 = video_scanner.import_video_file(
            session, self._make(tmp_path, "Shared Artist", "Second.mp4"), result
        )

        assert v1.artist_id == v2.artist_id
        assert session.scalar(select(func.count(Artist.id))) == 1

    @staticmethod
    def _make(tmp_path, artist_folder, filename):
        path = tmp_path / artist_folder / filename
        make_placeholder_video(path)
        return path


class TestScanVideoFolder:
    def test_adds_files_and_creates_the_watched_folder_row(self, session, tmp_path):
        make_placeholder_video(tmp_path / "Artist" / "Clip.mp4")

        result = video_scanner.scan_video_folder(session, tmp_path)

        assert result.added == 1
        assert result.scanned == 1
        folder = session.scalar(
            select(WatchedFolder).where(WatchedFolder.path == str(tmp_path))
        )
        assert folder is not None
        assert folder.last_scan_at is not None

    def test_a_folder_that_does_not_exist_reports_an_error_and_does_not_crash(self, session, tmp_path):
        missing = tmp_path / "does-not-exist"

        result = video_scanner.scan_video_folder(session, missing)

        assert result.errors
        assert result.scanned == 0

    def test_a_failing_file_is_recorded_as_an_error_without_aborting_the_scan(
        self, session, tmp_path, monkeypatch
    ):
        make_placeholder_video(tmp_path / "Artist" / "Bad Video.mp4")
        make_placeholder_video(tmp_path / "Artist" / "Good Video.mp4")

        real_import = video_scanner.import_video_file

        def flaky_import(session, path, result, **kwargs):
            if path.name == "Bad Video.mp4":
                raise RuntimeError("simulated import failure")
            return real_import(session, path, result, **kwargs)

        monkeypatch.setattr(video_scanner, "import_video_file", flaky_import)

        result = video_scanner.scan_video_folder(session, tmp_path)

        assert result.scanned == 2
        assert result.errors
        assert session.scalar(select(func.count(Video.id))) == 1


class TestScanVideoFolderShouldStop:
    """2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings" - same should_stop/break shape as
    scanner.py:scan_folder's own tests, since this is the video half of
    the exact same ScanThread run."""

    def test_should_stop_true_from_the_start_imports_nothing_but_still_stamps_the_folder(
        self, session, tmp_path
    ):
        make_placeholder_video(tmp_path / "Artist" / "Clip.mp4")

        result = video_scanner.scan_video_folder(session, tmp_path, should_stop=lambda: True)

        assert result.added == 0
        assert session.scalar(select(Video)) is None
        folder = session.scalar(
            select(WatchedFolder).where(WatchedFolder.path == str(tmp_path))
        )
        assert folder is not None and folder.last_scan_at is not None

    def test_should_stop_partway_through_keeps_the_video_already_imported(
        self, session, tmp_path
    ):
        make_placeholder_video(tmp_path / "Artist" / "First.mp4")
        make_placeholder_video(tmp_path / "Artist" / "Second.mp4")

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        result = video_scanner.scan_video_folder(session, tmp_path, should_stop=should_stop)

        assert result.added == 1
        assert session.scalar(select(func.count(Video.id))) == 1


class TestMarkMissingVideosShouldStop:
    def test_should_stop_true_from_the_start_flags_nothing(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        video_scanner.scan_video_folder(session, tmp_path)
        path.unlink()

        changed = video_scanner.mark_missing_videos(session, should_stop=lambda: True)

        assert changed == 0
        assert session.scalar(select(Video)).is_missing is False


class TestMarkMissingVideos:
    def test_flags_a_video_whose_file_no_longer_exists(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        video_scanner.scan_video_folder(session, tmp_path)
        path.unlink()

        changed = video_scanner.mark_missing_videos(session)

        assert changed == 1
        assert session.scalar(select(Video)).is_missing is True

    def test_clears_missing_once_the_file_reappears(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        video_scanner.scan_video_folder(session, tmp_path)
        data = path.read_bytes()
        path.unlink()
        video_scanner.mark_missing_videos(session)

        path.write_bytes(data)
        changed = video_scanner.mark_missing_videos(session)

        # matches services/scanner.py:mark_missing_files' own contract -
        # only newly-missing files bump the count, not files reappearing.
        assert changed == 0
        assert session.scalar(select(Video)).is_missing is False

    def test_progress_callback_reports_the_final_total(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        video_scanner.scan_video_folder(session, tmp_path)

        calls = []
        video_scanner.mark_missing_videos(session, progress=lambda done, total, name: calls.append((done, total)))

        assert calls
        assert calls[-1] == (1, 1)


class TestPurgeMissingVideos:
    def test_deletes_a_missing_video(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        video_scanner.scan_video_folder(session, tmp_path)
        path.unlink()
        video_scanner.mark_missing_videos(session)

        removed = video_scanner.purge_missing_videos(session)

        assert removed == 1
        assert session.scalar(select(func.count(Video.id))) == 0

    def test_a_present_video_is_never_purged(self, session, tmp_path):
        path = tmp_path / "Artist" / "Clip.mp4"
        make_placeholder_video(path)
        video_scanner.scan_video_folder(session, tmp_path)

        removed = video_scanner.purge_missing_videos(session)

        assert removed == 0
        assert session.scalar(func.count(Video.id)) == 1


class TestRescanAllVideos:
    def test_scans_every_enabled_watched_folder_and_marks_missing_files(self, session, tmp_path):
        folder_a = tmp_path / "a"
        folder_b = tmp_path / "b"
        path_a = folder_a / "Artist" / "One.mp4"
        path_b = folder_b / "Artist" / "Two.mp4"
        make_placeholder_video(path_a)
        make_placeholder_video(path_b)
        session.add(WatchedFolder(path=str(folder_a)))
        session.add(WatchedFolder(path=str(folder_b)))
        session.flush()

        result = video_scanner.rescan_all_videos(session)

        assert result.added == 2
        assert session.scalar(select(func.count(Video.id))) == 2

    def test_a_disabled_folder_is_not_scanned(self, session, tmp_path):
        folder = tmp_path / "disabled"
        make_placeholder_video(folder / "Artist" / "One.mp4")
        session.add(WatchedFolder(path=str(folder), enabled=False))
        session.flush()

        result = video_scanner.rescan_all_videos(session)

        assert result.added == 0
        assert session.scalar(select(func.count(Video.id))) == 0


class TestForceRescan:
    """Covers the 2026-09-18 "Re-read all tags (takes more time)" scan
    option (see services/scanner.py's matching TestForceRescan and
    ui/views/settings/'s ScanThread/force_rescan_checkbox) - an unchanged
    video file is ordinarily left alone entirely; force=True makes
    import_video_file/scan_video_folder re-import it anyway."""

    def test_an_unchanged_file_is_left_alone_without_force(self, session, tmp_path):
        make_placeholder_video(tmp_path / "Artist" / "One.mp4")
        video_scanner.scan_video_folder(session, tmp_path)

        result = video_scanner.scan_video_folder(session, tmp_path)

        assert result.unchanged == 1
        assert result.updated == 0

    def test_force_reimports_an_unchanged_file_anyway(self, session, tmp_path):
        make_placeholder_video(tmp_path / "Artist" / "One.mp4")
        video_scanner.scan_video_folder(session, tmp_path)

        result = video_scanner.scan_video_folder(session, tmp_path, force=True)

        assert result.updated == 1
        assert result.unchanged == 0
        # still exactly one Video row - force re-reads, it doesn't duplicate
        assert session.scalar(select(func.count(Video.id))) == 1

    def test_force_threads_through_rescan_all_videos(self, session, tmp_path):
        folder = tmp_path / "watched"
        make_placeholder_video(folder / "Artist" / "One.mp4")
        session.add(WatchedFolder(path=str(folder)))
        session.flush()
        video_scanner.rescan_all_videos(session)

        result = video_scanner.rescan_all_videos(session, force=True)

        assert result.updated == 1
        assert result.unchanged == 0


# -- videos an older scan took from a now-skipped folder (2026-10-21) ---------


def _video_row(session, path: Path, artist_name: str) -> Video:
    """A Video row as an older, pre-skip scan would have left it."""
    from musicmgr.services import library as lib

    artist = lib.get_or_create_artist(session, artist_name)
    video = Video(
        title=path.stem, title_key=path.stem.lower(), artist_display=artist.name,
        artist_id=artist.id, path=str(path), size_bytes=1, mtime=0.0, codec="mp4",
    )
    session.add(video)
    session.flush()
    return video


def test_remove_skipped_folder_videos_drops_gsdata_rows_and_empty_artist(session, tmp_path):
    """PC1 had 15 videos from D:\\Movies\\_gsdata_\\_saved_, four filed under
    an artist called "_saved_" (the folder name). They come back out, the
    "_saved_" artist goes with them, a real artist with other videos stays,
    and no file on disk is touched."""
    real = tmp_path / "Movies" / "Rush - R30.mkv"
    make_placeholder_video(real)
    saved_dir = tmp_path / "Movies" / "_gsdata_" / "_saved_"
    stray_named = saved_dir / "Rush - R30.mkv"
    stray_folder_artist = saved_dir / "JEREMY_CAMP_UNPLUGGED_Title1.mp4"
    for p in (stray_named, stray_folder_artist):
        make_placeholder_video(p)

    video_scanner.scan_video_folder(session, tmp_path / "Movies")  # picks up only the real one
    _video_row(session, stray_named, "Rush")
    _video_row(session, stray_folder_artist, "_saved_")
    assert session.scalar(select(func.count(Video.id))) == 3

    assert video_scanner.remove_skipped_folder_videos(session) == 2

    assert [v.path for v in session.scalars(select(Video))] == [str(real)]
    names = set(session.scalars(select(Artist.name)))
    assert "Rush" in names and "_saved_" not in names
    assert stray_named.exists() and stray_folder_artist.exists()


def test_rescan_all_videos_runs_the_skipped_folder_cleanup(session, tmp_path):
    stray = tmp_path / "_gsdata_" / "_saved_" / "Old Copy.mp4"
    make_placeholder_video(stray)
    session.add(WatchedFolder(path=str(tmp_path)))
    session.flush()
    _video_row(session, stray, "_saved_")

    result = video_scanner.rescan_all_videos(session)

    assert result.skipped_removed == 1
    assert session.scalar(select(func.count(Video.id))) == 0
    assert "1 copies in sync/system folders removed" in result.summary()



# -- guests out of the video's artist (2026-10-21) ------------------------------


@pytest.mark.parametrize("name, title, artist", [
    ("for KING & COUNTRY ft. Courtney - Without You.mp4", "Without You (feat. Courtney)", "for KING & COUNTRY"),
    ("for KING & COUNTRY ft. Lecrae & The WRLDFMS Tony Williams - Amen (Reborn).mp4",
     "Amen (Reborn) (feat. Lecrae & The WRLDFMS Tony Williams)", "for KING & COUNTRY"),
    ("Daft Punk feat. Pharrell Williams - Get Lucky.mp4", "Get Lucky (feat. Pharrell Williams)", "Daft Punk"),
    ("Dawn (featuring Tony Orlando) - Knock Three Times.mp4", "Knock Three Times (feat. Tony Orlando)", "Dawn"),
    # a title that already names the guest isn't given a second one
    ("Nelly ft. Kelly Rowland - Dilemma (feat. Kelly Rowland).mp4", "Dilemma (feat. Kelly Rowland)", "Nelly"),
    # "&" without ft. is one act - a video has only one artist
    ("Simon & Garfunkel - The Boxer.mp4", "The Boxer", "Simon & Garfunkel"),
    ("for KING & COUNTRY - TOGETHER (feat. Kirk Franklin & Tori Kelly) [Official Music Video].mp4",
     "TOGETHER (feat. Kirk Franklin & Tori Kelly) [Official Music Video]", "for KING & COUNTRY"),
])
def test_featured_guest_moves_from_artist_to_title(name, title, artist):
    assert video_scanner._parse_video_title_artist(Path("D:/Videos") / name) == (title, artist)


def test_repair_video_artists_files_guests_and_spellings_under_one_artist(session, tmp_path):
    """PC1: five "for KING & COUNTRY ft. X" artists plus "FOR KING +
    COUNTRY", one video each, beside the real for KING & COUNTRY."""
    from musicmgr.services import artist_merge

    band = _video_row(session, tmp_path / "for KING & COUNTRY - Priceless.mp4", "for KING & COUNTRY")
    guest = _video_row(session, tmp_path / "for KING & COUNTRY ft. Courtney - Without You.mp4",
                       "for KING & COUNTRY ft. Courtney")
    plus = _video_row(session, tmp_path / "for KING + COUNTRY - Drummer Boy Live.m4v", "FOR KING + COUNTRY")
    artist_merge.apply_artist_merges(session, [("for KING & COUNTRY", ("for KING + COUNTRY",))])

    assert video_scanner.repair_video_artists(session) == 3  # (these rows' titles were the whole file name)

    for video in (band, guest, plus):
        session.refresh(video)
        assert video.artist_display == "for KING & COUNTRY"
    assert guest.title == "Without You (feat. Courtney)"
    names = set(session.scalars(select(Artist.name)))
    assert not names & {"for KING & COUNTRY ft. Courtney", "FOR KING + COUNTRY"}
    assert video_scanner.repair_video_artists(session) == 0
