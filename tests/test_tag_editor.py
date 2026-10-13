"""Tests for the tag editor's service side (2026-10-06):
services/tag_writer.py (writing tags into every supported kind of file)
and services/tag_editor.py (writing, then bringing the library along -
history moving with the song, albums refreshed, charts re-matched).

Fixture audio: tests/fixtures/audio/silence.* - a tenth of a second of
silence in each format, made once with ffmpeg and checked in, so these
tests need no encoder.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr import config
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
)
from musicmgr.services import scanner
from musicmgr.services import tag_editor as te
from musicmgr.services import tag_writer as tw

FIXTURES = Path(__file__).parent / "fixtures" / "audio"
FORMATS = ["mp3", "flac", "ogg", "opus", "m4a", "wma", "wav", "aiff"]


def copy_fixture(ext: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / f"silence.{ext}", dest)
    return dest


FULL = tw.TagValues(
    title="Hells Bells", artist="AC/DC", album="Back in Black", album_artist="AC/DC",
    track="1", track_total="10", disc="1", disc_total="1", date="1980-07-25",
    genre="Rock", label="Atlantic", catalog_number="SD 16018", barcode="075678155820",
    country="US", isrc="USAT20001234", comment="[Rock][1980] #001#", compilation="",
    mb_album_id="a1", mb_album_artist_id="b2", mb_artist_id="c3",
    mb_release_group_id="d4", mb_release_track_id="e5", mb_recording_id="f6",
)


# --------------------------------------------------------------------------
# tag_writer
# --------------------------------------------------------------------------


class TestWriteEveryFormat:
    @pytest.mark.parametrize("ext", FORMATS)
    def test_round_trip(self, tmp_path, ext):
        path = copy_fixture(ext, tmp_path / f"song.{ext}")
        assert tw.write_values(path, FULL) is True
        back = tw.read_values(path)
        for name, value in FULL.changed_fields().items():
            assert getattr(back, name) == value, name

    @pytest.mark.parametrize("ext", FORMATS)
    def test_the_scanner_reads_what_was_written(self, tmp_path, ext):
        path = copy_fixture(ext, tmp_path / f"song.{ext}")
        tw.write_values(path, FULL)
        tags = scanner.read_tags(path)
        assert (tags.title, tags.artist, tags.album, tags.album_artist) == (
            "Hells Bells", "AC/DC", "Back in Black", "AC/DC")
        assert tags.track_no == 1
        assert tags.year == 1980
        assert tags.genres == ["Rock"]
        assert tags.comment == "[Rock][1980] #001#"

    @pytest.mark.parametrize("ext", FORMATS)
    def test_only_the_given_fields_change(self, tmp_path, ext):
        path = copy_fixture(ext, tmp_path / f"song.{ext}")
        tw.write_values(path, FULL)
        tw.write_values(path, tw.TagValues(title="Shoot to Thrill", comment=""))
        back = tw.read_values(path)
        assert back.title == "Shoot to Thrill"
        assert back.comment == ""
        assert back.album == "Back in Black"
        assert (back.track, back.track_total) == ("1", "10")
        assert back.label == "Atlantic"

    def test_nothing_to_change_leaves_the_file_alone(self, tmp_path):
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        tw.write_values(path, FULL)
        before = path.stat().st_mtime_ns
        assert tw.write_values(path, tw.TagValues(title="Hells Bells")) is False
        assert path.stat().st_mtime_ns == before

    def test_track_number_alone_keeps_the_total(self, tmp_path):
        path = copy_fixture("flac", tmp_path / "song.flac")
        tw.write_values(path, FULL)
        tw.write_values(path, tw.TagValues(track="2"))
        assert (tw.read_values(path).track, tw.read_values(path).track_total) == ("2", "10")

    def test_a_record_side_as_the_track_number(self, tmp_path):
        path = copy_fixture("mp3", tmp_path / "side.mp3")
        tw.write_values(path, tw.TagValues(title="Mule Train", track="A"))
        tags = scanner.read_tags(path)
        assert tags.side == "A"

    def test_m4a_refuses_a_side_letter(self, tmp_path):
        path = copy_fixture("m4a", tmp_path / "side.m4a")
        with pytest.raises(tw.TagWriteError, match="number"):
            tw.write_values(path, tw.TagValues(track="A"))

    def test_an_untagged_mp3_gets_id3v23(self, tmp_path):
        from mutagen.id3 import ID3
        from mutagen.mp3 import MP3
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        audio = MP3(str(path))
        audio.delete()  # ffmpeg's fixture carries a v2.4 encoder tag
        tw.write_values(path, FULL)
        assert ID3(str(path)).version[:2] == (2, 3)

    def test_an_id3v24_file_stays_v24(self, tmp_path):
        from mutagen.id3 import ID3, TIT2
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["x"]))
        tags.save(str(path), v2_version=4)
        tw.write_values(path, tw.TagValues(title="y"))
        assert ID3(str(path)).version[:2] == (2, 4)

    def test_new_label_replaces_an_old_organization_tag(self, tmp_path):
        from mutagen.flac import FLAC
        path = copy_fixture("flac", tmp_path / "song.flac")
        audio = FLAC(str(path))
        audio["ORGANIZATION"] = ["Old Label"]
        audio.save()
        tw.write_values(path, tw.TagValues(label="New Label"))
        assert scanner.read_tags(path).label == "New Label"

    def test_unwritable_kinds(self, tmp_path):
        aac = tmp_path / "raw.aac"
        aac.write_bytes(b"\xff\xf1" + bytes(50))
        assert not tw.can_write(aac)
        with pytest.raises(tw.TagWriteError):
            tw.write_values(aac, tw.TagValues(title="x"))

    def test_missing_file(self, tmp_path):
        with pytest.raises(tw.TagWriteError, match="isn't there"):
            tw.write_values(tmp_path / "gone.mp3", tw.TagValues(title="x"))

    def test_a_comment_edit_keeps_itunes_and_described_comments(self, tmp_path):
        from mutagen.id3 import COMM, ID3
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        tags = ID3(str(path))
        tags.add(COMM(encoding=3, lang="eng", desc="iTunSMPB", text=["00000000 00000210"]))
        tags.add(COMM(encoding=3, lang="eng", desc="iTunNORM", text=["000001A2"]))
        tags.add(COMM(encoding=3, lang="deu", desc="Notiz", text=["Hallo"]))
        tags.add(COMM(encoding=3, lang="eng", desc="", text=["old"]))
        tags.save(str(path))
        assert tw.read_values(path).comment == "old"
        tw.write_values(path, tw.TagValues(comment="[Rock][1980] #001#"))
        after = ID3(str(path))
        descs = sorted((f.desc, f.text[0]) for f in after.getall("COMM"))
        assert descs == [("", "[Rock][1980] #001#"), ("Notiz", "Hallo"),
                         ("iTunNORM", "000001A2"), ("iTunSMPB", "00000000 00000210")]
        assert scanner.read_tags(path).comment == "[Rock][1980] #001#"

    def test_scanner_never_takes_itunes_data_as_the_comment(self, tmp_path):
        from mutagen.id3 import COMM, ID3
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        tags = ID3(str(path))
        tags.add(COMM(encoding=3, lang="eng", desc="iTunNORM", text=["000001A2"]))
        tags.save(str(path))
        assert scanner.read_tags(path).comment == ""

    def test_a_v23_file_keeps_its_sort_frames_and_dates(self, tmp_path):
        from mutagen.id3 import ID3, TDRC, TIT2, TSOA, TSOP
        from mutagen.mp3 import MP3
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        MP3(str(path)).delete()
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["x"]))
        tags.add(TSOP(encoding=3, text=["Beatles, The"]))
        tags.add(TSOA(encoding=3, text=["Abbey Road"]))
        tags.add(TDRC(encoding=3, text=["1969-09-26"]))
        tags.save(str(path), v2_version=3)
        tw.write_values(path, tw.TagValues(title="Come Together"))
        after = ID3(str(path))
        assert after.version[:2] == (2, 3)
        assert after["TSOP"].text == ["Beatles, The"] and after["TSOA"].text == ["Abbey Road"]
        assert str(after["TDRC"].text[0]) == "1969-09-26"

    def test_no_temporary_copy_is_left_behind(self, tmp_path):
        path = copy_fixture("flac", tmp_path / "song.flac")
        tw.write_values(path, FULL)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["song.flac"]

    def test_a_briefly_locked_file_is_waited_for(self, tmp_path, monkeypatch):
        path = copy_fixture("flac", tmp_path / "song.flac")
        real, calls = tw.os.replace, []

        def busy_twice(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, "in use")
            return real(src, dst)
        monkeypatch.setattr(tw.os, "replace", busy_twice)
        monkeypatch.setattr(tw, "SWAP_STEP_S", 0.01)
        assert tw.write_values(path, tw.TagValues(title="After")) is True
        assert tw.read_values(path).title == "After" and len(calls) == 3

    def test_a_failed_swap_leaves_the_file_as_it_was(self, tmp_path, monkeypatch):
        path = copy_fixture("flac", tmp_path / "song.flac")
        tw.write_values(path, tw.TagValues(title="Before"))

        def locked(src, dst):
            raise PermissionError(13, "in use")
        monkeypatch.setattr(tw.os, "replace", locked)
        monkeypatch.setattr(tw, "SWAP_WAIT_S", 0.05)
        monkeypatch.setattr(tw, "SWAP_STEP_S", 0.01)
        with pytest.raises(tw.TagWriteError, match="in use"):
            tw.write_values(path, tw.TagValues(title="After"))
        assert tw.read_values(path).title == "Before"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["song.flac"]

    def test_several_genres(self, tmp_path):
        from mutagen.flac import FLAC
        path = copy_fixture("flac", tmp_path / "song.flac")
        audio = FLAC(str(path))
        audio["GENRE"] = ["Rock", "Pop"]
        audio.save()
        assert tw.read_values(path).genre == "Rock; Pop"
        tw.write_values(path, tw.TagValues(genre="Rock; Pop; Oldies"))
        assert FLAC(str(path))["GENRE"] == ["Rock", "Pop", "Oldies"]
        assert scanner.read_tags(path).genres == ["Rock", "Pop", "Oldies"]

    def test_an_old_organization_tag_is_kept_in_step(self, tmp_path):
        from mutagen.flac import FLAC
        path = copy_fixture("flac", tmp_path / "song.flac")
        audio = FLAC(str(path))
        audio["ORGANIZATION"] = ["Old Label"]
        audio.save()
        tw.write_values(path, tw.TagValues(label="New Label"))
        audio = FLAC(str(path))
        assert audio["LABEL"] == ["New Label"] and audio["ORGANIZATION"] == ["New Label"]

    @pytest.mark.parametrize("ext", ["m4a", "wav", "aiff", "wma"])
    def test_every_album_field_is_read_back_by_the_scanner(self, tmp_path, ext):
        path = copy_fixture(ext, tmp_path / f"song.{ext}")
        tw.write_values(path, FULL)
        tw.write_values(path, tw.TagValues(compilation="1"))
        t = scanner.read_tags(path)
        assert (t.label, t.catalog_number, t.barcode, t.country, t.isrc, t.compilation) == (
            "Atlantic", "SD 16018", "075678155820", "US", "USAT20001234", True)

    def test_diff_and_merge(self):
        before = tw.TagValues(title="a", album="b", genre="")
        after = tw.TagValues(title="a", album="c", genre="Rock")
        diff = tw.diff_values(before, after)
        assert diff.changed_fields() == {"album": "c", "genre": "Rock"}
        merged = tw.merge_values(before, diff)
        assert (merged.title, merged.album, merged.genre) == ("a", "c", "Rock")


# --------------------------------------------------------------------------
# tag_editor: the library follows the files
# --------------------------------------------------------------------------


def make_album(root: Path, album: str = "Back in Blak", artist: str = "AC/DC",
               titles=("Hells Bells", "Shoot to Thrill", "Back in Black"),
               ext: str = "mp3", year: str = "1980") -> list[Path]:
    paths = []
    for i, title in enumerate(titles, start=1):
        p = copy_fixture(ext, root / artist.replace("/", "-") / album / f"{i:02d} {title}.{ext}")
        tw.write_values(p, tw.TagValues(title=title, artist=artist, album=album,
                                        album_artist=artist, track=str(i), date=year,
                                        genre="Rock"))
        paths.append(p)
    return paths


@pytest.fixture
def art_dir(tmp_path, monkeypatch):
    folder = tmp_path / "artwork"
    folder.mkdir()
    monkeypatch.setattr(config, "ART_DIR", folder)
    return folder


def track_for(session, path: Path) -> Track:
    return session.scalar(select(MediaFile).where(MediaFile.path == str(path))).track


class TestLoadRows:
    def test_rows_come_from_the_files_in_album_order(self, session, tmp_path):
        paths = make_album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        ids = [track_for(session, p).id for p in reversed(paths)]
        rows = te.load_rows(session, ids)
        assert [r.values.title for r in rows] == ["Hells Bells", "Shoot to Thrill", "Back in Black"]
        assert all(r.editable for r in rows)
        assert rows[0].values.album == "Back in Blak"

    def test_a_missing_file_is_not_editable(self, session, tmp_path):
        paths = make_album(tmp_path, titles=("Only",))
        scanner.scan_folder(session, tmp_path)
        tid = track_for(session, paths[0]).id
        paths[0].unlink()
        row = te.load_rows(session, [tid])[0]
        assert not row.editable
        assert row.values.title == "Only"  # what the library knows


class TestSaveEdits:
    def _history(self, session, track):
        """Give a song a rating, plays, a playlist place, a jukebox card and
        a hand-fixed chart match."""
        track.rating = 5
        track.play_count = 7
        session.add(PlayEvent(track_id=track.id))
        playlist = Playlist(name="Favorites")
        session.add(playlist)
        session.flush()
        session.add(PlaylistItem(playlist_id=playlist.id, track_id=track.id, position=0))
        artist = session.scalar(select(Artist).where(Artist.name == track.release.artist_display))
        session.add(JukeboxSlot(slot_number=1, artist_id=artist.id, side_a_track_id=track.id))
        chart = Chart(name="Year End", slug="ye")
        session.add(chart)
        session.flush()
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(1980, 12, 31))
        session.add(issue)
        session.flush()
        locked = ChartEntry(issue_id=issue.id, rank=1, title=track.title, artist_name="AC/DC",
                            title_key="x", artist_key="ac dc", track_id=track.id,
                            match_locked=True, match_score=1.0)
        session.add(locked)
        session.flush()
        return locked

    def _assert_history(self, session, track, locked):
        assert track.rating == 5
        assert track.play_count == 7
        assert session.scalar(select(PlayEvent.track_id)) == track.id
        assert session.scalar(select(PlaylistItem.track_id)) == track.id
        assert session.scalar(select(JukeboxSlot.side_a_track_id)) == track.id
        session.refresh(locked)
        assert locked.track_id == track.id and locked.match_locked

    def test_fixing_a_whole_albums_title_renames_the_album(self, session, tmp_path, art_dir):
        from musicmgr.db.models import ReleaseFormat
        paths = make_album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        tracks = [track_for(session, p) for p in paths]
        rel = tracks[0].release
        rel_id, old_ids = rel.id, [t.id for t in tracks]
        rel.notes = "Bought at the record fair"
        for f in rel.formats:
            f.name, f.descriptions = "Vinyl", "LP, Reissue"
        cover = art_dir / "AC DC - Back in Blak.jpg"
        cover.write_bytes(b"\xff\xd8\xff fake jpeg")
        rel.cover_path = str(cover)
        locked = self._history(session, tracks[0])

        result = te.save_edits_in(session, {tid: tw.TagValues(album="Back in Black")
                                            for tid in old_ids})

        assert result.written == 3 and not result.failed
        assert result.moved == 0 and result.albums_removed == 0
        rel = session.get(Release, rel_id)
        assert rel.title == "Back in Black"
        assert rel.notes == "Bought at the record fair"
        assert [(f.name, f.descriptions) for f in rel.formats] == [("Vinyl", "LP, Reissue")]
        assert [track_for(session, p).id for p in paths] == old_ids
        assert "Back in Black" in Path(rel.cover_path).name
        assert len(list(session.scalars(select(Release)))) == 1
        self._assert_history(session, track_for(session, paths[0]), locked)

    def test_one_song_moved_to_another_album_takes_its_history(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        song = track_for(session, paths[0])
        old_id, rel_id = song.id, song.release_id
        locked = self._history(session, song)

        result = te.save_edits_in(session, {old_id: tw.TagValues(album="Singles")})

        new = track_for(session, paths[0])
        assert new.id != old_id and new.release.title == "Singles"
        assert result.moved == 1 and result.moves == {old_id: new.id}
        assert session.get(Track, old_id) is None
        assert session.get(Release, rel_id) is not None  # two songs stay on it
        self._assert_history(session, new, locked)

    def test_joining_an_album_thats_there_carries_the_cover(self, session, tmp_path, art_dir):
        good = make_album(tmp_path / "a", album="Back in Black", titles=("Hells Bells",))
        typo = make_album(tmp_path / "b", album="Back in Blak", titles=("Shoot to Thrill",))
        scanner.scan_folder(session, tmp_path)
        typo_track = track_for(session, typo[0])
        typo_rel = typo_track.release
        cover = art_dir / "typo.jpg"
        cover.write_bytes(b"\xff\xd8 cover")
        typo_rel.cover_path = str(cover)
        typo_rel_id = typo_rel.id

        result = te.save_edits_in(session, {typo_track.id: tw.TagValues(album="Back in Black")})

        assert result.albums_removed == 1
        assert session.get(Release, typo_rel_id) is None
        joined = track_for(session, typo[0]).release
        assert joined.id == track_for(session, good[0]).release_id
        assert joined.cover_path and Path(joined.cover_path).read_bytes() == b"\xff\xd8 cover"

    def test_titles_off_by_one_keep_each_songs_history(self, session, tmp_path, art_dir):
        # files whose titles were each one song off, fixed in one go:
        # the ratings must stay with the files, not follow the old titles
        paths = make_album(tmp_path, album="Fixed", titles=("Beta", "Gamma", "Delta"))
        scanner.scan_folder(session, tmp_path)
        playlist = Playlist(name="All")
        session.add(playlist)
        session.flush()
        for i, p in enumerate(paths):
            t = track_for(session, p)
            t.rating, t.play_count = i + 1, (i + 1) * 10
            session.add(PlaylistItem(playlist_id=playlist.id, track_id=t.id, position=i))
        session.flush()
        ids = [track_for(session, p).id for p in paths]
        edits = {ids[0]: tw.TagValues(title="Alpha"), ids[1]: tw.TagValues(title="Beta"),
                 ids[2]: tw.TagValues(title="Gamma")}

        te.save_edits_in(session, edits)

        got = [(track_for(session, p).title, track_for(session, p).rating,
                track_for(session, p).play_count) for p in paths]
        assert got == [("Alpha", 1, 10), ("Beta", 2, 20), ("Gamma", 3, 30)]
        items = [i.track_id for i in session.scalars(
            select(PlaylistItem).order_by(PlaylistItem.position))]
        assert items == [track_for(session, p).id for p in paths]

    def test_a_song_whose_second_file_fails_is_put_back(self, session, tmp_path, art_dir, monkeypatch):
        mp3 = copy_fixture("mp3", tmp_path / "AC-DC" / "Album" / "01 Song.mp3")
        flac = copy_fixture("flac", tmp_path / "AC-DC" / "Album" / "01 Song.flac")
        for p in (mp3, flac):
            tw.write_values(p, tw.TagValues(title="Song", artist="AC/DC", album="Album",
                                            album_artist="AC/DC", track="1"))
        scanner.scan_folder(session, tmp_path)
        track = track_for(session, mp3)
        assert track_for(session, flac).id == track.id
        real = tw.write_values

        def flaky(path, values):
            if str(path).endswith(".flac") and values.title == "Song (Live)":
                raise tw.TagWriteError("the file is in use by another program, or access was denied")
            return real(path, values)
        monkeypatch.setattr(tw, "write_values", flaky)

        result = te.save_edits_in(session, {track.id: tw.TagValues(title="Song (Live)")})

        assert result.written == 0
        assert result.failed and result.failed[0][0] == "01 Song.flac"
        assert tw.read_values(mp3).title == "Song"  # put back
        assert track_for(session, mp3).id == track_for(session, flac).id

    def test_case_only_fixes_reach_the_library(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path, album="back in black", titles=("hells bells",))
        scanner.scan_folder(session, tmp_path)
        t = track_for(session, paths[0])
        te.save_edits_in(session, {t.id: tw.TagValues(title="Hells Bells", album="Back in Black")})
        t = track_for(session, paths[0])
        assert t.title == "Hells Bells"
        assert t.release.title == "Back in Black"

    def test_a_looked_up_cover_only_goes_where_the_songs_went(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path)
        scanner.scan_folder(session, tmp_path)
        stay_rel = track_for(session, paths[1]).release
        mine = art_dir / "mine.jpg"
        mine.write_bytes(b"mine")
        stay_rel.cover_path = str(mine)
        te.save_edits_in(session, {track_for(session, paths[0]).id: tw.TagValues(album="Singles")},
                         cover=b"looked up")
        assert Path(track_for(session, paths[1]).release.cover_path).read_bytes() == b"mine"
        assert Path(track_for(session, paths[0]).release.cover_path).read_bytes() == b"looked up"

    def test_a_title_fix_stays_on_the_same_album(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path, titles=("Hells Bels", "Shoot to Thrill"))
        scanner.scan_folder(session, tmp_path)
        t = track_for(session, paths[0])
        rel_id = t.release_id
        t.rating = 4
        result = te.save_edits_in(session, {t.id: tw.TagValues(title="Hells Bells")})
        new = track_for(session, paths[0])
        assert new.title == "Hells Bells"
        assert new.release_id == rel_id
        assert new.rating == 4
        assert result.albums_removed == 0
        assert session.get(Release, rel_id) is not None

    def test_album_year_label_and_genre_follow_the_files(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path, year="1979")
        scanner.scan_folder(session, tmp_path)
        ids = [track_for(session, p).id for p in paths]
        te.save_edits_in(session, {tid: tw.TagValues(date="1980", genre="Hard Rock",
                                                     label="Atlantic")
                                   for tid in ids})
        rel = track_for(session, paths[0]).release
        assert rel.year == 1980
        assert [g.name for g in rel.genres] == ["Hard Rock"]
        assert rel.label.name == "Atlantic"

    def test_a_fixed_title_picks_up_a_chart_entry(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path, artist="Journey", album="Escape",
                           titles=("Dont Stop Believin",))
        scanner.scan_folder(session, tmp_path)
        t = track_for(session, paths[0])
        chart = Chart(name="Hot 100", slug="hot-100")
        session.add(chart)
        session.flush()
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(1981, 12, 5))
        session.add(issue)
        session.flush()
        entry = ChartEntry(issue_id=issue.id, rank=9, title="Don't Stop Believin'",
                           artist_name="Journey", title_key="dont stop believin",
                           artist_key="journey")
        from musicmgr.services.matching import normalize
        entry.title_key = normalize(entry.title)
        entry.artist_key = normalize(entry.artist_name)
        session.add(entry)
        session.flush()

        result = te.save_edits_in(session, {t.id: tw.TagValues(title="Don't Stop Believin'")})

        session.refresh(entry)
        assert entry.track_id == track_for(session, paths[0]).id
        assert result.charts_matched >= 1

    def test_a_failed_file_is_reported_and_the_rest_go_on(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path, titles=("One", "Two"))
        scanner.scan_folder(session, tmp_path)
        ids = [track_for(session, p).id for p in paths]
        paths[0].unlink()
        result = te.save_edits_in(session, {tid: tw.TagValues(genre="Pop") for tid in ids})
        assert result.written == 1
        assert result.failed == [("01 One.mp3", "the file isn't there any more")]
        assert tw.read_values(paths[1]).genre == "Pop"

    def test_compilation_without_album_artist_stays_one_album(self, session, tmp_path, art_dir):
        folder = tmp_path / "1949 Hits"
        for i, (title, artist) in enumerate((("Again", "Doris Day"), ("Mule Train", "Frankie Laine"),
                                             ("Riders", "Vaughn Monroe")), start=1):
            p = copy_fixture("mp3", folder / f"{i:02d} {title}.mp3")
            tw.write_values(p, tw.TagValues(title=title, artist=artist, album="1949 Hits",
                                            track=str(i)))
        scanner.scan_folder(session, tmp_path)
        assert session.scalar(select(Release.artist_display)) == "Various Artists"
        mule = track_for(session, folder / "02 Mule Train.mp3")
        te.save_edits_in(session, {mule.id: tw.TagValues(title="Mule Train (Single)")})
        releases = list(session.scalars(select(Release)))
        assert len(releases) == 1
        assert releases[0].artist_display == "Various Artists"

    def test_cancel_stops_before_writing(self, session, tmp_path, art_dir):
        paths = make_album(tmp_path, titles=("One", "Two"))
        scanner.scan_folder(session, tmp_path)
        ids = [track_for(session, p).id for p in paths]
        result = te.save_edits_in(session, {tid: tw.TagValues(genre="Pop") for tid in ids},
                                  should_stop=lambda: True)
        assert result.cancelled and result.written == 0

    def test_summary_text(self):
        r = te.SaveResult(written=3, moved=3, charts_matched=2)
        assert r.summary() == ("3 files updated · 3 songs moved with ratings, plays and "
                               "playlists · 2 chart rows matched")
        assert te.SaveResult().summary() == "Nothing needed changing"


class TestScannerKeepsHistoryWithTheFile:
    """The same history move happens when a rescan finds files retagged
    elsewhere - another PC after a USB sync, or Mp3tag/Picard - so USB
    sync (keyed by file path) never sees a song's rating as deleted."""

    def test_a_rescan_after_an_outside_retag(self, session, tmp_path):
        paths = make_album(tmp_path, album="Fixed", titles=("Beta", "Gamma", "Delta"))
        scanner.scan_folder(session, tmp_path)
        for i, p in enumerate(paths):
            track_for(session, p).rating = i + 1
        session.flush()
        for p, title in zip(paths, ("Alpha", "Beta", "Gamma")):
            tw.write_values(p, tw.TagValues(title=title))

        result = scanner.scan_folder(session, tmp_path)

        assert [(track_for(session, p).title, track_for(session, p).rating) for p in paths] == [
            ("Alpha", 1), ("Beta", 2), ("Gamma", 3)]
        assert len(result.history_moved) == 3
        assert len(list(session.scalars(select(Track)))) == 3

    def test_one_retitled_copy_leaves_the_other_with_the_history(self, session, tmp_path):
        folder = tmp_path / "AC-DC" / "Album"
        flac = copy_fixture("flac", folder / "0a.flac")
        mp3 = copy_fixture("mp3", folder / "b.mp3")
        for p in (flac, mp3):
            tw.write_values(p, tw.TagValues(title="Song", artist="AC/DC", album="Album",
                                            album_artist="AC/DC", track="1"))
        scanner.scan_folder(session, tmp_path)
        track = track_for(session, flac)
        assert track_for(session, mp3).id == track.id
        track.rating = 5
        session.flush()
        tw.write_values(mp3, tw.TagValues(title="Song Two"))

        scanner.scan_folder(session, tmp_path)

        assert track_for(session, flac).id == track.id
        assert track_for(session, flac).rating == 5
        assert track_for(session, mp3).title == "Song Two"
        assert track_for(session, mp3).rating is None

    def test_scanner_and_editor_agree_on_the_comment(self, tmp_path):
        from mutagen.id3 import COMM, ID3
        path = copy_fixture("mp3", tmp_path / "song.mp3")
        tags = ID3(str(path))
        tags.add(COMM(encoding=3, lang="deu", desc="", text=["Deutsch"]))
        tags.add(COMM(encoding=3, lang="eng", desc="", text=["Real comment"]))
        tags.save(str(path))
        assert scanner.read_tags(path).comment == tw.read_values(path).comment == "Real comment"

