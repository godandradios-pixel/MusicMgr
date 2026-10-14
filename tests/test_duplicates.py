"""Tests for services/duplicates.py and Settings › Duplicates (2026-10-13 -
James: "Duplicate finder ... from an identical file standpoint. Playlist
should point to songs in albums not chart folders in the music. For
example, all playlists that point to billboard should try to point to the
album songs")."""

from __future__ import annotations

import random
import struct
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db.models import MediaFile, PlaylistItem, Track, WatchedFolder
from musicmgr.services import duplicates as dup
from musicmgr.services import library as lib
from musicmgr.services import playlists as pl_svc
from musicmgr.services.matching import normalize


def audio_bytes(seed: int, n: int = 600_000) -> bytes:
    rnd = random.Random(seed)
    return b"\xff\xfb\x90\x64" + bytes(rnd.getrandbits(8) for _ in range(n))


def id3(title: str, pad: int = 0) -> bytes:
    body = b"TIT2" + struct.pack(">I", len(title) + 1) + b"\x00\x00\x00" + title.encode() + b"\x00" * pad
    size = len(body)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + body


def mp3(path: Path, seed: int, title: str = "x", pad: int = 0, v1: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = id3(title, pad) + audio_bytes(seed)
    if v1:
        data += b"TAG" + title.encode().ljust(125, b"\x00")
    path.write_bytes(data)
    return path


def flac(path: Path, md5: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate, channels, bps, samples = 44100, 2, 16, 44100
    packed = (rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | samples
    info = struct.pack(">HH", 4096, 4096) + b"\x00\x00\x10" + b"\x00\x20\x00" \
        + packed.to_bytes(8, "big") + md5
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info + b"\x00" * 64)
    return path


class TestFingerprint:
    def test_same_recording_different_tags(self, tmp_path):
        a = mp3(tmp_path / "Billboard" / "a.mp3", 1, "Jaded", pad=4000, v1=True)
        b = mp3(tmp_path / "Album" / "b.mp3", 1, "2-10 Jaded")
        assert a.stat().st_size != b.stat().st_size
        assert dup.fingerprint(str(a)) == dup.fingerprint(str(b)) is not None
        assert dup.audio_size(str(a)) == dup.audio_size(str(b)) == len(audio_bytes(1))

    def test_different_recordings(self, tmp_path):
        a = mp3(tmp_path / "a.mp3", 1)
        b = mp3(tmp_path / "b.mp3", 2)
        assert dup.fingerprint(str(a)) != dup.fingerprint(str(b))

    def test_flac_uses_the_audio_checksum_in_its_header(self, tmp_path):
        a = flac(tmp_path / "a.flac", b"\x11" * 16)
        b = flac(tmp_path / "b.flac", b"\x11" * 16)
        c = flac(tmp_path / "c.flac", b"\x22" * 16)
        assert dup.fingerprint(str(a)) == dup.fingerprint(str(b))
        assert dup.fingerprint(str(a)) != dup.fingerprint(str(c))
        assert dup.fingerprint(str(flac(tmp_path / "d.flac", b"\x00" * 16))) is None

    def test_unreadable_files(self, tmp_path):
        assert dup.fingerprint(str(tmp_path / "gone.mp3")) is None
        (tmp_path / "x.m4a").write_bytes(b"....")
        assert dup.fingerprint(str(tmp_path / "x.m4a")) is None


def test_chart_folders(tmp_path):
    roots = [dup._norm(r"D:\Music"), dup._norm(r"D:\MusicFLAC")]
    assert dup.chart_folder(r"D:\Music\Billboard\1960-69\1965\x.mp3", roots) == "Billboard"
    assert dup.chart_folder(r"D:\Music\Hot Country\2010-19\x.mp3", roots) == "Hot Country"
    assert dup.chart_folder(r"D:\Music\Oldies\1930 - 1939\x.mp3", roots) == "Oldies"
    assert dup.chart_folder(r"D:\Music\Aerosmith\O, Yeah!\x.mp3", roots) is None
    # a folder called Oldies inside an artist's folder is just an album
    assert dup.chart_folder(r"D:\Music\Some Band\Oldies\x.mp3", roots) is None


# -- the library -------------------------------------------------------------


def add_song(session, path: Path, title: str, artist: str, album: str, ms: int,
             album_artist: str | None = None, compilation: bool = False) -> Track:
    release = lib.get_or_create_release(session, album, album_artist or artist)
    release.is_compilation = compilation
    a = lib.get_or_create_artist(session, album_artist or artist)
    release.album_artist_id = a.id
    t = Track(release_id=release.id, title=title, title_key=normalize(title),
              artist_display=artist, duration_ms=ms)
    session.add(t)
    session.flush()
    session.add(MediaFile(track_id=t.id, path=str(path), duration_ms=ms,
                          size_bytes=path.stat().st_size, codec="mp3", bitrate=320000))
    session.flush()
    return t


@pytest.fixture
def library(tmp_path, session):
    music = tmp_path / "Music"
    session.add(WatchedFolder(path=str(music)))
    bb = music / "Billboard" / "2000-09" / "2001"
    s = {}
    # Jaded: the Billboard copy is the identical rip of the album track; a
    # compilation has a different master of the same length
    s["bb_jaded"] = add_song(session, mp3(bb / "Aerosmith - Jaded.mp3", 1, "Jaded", pad=900),
                             "Jaded", "Aerosmith", "2001 Top Hits", 227657, album_artist="Various Artists",
                             compilation=True)
    s["album_jaded"] = add_song(session, mp3(music / "Aerosmith" / "Hits" / "2-10 Jaded.mp3", 1, "Jaded"),
                                "Jaded", "Aerosmith", "O, Yeah! Ultimate Aerosmith Hits", 227657)
    s["comp_jaded"] = add_song(session, mp3(music / "Compilations" / "Rock 2001" / "Jaded.mp3", 9),
                               "Jaded", "Aerosmith", "Rock 2001", 227000,
                               album_artist="Various Artists", compilation=True)
    # a different master 1.5 s longer - "same song"
    s["bb_boogaloo"] = add_song(session, mp3(bb / "Ringo Starr - Back Off Boogaloo.mp3", 2),
                                "Back Off Boogaloo", "Ringo Starr", "1972 Billboard Top Hits", 201900,
                                album_artist="Various Artists", compilation=True)
    s["album_boogaloo"] = add_song(session, mp3(music / "Ringo" / "Goodnight Vienna" / "b.mp3", 3),
                                   "Back Off Boogaloo", "Ringo Starr", "Goodnight Vienna", 203400)
    # 4 s apart - only with the wider setting
    s["bb_night"] = add_song(session, mp3(bb / "Them - Here Comes The Night.mp3", 4),
                             "Here Comes The Night", "Them", "1965 Billboard Top Hits", 164900,
                             album_artist="Various Artists", compilation=True)
    s["album_night"] = add_song(session, mp3(music / "Them" / "Best" / "n.mp3", 5),
                                "Here Comes the Night", "Them", "The Best of Them", 168900)
    # no album copy at all
    s["bb_only"] = add_song(session, mp3(music / "Oldies" / "1930 - 1939" / "o.mp3", 6),
                            "Cement Mixer", "Alvino Rey (Rocky Coluccio, vocal)", "1946 Billboard Top Hits",
                            150000, album_artist="Various Artists", compilation=True)
    pl = pl_svc.create_playlist(session, "2001")
    pl_svc.add_tracks(session, pl.id, [s[k].id for k in ("bb_only", "bb_jaded", "bb_boogaloo", "bb_night")])
    session.commit()
    s["playlist"] = pl
    return s


def test_playlist_plan(session, library):
    report = dup.find_duplicates(session)
    by_old = {sw.old.track_id: sw for sw in report.switches_within()}
    jaded = by_old[library["bb_jaded"].id]
    assert jaded.new.track_id == library["album_jaded"].id and jaded.how == "identical"
    ringo = by_old[library["bb_boogaloo"].id]
    assert ringo.new.track_id == library["album_boogaloo"].id and ringo.how == "same song"
    assert library["bb_night"].id not in by_old
    assert len(report.switches_within(5000)) == 3
    assert "2 playlist songs can move" in report.summary()


def test_switching_keeps_each_songs_place(session, library):
    report = dup.find_duplicates(session, identical_files=False)
    changed = dup.switch_playlists(session, report.switches_within())
    session.commit()
    assert changed == 2
    items = session.scalars(select(PlaylistItem).where(
        PlaylistItem.playlist_id == library["playlist"].id).order_by(PlaylistItem.position)).all()
    assert [i.track_id for i in items] == [
        library["bb_only"].id, library["album_jaded"].id,
        library["album_boogaloo"].id, library["bb_night"].id,
    ]
    # running it again finds nothing more to do at 2 s
    assert dup.find_duplicates(session, identical_files=False).switches_within() == []


def test_identical_files(session, library):
    report = dup.find_duplicates(session)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert {f.track_id for f in group.files} == {library["bb_jaded"].id, library["album_jaded"].id}
    # the artist's own album is the one to keep
    assert group.files[0].track_id == library["album_jaded"].id


def test_cancel(session, library):
    report = dup.find_duplicates(session, should_stop=lambda: True)
    assert report.cancelled


def test_dialog_switches(session, library, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from musicmgr.ui.widgets.duplicates import DuplicatesDialog

    report = dup.find_duplicates(session)
    dialog = DuplicatesDialog(report)
    assert dialog.table.rowCount() == 2
    dialog.length_combo.setCurrentIndex(dialog.length_combo.findData(5000))
    assert dialog.table.rowCount() == 3
    dialog.length_combo.setCurrentIndex(dialog.length_combo.findData(2000))
    assert dialog.tree.topLevelItemCount() == 1

    got = []
    dialog.switched.connect(got.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dialog._switch()
    assert got == [2]
    assert dialog.table.rowCount() == 0
    session.expire_all()
    ids = [i.track_id for i in session.scalars(select(PlaylistItem).order_by(PlaylistItem.position))]
    assert library["album_jaded"].id in ids


def test_settings_has_the_tool(ctx):
    from musicmgr.ui.views.settings import SettingsView

    view = SettingsView(ctx)
    assert hasattr(view, "find_duplicates")


# -- 2026-10-13: the Charts page too ("proceed with option 1") ----------------

from musicmgr.db.models import ChartEntry
from musicmgr.services import charts as chart_svc


def _chart_with(session, songs, name="Billboard Hot 100"):
    chart = chart_svc.create_chart(session, name)
    from musicmgr.db.models import ChartIssue
    import datetime as dt

    entries = []
    for week in range(3):
        issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(2001, 1, 1 + week * 7))
        session.add(issue)
        session.flush()
        for rank, (title, artist) in enumerate(songs, start=1):
            e = ChartEntry(issue_id=issue.id, rank=rank, title=title, artist_name=artist,
                           title_key=normalize(title), artist_key=normalize(artist))
            session.add(e)
            entries.append(e)
    session.flush()
    return chart, entries


def test_album_copy_for(session, library):
    assert dup.album_copy_for(session, library["bb_boogaloo"].id) == library["album_boogaloo"].id
    # the identical rip: same length - album copy
    assert dup.album_copy_for(session, library["bb_jaded"].id) == library["album_jaded"].id
    # already an album song, or no album copy within 2 s
    assert dup.album_copy_for(session, library["album_jaded"].id) is None
    assert dup.album_copy_for(session, library["bb_night"].id) is None
    assert dup.album_copy_for(session, library["bb_only"].id) is None


def test_chart_matching_prefers_the_album_copy(session, library):
    _chart, entries = _chart_with(session, [("Back Off Boogaloo", "Ringo Starr"),
                                            ("Here Comes The Night", "Them")])
    for e in entries:
        chart_svc.match_entry(session, e)
    by_title = {e.title: e.track_id for e in entries}
    assert by_title["Back Off Boogaloo"] == library["album_boogaloo"].id
    # 4 s apart: not the same recording - left on whatever matched best
    assert by_title["Here Comes The Night"] in (library["bb_night"].id, library["album_night"].id)


def test_chart_switches(session, library):
    _chart, entries = _chart_with(session, [("Back Off Boogaloo", "Ringo Starr"),
                                            ("Cement Mixer", "Alvino Rey")])
    # matched to the Billboard copies the way older matching did, one by hand
    for e in entries:
        e.track_id = library["bb_boogaloo"].id if e.title.startswith("Back") else library["bb_only"].id
    entries[0].match_locked = True
    session.commit()

    report = dup.find_duplicates(session, identical_files=False)
    (cs,) = report.chart_switches_within()
    assert cs.old.track_id == library["bb_boogaloo"].id and cs.new.track_id == library["album_boogaloo"].id
    assert cs.entries == 3 and cs.charts == ["Billboard Hot 100"]
    assert report.chart_unmatched == 3
    assert "3 chart rows (1 song) can move" in report.summary()

    assert dup.switch_charts(session, [cs]) == 3
    session.commit()
    moved = [e for e in session.scalars(select(ChartEntry)) if e.title.startswith("Back")]
    assert {e.track_id for e in moved} == {library["album_boogaloo"].id}
    assert entries[0].match_locked  # a hand-fixed match stays fixed


def test_dialog_charts_tab(session, library, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from musicmgr.ui.widgets.duplicates import DuplicatesDialog

    _chart, entries = _chart_with(session, [("Back Off Boogaloo", "Ringo Starr")])
    for e in entries:
        e.track_id = library["bb_boogaloo"].id
    session.commit()
    dialog = DuplicatesDialog(dup.find_duplicates(session, identical_files=False))
    assert dialog.chart_table.rowCount() == 1
    assert "3 chart rows" in dialog.chart_btn.text()
    got = []
    dialog.charts_switched.connect(got.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    dialog._switch_charts()
    assert got == [3] and dialog.chart_table.rowCount() == 0
