"""2026-10-19 - James: "when I'm listening to a radio station, it doesn't
make sense that a record spins." Now Playing shows a tabletop radio for a
station or show (ui/widgets/table_radio.py) and, for a station, "Heard on
this station" (services/heard.py) in place of Up next / Lyrics."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from musicmgr.db.models import Playlist, PlaylistItem, Track
from musicmgr.services import heard as heard_svc
from musicmgr.services import library as lib
from musicmgr.services import stations
from musicmgr.services.matching import normalize
from musicmgr.services.player import QueueItem
from musicmgr.ui.views.nowplaying import NowPlayingView
from musicmgr.ui.widgets.table_radio import TableRadio, badge_text


def make_track(session, title, artist, album="Album") -> Track:
    release = lib.get_or_create_release(session, album, artist)
    release.album_artist_id = lib.get_or_create_artist(session, artist).id
    track = Track(release_id=release.id, title=title, title_key=normalize(title), artist_display=artist)
    session.add(track)
    session.flush()
    return track


class TestHeard:
    def test_parse_stream_title(self):
        assert heard_svc.parse_stream_title("Bob Seger - Night Moves") == ("Bob Seger", "Night Moves")
        assert heard_svc.parse_stream_title("Night Moves by Bob Seger") == ("Bob Seger", "Night Moves")
        assert heard_svc.parse_stream_title("Traffic and weather on the 8s") == ("", "Traffic and weather on the 8s")

    def test_log_keeps_newest_first_and_skips_repeats(self):
        log = heard_svc.HeardLog()
        t0 = dt.datetime(2026, 10, 19, 23, 0)
        assert log.add(7, "Bob Seger - Night Moves", t0)
        assert not log.add(7, "Bob Seger - Night Moves", t0)
        assert not log.add(7, "  ", t0)
        assert log.add(7, "Boston - More Than a Feeling", t0 + dt.timedelta(minutes=4))
        assert [h.title for h in log.for_station(7)] == ["More Than a Feeling", "Night Moves"]
        assert log.for_station(8) == [] and log.for_station(None) == []

    def test_find_and_add_to_the_heard_playlist(self, session):
        track = make_track(session, "Night Moves", "Bob Seger")
        song = heard_svc.Heard(dt.datetime.now(), "Bob Seger - Night Moves", "Bob Seger", "Night Moves")

        assert heard_svc.find_track(session, song) == track.id
        assert heard_svc.add_to_heard_playlist(session, track.id) is True
        assert heard_svc.add_to_heard_playlist(session, track.id) is False
        playlist = session.scalar(select(Playlist).where(Playlist.name == heard_svc.PLAYLIST_NAME))
        assert [i.track_id for i in session.scalars(
            select(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id))] == [track.id]

    def test_a_bare_title_or_an_unknown_song_isnt_matched(self, session):
        make_track(session, "Night Moves", "Bob Seger")
        assert heard_svc.find_track(session, heard_svc.Heard(dt.datetime.now(), "Night Moves", "", "Night Moves")) is None
        assert heard_svc.find_track(session, heard_svc.Heard(
            dt.datetime.now(), "Nobody - Nothing", "Nobody", "Nothing")) is None


class TestTableRadio:
    def test_station_signal_and_painting(self, qapp):
        radio = TableRadio(340)
        radio.set_station("am", stations.scale_position("am", 1100), "WTAM 1100", None, animate=False)
        assert radio.band == "am"
        assert abs(radio.pointer - stations.scale_position("am", 1100)) < 1e-9
        radio.set_station("nowhere", 2.0, "X", "/no/such/logo.png", animate=False)
        assert radio.band == "fm" and radio.pointer == 1.0
        for level in ("connecting", "good", "buffering", "lost", "none"):
            radio.set_signal(level)
            radio.set_playing(level == "good")
            assert not radio.grab().isNull()

    def test_badge_text_without_a_logo(self):
        assert badge_text("WTAM 1100") == "WTAM"
        assert badge_text("KNX-FM 97.1") == "KNX"
        assert badge_text("Radio Paradise") == "RP"
        assert badge_text("1.FM - Jazz") == "J"
        assert badge_text("") == "\u266a"

    def test_wtam_sits_halfway_between_1000_and_1200(self):
        mid = (stations.scale_position("am", 1000) + stations.scale_position("am", 1200)) / 2
        assert abs(stations.scale_position("am", 1100) - mid) < 1e-9


def stream_item(station_id=0, name="WTAM 1100") -> QueueItem:
    return QueueItem(track_id=0, title=name, artist="Internet radio", album="US · AAC",
                     path="http://example.invalid/wtam", kind="stream", station_id=station_id,
                     hydrated=True)


class TestNowPlayingRadio:
    def test_a_station_shows_the_radio_and_heard_list(self, ctx):
        with ctx.session() as s:
            st = stations.add_manual(s, "WTAM 1100", "http://example.invalid/wtam", band="am")
            sid = st.id
        view = NowPlayingView(ctx)
        view.set_turntable_shown(True)

        ctx.player.trackChanged.emit(stream_item(sid))

        assert view.art_stack.currentWidget() is view.table_radio
        assert view.table_radio.band == "am"
        assert view.heard_chip.isVisibleTo(view) and view.heard_chip.isChecked()
        assert not view.queue_chip.isVisibleTo(view) and not view.lyrics_chip.isVisibleTo(view)
        assert view.right_panes.currentIndex() == 2

        ctx.player.streamTitleChanged.emit("Bob Seger - Night Moves")
        ctx.player.streamTitleChanged.emit("Bob Seger - Night Moves")
        ctx.player.streamTitleChanged.emit("Boston - More Than a Feeling")

        assert view.heard_list.count() == 2
        assert view.chart_note.text() == "♪ Boston - More Than a Feeling"

    def test_flat_logo_when_the_turntable_is_off_and_back_to_music_after(self, ctx):
        view = NowPlayingView(ctx)
        view.set_turntable_shown(False)
        ctx.player.trackChanged.emit(stream_item())
        assert view.art_stack.currentWidget() is view.cover

        view.set_turntable_shown(True)
        assert view.art_stack.currentWidget() is view.table_radio

        song = QueueItem(track_id=1, title="Night Moves", artist="Bob Seger", album="Night Moves",
                         path="/x.mp3", hydrated=True)
        ctx.player.trackChanged.emit(song)
        assert view.art_stack.currentWidget() is view.turntable
        assert view.queue_chip.isVisibleTo(view) and view.queue_chip.isChecked()
        assert not view.heard_chip.isVisibleTo(view)
        assert view.right_panes.currentIndex() == 0

    def test_add_to_playlist(self, ctx, monkeypatch):
        with ctx.session() as s:
            make_track(s, "Night Moves", "Bob Seger")
        view = NowPlayingView(ctx)
        ctx.player.trackChanged.emit(stream_item(5))
        ctx.player.streamTitleChanged.emit("Bob Seger - Night Moves")
        notes = []
        ctx.notified.connect(notes.append)
        view.heard_list.setCurrentRow(0)

        view._add_heard_to_playlist()

        assert notes == [f"Added “Night Moves” to {heard_svc.PLAYLIST_NAME}"]
