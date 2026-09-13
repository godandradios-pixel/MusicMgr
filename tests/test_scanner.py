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
import wave
from pathlib import Path

from sqlalchemy import select

from musicmgr.db.models import (
    Artist,
    Chart,
    ChartEntry,
    ChartIssue,
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


def make_silent_wav(path: Path, seconds: float = 0.2) -> None:
    """A tiny, real, tagless WAV file - just enough for mutagen to read
    stream info from, with no metadata tags at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


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


class TestMarkMissingFiles:
    def test_flags_a_file_whose_path_no_longer_exists(self, session, tmp_path):
        wav = tmp_path / "Artist" / "Album" / "01 Song.wav"
        make_silent_wav(wav)
        scanner.scan_folder(session, tmp_path)
        wav.unlink()

        changed = scanner.mark_missing_files(session)

        assert changed == 1
        assert session.scalar(select(MediaFile)).is_missing is True

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

    def test_a_present_file_is_never_purged_regardless_of_history(self, session, tmp_path):
        make_silent_wav(tmp_path / "Artist" / "Album" / "01 Song.wav")
        scanner.scan_folder(session, tmp_path)

        removed = scanner.purge_orphaned_tracks(session)

        assert removed == 0
        assert len(session.scalars(select(Track)).all()) == 1
