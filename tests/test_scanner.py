"""Tests for services/scanner.py: the folder walk, the tagless-file
filename-convention fallback, the folder-wide album-artist decision, and
an end-to-end scan's bookkeeping (add/unchanged, missing-file marking, and
orphan purging).

Test fixtures are tiny real, tagless WAV files (see `make_silent_wav`) -
enough for mutagen to read basic stream info from without needing a real
encoder, real ID3/Vorfish tags, or a network fetch. Untagged is also the
actually-interesting case for most of this module: it's what a live
bootleg recording or a plain filename-convention rip looks like, and it's
exactly what exercises `_fallback_from_path`'s filename/folder-based
guessing rather than mutagen's own tag reading.
"""

from __future__ import annotations

import datetime as dt
import os
import wave
from pathlib import Path

from sqlalchemy import func, select

from musicmgr.db.models import (
    Artist,
    Chart,
    ChartEntry,
    ChartIssue,
    Credit,
    JukeboxSlot,
    MediaFile,
    PlayEvent,
    Playlist,
    PlaylistItem,
    Release,
    Track,
    WatchedFolder,
)
from musicmgr.services import scanner
from musicmgr.services.matching import normalize


def make_silent_wav(path: Path, seconds: float = 0.2) -> None:
    """A tiny, real, tagless WAV file - just enough for mutagen to read
    stream info from, with no metadata tags at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


def make_mp3_with_comment(path: Path, comment: str) -> None:
    """A tiny, real MP3 (silence) carrying a single ID3 COMM frame - the
    fixture for the 2026-09-17 fix: `read_tags()` used to never see an
    MP3's comment tag at all, no matter what was in it (see
    `TestReadTagsComment` below)."""
    from mutagen.id3 import COMM
    from mutagen.mp3 import MP3

    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    frame = header + bytes(417 - len(header))
    with open(path, "wb") as f:
        for _ in range(5):
            f.write(frame)

    audio = MP3(str(path))
    audio.add_tags()
    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
    audio.save()


class TestIterAudioFiles:
    def test_finds_only_recognized_audio_extensions(self, tmp_path):
        make_silent_wav(tmp_path / "song.wav")
        (tmp_path / "cover.jpg").write_bytes(b"not audio")
        (tmp_path / "notes.txt").write_text("not audio either")

        found = list(scanner.iter_audio_files(tmp_path))

        assert found == [tmp_path / "song.wav"]

    def test_skips_dotfiles_and_dot_directories(self, tmp_path):
        make_silent_wav(tmp_path / "visible.wav")
        make_silent_wav(tmp_path / ".hidden.wav")
        make_silent_wav(tmp_path / ".git" / "ghost.wav")

        found = list(scanner.iter_audio_files(tmp_path))

        assert found == [tmp_path / "visible.wav"]

    def test_recurses_into_subfolders(self, tmp_path):
        nested = tmp_path / "Artist" / "Album" / "01 Track.wav"
        make_silent_wav(nested)

        found = list(scanner.iter_audio_files(tmp_path))

        assert found == [nested]


class TestReadTagsComment:
    """Covers the 2026-09-17 fix: an MP3's ID3 COMM ("comment") frame was
    never read by `read_tags()`, regardless of what was tagged. `src` in
    `read_tags()` is mutagen's *easy* wrapper whenever one loads (true for
    virtually every MP3), and EasyID3 has no "comment" key at all - and
    "COMM::eng" is a raw-ID3 HashKey, not a valid EasyID3 key, so the old
    `_first(src, "comment", "COMM::eng", ...)` call could never find it
    through the easy wrapper. This silently broke every comment-based smart
    playlist rule for MP3s, which is the vast majority of a typical library.
    """

    def test_reads_a_comment_frame_from_a_real_mp3(self, tmp_path):
        path = tmp_path / "song.mp3"
        make_mp3_with_comment(path, "[Billboard] #001# [2020][Pop][2020]")

        tags = scanner.read_tags(path)

        assert tags.comment == "[Billboard] #001# [2020][Pop][2020]"

    def test_matches_regardless_of_the_frames_language_or_description(self, tmp_path):
        # Real-world taggers are inconsistent about the COMM frame's
        # language/description sub-fields, so the fallback must not
        # hardcode "eng" or an empty description.
        from mutagen.id3 import COMM
        from mutagen.mp3 import MP3

        path = tmp_path / "song.mp3"
        make_mp3_with_comment(path, "placeholder")
        audio = MP3(str(path))
        audio.tags.delall("COMM")
        audio.tags.add(
            COMM(encoding=3, lang="XXX", desc="iTunNORM", text=["not this one"])
        )
        audio.tags.add(
            COMM(encoding=3, lang="deu", desc="", text=["[Billboard] #045#"])
        )
        audio.save()

        tags = scanner.read_tags(path)

        assert tags.comment in ("not this one", "[Billboard] #045#")

    def test_an_untagged_mp3_still_has_no_comment(self, tmp_path):
        path = tmp_path / "song.mp3"
        make_mp3_with_comment(path, "")
        # Strip the COMM frame entirely rather than leaving it empty.
        from mutagen.mp3 import MP3

        audio = MP3(str(path))
        audio.tags.delall("COMM")
        audio.save()

        tags = scanner.read_tags(path)

        assert tags.comment == ""


class TestFallbackFromPath:
    """Covers the 2026-09-07 fix: an untagged file's title used to keep its
    raw filename's leading track-number prefix ("01 Midwest Midnight")
    instead of just the song title."""

    def test_strips_a_leading_track_number_from_an_untagged_title(self):
        # read_tags() falls back to the filename stem as the title when a
        # file has no title tag at all - tags.title == path.stem is exactly
        # that case.
        tags = scanner.TrackTags(title="01 Midwest Midnight", artist="", album_artist="")
        path = Path("/music/Some Band/Some Album/01 Midwest Midnight.wav")

        result = scanner._fallback_from_path(path, tags)

        assert result.title == "Midwest Midnight"
        assert result.track_no == 1

    def test_does_not_touch_a_real_tag_that_merely_starts_with_a_digit(self):
        # title came from a genuine tag, not the filename stem, so it must
        # be left alone even though it happens to start with digits
        tags = scanner.TrackTags(title="99 Luftballons", artist="Nena", album_artist="Nena")
        path = Path("/music/Nena/99 Luftballons Album/03 99 Luftballons.wav")

        result = scanner._fallback_from_path(path, tags)

        assert result.title == "99 Luftballons"

    def test_fills_a_missing_artist_from_the_grandparent_folder(self):
        tags = scanner.TrackTags(title="Some Song", artist="", album_artist="")
        path = Path("/music/The Meridian Set/Odd Hours/04 Some Song.wav")

        result = scanner._fallback_from_path(path, tags)

        assert result.artist == "The Meridian Set"
        assert result.album_artist == "The Meridian Set"

    def test_fills_the_artist_even_when_it_already_carries_the_unknown_placeholder(self):
        # 2026-09-13 fix: read_tags() always upgrades a blank artist to the
        # literal string "Unknown Artist" before this function ever runs
        # (both real call sites chain them directly), so tags.artist is
        # never actually "" in production - only ever "" in a hand-built
        # TrackTags like the test above. This is the case that matters.
        tags = scanner.TrackTags(
            title="Some Song", artist="Unknown Artist", album_artist="Unknown Artist"
        )
        path = Path("/music/The Meridian Set/Odd Hours/04 Some Song.wav")

        result = scanner._fallback_from_path(path, tags)

        assert result.artist == "The Meridian Set"
        assert result.album_artist == "The Meridian Set"

    def test_leaves_a_real_artist_tag_alone(self):
        tags = scanner.TrackTags(title="Some Song", artist="Real Artist", album_artist="")
        path = Path("/music/Some Folder/Some Album/04 Some Song.wav")

        result = scanner._fallback_from_path(path, tags)

        assert result.artist == "Real Artist"


class TestFolderAlbumArtist:
    """Covers the 2026-09-07 "Best of Women of Christian Music" fix: an
    untagged various-artists compilation used to fragment into one release
    per performer instead of collapsing into one compilation release."""

    def test_an_explicit_album_artist_tag_wins(self):
        tags_by_path = {
            Path("a.wav"): scanner.TrackTags(
                artist="Performer One", album_artist="The Band", had_explicit_album_artist=True
            ),
            Path("b.wav"): scanner.TrackTags(
                artist="Performer Two", album_artist="The Band", had_explicit_album_artist=True
            ),
        }

        artist, is_compilation = scanner._folder_album_artist(tags_by_path)

        assert artist == "The Band"
        assert is_compilation is False

    def test_no_tag_and_one_shared_performer_is_an_ordinary_album(self):
        tags_by_path = {
            Path("a.wav"): scanner.TrackTags(artist="Solo Artist", album_artist=""),
            Path("b.wav"): scanner.TrackTags(artist="Solo Artist", album_artist=""),
        }

        artist, is_compilation = scanner._folder_album_artist(tags_by_path)

        assert artist == "Solo Artist"
        assert is_compilation is False

    def test_no_tag_and_different_performers_collapses_to_various_artists(self):
        tags_by_path = {
            Path("a.wav"): scanner.TrackTags(artist="Performer One", album_artist=""),
            Path("b.wav"): scanner.TrackTags(artist="Performer Two", album_artist=""),
        }

        artist, is_compilation = scanner._folder_album_artist(tags_by_path)

        assert artist == "Various Artists"
        assert is_compilation is True

    def test_explicit_tag_disagreement_takes_the_most_common_value(self):
        tags_by_path = {
            Path("a.wav"): scanner.TrackTags(album_artist="The Band", had_explicit_album_artist=True),
            Path("b.wav"): scanner.TrackTags(album_artist="The Band", had_explicit_album_artist=True),
            Path("c.wav"): scanner.TrackTags(album_artist="Typo'd Name", had_explicit_album_artist=True),
        }

        artist, _ = scanner._folder_album_artist(tags_by_path)

        assert artist == "The Band"


class TestScanFolder:
    def test_adds_a_new_file_and_creates_the_watched_folder_row(self, session, tmp_path):
        make_silent_wav(tmp_path / "Test Artist" / "Test Album" / "01 Test Song.wav")

        result = scanner.scan_folder(session, tmp_path)

        assert result.added == 1
        assert result.scanned == 1
        assert not result.errors

        folder = session.scalar(
            select(WatchedFolder).where(WatchedFolder.path == str(tmp_path))
        )
        assert folder is not None
        assert folder.last_scan_at is not None

        track = session.scalar(select(Track))
        assert track.title == "Test Song"  # "01 " prefix stripped
        assert track.release.artist_display == "Test Artist"  # from the grandparent folder

    def test_rescanning_unchanged_files_does_not_duplicate_anything(self, session, tmp_path):
        make_silent_wav(tmp_path / "Test Artist" / "Test Album" / "01 Test Song.wav")
        scanner.scan_folder(session, tmp_path)

        result = scanner.scan_folder(session, tmp_path)

        assert result.added == 0
        assert result.unchanged == 1
        assert len(session.scalars(select(Track)).all()) == 1
        assert len(session.scalars(select(MediaFile)).all()) == 1

    def test_a_changed_file_is_reimported_in_place_rather_than_duplicated(self, session, tmp_path):
        wav = tmp_path / "Test Artist" / "Test Album" / "01 Test Song.wav"
        make_silent_wav(wav, seconds=0.2)
        scanner.scan_folder(session, tmp_path)

        make_silent_wav(wav, seconds=0.5)  # different size/mtime -> "changed"
        result = scanner.scan_folder(session, tmp_path)

        assert result.updated == 1
        assert result.added == 0
        assert len(session.scalars(select(MediaFile)).all()) == 1

    def test_an_unchanged_files_tags_are_never_reread_on_an_ordinary_rescan(
        self, session, tmp_path
    ):
        # Regression pin for the 2026-09-17 bug James actually hit: fixing
        # a `read_tags()` bug (the MP3 Comment-frame fix) does nothing at
        # all for an already-scanned library on its own, because an
        # ordinary rescan skips reading tags for any file whose mtime/size
        # on disk hasn't moved - see import_file's `force` docstring.
        mp3 = tmp_path / "Some Band" / "song.mp3"
        make_mp3_with_comment(mp3, "[Billboard] #001# [2020][Pop][2020]")
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        track.comment = None  # simulate having been imported before the fix existed

        result = scanner.scan_folder(session, tmp_path)

        assert result.unchanged == 1
        session.refresh(track)
        assert track.comment is None

    def test_force_rereads_an_unchanged_files_tags(self, session, tmp_path):
        mp3 = tmp_path / "Some Band" / "song.mp3"
        make_mp3_with_comment(mp3, "[Billboard] #001# [2020][Pop][2020]")
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        track.comment = None  # simulate having been imported before the fix existed

        result = scanner.scan_folder(session, tmp_path, force=True)

        assert result.unchanged == 0
        assert result.updated == 1
        session.refresh(track)
        assert track.comment == "[Billboard] #001# [2020][Pop][2020]"

    def test_forcing_a_rescan_does_not_duplicate_anything(self, session, tmp_path):
        # force=True re-runs the whole import_file body for every file,
        # including the get_or_create_*/add_credit calls further down -
        # confirms those stay idempotent rather than creating a second
        # release/track/credit each time.
        make_silent_wav(tmp_path / "Test Artist" / "Test Album" / "01 Test Song.wav")
        scanner.scan_folder(session, tmp_path)

        result = scanner.scan_folder(session, tmp_path, force=True)

        assert result.updated == 1
        assert len(session.scalars(select(Track)).all()) == 1
        assert len(session.scalars(select(MediaFile)).all()) == 1

    def test_a_folder_that_does_not_exist_reports_an_error_and_does_not_crash(self, session, tmp_path):
        missing = tmp_path / "does-not-exist"

        result = scanner.scan_folder(session, missing)

        assert result.errors
        assert result.scanned == 0

    def test_the_folder_wide_override_is_applied_to_every_file_in_it(self, session, tmp_path):
        # regression check for the plumbing itself (the decision logic is
        # covered directly above in TestFolderAlbumArtist): both of these
        # tagless files share one folder and, via the grandparent-folder
        # fallback, the exact same inferred artist - so
        # _folder_album_artist's "one shared performer" branch applies and
        # both tracks must land on one ordinary (non-compilation) release,
        # not two.
        make_silent_wav(tmp_path / "Box Set" / "01 First Song.wav")
        make_silent_wav(tmp_path / "Box Set" / "02 Second Song.wav")

        scanner.scan_folder(session, tmp_path)

        releases = session.scalars(select(Release)).all()
        assert len(releases) == 1
        assert releases[0].is_compilation is False
        assert len(releases[0].tracks) == 2


class TestScanFolderShouldStop:
    """2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings" - `should_stop`, checked once per file before
    that file's own import, must `break` cleanly rather than raising: the
    folder's own end-of-scan bookkeeping (WatchedFolder row/last_scan_at,
    the final flush) still has to run over whatever got imported before
    the stop, exactly as if the scan had simply been asked to cover fewer
    files (see scan_folder's own should_stop docstring note)."""

    def test_should_stop_true_from_the_start_imports_nothing(self, session, tmp_path):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")

        result = scanner.scan_folder(session, tmp_path, should_stop=lambda: True)

        assert result.added == 0
        assert result.scanned == 0
        assert session.scalar(select(Track)) is None

    def test_should_stop_true_from_the_start_still_updates_the_watched_folder_row(
        self, session, tmp_path
    ):
        # the point of "break, never raise" - a cancelled scan still counts
        # as having scanned this folder just now, same as a completed one.
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")

        scanner.scan_folder(session, tmp_path, should_stop=lambda: True)

        folder = session.scalar(
            select(WatchedFolder).where(WatchedFolder.path == str(tmp_path))
        )
        assert folder is not None
        assert folder.last_scan_at is not None

    def test_should_stop_partway_through_keeps_files_already_imported(
        self, session, tmp_path
    ):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 First.wav")
        make_silent_wav(tmp_path / "Artist" / "Album" / "02 Second.wav")
        make_silent_wav(tmp_path / "Artist" / "Album" / "03 Third.wav")

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let the first file through, stop before the rest

        result = scanner.scan_folder(session, tmp_path, should_stop=should_stop)

        assert result.added == 1
        assert result.scanned == 1
        assert len(session.scalars(select(Track)).all()) == 1
        # the one file that did get imported is really and fully committed,
        # not just flushed-and-forgotten - a second scan sees it as already
        # there rather than re-adding it.
        result2 = scanner.scan_folder(session, tmp_path)
        assert result2.added == 2  # the other two, still never having run
        assert result2.unchanged == 1  # the first one, untouched by the stop

    def test_no_should_stop_argument_behaves_exactly_as_before(self, session, tmp_path):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")

        result = scanner.scan_folder(session, tmp_path)

        assert result.added == 1


class TestMarkMissingFiles:
    def test_flags_a_file_whose_path_no_longer_exists(self, session, tmp_path):
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        wav.unlink()

        changed = scanner.mark_missing_files(session)

        assert changed == 1
        assert session.scalar(select(MediaFile)).is_missing is True

    def test_should_stop_true_from_the_start_flags_nothing(self, session, tmp_path):
        # 2026-09-22 - "add a real cancel button..." - should_stop, checked
        # once per row, break-only (see scanner.scan_folder's own note for
        # the general shape); no per-row commit here either, so a stop
        # just means fewer rows get checked this run.
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        wav.unlink()

        changed = scanner.mark_missing_files(session, should_stop=lambda: True)

        assert changed == 0
        assert session.scalar(select(MediaFile)).is_missing is False

    def test_clears_missing_once_the_file_reappears(self, session, tmp_path):
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        data = wav.read_bytes()
        wav.unlink()
        scanner.mark_missing_files(session)

        wav.write_bytes(data)
        changed = scanner.mark_missing_files(session)

        # the return value only counts files newly flagged missing (see
        # SettingsView.verify_files's "N file(s) newly marked missing"
        # message) - a file coming back is reflected on the row itself but
        # deliberately not added to this count.
        assert changed == 0
        assert session.scalar(select(MediaFile)).is_missing is False

    def test_an_untouched_present_file_is_not_recounted_every_time(self, session, tmp_path):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")
        scanner.scan_folder(session, tmp_path)

        changed = scanner.mark_missing_files(session)

        assert changed == 0

    def test_progress_callback_reports_the_final_total(self, session, tmp_path):
        """2026-09-22 - James: "does the progress bar also work on ...
        verify files" - SettingsView.verify_files now runs this on a
        background thread and needs real progress out of it."""
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")
        scanner.scan_folder(session, tmp_path)

        calls = []
        scanner.mark_missing_files(session, progress=lambda done, total, name: calls.append((done, total)))

        assert calls
        assert calls[-1] == (1, 1)


class TestPurgeOrphanedTracks:
    """Covers the 2026-09-07 fix: a track sitting only on a jukebox slot
    (no playlist/chart/play-event history) used to be silently deleted by
    this exact function once its file went missing, quietly emptying that
    side of its jukebox title strip.
    """

    def _missing_track(self, session, tmp_path) -> Track:
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        wav.unlink()
        scanner.mark_missing_files(session)
        return session.scalar(select(Track))

    def test_deletes_a_missing_track_with_no_history_at_all(self, session, tmp_path):
        track = self._missing_track(session, tmp_path)

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 1
        assert session.get(Track, track.id) is None

    def test_keeps_a_missing_track_present_on_a_playlist(self, session, tmp_path):
        track = self._missing_track(session, tmp_path)
        playlist = Playlist(name="Favorites")
        session.add(playlist)
        session.flush()
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=track.id))
        session.flush()

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 0
        assert session.get(Track, track.id) is not None

    def test_keeps_a_missing_track_that_appears_on_a_chart(self, session, tmp_path):
        track = self._missing_track(session, tmp_path)
        chart = Chart(name="Test Chart", slug="test-chart")
        session.add(chart)
        session.flush()
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2020, 1, 1))
        session.add(issue)
        session.flush()
        session.add(
            ChartEntry(
                issue_id=issue.id, rank=1, title="Song", artist_name="Artist",
                title_key="song", artist_key="artist", track_id=track.id,
            )
        )
        session.flush()

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 0
        assert session.get(Track, track.id) is not None

    def test_keeps_a_missing_track_with_a_play_event(self, session, tmp_path):
        track = self._missing_track(session, tmp_path)
        session.add(PlayEvent(track_id=track.id))
        session.flush()

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 0
        assert session.get(Track, track.id) is not None

    def test_keeps_a_missing_track_sitting_on_a_jukebox_slot(self, session, tmp_path):
        track = self._missing_track(session, tmp_path)
        artist = session.scalar(select(Artist))
        session.add(
            JukeboxSlot(slot_number=1, artist_id=artist.id, side_a_track_id=track.id)
        )
        session.flush()

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 0
        assert session.get(Track, track.id) is not None

    def test_missing_summary_previews_the_purge_without_changing_anything(self, session, tmp_path):
        # 2026-10-18: Purge's dialog now reports what it will do, not the
        # count of missing file rows
        kept = self._missing_track(session, tmp_path)
        playlist = Playlist(name="Favorites")
        session.add(playlist)
        session.flush()
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=kept.id))
        gone_wav = tmp_path / "Other" / "Album" / "01 Gone.wav"
        make_silent_wav(gone_wav)
        scanner.scan_folder(session, tmp_path)
        gone_wav.unlink()
        scanner.mark_missing_files(session)

        summary = scanner.missing_summary(session)

        assert summary.files == 2
        assert summary.removable == 1
        assert dict(summary.kept) == {"playlist": 1}
        assert summary.stale_copies == 0
        assert session.scalar(select(func.count(Track.id))) == 2
        assert scanner.purge_orphaned_tracks(session) == summary.removable

    def test_a_missing_extra_copy_of_a_playable_song_is_a_stale_copy(
        self, session, tmp_path
    ):
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        session.add(MediaFile(track_id=track.id, path=str(tmp_path / "gone.mp3"), is_missing=True))
        session.flush()

        summary = scanner.missing_summary(session)

        assert (summary.files, summary.stale_copies, summary.removable) == (1, 1, 0)
        assert scanner.purge_stale_copies(session) == 1
        assert [f.path for f in session.scalars(select(MediaFile))] == [str(wav)]
        assert session.get(Track, track.id) is not None

    def test_scans_skip_goodsync_folders_and_drop_copies_already_imported(
        self, session, tmp_path
    ):
        # 2026-10-18: GoodSync's _gsdata_ old versions had been imported
        # as extra copies of real songs
        real = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(real)
        saved = tmp_path / "_gsdata_" / "_saved_" / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(saved)
        assert list(scanner.iter_audio_files(tmp_path)) == [real]
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        session.add(MediaFile(track_id=track.id, path=str(saved)))
        session.flush()

        assert scanner.remove_skipped_folder_files(session) == 1
        assert [f.path for f in session.scalars(select(MediaFile))] == [str(real)]

    def test_a_present_file_is_never_purged_regardless_of_history(self, session, tmp_path):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")
        scanner.scan_folder(session, tmp_path)

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 0
        assert len(session.scalars(select(Track)).all()) == 1


