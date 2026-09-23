"""Tests for services/updater.py (2026-09-23 in-app updates).

No real network and no real executables: a tiny fake `requests.Session`
serves canned GitHub JSON and asset bytes, and the swap/rollback tests run
on dummy files in tmp_path. Signatures are made with a throwaway Ed25519 key
generated per test run - never the real release key.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from musicmgr.services import updater

REPO = "someone/MusicMgr"
WIN = "windows-x64.exe"


# -- fixtures / fakes --------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, json_data=None, content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def iter_content(self, size):
        for i in range(0, len(self.content), max(1, size // 64 or 1)):
            yield self.content[i:i + max(1, size // 64 or 1)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404)
        if isinstance(route, Exception):
            raise route
        return route


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    return key, pub


def sign(key, data: bytes) -> bytes:
    return base64.b64encode(key.sign(data)) + b"\n"


def release_json(version="1.6.0", names=None, prerelease=False, draft=False):
    names = names if names is not None else [
        f"MusicMgr-{version}-{WIN}", f"MusicMgr-{version}-linux-x86_64",
        updater.SUMS_NAME, updater.SIG_NAME,
    ]
    return {
        "tag_name": f"v{version}",
        "name": f"MusicMgr {version}",
        "body": "## What's new\n- things",
        "html_url": f"https://github.com/{REPO}/releases/tag/v{version}",
        "published_at": "2026-09-30T12:00:00Z",
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {"name": n, "browser_download_url": f"https://dl/{n}", "size": 10} for n in names
        ],
    }


def latest_url():
    return f"{updater.API_BASE}/repos/{REPO}/releases/latest"


def served_release(key, binary=b"NEW-EXE-BYTES" * 1000, version="1.6.0", sig_key=None, sums_override=None):
    """(http, info) for a complete, correctly signed release."""
    name = f"MusicMgr-{version}-{WIN}"
    sums = sums_override or f"{hashlib.sha256(binary).hexdigest()}  {name}\n".encode()
    rel = release_json(version)
    http = FakeHttp({
        latest_url(): FakeResponse(json_data=rel),
        f"https://dl/{name}": FakeResponse(content=binary, headers={"Content-Length": str(len(binary))}),
        f"https://dl/{updater.SUMS_NAME}": FakeResponse(content=sums),
        f"https://dl/{updater.SIG_NAME}": FakeResponse(content=sign(sig_key or key, sums)),
    })
    result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
    assert result.update is not None
    return http, result.update


# -- versions & platform -----------------------------------------------------


class TestVersions:
    def test_parse_strips_v(self):
        assert str(updater.parse_version("v1.5.0")) == "1.5.0"
        assert updater.parse_version("banana") is None

    @pytest.mark.parametrize("cand,cur,newer", [
        ("1.5.1", "1.5.0", True),
        ("v1.10.0", "1.9.9", True),
        ("1.5.0", "1.5.0", False),
        ("1.4.9", "1.5.0", False),
        ("1.6.0rc1", "1.5.0", True),
        ("1.6.0rc1", "1.6.0", False),
        ("junk", "1.5.0", False),
    ])
    def test_is_newer(self, cand, cur, newer):
        assert updater.is_newer(cand, cur) is newer

    def test_platform_tag(self):
        assert updater.platform_tag("Windows", "AMD64") == WIN
        assert updater.platform_tag("Linux", "x86_64") == "linux-x86_64"
        assert updater.platform_tag("Linux", "aarch64") is None
        assert updater.platform_tag("Darwin", "arm64") is None


# -- check -------------------------------------------------------------------


class TestCheck:
    def test_update_available(self):
        http = FakeHttp({latest_url(): FakeResponse(json_data=release_json("1.6.0"))})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
        assert not result.error
        info = result.update
        assert info.version == "1.6.0"
        assert info.binary.name == f"MusicMgr-1.6.0-{WIN}"
        assert info.notes.startswith("## What's new")
        assert info.published_at == "2026-09-30"

    def test_up_to_date(self):
        http = FakeHttp({latest_url(): FakeResponse(json_data=release_json("1.5.0"))})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
        assert result.update is None and not result.error
        assert "up to date" in result.message

    def test_no_releases_yet(self):
        result = updater.check_for_update("1.5.0", repo=REPO, http=FakeHttp({}), plat=WIN)
        assert result.update is None and not result.error

    def test_network_error_is_reported_not_raised(self):
        import requests

        http = FakeHttp({latest_url(): requests.ConnectionError("down")})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
        assert result.error and "reach GitHub" in result.message

    def test_rate_limited(self):
        http = FakeHttp({latest_url(): FakeResponse(403, headers={"X-RateLimit-Remaining": "0"})})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
        assert result.error and "limit" in result.message

    def test_unsigned_release_is_refused(self):
        rel = release_json("1.6.0", names=[f"MusicMgr-1.6.0-{WIN}", updater.SUMS_NAME])
        http = FakeHttp({latest_url(): FakeResponse(json_data=rel)})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
        assert result.update is None and result.error and "signed" in result.message

    def test_missing_platform_binary(self):
        rel = release_json("1.6.0", names=["MusicMgr-1.6.0-linux-x86_64", updater.SUMS_NAME, updater.SIG_NAME])
        http = FakeHttp({latest_url(): FakeResponse(json_data=rel)})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN)
        assert result.update is None and result.error

    def test_unsupported_platform(self):
        http = FakeHttp({latest_url(): FakeResponse(json_data=release_json("1.6.0"))})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=None)
        assert result.update is None and result.latest == "1.6.0"

    def test_prerelease_channel_uses_list_and_skips_drafts(self):
        url = f"{updater.API_BASE}/repos/{REPO}/releases?per_page=10"
        http = FakeHttp({url: FakeResponse(json_data=[
            release_json("1.7.0", draft=True),
            release_json("1.6.0rc1", prerelease=True),
            release_json("1.5.0"),
        ])})
        result = updater.check_for_update("1.5.0", repo=REPO, http=http, plat=WIN, include_prerelease=True)
        assert result.update.version == "1.6.0rc1"
        assert http.calls == [url]


# -- signature & sums ----------------------------------------------------------


class TestSignature:
    def test_valid(self, keypair):
        key, pub = keypair
        assert updater.verify_signature(b"data", sign(key, b"data").decode(), [pub])

    def test_tampered_data(self, keypair):
        key, pub = keypair
        assert not updater.verify_signature(b"data!", sign(key, b"data").decode(), [pub])

    def test_wrong_key(self, keypair):
        key, _ = keypair
        _, other_pub = (lambda k: (k, base64.b64encode(k.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()))(Ed25519PrivateKey.generate())
        assert not updater.verify_signature(b"data", sign(key, b"data").decode(), [other_pub])

    def test_any_of_several_keys(self, keypair):
        key, pub = keypair
        assert updater.verify_signature(b"d", sign(key, b"d").decode(), ["not-base64!!", "AAAA", pub])

    def test_garbage_signature(self, keypair):
        _, pub = keypair
        assert not updater.verify_signature(b"d", "%%%", [pub])

    def test_parse_sums(self):
        text = f"{'a' * 64}  one.exe\n{'B' * 64} *two\nnonsense\n"
        assert updater.parse_sums(text) == {"one.exe": "a" * 64, "two": "b" * 64}


# -- download ------------------------------------------------------------------


class TestDownload:
    def test_verified_download(self, keypair, tmp_path):
        key, pub = keypair
        http, info = served_release(key)
        dest = tmp_path / "MusicMgr.exe.download"
        seen = []
        out = updater.download_update(
            info, dest, current_version="1.5.0", public_keys=[pub], http=http,
            progress=lambda d, t: seen.append((d, t)),
        )
        assert out == dest and dest.read_bytes() == b"NEW-EXE-BYTES" * 1000
        assert seen and seen[-1][0] == seen[-1][1]

    def test_signature_checked_before_big_download(self, keypair, tmp_path):
        key, pub = keypair
        http, info = served_release(key, sig_key=Ed25519PrivateKey.generate())
        with pytest.raises(updater.UpdateError, match="signature"):
            updater.download_update(info, tmp_path / "x", current_version="1.5.0", public_keys=[pub], http=http)
        assert f"https://dl/{info.binary.name}" not in http.calls
        assert not (tmp_path / "x").exists()

    def test_checksum_mismatch_deletes_file(self, keypair, tmp_path):
        key, pub = keypair
        name = f"MusicMgr-1.6.0-{WIN}"
        http, info = served_release(key, sums_override=f"{'0' * 64}  {name}\n".encode())
        dest = tmp_path / "x"
        with pytest.raises(updater.UpdateError, match="checksum"):
            updater.download_update(info, dest, current_version="1.5.0", public_keys=[pub], http=http)
        assert not dest.exists()

    def test_binary_not_in_sums(self, keypair, tmp_path):
        key, pub = keypair
        http, info = served_release(key, sums_override=f"{'0' * 64}  something-else\n".encode())
        with pytest.raises(updater.UpdateError, match="isn't listed"):
            updater.download_update(info, tmp_path / "x", current_version="1.5.0", public_keys=[pub], http=http)

    def test_no_keys_in_build(self, keypair, tmp_path):
        key, _ = keypair
        http, info = served_release(key)
        with pytest.raises(updater.UpdateError, match="signing key"):
            updater.download_update(info, tmp_path / "x", current_version="1.5.0", public_keys=[], http=http)

    def test_cancel_removes_partial(self, keypair, tmp_path):
        key, pub = keypair
        http, info = served_release(key)
        dest = tmp_path / "x"
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        out = updater.download_update(
            info, dest, current_version="1.5.0", public_keys=[pub], http=http, should_stop=stop
        )
        assert out is None and not dest.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="posix permission bits")
    def test_marked_executable_on_linux(self, keypair, tmp_path):
        key, pub = keypair
        http, info = served_release(key)
        dest = tmp_path / "x"
        updater.download_update(info, dest, current_version="1.5.0", public_keys=[pub], http=http)
        assert dest.stat().st_mode & 0o111


# -- swap / rollback -----------------------------------------------------------


@pytest.fixture
def install(tmp_path):
    exe = tmp_path / "MusicMgr.exe"
    exe.write_bytes(b"OLD")
    dl = updater.download_path_for(exe)
    dl.write_bytes(b"NEW")
    return exe, dl


class TestSwap:
    def test_install_swaps_and_keeps_old(self, install):
        exe, dl = install
        updater.install_downloaded(dl, exe)
        assert exe.read_bytes() == b"NEW"
        assert updater.old_path_for(exe).read_bytes() == b"OLD"
        assert not dl.exists()
        assert updater.can_roll_back(exe)

    def test_install_replaces_an_older_old(self, install):
        exe, dl = install
        updater.old_path_for(exe).write_bytes(b"ANCIENT")
        updater.install_downloaded(dl, exe)
        assert updater.old_path_for(exe).read_bytes() == b"OLD"

    def test_failed_second_rename_restores_original(self, install, monkeypatch):
        exe, dl = install
        real_replace = updater.os.replace

        def flaky(src, dst):
            if str(src).endswith(updater.DOWNLOAD_SUFFIX):
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        monkeypatch.setattr(updater.os, "replace", flaky)
        with pytest.raises(updater.UpdateError, match="new version"):
            updater.install_downloaded(dl, exe)
        assert exe.read_bytes() == b"OLD"
        assert not updater.old_path_for(exe).exists()

    def test_old_still_running(self, install, monkeypatch):
        exe, dl = install
        old = updater.old_path_for(exe)
        old.write_bytes(b"RUNNING")
        real_unlink = type(old).unlink

        def locked(self, *a, **k):
            if self == old:
                raise PermissionError(13, "in use")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(type(old), "unlink", locked)
        with pytest.raises(updater.UpdateError, match="restart"):
            updater.install_downloaded(dl, exe)
        assert exe.read_bytes() == b"OLD" and dl.exists()

    def test_rollback_round_trip(self, install):
        exe, dl = install
        updater.install_downloaded(dl, exe)
        updater.rollback(exe)
        assert exe.read_bytes() == b"OLD"
        assert updater.old_path_for(exe).read_bytes() == b"NEW"

    def test_rollback_without_old(self, tmp_path):
        exe = tmp_path / "MusicMgr.exe"
        exe.write_bytes(b"X")
        assert not updater.can_roll_back(exe)
        with pytest.raises(updater.UpdateError):
            updater.rollback(exe)

    def test_cleanup_leftovers(self, install):
        exe, dl = install
        parked = exe.with_name(exe.name + ".rollback")
        parked.write_bytes(b"P")
        updater.cleanup_leftovers(exe)
        assert not dl.exists() and not parked.exists() and exe.exists()


class TestSelfUpdateBlocker:
    def test_source_checkout(self):
        assert "source" in updater.self_update_blocker(None, plat=WIN)

    def test_frozen_writable(self, tmp_path):
        assert updater.self_update_blocker(tmp_path / "MusicMgr.exe", plat=WIN) is None

    def test_unsupported_platform(self, tmp_path):
        assert updater.self_update_blocker(tmp_path / "MusicMgr", plat=None) is not None

    def test_current_executable_from_source_is_none(self):
        assert updater.current_executable() is None


# -- restart -------------------------------------------------------------------


class TestRestart:
    def test_relaunch_args_carry_display_flags(self, tmp_path):
        exe = tmp_path / "MusicMgr.exe"
        args = updater.relaunch_args(exe, 4242, ["MusicMgr.exe", "--maximized", "--after-update", "junk"])
        assert args == [str(exe), "--maximized", "--after-update", "--wait-pid", "4242"]

    def test_parse_wait_pid(self):
        assert updater.parse_wait_pid(["x", "--wait-pid", "123"]) == 123
        assert updater.parse_wait_pid(["x", "--wait-pid"]) is None
        assert updater.parse_wait_pid(["x", "--wait-pid", "abc"]) is None
        assert updater.parse_wait_pid(["x"]) is None

    def test_wait_for_dead_pid_returns_immediately(self):
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert updater.wait_for_pid_exit(proc.pid, timeout=2)

    def test_wait_times_out_on_live_pid(self):
        import os

        assert updater.wait_for_pid_exit(os.getpid(), timeout=0.3, poll=0.05) is False

    def test_relaunch_sets_reset_environment(self, tmp_path, monkeypatch):
        captured = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"], captured["kwargs"] = args, kwargs

        monkeypatch.setattr(updater.subprocess, "Popen", FakePopen)
        exe = tmp_path / "MusicMgr.exe"
        updater.relaunch(exe, ["MusicMgr.exe", "--fullscreen"], pid=7)
        assert captured["args"][-2:] == ["--wait-pid", "7"]
        assert "--fullscreen" in captured["args"]
        assert captured["kwargs"]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


# -- database backup -----------------------------------------------------------


def make_db(path, rows=3):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn  # left open, so rows may still sit in the -wal file


class TestDbBackup:
    def test_first_run_of_new_version_backs_up(self, tmp_path):
        db = tmp_path / "library.db"
        conn = make_db(db)
        marker = tmp_path / "last_run_version.txt"
        marker.write_text("1.5.0")
        made = updater.backup_database_if_version_changed(db, tmp_path / "backups", marker, "1.6.0")
        conn.close()
        assert made.name == "library-before-1.6.0.db"
        copy = sqlite3.connect(made)
        assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3  # includes WAL contents
        copy.close()
        assert marker.read_text() == "1.6.0"

    def test_same_version_does_nothing(self, tmp_path):
        db = tmp_path / "library.db"
        make_db(db).close()
        marker = tmp_path / "m"
        marker.write_text("1.6.0")
        assert updater.backup_database_if_version_changed(db, tmp_path / "b", marker, "1.6.0") is None
        assert not (tmp_path / "b").exists()

    def test_no_marker_yet_still_backs_up_existing_db(self, tmp_path):
        db = tmp_path / "library.db"
        make_db(db).close()
        made = updater.backup_database_if_version_changed(db, tmp_path / "b", tmp_path / "m", "1.5.0")
        assert made is not None and made.exists()

    def test_fresh_install_without_db(self, tmp_path):
        marker = tmp_path / "m"
        assert updater.backup_database_if_version_changed(
            tmp_path / "library.db", tmp_path / "b", marker, "1.5.0") is None
        assert marker.read_text() == "1.5.0"

    def test_keeps_only_newest(self, tmp_path):
        import os
        import time

        db = tmp_path / "library.db"
        make_db(db).close()
        marker = tmp_path / "m"
        backups = tmp_path / "b"
        for i, ver in enumerate(["1.1.0", "1.2.0", "1.3.0", "1.4.0"]):
            made = updater.backup_database_if_version_changed(db, backups, marker, ver, keep=3)
            os.utime(made, (time.time() + i, time.time() + i))
        names = sorted(p.name for p in backups.iterdir())
        assert names == ["library-before-1.2.0.db", "library-before-1.3.0.db", "library-before-1.4.0.db"]


# -- prefs -----------------------------------------------------------------------


class TestPrefs:
    def test_round_trip_and_bool_default(self, session):
        assert updater.get_bool_pref(session, updater.PREF_AUTO_CHECK, True) is True
        updater.set_pref(session, updater.PREF_AUTO_CHECK, "0")
        assert updater.get_bool_pref(session, updater.PREF_AUTO_CHECK, True) is False
        updater.set_pref(session, updater.PREF_AUTO_CHECK, "1")
        assert updater.get_pref(session, updater.PREF_AUTO_CHECK) == "1"

    def test_auto_check_due(self):
        assert updater.auto_check_due(None, now=1000.0)
        assert updater.auto_check_due("junk", now=1000.0)
        assert not updater.auto_check_due("1000", now=1000.0 + 3600)
        assert updater.auto_check_due("1000", now=1000.0 + updater.AUTO_CHECK_INTERVAL_S)
