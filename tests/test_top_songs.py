"""Tests for an artist's top songs (2026-10-13 - James: "I would like the
ability to change most popular songs. Some are repeated and I want 5
different songs, even if that song has a variation"): services/top_songs.py
and the artist page's list and "Choose…" dialog."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from musicmgr.db import session as db_session
from musicmgr.db.models import Artist, ArtistTopPick, ArtistTopTrack
from musicmgr.services import library as lib
from musicmgr.services import top_songs as ts


class TestOneRowPerSong:
    @pytest.mark.parametrize("title, plain", [
        ("Hells Bells - Remastered 2003", "Hells Bells"),
        ("Tom Sawyer (Live in Cleveland)", "Tom Sawyer"),
        ("Back In Black (2003 Remaster)", "Back In Black"),
        ("Hold On Loosely - Single Version", "Hold On Loosely"),
        ("Shoot to Thrill [Live at River Plate]", "Shoot to Thrill"),
        ("Rockin' into the Night (feat. Someone)", "Rockin' into the Night"),
        ("Live Wire", "Live Wire"),
        ("Live and Let Die", "Live and Let Die"),
        ("Rock and Roll Ain't Noise Pollution", "Rock and Roll Ain't Noise Pollution"),
    ])
    def test_clean_title(self, title, plain):
        assert ts.clean_title(title) == plain

    def test_versions_share_a_key(self):
        assert ts.song_key("The Spirit of Radio") == ts.song_key("Spirit of Radio - 2015 Remaster")
        assert ts.song_key("Live Wire") != ts.song_key("Wire")

    def test_a_chart_folds_to_distinct_songs(self):
        chart = [SimpleNamespace(title=t, playcount=p) for t, p in [
            ("Tom Sawyer", 900), ("Tom Sawyer - Live", 400), ("Limelight", 800),
            ("Tom Sawyer (2011 Remaster)", 300), ("Limelight - Remastered", 100),
            ("Closer to the Heart", 700), ("Fly by Night", None),
        ]]
        songs = ts.distinct_chart(chart)
        assert [(s.title, s.playcount) for s in songs] == [
            ("Tom Sawyer", 1600), ("Limelight", 900), ("Closer to the Heart", 700),
            ("Fly by Night", None)]


def make_artist(session, name="Rush"):
    artist = lib.get_or_create_artist(session, name)
    session.flush()
    return artist


class TestPicks:
    def test_saved_in_order_without_repeats_or_blanks(self, session):
        artist = make_artist(session)
        kept = ts.set_picks(session, artist.id, ["Limelight", "", "Tom Sawyer",
                                                 "Tom Sawyer (Live)", "YYZ"])
        assert kept == ["Limelight", "Tom Sawyer", "YYZ"]
        assert ts.get_picks(session, artist.id) == ["Limelight", "Tom Sawyer", "YYZ"]

    def test_clearing_goes_back_to_lastfm(self, session):
        artist = make_artist(session)
        ts.set_picks(session, artist.id, ["YYZ"])
        ts.clear_picks(session, artist.id)
        assert ts.get_picks(session, artist.id) == []
        assert session.scalar(select(ArtistTopPick)) is None


# -- the artist page -------------------------------------------------------


@pytest.fixture
def panel(ctx):
    from musicmgr.ui.widgets.artist_panel import ArtistDetailPanel
    return ArtistDetailPanel(ctx)


def with_chart(name, rows):
    with db_session.session_scope() as s:
        artist = lib.get_or_create_artist(s, name)
        s.flush()
        for i, (title, plays) in enumerate(rows, start=1):
            s.add(ArtistTopTrack(artist_id=artist.id, rank=i, title=title, playcount=plays))
        return artist.id


RUSH = [("Tom Sawyer", 900), ("Tom Sawyer - Live", 400), ("Limelight", 800),
        ("Tom Sawyer (2011 Remaster)", 300), ("Limelight - Remastered", 100),
        ("Closer to the Heart", 700), ("Fly by Night", 600), ("YYZ", 500),
        ("Red Barchetta", 450)]


def shown(panel):
    return [panel.top_tracks.payload_at(i)["primary"] for i in range(panel.top_tracks.count())]


class TestArtistPage:
    def test_different_songs_up_to_eight(self, panel):
        # one row per song, versions folded together; up to eight rows
        # (2026-10-22, was five) - Rush's chart here has six songs
        panel.set_artist(with_chart("Rush", RUSH))
        assert shown(panel) == ["Tom Sawyer", "Limelight", "Closer to the Heart",
                                "Fly by Night", "YYZ", "Red Barchetta"]
        assert panel.top_label.text() == "Popular on Last.fm"
        assert panel.top_tracks.payload_at(0)["trail"] == "2K plays"

    def test_picks_replace_the_chart(self, panel, monkeypatch):
        aid = with_chart("Rush", RUSH)
        panel.set_artist(aid)

        class Picker:
            def __init__(self, name, current, chart, owned, has_picks=False, parent=None):
                Picker.got = (name, current, [s.title for s in chart], has_picks)
            def exec(self):
                return 1
            def titles(self):
                return ["Red Barchetta", "YYZ", "Limelight (Live)", "Subdivisions", "Limelight"]
        monkeypatch.setattr(panel, "top_songs_dialog_class", Picker)
        panel.choose_top_songs()

        assert Picker.got[0] == "Rush" and Picker.got[1][0] == "Tom Sawyer"
        assert shown(panel) == ["Red Barchetta", "YYZ", "Limelight (Live)", "Subdivisions"]
        assert panel.top_label.text() == "Top Songs · your picks"
        assert panel.top_tracks.payload_at(2)["trail"] == "900 plays"  # Limelight's, all versions
        assert panel.top_tracks.payload_at(3)["secondary"] == "Not in your library"
        # picks stay put across a page reload
        panel.set_artist(aid)
        assert shown(panel)[0] == "Red Barchetta"

    def test_dialog(self, qapp):
        from musicmgr.ui.widgets.artist_panel import TopSongsDialog
        chart = ts.distinct_chart([SimpleNamespace(title=t, playcount=p) for t, p in RUSH])
        d = TopSongsDialog("Rush", ["Tom Sawyer", "Limelight"], chart, ["Subdivisions", "Tom Sawyer"])
        items = [d.boxes[0].itemData(i) for i in range(d.boxes[0].count())]
        assert items[:3] == ["", "Tom Sawyer", "Limelight"] and "Subdivisions" in items
        assert items.count("Tom Sawyer") == 1
        assert d.titles() == ["Tom Sawyer", "Limelight"]
        d.boxes[2].setCurrentIndex(d.boxes[2].findData("YYZ"))
        d.boxes[2].activated.emit(d.boxes[2].currentIndex())
        d.boxes[3].setEditText("Spirit of Radio")
        assert d.titles() == ["Tom Sawyer", "Limelight", "YYZ", "Spirit of Radio"]
        assert not d.lastfm_btn.isEnabled()
        d2 = TopSongsDialog("Rush", ["YYZ"], chart, [], has_picks=True)
        d2.lastfm_btn.click()
        assert d2.titles() == []