def make_tagged_mp3(path: Path, title: str, artist: str, album: str, *,
                    album_artist: str = "", catalog: str = "", track: str = "") -> None:
    """A tiny silent MP3 with title/artist/album tags (and, if given, an
    album artist, catalog number and track number)."""
    from mutagen.id3 import TALB, TIT2, TPE1, TPE2, TRCK, TXXX
    from mutagen.mp3 import MP3

    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    frame = header + bytes(417 - len(header))
    with open(path, "wb") as f:
        for _ in range(5):
            f.write(frame)
    audio = MP3(str(path))
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[title]))
    audio.tags.add(TPE1(encoding=3, text=[artist]))
    audio.tags.add(TALB(encoding=3, text=[album]))
    if album_artist:
        audio.tags.add(TPE2(encoding=3, text=[album_artist]))
    if catalog:
        audio.tags.add(TXXX(encoding=3, desc="CATALOGNUMBER", text=[catalog]))
    if track:
        audio.tags.add(TRCK(encoding=3, text=[track]))
    audio.save()


class TestSameSongByDifferentArtists:
    """2026-10-01 - James's "1949 Billboard Top Hits" folder has several
    artists' versions of one song; they were folded into one track (James:
    "why does the search only show the russ morgan when I clearly want
    Perry como")."""

    ALBUM = "1949 Billboard Top Hits"
    VERSIONS = ("Russ Morgan", "Perry Como", "Dinah Shore")

    def _folder(self, root: Path) -> Path:
        folder = root / "1949"
        for artist in self.VERSIONS:
            make_tagged_mp3(folder / f"{artist} - Forever And Ever.mp3",
                            "Forever And Ever", artist, self.ALBUM)
        make_tagged_mp3(folder / "Vaughn Monroe - Riders In The Sky.mp3",
                        "Riders In The Sky", "Vaughn Monroe", self.ALBUM)
        return folder

    def _versions(self, session):
        tracks = session.scalars(select(Track).where(Track.title == "Forever And Ever")).all()
        return {t.artist_display: t for t in tracks}

    def test_each_artists_version_is_its_own_track(self, session, tmp_path):
        self._folder(tmp_path)
        scanner.scan_folder(session, tmp_path)
        versions = self._versions(session)
        assert set(versions) == set(self.VERSIONS)
        for artist, track in versions.items():
            assert [Path(m.path).name for m in track.files] == [f"{artist} - Forever And Ever.mp3"]
            assert {c.artist.name for c in track.credits} == {artist}
        assert len({t.release_id for t in versions.values()}) == 1

    def test_two_files_of_one_recording_still_share_a_track(self, session, tmp_path):
        folder = tmp_path / "Album"
        make_tagged_mp3(folder / "a.mp3", "Galway Bay", "Bing Crosby", "Hits")
        make_tagged_mp3(folder / "b.mp3", "Galway Bay", "Bing Crosby", "Hits")
        scanner.scan_folder(session, tmp_path)
        tracks = session.scalars(select(Track)).all()
        assert len(tracks) == 1 and len(tracks[0].files) == 2

    def test_a_rescan_separates_versions_folded_together_before(self, session, tmp_path):
        self._folder(tmp_path)
        scanner.scan_folder(session, tmp_path)
        # recreate the old state: every version folded into Russ Morgan's track
        versions = self._versions(session)
        keep = versions["Russ Morgan"]
        release_id = keep.release_id
        for artist, track in versions.items():
            if track is keep:
                continue
            for mf in list(track.files):
                mf.track = keep
            session.delete(track)
        session.flush()
        playlist = Playlist(name="1949")
        session.add(playlist)
        session.flush()
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=keep.id, position=0))
        chart = Chart(name="Year End", slug="year-end")
        session.add(chart)
        session.flush()
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(1949, 12, 31))
        session.add(issue)
        session.flush()
        from musicmgr.services.matching import normalize

        def entry(rank, artist, locked=False):
            e = ChartEntry(issue_id=issue.id, rank=rank, title="Forever and Ever",
                           artist_name=artist, title_key=normalize("Forever and Ever"),
                           artist_key=normalize(artist), track_id=keep.id,
                           match_score=0.78, match_locked=locked)
            session.add(e)
            return e

        russ, como, shore = entry(14, "Russ Morgan"), entry(15, "Perry Como"), \
            entry(16, "Dinah Shore", locked=True)
        # fixed by hand to the only track there was, artist not on the album
        other = entry(17, "Margaret Whiting", locked=True)
        session.flush()
        session.expire_all()
        assert len(self._versions(session)) == 1

        result = scanner.scan_folder(session, tmp_path)  # files unchanged on disk

        assert result.separated == 2
        assert "2 recordings separated" in result.summary()
        session.expire_all()
        versions = self._versions(session)
        assert set(versions) == set(self.VERSIONS)
        assert versions["Russ Morgan"].id == keep.id  # playlists keep pointing at it
        assert {t.release_id for t in versions.values()} == {release_id}
        assert all(len(t.files) == 1 for t in versions.values())
        assert session.scalar(select(PlaylistItem)).track_id == keep.id
        assert russ.track_id == keep.id
        assert como.track_id == versions["Perry Como"].id
        # fixed by hand: moves to the hidden version that is plainly its artist
        assert shore.track_id == versions["Dinah Shore"].id and shore.match_locked
        assert other.track_id == keep.id  # no such version - left alone

        again = scanner.scan_folder(session, tmp_path)
        assert again.separated == 0 and again.unchanged == 4

    def test_picking_a_folded_file_by_hand_gives_its_own_track(self, session, tmp_path):
        # James: "When I pick Perry Como with the file picker it still shows
        # Russ Morgan" (Charts › Fix match › Browse for file...)
        folder = self._folder(tmp_path)
        scanner.scan_folder(session, tmp_path)
        versions = self._versions(session)
        keep = versions["Russ Morgan"]
        for artist, track in versions.items():
            if track is not keep:
                for mf in list(track.files):
                    mf.track = keep
                session.delete(track)
        session.flush()

        track = scanner.import_single(session, folder / "Perry Como - Forever And Ever.mp3")

        assert track.artist_display == "Perry Como" and track.id != keep.id
        assert [Path(m.path).name for m in track.files] == ["Perry Como - Forever And Ever.mp3"]
        # a file that's already its own track just comes back as it is
        again = scanner.import_single(session, folder / "Perry Como - Forever And Ever.mp3")
        assert again.id == track.id


