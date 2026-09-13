"""Tests for services/library.py's get-or-create upserts, format_duration,
library_stats, and backfill_album_artists.

Idempotency is the whole point of every get_or_create_* function here: a
rescan calls each of them again for every unchanged file in the library
(see scanner.py:import_file), so a bug that makes one of them create a
second row for something that already exists would duplicate a real
chunk of the library on the very next scan. That's what most of these
tests are actually checking, even though each one looks like a small,
obvious thing to assert.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import Artist, Credit, MediaFile, PlayEvent, Playlist, Release, Track
from musicmgr.services import library as lib


def count(session, model) -> int:
    return session.scalar(select(func.count(model.id))) or 0


class TestGetOrCreateArtist:
    def test_creates_once_and_returns_the_same_row_on_repeat_calls(self, session):
        a1 = lib.get_or_create_artist(session, "Rush")
        a2 = lib.get_or_create_artist(session, "Rush")

        assert a1.id == a2.id
        assert count(session, Artist) == 1

    def test_matching_is_case_and_accent_insensitive(self, session):
        a1 = lib.get_or_create_artist(session, "Sigur Ros")
        a2 = lib.get_or_create_artist(session, "SIGUR ROS")

        assert a1.id == a2.id

    def test_blank_name_becomes_unknown_artist(self, session):
        artist = lib.get_or_create_artist(session, "")

        assert artist.name == "Unknown Artist"
        assert artist.name_key == "unknown artist"

    def test_sort_name_strips_the_leading_article(self, session):
        artist = lib.get_or_create_artist(session, "The Beatles")

        assert artist.name_sort == "Beatles, The"


class TestGetOrCreateGenreStyleLabel:
    @pytest.mark.parametrize(
        "factory", [lib.get_or_create_genre, lib.get_or_create_style, lib.get_or_create_label]
    )
    def test_blank_or_whitespace_name_returns_none(self, session, factory):
        assert factory(session, "") is None
        assert factory(session, "   ") is None

    def test_genre_is_idempotent_case_insensitively(self, session):
        g1 = lib.get_or_create_genre(session, "Rock")
        g2 = lib.get_or_create_genre(session, "rock")

        assert g1.id == g2.id

    def test_style_is_idempotent_case_insensitively(self, session):
        s1 = lib.get_or_create_style(session, "Prog Rock")
        s2 = lib.get_or_create_style(session, "PROG ROCK")

        assert s1.id == s2.id

    def test_label_is_idempotent_by_normalized_name(self, session):
        l1 = lib.get_or_create_label(session, "Anthem Records")
        l2 = lib.get_or_create_label(session, "ANTHEM RECORDS")

        assert l1.id == l2.id


class TestGetOrCreateMaster:
    def test_creates_once_for_the_same_title_and_artist(self, session):
        m1 = lib.get_or_create_master(session, "Moving Pictures", "Rush", 1981)
        m2 = lib.get_or_create_master(session, "Moving Pictures", "Rush", 1981)

        assert m1.id == m2.id

    def test_the_same_title_by_a_different_artist_is_a_different_master(self, session):
        m1 = lib.get_or_create_master(session, "Greatest Hits", "Artist A", 2000)
        m2 = lib.get_or_create_master(session, "Greatest Hits", "Artist B", 2000)

        assert m1.id != m2.id

    def test_an_earlier_year_seen_later_replaces_the_stored_one(self, session):
        master = lib.get_or_create_master(session, "A Show of Hands", "Rush", 1989)
        lib.get_or_create_master(session, "A Show of Hands", "Rush", 1988)

        assert master.year == 1988  # earliest known pressing wins

    def test_a_later_year_seen_after_an_earlier_one_does_not_overwrite_it(self, session):
        master = lib.get_or_create_master(session, "A Show of Hands", "Rush", 1988)
        lib.get_or_create_master(session, "A Show of Hands", "Rush", 1989)

        assert master.year == 1988


class TestGetOrCreateRelease:
    def test_creates_once_for_the_same_title_and_artist(self, session):
        r1 = lib.get_or_create_release(session, "Signals", "Rush")
        r2 = lib.get_or_create_release(session, "Signals", "Rush")

        assert r1.id == r2.id

    def test_the_same_title_by_a_different_artist_is_a_different_release(self, session):
        r1 = lib.get_or_create_release(session, "Greatest Hits", "Artist A")
        r2 = lib.get_or_create_release(session, "Greatest Hits", "Artist B")

        assert r1.id != r2.id

    def test_a_catalog_number_does_not_split_a_release_that_has_none_yet(self, session):
        # first import has no catalog number; a later, better-tagged import
        # of the same physical release supplies one - should still be
        # recognized as the same release, not a second one.
        r1 = lib.get_or_create_release(session, "Hemispheres", "Rush")
        r2 = lib.get_or_create_release(
            session, "Hemispheres", "Rush", catalog_number="SRM-1-3743"
        )

        assert r1.id == r2.id

    def test_a_conflicting_catalog_number_creates_a_second_release(self, session):
        r1 = lib.get_or_create_release(
            session, "Hemispheres", "Rush", catalog_number="SRM-1-3743"
        )
        r2 = lib.get_or_create_release(
            session, "Hemispheres", "Rush", catalog_number="SRM-1-9999"
        )

        assert r1.id != r2.id


class TestAddCredit:
    def test_the_same_credit_is_not_duplicated(self, session):
        artist = lib.get_or_create_artist(session, "Geddy Lee")
        release = lib.get_or_create_release(session, "Signals", "Rush")

        c1 = lib.add_credit(session, artist, Credit.ROLE_MAIN, release=release)
        c2 = lib.add_credit(session, artist, Credit.ROLE_MAIN, release=release)

        assert c1.id == c2.id
        assert count(session, Credit) == 1

    def test_a_different_role_for_the_same_artist_and_release_is_a_separate_credit(self, session):
        artist = lib.get_or_create_artist(session, "Terry Brown")
        release = lib.get_or_create_release(session, "Signals", "Rush")

        lib.add_credit(session, artist, Credit.ROLE_MAIN, release=release)
        lib.add_credit(session, artist, "Producer", release=release)

        assert count(session, Credit) == 2


class TestFormatDuration:
    @pytest.mark.parametrize(
        "ms,expected",
        [
            (None, "--:--"),
            (0, "--:--"),
            (5_000, "0:05"),
            (61_000, "1:01"),
            (3_600_000, "1:00:00"),
            (3_661_000, "1:01:01"),
        ],
    )
    def test_formats(self, ms, expected):
        assert lib.format_duration(ms) == expected


class TestLibraryStats:
    def test_empty_database_is_all_zero(self, session):
        stats = lib.library_stats(session)

        assert stats == {
            "artists": 0, "releases": 0, "tracks": 0, "files": 0, "missing": 0,
            "playlists": 0, "charts": 0, "plays": 0, "total_ms": 0,
        }

    def test_counts_reflect_what_is_actually_in_the_database(self, session):
        lib.get_or_create_artist(session, "Rush")
        release = lib.get_or_create_release(session, "Signals", "Rush")
        track = Track(
            release_id=release.id, title="Subdivisions", title_key="subdivisions",
            duration_ms=305_000,
        )
        session.add(track)
        session.flush()
        session.add(MediaFile(track_id=track.id, path="/music/subdivisions.flac"))
        session.add(
            MediaFile(track_id=track.id, path="/music/missing.flac", is_missing=True)
        )
        session.add(Playlist(name="Favorites"))
        session.add(PlayEvent(track_id=track.id))
        session.flush()

        stats = lib.library_stats(session)

        assert stats["artists"] == 1
        assert stats["releases"] == 1
        assert stats["tracks"] == 1
        assert stats["files"] == 2
        assert stats["missing"] == 1
        assert stats["playlists"] == 1
        assert stats["plays"] == 1
        assert stats["total_ms"] == 305_000


class TestBackfillAlbumArtists:
    def test_fills_from_the_releases_own_main_credit(self, session):
        artist = lib.get_or_create_artist(session, "Rush")
        release = lib.get_or_create_release(session, "Signals", "Rush")
        lib.add_credit(session, artist, Credit.ROLE_MAIN, release=release)
        session.flush()
        assert release.album_artist_id is None  # simulating a pre-column database

        updated = lib.backfill_album_artists(session)

        assert updated == 1
        assert release.album_artist_id == artist.id

    def test_falls_back_to_matching_artist_display_when_there_is_no_credit_row(self, session):
        artist = lib.get_or_create_artist(session, "Rush")
        # get_or_create_release never adds a Credit itself - only the
        # denormalised artist_display string, same as a release that
        # predates the Main-credit-on-scan behavior
        release = lib.get_or_create_release(session, "Signals", "Rush")
        assert release.album_artist_id is None

        updated = lib.backfill_album_artists(session)

        assert updated == 1
        assert release.album_artist_id == artist.id

    def test_does_not_touch_a_release_that_already_has_one(self, session):
        lib.get_or_create_artist(session, "Rush")
        other = lib.get_or_create_artist(session, "Not Rush")
        release = lib.get_or_create_release(session, "Signals", "Rush")
        release.album_artist_id = other.id
        session.flush()

        updated = lib.backfill_album_artists(session)

        assert updated == 0
        assert release.album_artist_id == other.id

    def test_a_release_with_no_credit_and_no_matching_artist_name_is_left_alone(self, session):
        release = lib.get_or_create_release(session, "Orphan Album", "Nobody Registered")

        updated = lib.backfill_album_artists(session)

        assert updated == 0
        assert release.album_artist_id is None
