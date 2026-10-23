"""Video years and thumbnails (services/video_details.py, 2026-10-22)."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr import config
from musicmgr.db.models import Chart, ChartEntry, ChartIssue, Credit, Release, Track, Video
from musicmgr.services import library as lib
from musicmgr.services import video_details as vd
from musicmgr.services import video_scanner
from musicmgr.services.matching import normalize


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


def video(session, path: Path, title: str, artist: str, **fields) -> Video:
    a = lib.get_or_create_artist(session, artist)
    v = Video(path=str(path), title=title, title_key=normalize(title), artist_display=a.name,
              artist_id=a.id, **fields)
    session.add(v)
    session.flush()
    return v


def chart_entry(session, year: int, title: str, artist: str) -> None:
    chart = session.scalar(select(Chart)) or Chart(name="Hot 100", slug="hot-100")
    session.add(chart)
    session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(year, 12, 31))
    session.add(issue)
    session.flush()
    session.add(ChartEntry(issue_id=issue.id, rank=1, title=title, artist_name=artist,
                           title_key=normalize(title), artist_key=normalize(artist)))
    session.flush()


class TestCleanTitleAndTagYear:
    @pytest.mark.parametrize("title, clean", [
        ("Back In Black (Official Video)", "Back In Black"),
        ("Hot For Teacher (Official Music Video)", "Hot For Teacher"),
        ("Real and True ft. Mr Hudson", "Real and True"),
        ("Jireh | Live", "Jireh"),
        ("Take It on the Run (Video Version)", "Take It on the Run"),
        ("Tom Sawyer (Time Machine Tour 2011 Live In Cleveland)",
         "Tom Sawyer (Time Machine Tour 2011 Live In Cleveland)"),
    ])
    def test_clean_title(self, title, clean):
        assert vd.clean_title(title) == clean

    @pytest.mark.parametrize("value, year", [
        ("1980", 1980), (["2011"], 2011), ("1983-11-11T08:00:00Z", 1983),
        ("20091027", None),   # yt-dlp's upload date, not the song's year
        ("", None), (None, None), ("abc", None),
    ])
    def test_year_from_tag(self, value, year):
        assert vd.year_from_tag(value) == year


class TestYear:
    def test_chart_year_is_the_first_chart_with_this_artist(self, session):
        chart_entry(session, 1981, "One Of Us", "ABBA")
        chart_entry(session, 1982, "One Of Us", "ABBA")
        chart_entry(session, 1979, "One Of Us", "Somebody Else")
        assert vd.chart_year(session, "ABBA", "One Of Us (Official Video)") == 1981

    def test_chart_year_matches_a_featuring_credit(self, session):
        chart_entry(session, 2004, "Yeah!", "Usher Featuring Lil Jon & Ludacris")
        assert vd.chart_year(session, "Usher", "Yeah!") == 2004

    def test_library_year_skips_compilations(self, session):
        artist = lib.get_or_create_artist(session, "Dire Straits")
        for title, year, comp in (("Brothers in Arms", 1985, False), ("Now 1", 1983, True)):
            r = Release(title=title, title_key=title.lower(), year=year, is_compilation=comp,
                        album_artist_id=artist.id, artist_display="Dire Straits")
            session.add(r)
            session.flush()
            t = Track(release_id=r.id, title="Money for Nothing", title_key=normalize("Money for Nothing"))
            session.add(t)
            session.flush()
            lib.add_credit(session, artist, Credit.ROLE_MAIN, track=t)
        session.flush()
        assert vd.library_year(session, artist.id, "Money For Nothing") == 1985


class TestFill:
    def test_tag_year_then_chart_then_none(self, session, tmp_path):
        chart_entry(session, 1976, "Fernando", "ABBA")
        a = video(session, tmp_path / "ABBA - Fernando.mp4", "Fernando", "ABBA")
        b = video(session, tmp_path / "X - Unknown.mp4", "Unknown", "X")
        session.commit()
        years, _ = vd.fill_video_details(session)
        assert (a.year, b.year, years) == (1976, None, 1)

    def test_sidecar_then_embedded_cover_then_grab(self, session, tmp_path, monkeypatch):
        side = tmp_path / "A - Side.mp4"
        cover = tmp_path / "A - Cover.mp4"
        grab = tmp_path / "A - Grab.mp4"
        for p in (side, cover, grab):
            p.write_bytes(b"x")
        (tmp_path / "A - Side.jpg").write_bytes(b"jpg")
        details = {str(cover): (1000, None, b"COVER", ".jpg")}
        monkeypatch.setattr(vd, "read_file_details",
                            lambda p: details.get(str(p), (60_000, None, None, ".jpg")))
        grabbed = []

        def grabber(path, out, duration):
            grabbed.append((path.name, duration))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"frame")
            return True

        vs = [video(session, p, p.stem, "A", year=2000) for p in (side, cover, grab)]
        session.commit()
        _, thumbs = vd.fill_video_details(session, grab=grabber)
        assert thumbs == 3
        assert vs[0].thumbnail_path == str(tmp_path / "A - Side.jpg")
        assert Path(vs[1].thumbnail_path).read_bytes() == b"COVER"
        assert Path(vs[2].thumbnail_path).read_bytes() == b"frame"
        assert Path(vs[1].thumbnail_path).parent == config.DATA_DIR / "video_thumbs"
        assert grabbed == [("A - Grab.mp4", 60_000)]
        # tried once: a second run does nothing
        assert vd.pending_count(session) == 0

    def test_nothing_found_is_marked_tried(self, session, tmp_path, monkeypatch):
        p = tmp_path / "A - None.mp4"
        p.write_bytes(b"x")
        monkeypatch.setattr(vd, "read_file_details", lambda p: (None, None, None, ".jpg"))
        v = video(session, p, "None", "A", year=1999)
        session.commit()
        vd.fill_video_details(session, grab=lambda *a: False)
        assert v.thumbnail_path == vd.NO_THUMB

    def test_stuck_grabber_stops_grabbing_and_leaves_the_rest_untried(self, session, tmp_path, monkeypatch):
        paths = [tmp_path / f"A - {i}.mp4" for i in range(3)]
        for p in paths:
            p.write_bytes(b"x")
        monkeypatch.setattr(vd, "read_file_details", lambda p: (None, None, None, ".jpg"))
        calls = []
        vs = [video(session, p, p.stem, "A", year=1999) for p in paths]
        session.commit()
        vd.fill_video_details(session, grab=lambda path, out, d: calls.append(path) or None)
        assert len(calls) == 1
        assert [v.thumbnail_path for v in vs] == [None, None, None]

    def test_changed_file_is_tried_again(self, session, tmp_path):
        p = tmp_path / "A - Song.mp4"
        p.write_bytes(b"x")
        result = video_scanner.ScanResult()
        v = video_scanner.import_video_file(session, p, result)
        v.thumbnail_path = vd.NO_THUMB
        session.flush()
        p.write_bytes(b"xy")
        video_scanner.import_video_file(session, p, result)
        assert v.thumbnail_path is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg to make a test video")
def test_real_mp4_cover_and_ffmpeg_grab(session, tmp_path):
    src = tmp_path / "A - Real.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=320x180:rate=10", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    v = video(session, src, "Real", "A", year=2001)
    session.commit()
    vd.fill_video_details(session, grab=vd.ffmpeg_grabber())
    assert v.thumbnail_path and Path(v.thumbnail_path).stat().st_size > 0


def test_no_write_lock_is_held_while_a_frame_is_grabbed(qapp, tmp_path, monkeypatch):
    """2026-10-22: the first run held its write transaction through up to 25
    frame grabs, and a chart match James saved meanwhile never took."""
    import sqlite3

    from musicmgr.db import session as db_session

    db_file = tmp_path / "library.db"
    db_session.init_engine(db_path=str(db_file))
    monkeypatch.setattr(vd, "read_file_details", lambda p: (None, None, None, ".jpg"))
    chart_entry_paths = [tmp_path / f"A - {i}.mp4" for i in range(3)]
    with db_session.session_scope() as s:
        for p in chart_entry_paths:
            p.write_bytes(b"x")
            video(s, p, p.stem, "A")   # no year: find_year runs first
    writes = []

    def grabber(path, out, duration):
        other = sqlite3.connect(str(db_file), timeout=0)
        other.execute("CREATE TABLE IF NOT EXISTS probe (x)")
        other.execute("INSERT INTO probe VALUES (1)")   # raises if locked
        other.commit()
        other.close()
        writes.append(path.name)
        return False

    with db_session.session_scope() as s:
        vd.fill_video_details(s, grab=grabber)
    assert len(writes) == 3


class TestGenericCovers:
    """2026-10-23: 300 of James's videos carry the same "MUSIC VIDEOS"
    placeholder as their embedded cover - a frame is grabbed instead."""

    def _grabber(self, grabbed):
        def grab(path, out, duration):
            grabbed.append(path.name)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"frame of " + path.name.encode())
            return True
        return grab

    def _videos(self, session, tmp_path, monkeypatch, covers):
        paths = []
        for name in covers:
            p = tmp_path / f"A - {name}.mp4"
            p.write_bytes(b"x")
            paths.append(p)
        monkeypatch.setattr(vd, "read_file_details",
                            lambda p: (60_000, None, covers[p.stem[4:]], ".jpg"))
        vs = [video(session, p, p.stem, "A", year=2000) for p in paths]
        session.commit()
        return vs

    def test_a_cover_shared_by_three_videos_is_replaced_by_frames(self, session, tmp_path, monkeypatch):
        covers = {"One": b"GENERIC", "Two": b"GENERIC", "Three": b"GENERIC", "Own": b"OWN COVER"}
        vs = self._videos(session, tmp_path, monkeypatch, covers)
        grabbed = []

        _, thumbs = vd.fill_video_details(session, grab=self._grabber(grabbed))

        assert sorted(grabbed) == ["A - One.mp4", "A - Three.mp4", "A - Two.mp4"]
        assert [Path(v.thumbnail_path).read_bytes()[:8] for v in vs[:3]] == [b"frame of"] * 3
        assert Path(vs[3].thumbnail_path).read_bytes() == b"OWN COVER"
        assert thumbs == 4
        assert vd.cover_hash(b"GENERIC") in vd.generic_covers(session)
        assert vd.pending_count(session) == 0

    def test_two_videos_sharing_a_cover_keep_it(self, session, tmp_path, monkeypatch):
        vs = self._videos(session, tmp_path, monkeypatch, {"One": b"PAIR", "Two": b"PAIR"})
        grabbed = []

        vd.fill_video_details(session, grab=self._grabber(grabbed))

        assert grabbed == []
        assert all(Path(v.thumbnail_path).read_bytes() == b"PAIR" for v in vs)

    def test_a_known_generic_cover_is_skipped_on_later_videos(self, session, tmp_path, monkeypatch):
        from musicmgr.services import updater
        import json

        updater.set_pref(session, vd.GENERIC_COVERS_PREF, json.dumps([vd.cover_hash(b"GENERIC")]))
        vs = self._videos(session, tmp_path, monkeypatch, {"New": b"GENERIC"})
        grabbed = []

        vd.fill_video_details(session, grab=self._grabber(grabbed))

        assert grabbed == ["A - New.mp4"]
        assert Path(vs[0].thumbnail_path).read_bytes().startswith(b"frame of")

    def test_existing_library_reset_by_the_one_time_pass(self, session, tmp_path):
        # what an older run left: the same cover saved for three videos
        vs = []
        for i in range(3):
            p = tmp_path / f"A - {i}.mp4"
            p.write_bytes(b"x")
            thumb = vd.save_cover(p, b"GENERIC")
            vs.append(video(session, p, str(i), "A", year=2000, thumbnail_path=thumb))
        side = tmp_path / "A - side.jpg"
        side.write_bytes(b"GENERIC")  # a picture beside a video is the owner's choice
        own = video(session, tmp_path / "A - side.mp4", "side", "A", year=2000,
                    thumbnail_path=str(side))
        session.commit()

        assert vd.find_generic_covers(session) == 3

        assert [v.thumbnail_path for v in vs] == [None, None, None]
        assert own.thumbnail_path == str(side)
        assert not any(vd.thumb_dir().glob("*.jpg"))
        assert vd.pending_count(session) == 3