class TestVersionsAtDifferentTrackNumbers:
    """2026-10-18 - James: "Why is 2. Lover missing from the Michael Stanley
    Band Heartland album". The title key ignores "(Live)", so 02 Lover and
    12 Lover (Live) were one track with two files."""

    ALBUM, BAND = "Heartland", "The Michael Stanley Band"

    def _album(self, root: Path) -> Path:
        folder = root / self.BAND / self.ALBUM
        make_tagged_mp3(folder / "01 I'll Never Need Anyone More.mp3", "I'll Never Need Anyone More",
                        self.BAND, self.ALBUM, track="1/13")
        make_tagged_mp3(folder / "02 Lover.mp3", "Lover", self.BAND, self.ALBUM, track="2/13")
        make_tagged_mp3(folder / "12 Lover (Live).mp3", "Lover (Live)", self.BAND, self.ALBUM, track="12/13")
        return folder

    def _lovers(self, session):
        return {t.track_no: t for t in session.scalars(
            select(Track).where(Track.title.like("Lover%"))).all()}

    def test_a_live_version_at_another_number_is_its_own_track(self, session, tmp_path):
        self._album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        lovers = self._lovers(session)
        assert set(lovers) == {2, 12}
        assert lovers[2].title == "Lover" and lovers[12].title == "Lover (Live)"
        assert [Path(m.path).name for m in lovers[2].files] == ["02 Lover.mp3"]
        assert [Path(m.path).name for m in lovers[12].files] == ["12 Lover (Live).mp3"]
        assert lovers[2].release_id == lovers[12].release_id

    def test_two_copies_at_the_same_number_still_share_a_track(self, session, tmp_path):
        folder = tmp_path / "Album"
        make_tagged_mp3(folder / "03 Lover.mp3", "Lover", self.BAND, self.ALBUM, track="3")
        make_tagged_mp3(folder / "03 Lover copy.mp3", "Lover", self.BAND, self.ALBUM, track="3/13")
        scanner.scan_folder(session, tmp_path)
        tracks = session.scalars(select(Track)).all()
        assert len(tracks) == 1 and len(tracks[0].files) == 2

    def test_a_999_copy_joins_the_numbered_one(self, session, tmp_path):
        # the Billboard folders' extra copies are tagged 999 - not a position
        folder = tmp_path / "1950"
        make_tagged_mp3(folder / "Ames Bros - Rag Mop.mp3", "Rag Mop", "Ames Bros",
                        "1950 Billboard Top Hits", track="23")
        make_tagged_mp3(folder / "Ames Bros (original) - Rag Mop.mp3", "Rag Mop", "Ames Bros",
                        "1950 Billboard Top Hits", track="999")
        scanner.scan_folder(session, tmp_path)
        tracks = session.scalars(select(Track)).all()
        assert len(tracks) == 1 and len(tracks[0].files) == 2 and tracks[0].track_no == 23

    def test_a_rescan_separates_versions_folded_together_before(self, session, tmp_path):
        self._album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        # recreate PC1's state: both files on the live track, which keeps
        # its playlist place
        lovers = self._lovers(session)
        keep, studio = lovers[12], lovers[2]
        for mf in list(studio.files):
            mf.track = keep
        session.delete(studio)
        playlist = Playlist(name="MSB")
        session.add(playlist)
        session.flush()
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=keep.id, position=0))
        session.flush()
        session.expire_all()
        assert set(self._lovers(session)) == {12}

        result = scanner.scan_folder(session, tmp_path)  # files unchanged on disk

        assert result.separated == 1
        session.expire_all()
        lovers = self._lovers(session)
        assert set(lovers) == {2, 12}
        assert lovers[12].id == keep.id
        assert [Path(m.path).name for m in lovers[2].files] == ["02 Lover.mp3"]
        assert session.scalar(select(PlaylistItem)).track_id == keep.id

        again = scanner.scan_folder(session, tmp_path)
        assert again.separated == 0 and again.unchanged == 3

    def test_renumbering_a_lone_file_keeps_its_track(self, session, tmp_path):
        folder = self._album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        studio_id = self._lovers(session)[2].id
        path = folder / "02 Lover.mp3"
        make_tagged_mp3(path, "Lover", self.BAND, self.ALBUM, track="4/13")
        _touch_newer(path)
        scanner.scan_folder(session, tmp_path)
        session.expire_all()
        lovers = self._lovers(session)
        assert set(lovers) == {4, 12} and lovers[4].id == studio_id


