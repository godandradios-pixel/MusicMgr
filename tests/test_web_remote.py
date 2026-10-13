"""Tests for services/web_remote.py - the phone remote (2026-10-13).

`RemoteApi` is exercised directly (it's what runs on the main thread), then
one round trip goes through the real HTTP server to prove the main-thread
hand-off, the command header and the page itself all work together.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

from musicmgr.db.models import MediaFile, Playlist, Track
from musicmgr.services import library as lib
from musicmgr.services import playlists as pl_svc
from musicmgr.services import web_remote
from musicmgr.services.matching import normalize


def make_wav(path: Path, seconds: float = 1.0) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return str(path)


def make_track(session, tmp_path, title, *, artist="Artist", album="Album", n=1) -> Track:
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist)
    release.album_artist_id = artist_obj.id
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display=artist, track_no=n, duration_ms=1000)
    session.add(track)
    session.flush()
    path = make_wav(tmp_path / f"{album}-{n}-{title}.wav")
    session.add(MediaFile(track_id=track.id, path=path))
    session.flush()
    return track


@pytest.fixture
def api(ctx):
    return web_remote.RemoteApi(ctx, lambda: "Den PC")


@pytest.fixture
def album(session, tmp_path):
    tracks = [make_track(session, tmp_path, t, album="Hysteria", artist="Def Leppard", n=i + 1)
              for i, t in enumerate(["Women", "Rocket", "Animal"])]
    session.commit()
    return tracks


class TestPrefs:
    def test_defaults_when_nothing_saved(self, session):
        prefs = web_remote.load_prefs(session)
        assert (prefs.enabled, prefs.port, prefs.name) == (False, web_remote.DEFAULT_PORT, "")

    def test_round_trip(self, session):
        web_remote.save_prefs(session, web_remote.RemotePrefs(True, 9000, " Cabin "))
        prefs = web_remote.load_prefs(session)
        assert (prefs.enabled, prefs.port, prefs.name) == (True, 9000, "Cabin")

    def test_bad_port_falls_back(self, session):
        web_remote.save_prefs(session, web_remote.RemotePrefs(True, 80, ""))
        assert web_remote.load_prefs(session).port == web_remote.DEFAULT_PORT


class TestClientCheck:
    @pytest.mark.parametrize("addr", ["192.168.1.40", "10.0.0.7", "172.16.4.2", "127.0.0.1",
                                      "169.254.3.3", "::1", "fe80::1%en0", "::ffff:192.168.1.9"])
    def test_local_network_allowed(self, addr):
        assert web_remote._allowed_client(addr)

    @pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "::ffff:8.8.8.8", "nonsense"])
    def test_internet_refused(self, addr):
        assert not web_remote._allowed_client(addr)


class TestApi:
    def test_state_when_idle(self, api):
        s = api.state()
        assert s["name"] == "Den PC"
        assert s["state"] == "stopped"
        assert s["item"] is None

    def test_search_finds_albums_and_songs(self, api, album):
        r = api.search("lep")
        assert [a["title"] for a in r["albums"]] == ["Hysteria"]
        assert {t["title"] for t in r["tracks"]} == {"Women", "Rocket", "Animal"}
        assert api.search("a") == {"albums": [], "tracks": []}

    def test_release_lists_tracks_in_order(self, api, album):
        r = api.release(album[0].release_id)
        assert [t["title"] for t in r["tracks"]] == ["Women", "Rocket", "Animal"]

    def test_play_track_starts_its_album_at_that_song(self, api, ctx, album):
        out = api.command({"cmd": "play_track", "id": album[2].id})
        assert out["ok"]
        assert [i.title for i in ctx.player.queue] == ["Women", "Rocket", "Animal"]
        assert ctx.player.current.title == "Animal"
        assert out["state"]["item"]["title"] == "Animal"

    def test_playlist_browse_and_play(self, api, ctx, session, album):
        folder = pl_svc.create_folder(session, "Rock")
        pl = pl_svc.create_playlist(session, "Leppard", folder_id=folder.id)
        pl_svc.add_tracks(session, pl.id, [album[1].id, album[0].id])
        session.commit()

        lists = api.playlists()
        assert lists["folders"][0]["name"] == "Rock"
        assert lists["playlists"][0] | {"cover": None} == {
            "id": pl.id, "name": "Leppard", "folder_id": folder.id,
            "kind": Playlist.KIND_MANUAL, "cover": None}
        assert [t["title"] for t in api.playlist(pl.id)["tracks"]] == ["Rocket", "Women"]

        api.command({"cmd": "play_playlist", "id": pl.id, "start": 1})
        assert ctx.player.current.title == "Women"
        assert ctx.player.source == f"playlist:{pl.id}"

    def test_queue_jump_remove_and_enqueue(self, api, ctx, album):
        api.command({"cmd": "play_release", "id": album[0].release_id})
        rev = api.queue_rev
        api.command({"cmd": "jump", "index": 2})
        assert ctx.player.current.title == "Animal"
        api.command({"cmd": "remove", "index": 0})
        assert [i["title"] for i in api.queue()["items"]] == ["Rocket", "Animal"]
        api.command({"cmd": "enqueue_track", "id": album[0].id})
        assert [i["title"] for i in api.queue()["items"]][-1] == "Women"
        assert api.queue_rev > rev

    def test_volume_is_clamped(self, api, ctx):
        api.command({"cmd": "volume", "value": 3})
        assert ctx.player.volume == 1.0
        api.command({"cmd": "volume", "value": 0.25})
        assert api.state()["volume"] == 0.25

    def test_unknown_command(self, api):
        with pytest.raises(ValueError):
            api.command({"cmd": "format_c"})

    def test_missing_album(self, api):
        with pytest.raises(LookupError):
            api.release(999)


def _pump_until(qapp, done: threading.Event, timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while not done.is_set() and time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def _request(qapp, req):
    """Run an HTTP request on a worker thread while the Qt main thread keeps
    processing events (that's where the server hands its work)."""
    out: dict = {}
    done = threading.Event()

    def work():
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                out["status"], out["body"] = r.status, r.read()
        except urllib.error.HTTPError as e:
            out["status"], out["body"] = e.code, e.read()
        finally:
            done.set()

    threading.Thread(target=work, daemon=True).start()
    _pump_until(qapp, done)
    assert done.is_set(), "request never finished"
    return out["status"], out["body"]


class TestServer:
    def test_round_trip(self, qapp, ctx, album):
        server = ctx.web_remote
        ok, msg = server.start(_free_port(), "Kitchen")
        assert ok, msg
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, body = _request(qapp, base + "/")
            assert status == 200 and b"MusicMgr" in body

            status, body = _request(qapp, base + "/api/state")
            assert status == 200
            assert json.loads(body)["name"] == "Kitchen"

            payload = json.dumps({"cmd": "play_release", "id": album[0].release_id}).encode()
            # without the header a command is refused
            status, _ = _request(qapp, urllib.request.Request(
                base + "/api/cmd", data=payload, method="POST",
                headers={"Content-Type": "application/json"}))
            assert status == 403
            assert ctx.player.current is None

            status, body = _request(qapp, urllib.request.Request(
                base + "/api/cmd", data=payload, method="POST",
                headers={"Content-Type": "application/json", "X-MusicMgr": "1"}))
            assert status == 200
            assert json.loads(body)["state"]["item"]["title"] == "Women"

            status, _ = _request(qapp, base + "/api/release?id=999")
            assert status == 404
        finally:
            server.stop()
        assert not server.running

    def test_port_in_use_reports_instead_of_crashing(self, ctx, qapp):
        import socket

        blocker = socket.socket()
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        try:
            ok, msg = ctx.web_remote.start(blocker.getsockname()[1])
            assert not ok and "port" in msg
            assert not ctx.web_remote.running
        finally:
            blocker.close()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_qr_matrix_is_square():
    m = web_remote.qr_matrix("http://192.168.1.20:8765/")
    if m is None:
        pytest.skip("qrcode not installed")
    assert len(m) == len(m[0]) > 20


def test_settings_section_turns_the_remote_on_and_off(ctx, qapp, monkeypatch):
    from musicmgr.ui.views.settings import SettingsView

    started = []
    monkeypatch.setattr(ctx.web_remote, "start",
                        lambda port, name="": started.append((port, name)) or (True, "Running"))
    view = SettingsView(ctx)
    assert not view.remote_cb.isChecked()
    view.remote_name_edit.setText("Basement")
    view.remote_cb.setChecked(True)
    assert started[-1] == (web_remote.DEFAULT_PORT, "Basement")
    with ctx.session() as db:
        assert web_remote.load_prefs(db).enabled
    assert view.remote_state.text() == "Phone remote is ON"
    assert view.remote_cb.text() == "Turn phone remote off"
    view.remote_cb.setChecked(False)
    with ctx.session() as db:
        assert not web_remote.load_prefs(db).enabled
    assert view.remote_state.text() == "Phone remote is OFF"
    assert view.remote_cb.text() == "Turn phone remote on"
    assert view.remote_cb.objectName() == "PrimaryWarm"
