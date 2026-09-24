"""services/artwork_names.py - MusicBee-style artwork names (2026-09-24,
USB sync step 2, plan §2)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musicmgr import config
from musicmgr.db import session as db_session
from musicmgr.db.models import Artist, Release
from musicmgr.services import artwork_names as an
from musicmgr.services import usb_sync as us


@pytest.fixture
def data(tmp_path, monkeypatch, db):
    """A throwaway data folder with artwork\\ and artists\\."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(config, "ART_DIR", root / "artwork")
    monkeypatch.setattr(config, "ARTIST_IMG_DIR", root / "artists")
    monkeypatch.setattr(config, "DB_BACKUP_DIR", root / "backups")
    (root / "artwork").mkdir(parents=True)
    (root / "artists").mkdir(parents=True)
    return root


def img(path: Path, data: bytes = b"\xff\xd8jpeg", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def release(s, artist="Captain & Tennille", title="Love Will Keep Us Together", **kw):
    r = Release(title=title, title_key=title.lower(), artist_display=artist, **kw)
    s.add(r)
    s.flush()
    return r


def artist(s, name="Captain & Tennille", **kw):
    a = Artist(name=name, name_key=name.lower(), **kw)
    s.add(a)
    s.flush()
    return a


class TestNames:
    @pytest.mark.parametrize("raw,expected", [
        ("AC/DC", "AC DC"),                           # MusicBee: E:\...\Thumb\AC DC.jpg
        ("’Til Tuesday", "’Til Tuesday"),
        ("“Weird Al” Yankovic", "“Weird Al” Yankovic"),
        ("Blood, Sweat & Tears", "Blood, Sweat & Tears"),
        ('Say "What?"', "Say What"),
        ("Trailing dots...", "Trailing dots"),
        ("  lots   of   space  ", "lots of space"),
        ("CON", "CON_"),
        ("", "Unknown"),
    ])
    def test_safe_image_name(self, raw, expected):
        assert an.safe_image_name(raw) == expected

    def test_long_names_are_cut_with_a_stable_hash(self):
        a = an.safe_image_name("x" * 300)
        b = an.safe_image_name("x" * 299 + "y")
        assert len(a) <= an.MAX_NAME and a != b
        assert a == an.safe_image_name("x" * 300)

    def test_lookup_ignores_case_and_punctuation(self, tmp_path):
        img(tmp_path / "AC-DC.png")
        for stem in ("AC DC", "ac/dc", "AC-DC"):
            assert an.find_image(tmp_path, stem).name == "AC-DC.png"
        assert an.find_image(tmp_path, "ACDC") is None

    def test_live_album_is_not_the_studio_album(self, tmp_path):
        img(tmp_path / "Eagles - Hotel California.jpg")
        assert an.find_image(tmp_path, "Eagles - Hotel California (Live)") is None

    def test_release_and_compilation_stems(self, data):
        with db_session.session_scope() as s:
            r = release(s, "AC/DC", "Back in Black")
            c = release(s, "Various", "Northbeam Singles", is_compilation=True)
            assert an.release_stem(r) == "AC DC - Back in Black"
            assert an.release_stem(c) == "Various Artists - Northbeam Singles"


class TestSaving:
    def test_scanner_never_overwrites(self, data):
        with db_session.session_scope() as s:
            r = release(s)
            first = an.save_release_cover(r, b"\xff\xd8chosen", overwrite=True)
            again = an.save_release_cover(r, b"\xff\xd8embedded", overwrite=False)
            assert first == again
            assert Path(first).read_bytes() == b"\xff\xd8chosen"
            assert Path(first).name == "Captain & Tennille - Love Will Keep Us Together.jpg"

    def test_download_replaces_and_sets_old_extension_aside(self, data):
        with db_session.session_scope() as s:
            r = release(s)
            png = an.save_release_cover(r, b"\x89PNG\r\n\x1a\nold")
            jpg = an.save_release_cover(r, b"\xff\xd8new")
            assert Path(png).suffix == ".png" and Path(jpg).suffix == ".jpg"
            assert not Path(png).exists()
            assert list((config.ART_DIR / an.SUPERSEDED_DIR).iterdir())

    def test_existing_spelling_is_reused(self, data):
        img(config.ARTIST_IMG_DIR / "ACE.jpg", b"old")
        with db_session.session_scope() as s:
            a = artist(s, "Ace")
            src = img(Path(data) / "in" / "ace photo.jpg", b"new")
            path = an.save_artist_image(a, src)
        assert Path(path).name == "ACE.jpg" and Path(path).read_bytes() == b"new"
        assert [p.name for p in config.ARTIST_IMG_DIR.iterdir()] == ["ACE.jpg"]

    def test_scanner_and_downloader_use_names(self, data):
        from musicmgr.services import artwork_downloader, scanner

        with db_session.session_scope() as s:
            r = release(s, "Rush", "Moving Pictures")
            assert Path(scanner._save_cover(b"\xff\xd8a", r)).name == "Rush - Moving Pictures.jpg"
            path = artwork_downloader._save_cover(b"\xff\xd8b", r)
            assert Path(path).read_bytes() == b"\xff\xd8b"

    def test_artist_image_import_uses_names(self, data, tmp_path):
        from musicmgr.services import artist_images

        with db_session.session_scope() as s:
            a = artist(s, "Bill Haley & His Comets")
            dest = artist_images._store(img(tmp_path / "thumb.jpg"), a)
        assert Path(dest).name == "Bill Haley & His Comets.jpg"


class TestMigration:
    def test_renames_keeps_twins_sets_aside_unused_and_backs_up(self, data, tmp_path):
        art, arts = config.ART_DIR, config.ARTIST_IMG_DIR
        old1 = img(art / "release_1_aaaa.jpg", b"one", 1000)
        old2 = img(art / "release_2_bbbb.jpg", b"two-newer", 2000)   # same name, newer
        old3 = img(art / "release_3_cccc.png", b"three")
        img(art / "release_99_dead.jpg", b"orphan")
        outside = img(tmp_path / "Pictures" / "rush.jpg", b"rush")
        img(arts / "7_Rush.jpg", b"rush-old")
        db_file = tmp_path / "library.db"
        import sqlite3
        sqlite3.connect(db_file).close()
        with db_session.session_scope() as s:
            r1 = release(s, "Rush", "Moving Pictures", cover_path=str(old1))
            r2 = release(s, "Rush", "Moving Pictures", cover_path=str(old2))
            r3 = release(s, "AC/DC", "Back in Black", cover_path=str(old3))
            a = artist(s, "Rush", image_path=str(outside))
            res = an.migrate(s, db_path=db_file)
            # two different pictures for one name: newest keeps the plain name
            assert r2.cover_path == str(art / "Rush - Moving Pictures.jpg")
            assert r1.cover_path == str(art / "Rush - Moving Pictures (2).jpg")
            assert r3.cover_path == str(art / "AC DC - Back in Black.png")
            assert a.image_path == str(arts / "Rush.jpg")
            assert an.naming_done(s)
        assert (art / "Rush - Moving Pictures.jpg").read_bytes() == b"two-newer"
        assert outside.exists()                          # copied in, never moved
        assert (arts / "Rush.jpg").read_bytes() == b"rush"
        assert (art / "Rush - Moving Pictures (2).jpg").read_bytes() == b"one"
        assert {p.name for p in (art / an.UNUSED_DIR).iterdir()} == {"release_99_dead.jpg"}
        assert {p.name for p in (arts / an.UNUSED_DIR).iterdir()} == {"7_Rush.jpg"}
        assert res.backup and res.backup.exists()
        assert res.log_path and res.log_path.exists()

    def test_identical_pictures_share_one_file(self, data):
        a = img(config.ART_DIR / "release_1_a.jpg", b"same")
        b = img(config.ART_DIR / "release_2_b.jpg", b"same")
        with db_session.session_scope() as s:
            r1 = release(s, "Rush", "2112", cover_path=str(a))
            r2 = release(s, "Rush", "2112", cover_path=str(b))
            an.migrate(s)
            assert r1.cover_path == r2.cover_path == str(config.ART_DIR / "Rush - 2112.jpg")

    def test_second_run_changes_nothing(self, data):
        img(config.ART_DIR / "release_1_x.jpg", b"one")
        with db_session.session_scope() as s:
            release(s, "Rush", "2112", cover_path=str(config.ART_DIR / "release_1_x.jpg"))
            an.migrate(s)
            before = sorted(p.name for p in config.ART_DIR.rglob("*"))
            res = an.migrate(s)
        assert res.renamed == 0 and res.already_named == 1
        assert sorted(p.name for p in config.ART_DIR.rglob("*")) == before

    def test_moved_data_folder_path_still_resolves(self, data):
        img(config.ART_DIR / "release_5_x.jpg", b"five")
        with db_session.session_scope() as s:
            r = release(s, "Rush", "Signals", cover_path=r"C:\Old\data\artwork\release_5_x.jpg")
            an.migrate(s)
            assert Path(r.cover_path).name == "Rush - Signals.jpg"

    def test_migrate_if_needed_runs_once(self, data):
        with db_session.session_scope() as s:
            assert an.migrate_if_needed(s) is not None
            assert an.migrate_if_needed(s) is None


class TestRelink:
    def test_links_pictures_that_arrived(self, data):
        img(config.ART_DIR / "Rush - Signals.jpg")
        img(config.ARTIST_IMG_DIR / "Rush.jpg")
        with db_session.session_scope() as s:
            r = release(s, "Rush", "Signals")
            artist(s, "Rush")
            res = an.relink(s)
            assert res.covers == 1 and res.artist_images == 1
            assert r.cover_path == str(config.ART_DIR / "Rush - Signals.jpg")
            assert an.relink(s).covers == 0


class TestSyncPairs:
    def test_artwork_pairs_only_after_migration(self, data, tmp_path):
        drive = us.write_marker(tmp_path / "USB")
        with db_session.session_scope() as s:
            assert us.ensure_default_pairs(s, drive) == []
            an.migrate(s)
            pairs = us.ensure_default_pairs(s, drive)
            assert {(p.usb_rel_path, p.kind) for p in pairs} == {
                ("MusicMgr/artwork", "artwork"), ("MusicMgr/artists", "artwork")}

    def test_round_trip_between_two_libraries_with_different_ids(self, data, tmp_path, monkeypatch):
        """PC1 picks a cover; the USB carries it; PC2 (different release id)
        links it after its sync."""
        drive = us.write_marker(tmp_path / "USB")
        with db_session.session_scope() as s:
            for _ in range(5):
                release(s, "Filler", f"Album {_}")      # push PC1's ids up
            r = release(s, "Rush", "Signals")
            an.save_release_cover(r, b"\xff\xd8pc1 choice")
            an.migrate(s)
            plan = us.build_plan(s, drive)
            us.apply(s, plan, trash=lambda p: None)
        assert (tmp_path / "USB" / "MusicMgr" / "artwork" / "Rush - Signals.jpg").exists()

        # PC2: a fresh library and data folder
        db_session.init_engine(db_path=":memory:")
        pc2 = tmp_path / "pc2"
        monkeypatch.setattr(config, "DATA_DIR", pc2)
        monkeypatch.setattr(config, "ART_DIR", pc2 / "artwork")
        monkeypatch.setattr(config, "ARTIST_IMG_DIR", pc2 / "artists")
        (pc2 / "artwork").mkdir(parents=True)
        (pc2 / "artists").mkdir(parents=True)
        with db_session.session_scope() as s:
            r2 = release(s, "Rush", "Signals")
            assert r2.id == 1
            an.migrate(s)
            plan = us.build_plan(s, drive)
            result = us.apply(s, plan, trash=lambda p: None)
            msg = us.update_library(s, result)
            assert "Linked 1 covers" in msg
            assert Path(r2.cover_path).read_bytes() == b"\xff\xd8pc1 choice"


class TestSameNameDifferentAlbums:
    def test_two_greatest_hits_keep_their_own_covers(self, data):
        art = config.ART_DIR
        a = img(art / "release_1_a.jpg", b"eighties", 2000)
        b = img(art / "release_2_b.jpg", b"seventies", 1000)
        with db_session.session_scope() as s:
            r1 = release(s, "Various", "Greatest Hits", is_compilation=True, year=1985, cover_path=str(a))
            r2 = release(s, "Various", "Greatest Hits", is_compilation=True, year=1975, cover_path=str(b))
            an.migrate(s)
            assert Path(r1.cover_path).name == "Various Artists - Greatest Hits.jpg"
            assert Path(r2.cover_path).name == "Various Artists - Greatest Hits (1975).jpg"
            assert Path(r2.cover_path).read_bytes() == b"seventies"
            # a relink after a sync must not pull r2 back to the plain name
            an.relink(s)
            assert Path(r2.cover_path).name == "Various Artists - Greatest Hits (1975).jpg"