def _touch_newer(path: Path) -> None:
    """Make a rewritten file's mtime differ for sure, so a rescan sees it changed."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


class TestSongNumbersInTitles:
    """2026-10-16 - James: "6 Inch", "99 Red Balloons", "777-9311" showed up
    as "Inch", "Red Balloons", "9311": a song named with a number, in a file
    named after it, lost the number as if it were a track number."""

    def test_a_tagged_title_that_matches_the_filename_keeps_its_number(self):
        tags = scanner.TrackTags(title="6 Inch", artist="Beyoncé", track_no=5, had_title_tag=True)

        result = scanner._fallback_from_path(Path("/music/Beyonce/Lemonade/6 Inch.mp3"), tags)

        assert result.title == "6 Inch"
        assert result.track_no == 5

    def test_the_songs_number_is_not_taken_as_a_missing_track_number(self):
        tags = scanner.TrackTags(title="99 Red Balloons", artist="Nena", had_title_tag=True)

        result = scanner._fallback_from_path(Path("/music/Nena/99/99 Red Balloons.mp3"), tags)

        assert result.title == "99 Red Balloons"
        assert result.track_no is None

    def test_numbers_joined_by_a_hyphen_are_one_title_even_untagged(self):
        for stem in ("777-9311", "20-20", "5-10-15 Hours"):
            tags = scanner.TrackTags(title=stem, artist="")
            result = scanner._fallback_from_path(Path(f"/music/A/B/{stem}.wav"), tags)
            assert (result.title, result.track_no) == (stem, None)

    def test_an_untagged_tracknumber_prefix_is_still_stripped(self):
        for stem, title, no in (("01 Song", "Song", 1), ("3 - Song", "Song", 3), ("07. Song", "Song", 7)):
            tags = scanner.TrackTags(title=stem, artist="")
            result = scanner._fallback_from_path(Path(f"/music/A/B/{stem}.wav"), tags)
            assert (result.title, result.track_no) == (title, no)

    def test_a_scan_keeps_the_title_tag(self, session, tmp_path):
        make_tagged_mp3(tmp_path / "Beyonce" / "Lemonade" / "6 Inch.mp3", "6 Inch", "Beyoncé", "Lemonade")

        scanner.scan_folder(session, tmp_path)

        assert session.scalar(select(Track)).title == "6 Inch"


class TestRepeatedCatalogNumbers:
    """2026-10-16 - a file tagged "986113-7, 986113-7, 986113-7" no longer
    matched its album's catalog number, so the album was made again."""

    def test_repeats_and_placeholders_collapse(self):
        assert scanner._distinct_catalog_numbers("986113-7, 986113-7, 986113-7") == "986113-7"
        assert scanner._distinct_catalog_numbers("196588383021, [none], [none]") == "196588383021"
        assert scanner._distinct_catalog_numbers("none, none") == ""
        assert scanner._distinct_catalog_numbers("810 308-2, D-170402") == "810 308-2, D-170402"

    def test_only_a_label_repeated_whole_is_shortened(self):
        assert scanner._distinct_label("RCA, RCA") == "RCA"
        assert scanner._distinct_label("Island; Island, Island") == "Island"
        assert scanner._distinct_label("BMG Direct Marketing, Inc.") == "BMG Direct Marketing, Inc."
        assert scanner._distinct_label("Def Jam South, Disturbing tha Peace") == \
            "Def Jam South, Disturbing tha Peace"

    def test_a_retag_repeating_the_catalog_number_stays_on_the_same_album(self, session, tmp_path):
        song = tmp_path / "Ludacris" / "Chicken-N-Beer" / "Stand Up.mp3"
        make_tagged_mp3(song, "Stand Up", "Ludacris", "Chicken-N-Beer",
                        album_artist="Ludacris", catalog="986113-7")
        scanner.scan_folder(session, tmp_path)
        first = session.scalar(select(Release))

        make_tagged_mp3(song, "Stand Up", "Ludacris", "Chicken-N-Beer",
                        album_artist="Ludacris", catalog="986113-7, 986113-7, 986113-7")
        _touch_newer(song)
        scanner.scan_folder(session, tmp_path)

        releases = session.scalars(select(Release)).all()
        assert [r.id for r in releases] == [first.id]
        assert releases[0].catalog_number == "986113-7"


