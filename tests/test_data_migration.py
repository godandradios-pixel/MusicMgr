"""Tests for services/data_migration.py: moving the data folder (database +
artwork/portrait caches) to a new location, with the cover_path/image_path
rewrite that copying the folder alone can't give you, plus the safety
checks (`refuse_unsafe_removal`, the "dest already has a database" guard)
that stand between a wrong argument and `shutil.rmtree`-ing the wrong
folder.

This module speaks straight to sqlite3 and the filesystem rather than
going through the app's ORM session, so these tests build real,
schema-correct `library.db` files with `db_session.init_engine` (the same
call the real app makes at startup) and real files on disk for
artwork/portraits - not fakes standing in for either, since `verify()`'s
whole job is checking real files actually exist at the paths the database
claims.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musicmgr import config
from musicmgr.db import session as db_session
from musicmgr.db.models import Track
from musicmgr.services import data_migration as dm
from musicmgr.services import library as lib


def make_data_dir(
    tmp_path: Path,
    name: str,
    *,
    with_cover: bool = False,
    with_portrait: bool = False,
    cover_name: str = "cover.jpg",
    portrait_name: str = "1_artist.jpg",
) -> Path:
    """A real data folder: library.db (built via the app's own schema),
    plus an artwork/ and artists/ folder, each with a real file when asked
    for - matching exactly what a real install's data directory looks
    like, since data_migration reads/writes both the db and the files on
    disk directly."""
    data_dir = tmp_path / name
    (data_dir / "artwork").mkdir(parents=True)
    (data_dir / "artists").mkdir(parents=True)
    db_path = data_dir / "library.db"
    db_session.init_engine(db_path=db_path)

    cover_path = data_dir / "artwork" / cover_name
    portrait_path = data_dir / "artists" / portrait_name
    artist_kwargs = {"image_path": str(portrait_path)} if with_portrait else {}

    with db_session.session_scope() as session:
        artist = lib.get_or_create_artist(session, "Test Artist", **artist_kwargs)
        release_kwargs = {"cover_path": str(cover_path)} if with_cover else {}
        release = lib.get_or_create_release(
            session, "Test Album", "Test Artist", **release_kwargs
        )
        release.album_artist_id = artist.id
        session.add(
            Track(
                release_id=release.id,
                title="Song",
                title_key="song",
                artist_display="Test Artist",
            )
        )

    if with_cover:
        cover_path.write_bytes(b"fake jpg bytes")
    if with_portrait:
        portrait_path.write_bytes(b"fake jpg bytes")
    # dispose the engine's pooled connection(s) so this behaves like a real
    # data folder with the app closed - migrate()/repair() assume that (see
    # the module docstring's "close the app first"), and a leftover open
    # connection from building this fixture is otherwise the one thing here
    # that isn't representative of a real target folder.
    db_session.get_engine().dispose()
    return data_dir


class TestBasename:
    def test_unix_style_path(self):
        assert dm._basename("/data/artwork/cover.jpg") == "cover.jpg"

    def test_windows_style_path_on_any_platform(self):
        # the whole reason this helper exists instead of Path(...).name:
        # a Windows-written path with backslashes must still split
        # correctly when read back on Linux.
        assert dm._basename("C:\\Users\\James\\data\\artwork\\cover.jpg") == "cover.jpg"

    def test_a_bare_filename_with_no_directory(self):
        assert dm._basename("cover.jpg") == "cover.jpg"


class TestCheckpoint:
    def test_a_missing_database_is_a_no_op(self, tmp_path):
        dm.checkpoint(tmp_path / "does-not-exist.db")  # should not raise

    def test_folds_the_wal_back_into_the_main_file_without_losing_data(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src")
        db_path = data_dir / "library.db"
        before = dm.snapshot_counts(db_path)

        dm.checkpoint(db_path)

        assert dm.snapshot_counts(db_path) == before
        assert before["artists"] == 1


class TestSnapshotCounts:
    def test_a_missing_database_returns_an_empty_dict(self, tmp_path):
        assert dm.snapshot_counts(tmp_path / "nope.db") == {}

    def test_counts_reflect_the_real_row_counts(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src")

        counts = dm.snapshot_counts(data_dir / "library.db")

        assert counts["artists"] == 1
        assert counts["releases"] == 1
        assert counts["tracks"] == 1
        assert counts["media_files"] == 0
        assert counts["chart_entries"] == 0

    def test_a_table_that_does_not_exist_reports_negative_one_not_a_crash(self, tmp_path):
        db_path = tmp_path / "bare.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE artists (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        counts = dm.snapshot_counts(db_path)

        assert counts["artists"] == 0
        assert counts["releases"] == -1  # table doesn't exist in this bare db


class TestRewriteCoverPaths:
    def test_repoints_a_path_that_does_not_match_the_new_folder(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_cover=True)
        db_path = data_dir / "library.db"
        new_art = tmp_path / "elsewhere" / "artwork"

        changed = dm.rewrite_cover_paths(db_path, data_dir / "artwork", new_art)

        assert changed == 1
        conn = sqlite3.connect(str(db_path))
        (cover,) = conn.execute("SELECT cover_path FROM releases").fetchone()
        conn.close()
        assert cover == str(new_art / "cover.jpg")

    def test_a_path_that_already_matches_is_left_alone_and_not_counted(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_cover=True)
        db_path = data_dir / "library.db"

        changed = dm.rewrite_cover_paths(db_path, data_dir / "artwork", data_dir / "artwork")

        assert changed == 0

    def test_rows_with_no_cover_path_are_skipped(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_cover=False)
        db_path = data_dir / "library.db"

        changed = dm.rewrite_cover_paths(db_path, data_dir / "artwork", tmp_path / "new")

        assert changed == 0

    def test_matches_purely_by_filename_ignoring_old_art_argument(self, tmp_path):
        # old_art isn't consulted - passing garbage for it must not matter,
        # since this same function doubles as `repair()`'s in-place pass.
        data_dir = make_data_dir(tmp_path, "src", with_cover=True)
        db_path = data_dir / "library.db"

        changed = dm.rewrite_cover_paths(
            db_path, Path("/nonsense/does/not/exist"), data_dir / "artwork" / "renamed_dest"
        )

        assert changed == 1


class TestRewriteArtistImagePaths:
    def test_repoints_a_path_that_does_not_match_the_new_folder(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_portrait=True)
        db_path = data_dir / "library.db"
        new_dir = tmp_path / "elsewhere" / "artists"

        changed = dm.rewrite_artist_image_paths(db_path, data_dir / "artists", new_dir)

        assert changed == 1
        conn = sqlite3.connect(str(db_path))
        (image,) = conn.execute("SELECT image_path FROM artists").fetchone()
        conn.close()
        assert image == str(new_dir / "1_artist.jpg")

    def test_a_path_that_already_matches_is_left_alone_and_not_counted(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_portrait=True)
        db_path = data_dir / "library.db"

        changed = dm.rewrite_artist_image_paths(
            db_path, data_dir / "artists", data_dir / "artists"
        )

        assert changed == 0

    def test_rows_with_no_image_path_are_skipped(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_portrait=False)
        db_path = data_dir / "library.db"

        changed = dm.rewrite_artist_image_paths(db_path, data_dir / "artists", tmp_path / "new")

        assert changed == 0


class TestVerify:
    def test_a_missing_database_reports_a_single_problem(self, tmp_path):
        problems = dm.verify(tmp_path / "nope.db", tmp_path / "artwork", {})

        assert len(problems) == 1
        assert "missing" in problems[0]

    def test_a_row_count_mismatch_is_reported(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src")
        db_path = data_dir / "library.db"

        problems = dm.verify(db_path, data_dir / "artwork", {"artists": 5})

        assert any("artists" in p and "expected 5" in p for p in problems)

    def test_a_matching_expected_count_reports_no_problem_for_it(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src")
        db_path = data_dir / "library.db"
        expected = dm.snapshot_counts(db_path)

        problems = dm.verify(db_path, data_dir / "artwork", expected)

        assert problems == []

    def test_a_severely_corrupted_database_is_reported_not_raised(self, tmp_path):
        # 2026-09-13 fix: corruption this severe fails inside
        # snapshot_counts()'s own row-count queries, before
        # PRAGMA integrity_check ever gets to run - real bytes scrambled on
        # disk, not a mock, to reproduce that exact ordering. This used to
        # propagate out of verify() as an unhandled sqlite3.DatabaseError
        # ("database disk image is malformed"); it must come back as an
        # ordinary problem string instead.
        data_dir = make_data_dir(tmp_path, "src")
        db_path = data_dir / "library.db"
        dm.checkpoint(db_path)  # fold WAL in so the whole db lives in one file
        # scramble the back half of the file - real corruption, not a mock
        raw = bytearray(db_path.read_bytes())
        midpoint = len(raw) // 2
        for i in range(midpoint, len(raw)):
            raw[i] = (raw[i] + 137) % 256
        db_path.write_bytes(bytes(raw))

        problems = dm.verify(db_path, data_dir / "artwork", {})

        assert any("unreadable" in p for p in problems)

    def test_a_cover_path_pointing_at_a_missing_file_is_reported(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_cover=True)
        db_path = data_dir / "library.db"
        (data_dir / "artwork" / "cover.jpg").unlink()

        problems = dm.verify(db_path, data_dir / "artwork", dm.snapshot_counts(db_path))

        assert any("cover image" in p for p in problems)

    def test_a_portrait_path_pointing_at_a_missing_file_is_reported(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_portrait=True)
        db_path = data_dir / "library.db"
        (data_dir / "artists" / "1_artist.jpg").unlink()

        problems = dm.verify(
            db_path,
            data_dir / "artwork",
            dm.snapshot_counts(db_path),
            artist_img_dir=data_dir / "artists",
        )

        assert any("portrait" in p for p in problems)

    def test_omitting_artist_img_dir_skips_the_portrait_check_entirely(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "src", with_portrait=True)
        db_path = data_dir / "library.db"
        (data_dir / "artists" / "1_artist.jpg").unlink()  # would fail if checked

        problems = dm.verify(db_path, data_dir / "artwork", dm.snapshot_counts(db_path))

        assert problems == []


class TestIsWithin:
    def test_a_direct_child_is_within(self):
        assert dm._is_within(Path("/a/b/c"), Path("/a/b")) is True

    def test_the_same_path_is_within_itself(self):
        assert dm._is_within(Path("/a/b"), Path("/a/b")) is True

    def test_an_unrelated_path_is_not_within(self):
        assert dm._is_within(Path("/a/b"), Path("/x/y")) is False

    def test_a_parent_is_not_within_its_own_child(self):
        assert dm._is_within(Path("/a/b"), Path("/a/b/c")) is False


class TestLooksLikeProjectRoot:
    def test_a_folder_with_run_py_looks_like_the_project_root(self, tmp_path):
        (tmp_path / "run.py").write_text("# entry point")

        assert dm._looks_like_project_root(tmp_path) is True

    def test_a_folder_with_a_musicmgr_package_directory_looks_like_the_project_root(
        self, tmp_path
    ):
        (tmp_path / "musicmgr").mkdir()

        assert dm._looks_like_project_root(tmp_path) is True

    def test_a_plain_data_folder_does_not_look_like_the_project_root(self, tmp_path):
        (tmp_path / "library.db").write_bytes(b"")

        assert dm._looks_like_project_root(tmp_path) is False


class TestRefuseUnsafeRemoval:
    def test_a_destination_inside_the_source_is_refused(self, tmp_path):
        source = tmp_path / "data"
        source.mkdir()
        dest = source / "nested"

        reason = dm.refuse_unsafe_removal(source, dest)

        assert reason is not None
        assert "under" in reason

    def test_a_source_inside_the_destination_is_refused(self, tmp_path):
        dest = tmp_path / "new_home"
        dest.mkdir()
        source = dest / "old_data"

        reason = dm.refuse_unsafe_removal(source, dest)

        assert reason is not None

    def test_a_source_that_looks_like_the_project_root_is_refused(self, tmp_path):
        source = tmp_path / "MusicMgr"
        source.mkdir()
        (source / "run.py").write_text("# entry point")
        dest = tmp_path / "elsewhere"

        reason = dm.refuse_unsafe_removal(source, dest)

        assert reason is not None
        assert "run.py" in reason or "musicmgr" in reason

    def test_a_normal_sibling_move_is_safe(self, tmp_path):
        source = tmp_path / "old_data"
        source.mkdir()
        dest = tmp_path / "new_data"

        assert dm.refuse_unsafe_removal(source, dest) is None


class TestMigrationResultSummary:
    def test_noop(self, tmp_path):
        result = dm.MigrationResult(source=tmp_path, dest=tmp_path, reason="noop")
        assert "nothing to do" in result.summary().lower()

    def test_no_source_db(self, tmp_path):
        result = dm.MigrationResult(source=tmp_path, dest=tmp_path, reason="no_source_db")
        assert "nothing to move" in result.summary().lower()

    def test_dest_exists(self, tmp_path):
        result = dm.MigrationResult(source=tmp_path, dest=tmp_path, reason="dest_exists")
        assert "already exists" in result.summary().lower()

    def test_unsafe_removal(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path, dest=tmp_path, reason="unsafe_removal", error="bad idea"
        )
        assert "bad idea" in result.summary()

    def test_db_busy(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path, dest=tmp_path, reason="db_busy", error="database is locked"
        )
        assert "database is locked" in result.summary()
        assert "close musicmgr" in result.summary().lower()

    def test_io_error(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path, dest=tmp_path, reason="io_error", error="disk full"
        )
        assert "disk full" in result.summary()

    def test_verify_failed_lists_every_problem(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path,
            dest=tmp_path,
            reason="verify_failed",
            problems=["artists: expected 1, found 0", "integrity_check said: corrupt"],
        )
        summary = result.summary()
        assert "artists: expected 1, found 0" in summary
        assert "integrity_check said: corrupt" in summary

    def test_repaired_with_nothing_to_fix(self, tmp_path):
        result = dm.MigrationResult(source=tmp_path, dest=tmp_path, reason="repaired")
        summary = result.summary()
        assert "no cover art paths needed fixing" in summary.lower()
        assert "no artist portrait paths needed fixing" in summary.lower()

    def test_repaired_reports_counts_and_remaining_problems(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path,
            dest=tmp_path,
            reason="repaired",
            changed_covers=3,
            changed_artist_images=2,
            problems=["1 cover image(s) not found at their new path"],
        )
        summary = result.summary()
        assert "3 cover art path(s)" in summary
        assert "2 artist portrait path(s)" in summary
        assert "still don't resolve" in summary.lower()

    def test_success_with_source_removed(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path / "old",
            dest=tmp_path / "new",
            reason="success",
            removed_source=True,
        )
        summary = result.summary()
        assert "deleted" in summary.lower()
        assert "delete these by hand" not in summary.lower()

    def test_success_with_leftovers_lists_them(self, tmp_path):
        leftover = tmp_path / "old" / "library.db"
        result = dm.MigrationResult(
            source=tmp_path / "old",
            dest=tmp_path / "new",
            reason="success",
            leftovers=[leftover],
        )
        summary = result.summary()
        assert "delete these by hand" in summary.lower()
        assert str(leftover) in summary

    def test_success_portable_dest_without_stale_env_var(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path / "old",
            dest=tmp_path / "new",
            reason="success",
            is_portable_dest=True,
        )
        summary = result.summary()
        assert "portable mode is now active" in summary.lower()
        assert "musicmgr_home" not in summary.lower()

    def test_success_portable_dest_with_stale_env_var_warns(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path / "old",
            dest=tmp_path / "new",
            reason="success",
            is_portable_dest=True,
            stale_musicmgr_home="C:\\old\\location",
        )
        summary = result.summary()
        assert "MUSICMGR_HOME is still set" in summary
        assert "C:\\old\\location" in summary

    def test_success_non_portable_dest_tells_you_to_set_musicmgr_home(self, tmp_path):
        result = dm.MigrationResult(
            source=tmp_path / "old", dest=tmp_path / "new", reason="success"
        )
        summary = result.summary()
        assert f"MUSICMGR_HOME={tmp_path / 'new'}" in summary


class TestRepair:
    def test_no_source_db_is_reported_and_nothing_raises(self, tmp_path):
        result = dm.repair(tmp_path)

        assert result.ok is False
        assert result.reason == "no_source_db"

    def test_repoints_paths_already_correct_reports_zero_changes(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "data", with_cover=True, with_portrait=True)

        result = dm.repair(data_dir)

        assert result.ok is True
        assert result.reason == "repaired"
        assert result.changed_covers == 0
        assert result.changed_artist_images == 0
        assert result.problems == []

    def test_repoints_a_path_left_over_from_another_machine(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "data", with_cover=True, with_portrait=True)
        db_path = data_dir / "library.db"
        # simulate a data/ folder copied by hand: the db still has the old
        # machine's absolute path even though the file made the trip fine
        # under the same name.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE releases SET cover_path = ?",
            (r"C:\Users\OldMachine\data\artwork\cover.jpg",),
        )
        conn.execute(
            "UPDATE artists SET image_path = ?",
            (r"C:\Users\OldMachine\data\artists\1_artist.jpg",),
        )
        conn.commit()
        conn.close()

        result = dm.repair(data_dir)

        assert result.changed_covers == 1
        assert result.changed_artist_images == 1
        assert result.problems == []

    def test_reports_a_reference_that_still_does_not_resolve(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "data", with_cover=True)
        (data_dir / "artwork" / "cover.jpg").unlink()

        result = dm.repair(data_dir)

        assert result.ok is True  # repair still completes; it just reports
        assert any("cover image" in p for p in result.problems)


class TestMigrate:
    def test_source_equal_to_dest_is_a_noop(self, tmp_path):
        data_dir = make_data_dir(tmp_path, "data")

        result = dm.migrate(data_dir, data_dir)

        assert result.ok is True
        assert result.reason == "noop"

    def test_no_database_at_the_source_is_reported(self, tmp_path):
        source = tmp_path / "empty"
        source.mkdir()

        result = dm.migrate(source, tmp_path / "dest")

        assert result.ok is False
        assert result.reason == "no_source_db"

    def test_an_existing_database_at_the_destination_blocks_the_move(self, tmp_path):
        source = make_data_dir(tmp_path, "source")
        dest = make_data_dir(tmp_path, "dest")

        result = dm.migrate(source, dest)

        assert result.ok is False
        assert result.reason == "dest_exists"

    def test_force_overrides_an_existing_destination_database(self, tmp_path):
        source = make_data_dir(tmp_path, "source", with_cover=True)
        dest = make_data_dir(tmp_path, "dest")

        result = dm.migrate(source, dest, force=True)

        assert result.ok is True
        assert result.reason == "success"

    def test_an_unsafe_removal_request_is_refused_before_touching_anything(self, tmp_path):
        source = make_data_dir(tmp_path, "data")
        dest = source / "nested"

        result = dm.migrate(source, dest, remove_source=True)

        assert result.ok is False
        assert result.reason == "unsafe_removal"
        assert source.exists()  # never touched

    def test_a_successful_move_copies_and_rewrites_everything(self, tmp_path):
        source = make_data_dir(tmp_path, "source", with_cover=True, with_portrait=True)
        dest = tmp_path / "dest"

        result = dm.migrate(source, dest)

        assert result.ok is True
        assert result.reason == "success"
        assert result.changed_covers == 1
        assert result.changed_artist_images == 1
        assert (dest / "library.db").exists()
        assert (dest / "artwork" / "cover.jpg").exists()
        assert (dest / "artists" / "1_artist.jpg").exists()
        conn = sqlite3.connect(str(dest / "library.db"))
        (cover,) = conn.execute("SELECT cover_path FROM releases").fetchone()
        (image,) = conn.execute("SELECT image_path FROM artists").fetchone()
        conn.close()
        assert cover == str(dest / "artwork" / "cover.jpg")
        assert image == str(dest / "artists" / "1_artist.jpg")

    def test_the_source_is_never_modified(self, tmp_path):
        source = make_data_dir(tmp_path, "source", with_cover=True)
        original_cover = sqlite3.connect(str(source / "library.db")).execute(
            "SELECT cover_path FROM releases"
        ).fetchone()[0]

        dm.migrate(source, tmp_path / "dest")

        after_cover = sqlite3.connect(str(source / "library.db")).execute(
            "SELECT cover_path FROM releases"
        ).fetchone()[0]
        assert after_cover == original_cover

    def test_data_written_only_to_the_wal_still_makes_it_across(self, tmp_path):
        # migrate() checkpoints the source before copying specifically so a
        # write that's only in -wal at copy time isn't silently dropped -
        # this is the behavior that guards against exactly that.
        source = make_data_dir(tmp_path, "source")
        with db_session.session_scope() as session:
            lib.get_or_create_artist(session, "Second Artist")
        # deliberately not checkpointing by hand - migrate() must do it

        result = dm.migrate(source, tmp_path / "dest")

        assert result.ok is True
        assert dm.snapshot_counts(tmp_path / "dest" / "library.db")["artists"] == 2

    def test_progress_callback_receives_a_line_for_each_step(self, tmp_path):
        source = make_data_dir(tmp_path, "source", with_cover=True)
        lines: list[str] = []

        dm.migrate(source, tmp_path / "dest", progress=lines.append)

        joined = "\n".join(lines)
        assert "Copying database" in joined
        assert "Copying artwork cache" in joined
        assert "Rewriting cover art paths" in joined
        assert "Verifying" in joined

    def test_remove_source_true_deletes_the_original_folder(self, tmp_path):
        source = make_data_dir(tmp_path, "source")

        result = dm.migrate(source, tmp_path / "dest", remove_source=True)

        assert result.ok is True
        assert result.removed_source is True
        assert not source.exists()

    def test_remove_source_false_reports_the_original_files_as_leftovers(self, tmp_path):
        source = make_data_dir(tmp_path, "source", with_cover=True, with_portrait=True)

        result = dm.migrate(source, tmp_path / "dest", remove_source=False)

        assert result.removed_source is False
        assert source.exists()
        assert source / "library.db" in result.leftovers
        assert source / "artwork" in result.leftovers
        assert source / "artists" in result.leftovers

    def test_a_cover_that_never_existed_at_the_source_fails_verification(self, tmp_path):
        source = make_data_dir(tmp_path, "source", with_cover=True)
        # the file the database points at never made it into artwork/ at
        # all (e.g. deleted by hand between scans) - copying can't produce
        # what was never there to begin with, so this must surface as a
        # reported failure rather than a silently "successful" move.
        (source / "artwork" / "cover.jpg").unlink()

        result = dm.migrate(source, tmp_path / "dest")

        assert result.ok is False
        assert result.reason == "verify_failed"
        assert any("cover image" in p for p in result.problems)
        # nothing renamed/removed at the source on a failed verify
        assert (source / "library.db").exists()

    def test_a_checkpoint_failure_is_reported_as_db_busy_not_raised(self, tmp_path, monkeypatch):
        source = make_data_dir(tmp_path, "source")

        def flaky_checkpoint(_db_path):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dm, "checkpoint", flaky_checkpoint)

        result = dm.migrate(source, tmp_path / "dest")

        assert result.ok is False
        assert result.reason == "db_busy"
        assert "locked" in result.error

    def test_a_filesystem_failure_during_copy_is_reported_as_io_error(self, tmp_path, monkeypatch):
        source = make_data_dir(tmp_path, "source")

        def flaky_copy2(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(dm.shutil, "copy2", flaky_copy2)

        result = dm.migrate(source, tmp_path / "dest")

        assert result.ok is False
        assert result.reason == "io_error"
        assert "disk full" in result.error

    def test_missing_artwork_and_artists_folders_at_the_source_are_not_an_error(self, tmp_path):
        # a data dir with no covers or portraits imported yet - migrate()
        # must still succeed and simply create empty folders at dest.
        data_dir = tmp_path / "bare"
        data_dir.mkdir()
        db_session.init_engine(db_path=data_dir / "library.db")
        with db_session.session_scope() as session:
            lib.get_or_create_artist(session, "No Art Artist")

        result = dm.migrate(data_dir, tmp_path / "dest")

        assert result.ok is True
        assert (tmp_path / "dest" / "artwork").is_dir()
        assert (tmp_path / "dest" / "artists").is_dir()

    def test_is_portable_dest_true_when_moving_into_the_project_roots_data_folder(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("MUSICMGR_HOME", raising=False)
        source = make_data_dir(tmp_path, "source")

        result = dm.migrate(source, tmp_path / "data")

        assert result.is_portable_dest is True
        assert result.stale_musicmgr_home is None

    def test_is_portable_dest_flags_a_stale_musicmgr_home_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("MUSICMGR_HOME", str(tmp_path / "somewhere_else"))
        source = make_data_dir(tmp_path, "source")

        result = dm.migrate(source, tmp_path / "data")

        assert result.is_portable_dest is True
        assert result.stale_musicmgr_home == str(tmp_path / "somewhere_else")

    def test_is_portable_dest_false_for_an_ordinary_destination(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "project")
        source = make_data_dir(tmp_path, "source")

        result = dm.migrate(source, tmp_path / "dest")

        assert result.is_portable_dest is False
