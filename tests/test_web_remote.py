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
from PySide6.QtCore import Qt

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


# -- networks: which address to offer, and Windows' Public networks (2026-10-15)

class TestAddresses:
    def test_virtual_adapter_dot_one_is_left_out(self, monkeypatch):
        monkeypatch.setattr(web_remote, "_route_address", lambda: "192.168.4.24")
        monkeypatch.setattr(web_remote, "_hostname_addresses",
                            lambda: ["172.23.192.1", "192.168.4.24", "127.0.0.1", "10.0.0.50"])
        assert web_remote.local_addresses() == ["192.168.4.24", "10.0.0.50"]

    def test_the_route_address_is_kept_even_ending_in_dot_one(self, monkeypatch):
        monkeypatch.setattr(web_remote, "_route_address", lambda: "192.168.0.1")
        monkeypatch.setattr(web_remote, "_hostname_addresses", lambda: [])
        assert web_remote.local_addresses() == ["192.168.0.1"]

    def test_parse_windows_check(self):
        one = ('{"networks":{"name":"912Latonka","alias":"Ethernet 2","category":"Private",'
               '"ips":"192.168.4.24"},"ports":[],"blocked":0}')
        check = web_remote.parse_check(one)
        assert [(n.name, n.alias, n.public, n.ips) for n in check.networks] == \
            [("912Latonka", "Ethernet 2", False, ["192.168.4.24"])]
        assert (check.allowed_ports, check.blocked) == ([], 0)
        two = ('{"networks":[{"name":"Home","alias":"Wi-Fi","category":"Public","ips":["192.168.1.9","169.254.1.2"]},'
               '{"name":"Work","alias":"Ethernet","category":"DomainAuthenticated","ips":[]}],'
               '"ports":"8765","blocked":2}')
        check = web_remote.parse_check(two)
        assert [n.public for n in check.networks] == [True, False]
        assert check.networks[0].ips == ["192.168.1.9"]  # link-local dropped
        assert (check.allowed_ports, check.blocked) == (["8765"], 2)
        assert web_remote.parse_check("") is None
        assert web_remote.parse_check("not json") is None

    def test_james_pc_private_but_no_firewall_rule(self, monkeypatch):
        """2026-10-15: network already Private, but no firewall rule at all."""
        monkeypatch.setattr(web_remote, "_route_address", lambda: "192.168.4.24")
        check = web_remote.WindowsCheck(
            [web_remote.HomeNetwork("912Latonka", "Ethernet 2", "Private", ["192.168.4.24"])], [], 0)
        info = web_remote.network_info(check, 8765)
        assert info.addresses == ["192.168.4.24"]  # not the Hyper-V 172.23.192.1
        assert info.public == [] and info.firewall_closed and info.needs_fix
        assert web_remote.fix_command(info, 8765) == (
            "Remove-NetFirewallRule -DisplayName 'MusicMgr phone remote' -ErrorAction SilentlyContinue; "
            "New-NetFirewallRule -DisplayName 'MusicMgr phone remote' -Direction Inbound -Action Allow "
            "-Protocol TCP -LocalPort 8765 -Profile Private,Domain")
        # once the rule is there, nothing to fix
        check.allowed_ports = ["8765"]
        assert not web_remote.network_info(check, 8765).needs_fix
        # ...unless the port changed
        assert web_remote.network_info(check, 9000).firewall_closed

    def test_fix_command_covers_everything(self):
        info = web_remote.NetworkInfo(
            ["192.168.1.9"], [web_remote.HomeNetwork("x", "Bob's Wi-Fi", "Public", [])],
            firewall_closed=False, firewall_blocked=1)
        cmd = web_remote.fix_command(info, 8765)
        assert cmd.startswith("Set-NetConnectionProfile -InterfaceAlias 'Bob''s Wi-Fi' -NetworkCategory Private; ")
        assert "Remove-NetFirewallRule" in cmd and "[string]$_.Action -eq 'Block'" in cmd
        assert cmd.endswith("-LocalPort 8765 -Profile Private,Domain")

    def test_no_windows_check_means_no_warnings(self, monkeypatch):
        monkeypatch.setattr(web_remote.sys, "platform", "linux")
        assert web_remote.windows_check() is None
        assert not web_remote.network_info(None).needs_fix


def test_check_network_runs_in_the_background(ctx, qapp, monkeypatch):
    monkeypatch.setattr(web_remote, "windows_check", lambda: web_remote.WindowsCheck(
        [web_remote.HomeNetwork("912Latonka", "Ethernet 2", "Public", ["192.168.4.24"])], [], 0))
    monkeypatch.setattr(web_remote, "_route_address", lambda: "192.168.4.24")
    done = []
    ctx.web_remote.networkChecked.connect(lambda: done.append(1), Qt.QueuedConnection)
    ctx.web_remote.check_network()
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert done
    assert ctx.web_remote.urls() == [f"http://192.168.4.24:{ctx.web_remote.port}/"]
    assert ctx.web_remote.network.needs_fix


def test_settings_offers_the_fix(ctx, qapp, monkeypatch):
    from musicmgr.ui.views.settings import SettingsView

    server = ctx.web_remote
    monkeypatch.setattr(type(server), "running", property(lambda self: True))
    monkeypatch.setattr(server, "check_network", lambda: None)
    server.network = web_remote.NetworkInfo(
        ["192.168.4.24"], [web_remote.HomeNetwork("912Latonka", "Ethernet 2", "Public", ["192.168.4.24"])],
        firewall_closed=True)
    view = SettingsView(ctx)
    view._refresh_remote_status()
    text = view.remote_status.text()
    assert "192.168.4.24" in text and "912Latonka" in text and "Public" in text
    assert f"no rule letting the phone reach port {server.port}" in text
    assert not view.remote_fix_btn.isHidden()

    ran = []
    monkeypatch.setattr(web_remote, "run_elevated_powershell", lambda cmd: ran.append(cmd) or True)
    view._fix_remote_network()
    assert len(ran) == 1 and "Set-NetConnectionProfile" in ran[0] and "New-NetFirewallRule" in ran[0]
    assert not view.remote_fix_btn.isEnabled()

    server.network = web_remote.NetworkInfo(["192.168.4.24"])
    view._refresh_remote_status()
    assert "can't get in" not in view.remote_status.text()
    assert view.remote_fix_btn.isHidden()