class TestAFolderOfManyAlbums:
    """2026-10-16 - James's "Old78Records" and "BPM" folders hold one record
    or single per file, each tagged with its own album artist; the folder's
    most common album artist was put on all of them."""

    def test_each_album_in_a_folder_keeps_its_own_album_artist(self, session, tmp_path):
        folder = tmp_path / "Old78Records"
        make_tagged_mp3(folder / "Bing Crosby - White Christmas.mp3", "White Christmas",
                        "Bing Crosby", "DECCA - 18429", album_artist="Bing Crosby")
        make_tagged_mp3(folder / "Bing Crosby - Let It Snow.mp3", "Let It Snow",
                        "Bing Crosby", "DECCA - 23281", album_artist="Bing Crosby")
        make_tagged_mp3(folder / "Dinah Shore - Sleepy Lagoon.mp3", "Sleepy Lagoon",
                        "Dinah Shore", "Victor - 27875", album_artist="Dinah Shore")
        make_tagged_mp3(folder / "Dinah Shore - Three Little Sisters.mp3", "Three Little Sisters",
                        "Dinah Shore", "Victor - 27875", album_artist="Dinah Shore")

        scanner.scan_folder(session, tmp_path)

        by_title = {r.title: r for r in session.scalars(select(Release))}
        assert by_title["Victor - 27875"].artist_display == "Dinah Shore"
        assert by_title["Victor - 27875"].album_artist.name == "Dinah Shore"
        assert len(by_title["Victor - 27875"].tracks) == 2
        assert by_title["DECCA - 18429"].artist_display == "Bing Crosby"

    def test_untagged_singles_in_one_folder_are_each_their_performers(self, session, tmp_path):
        folder = tmp_path / "BPM"
        make_tagged_mp3(folder / "Adam Port & Stryv - Move.mp3", "Move", "Adam Port & Stryv", "Move")
        make_tagged_mp3(folder / "Dom Dolla - Saving Up.mp3", "Saving Up", "Dom Dolla", "Saving Up")

        scanner.scan_folder(session, tmp_path)

        assert {r.title: r.artist_display for r in session.scalars(select(Release))} == {
            "Move": "Adam Port & Stryv", "Saving Up": "Dom Dolla"}

    def test_one_album_in_a_folder_is_still_decided_as_a_whole(self, session, tmp_path):
        # an untagged compilation: one album, a different performer per file
        folder = tmp_path / "Hits"
        make_tagged_mp3(folder / "a.mp3", "One", "Performer One", "Hits")
        make_tagged_mp3(folder / "b.mp3", "Two", "Performer Two", "Hits")

        scanner.scan_folder(session, tmp_path)

        release = session.scalar(select(Release))
        assert release.artist_display == "Various Artists" and release.is_compilation


