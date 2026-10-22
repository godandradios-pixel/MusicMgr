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