class TestEmptyReleases:
    """2026-10-16 - an album whose tracks all moved to another release (a
    retag changed its album artist) was left on the artist page, empty."""

    def test_a_release_its_tracks_all_left_is_removed(self, session, tmp_path):
        from musicmgr.db.models import Master

        song = tmp_path / "Album" / "Song.mp3"
        make_tagged_mp3(song, "Song", "Singer", "Album", album_artist="Wrong Artist")
        scanner.scan_folder(session, tmp_path)
        make_tagged_mp3(song, "Song", "Singer", "Album", album_artist="Singer")
        _touch_newer(song)

        scanner.scan_folder(session, tmp_path)

        releases = session.scalars(select(Release)).all()
        assert [r.artist_display for r in releases] == ["Singer"]
        masters = session.scalars(select(Master)).all()
        assert [m.artist_display for m in masters] == ["Singer"]
        assert masters[0].main_release_id == releases[0].id

    def test_purge_empty_releases_leaves_releases_with_tracks(self, session, tmp_path):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")
        scanner.scan_folder(session, tmp_path)

        assert scanner.purge_empty_releases(session) == 0
        assert len(session.scalars(select(Release)).all()) == 1


class TestOrphansFindTheirFileAgain:
    """2026-10-16 - a rescan that found a song's file under a new path left
    the old track behind with no file, keeping its plays and chart matches
    while the new track had none."""

    def _scan_then_move(self, session, tmp_path, new_path: Path) -> Track:
        # moved to a new folder and retagged onto a best-of - a file under
        # a new path on a new album is a new track to the scanner
        old = tmp_path / "Old Folder" / "Song.mp3"
        make_tagged_mp3(old, "Song", "Singer", "Album", album_artist="Singer")
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        session.add(PlayEvent(track_id=track.id))
        track.play_count = 3
        session.flush()
        old.unlink()
        make_tagged_mp3(new_path, "Song", "Singer", "Greatest Hits", album_artist="Singer")
        scanner.scan_folder(session, tmp_path)
        scanner.mark_missing_files(session)
        return track

    def test_a_moved_files_old_track_hands_over_its_history(self, session, tmp_path):
        old_track = self._scan_then_move(session, tmp_path, tmp_path / "New Folder" / "Song.mp3")
        old_id = old_track.id
        assert len(session.scalars(select(Track)).all()) == 2

        removed = scanner.purge_orphaned_tracks(session)

        session.expire_all()
        tracks = session.scalars(select(Track)).all()
        assert removed == 1 and len(tracks) == 1 and tracks[0].id != old_id
        assert tracks[0].play_count == 3
        assert session.scalar(select(PlayEvent)).track_id == tracks[0].id
        assert len(session.scalars(select(Release)).all()) == 1

    def test_no_handover_when_two_live_versions_could_be_it(self, session, tmp_path):
        self._scan_then_move(session, tmp_path, tmp_path / "New Folder" / "Song.mp3")
        make_tagged_mp3(tmp_path / "Other" / "Song.mp3", "Song", "Singer", "Other Album",
                        album_artist="Singer")
        scanner.scan_folder(session, tmp_path)

        scanner.purge_orphaned_tracks(session)

        # the old track keeps its play - it's not clear which version it was
        session.expire_all()
        event = session.scalar(select(PlayEvent))
        assert all(f.is_missing for f in session.get(Track, event.track_id).files)

    def test_another_artists_song_of_the_same_name_is_not_a_twin(self, session, tmp_path):
        old = tmp_path / "Old Folder" / "Song.mp3"
        make_tagged_mp3(old, "Song", "Singer", "Album", album_artist="Singer")
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        session.add(PlayEvent(track_id=track.id))
        session.flush()
        old.unlink()
        make_tagged_mp3(tmp_path / "Album" / "Song.mp3", "Song", "Someone Else", "Album",
                        album_artist="Singer")
        scanner.scan_folder(session, tmp_path)
        scanner.mark_missing_files(session)

        scanner.purge_orphaned_tracks(session)

        session.expire_all()
        assert session.scalar(select(PlayEvent)).track_id == track.id


class TestRepairFolderAlbumArtists:
    """2026-10-17 - James: the 78s in D:\\Music\\Old78Records "now there are
    many that are blank". The old folder-wide rule had filed all 179 records
    under Bing Crosby, and their covers were then looked for under
    "Bing Crosby - <record>.jpg", which doesn't exist. An unchanged file is
    never re-read, so repair_folder_album_artists puts the rows right."""

    def _library(self, session, tmp_path, monkeypatch):
        from musicmgr import config
        from musicmgr.services import library as lib_svc

        art = tmp_path / "artwork"
        art.mkdir()
        monkeypatch.setattr(config, "ART_DIR", art)
        folder = tmp_path / "Old78Records"
        make_tagged_mp3(folder / "Bing Crosby - White Christmas.mp3", "White Christmas",
                        "Bing Crosby", "DECCA - 18429", album_artist="Bing Crosby")
        make_tagged_mp3(folder / "Bing Crosby - Silent Night.mp3", "Silent Night",
                        "Bing Crosby", "DECCA - 18429", album_artist="Bing Crosby")
        make_tagged_mp3(folder / "Abe Lyman - Help Me.mp3", "Help Me",
                        "Abe Lyman and his Californians", "Bluebird - B-10887",
                        album_artist="Abe Lyman and his Californians")
        make_tagged_mp3(folder / "Dinah Shore - Sleepy Lagoon.mp3", "Sleepy Lagoon",
                        "Dinah Shore", "Victor - 27875", album_artist="Dinah Shore")
        scanner.scan_folder(session, tmp_path)
        # the pictures saved back when the albums were filed right
        (art / "Abe Lyman and his Californians - Bluebird - B-10887.jpg").write_bytes(b"abe")
        (art / "Dinah Shore - Victor - 27875.jpg").write_bytes(b"dinah")
        (art / "Bing Crosby - DECCA - 18429.jpg").write_bytes(b"bing")
        # ...then the old folder-wide rule: everything under Bing Crosby,
        # covers pointing at "Bing Crosby - <record>.jpg"
        bing = lib_svc.get_or_create_artist(session, "Bing Crosby")
        for release in session.scalars(select(Release)):
            release.artist_display = "Bing Crosby"
            release.album_artist_id = bing.id
            release.cover_path = str(art / f"Bing Crosby - {release.title}.jpg")
        session.flush()
        return art

    def test_records_go_back_to_their_artists_with_their_covers(self, session, tmp_path, monkeypatch):
        art = self._library(session, tmp_path, monkeypatch)

        # Bing's own record was right all along
        assert scanner.repair_folder_album_artists(session) == 2

        rel = {r.title: r for r in session.scalars(select(Release))}
        assert rel["Bluebird - B-10887"].artist_display == "Abe Lyman and his Californians"
        assert rel["Bluebird - B-10887"].album_artist.name == "Abe Lyman and his Californians"
        assert rel["Victor - 27875"].album_artist.name == "Dinah Shore"
        assert rel["DECCA - 18429"].artist_display == "Bing Crosby"
        assert Path(rel["Bluebird - B-10887"].cover_path) == art / "Abe Lyman and his Californians - Bluebird - B-10887.jpg"
        assert Path(rel["Victor - 27875"].cover_path).read_bytes() == b"dinah"
        assert Path(rel["DECCA - 18429"].cover_path).read_bytes() == b"bing"
        # Bing's page no longer claims the other records
        mains = session.scalars(select(Credit).where(
            Credit.role == Credit.ROLE_MAIN, Credit.track_id.is_(None),
            Credit.release_id == rel["Victor - 27875"].id)).all()
        assert [c.artist.name for c in mains] == ["Dinah Shore"]

    def test_running_again_changes_nothing(self, session, tmp_path, monkeypatch):
        self._library(session, tmp_path, monkeypatch)
        scanner.repair_folder_album_artists(session)
        assert scanner.repair_folder_album_artists(session) == 0

    def test_joins_the_artists_existing_copy_of_the_album(self, session, tmp_path, monkeypatch):
        art = self._library(session, tmp_path, monkeypatch)
        # the album as it was before, left behind empty under its right artist
        old = Release(title="Victor - 27875", title_key=normalize("Victor - 27875"),
                      artist_display="Dinah Shore",
                      cover_path=str(art / "Dinah Shore - Victor - 27875.jpg"))
        session.add(old)
        session.flush()

        scanner.repair_folder_album_artists(session)

        victor = session.scalars(select(Release).where(Release.title == "Victor - 27875")).all()
        assert [r.id for r in victor] == [old.id]
        assert [t.title for t in victor[0].tracks] == ["Sleepy Lagoon"]

    def test_a_missing_cover_comes_back_from_the_files(self, session, tmp_path, monkeypatch):
        art = self._library(session, tmp_path, monkeypatch)
        (art / "Dinah Shore - Victor - 27875.jpg").unlink()
        monkeypatch.setattr(scanner, "read_tags", _with_cover(scanner.read_tags, b"\xff\xd8embedded"))

        scanner.repair_folder_album_artists(session)

        victor = session.scalar(select(Release).where(Release.title == "Victor - 27875"))
        assert Path(victor.cover_path).is_file()
        assert Path(victor.cover_path).read_bytes() == b"\xff\xd8embedded"

    def test_a_folder_name_guess_never_becomes_an_album_artist(self, session, tmp_path, monkeypatch):
        """Untagged files (no album-artist tag) are left with what they have."""
        from musicmgr import config

        monkeypatch.setattr(config, "ART_DIR", tmp_path / "artwork")
        folder = tmp_path / "2000-09" / "2008"
        make_tagged_mp3(folder / "a.mp3", "One", "Performer One", "2008")
        make_tagged_mp3(folder / "b.mp3", "Two", "Performer Two", "Other")
        scanner.scan_folder(session, tmp_path)
        before = {r.title: r.artist_display for r in session.scalars(select(Release))}

        scanner.repair_folder_album_artists(session)

        assert {r.title: r.artist_display for r in session.scalars(select(Release))} == before


def _with_cover(read_tags, data):
    def wrapped(path):
        tags = read_tags(path)
        tags.cover = data
        return tags
    return wrapped


class TestEllipsisFoldersAndCaseOnlyRenames:
    """2026-10-19: album folders whose names start with "..." were skipped
    as hidden, and on Windows a file could be imported twice when only its
    capitalization changed."""

    def test_ellipsis_names_are_scanned_but_dot_names_are_not(self, tmp_path):
        album = tmp_path / "Phil Collins" / "...But Seriously" / "01 Hang in Long Enough.wav"
        song = tmp_path / "Taylor Swift" / "Reputation" / "...Ready for It.wav"
        hidden = tmp_path / ".hidden" / "01 Nope.wav"
        dotfile = tmp_path / "Artist" / ".01 Nope.wav"
        for p in (album, song, hidden, dotfile):
            make_silent_wav(p)

        found = set(scanner.iter_audio_files(tmp_path))

        assert found == {album, song}

    def test_usb_sync_does_not_skip_ellipsis_folders(self):
        from musicmgr.services import usb_sync

        assert not usb_sync._skip_dir("...And Out Come The Wolves")
        assert usb_sync._skip_dir(".git")
        assert not usb_sync._skip_file("...Ready for It.mp3")

    def test_a_case_only_rename_reuses_the_same_row(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(scanner.config, "CASE_INSENSITIVE_PATHS", True)
        wav = tmp_path / "Third Day" / "Miracle" / "03 Your Love Is Like a River.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        mf = session.scalar(select(MediaFile))
        # the library remembers the old capitalization
        mf.path = str(wav).replace("Like a River", "Like A River")
        mf.mtime = 0  # changed on disk, so it's read again
        session.flush()

        scanner.scan_folder(session, tmp_path)

        assert [f.path for f in session.scalars(select(MediaFile))] == [str(wav)]
        assert session.scalar(select(func.count(Track.id))) == 1

    def test_merge_case_duplicate_files_keeps_the_newest_spelling(
        self, session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(scanner.config, "CASE_INSENSITIVE_PATHS", True)
        wav = tmp_path / "Third Day" / "Miracle" / "03 Your Love Is Like a River.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        old = str(wav).replace("Like a River", "Like A River")
        session.add(MediaFile(track_id=track.id, path=old))
        session.flush()

        assert scanner.merge_case_duplicate_files(session) == 1
        assert [f.path for f in session.scalars(select(MediaFile))] == [old]

    def test_merge_case_duplicate_files_does_nothing_on_case_sensitive_systems(
        self, session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(scanner.config, "CASE_INSENSITIVE_PATHS", False)
        wav = tmp_path / "A" / "B" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        track = session.scalar(select(Track))
        session.add(MediaFile(track_id=track.id, path=str(wav).replace("Song", "SONG")))
        session.flush()

        assert scanner.merge_case_duplicate_files(session) == 0



def test_rescan_all_tidies_artist_sort_keys(session, tmp_path):
    """2026-10-21: fix_artist_sort_keys moved off startup to the end of every
    scan (scanner.tidy_after_scan) - a scan still puts a bad key right."""
    from musicmgr.db.models import Artist, WatchedFolder

    make_silent_wav(tmp_path / "The Romantics" / "Album" / "01 Song.wav")
    session.add(WatchedFolder(path=str(tmp_path)))
    session.flush()
    scanner.rescan_all(session)
    artist = session.scalar(select(Artist).where(Artist.name == "The Romantics"))
    artist.name_sort = " Romantics, The"  # what the old sort_name left
    session.flush()

    scanner.rescan_all(session)

    assert artist.name_sort == "Romantics, The"
