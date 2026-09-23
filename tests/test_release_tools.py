"""tools/make_signing_key.py + tools/sign_release.py helpers: a key written
by one must sign data the app's own updater.verify_signature accepts, and
the pre-signing checksum check must catch a missing or altered build."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from musicmgr.services import updater

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_key = load("make_signing_key")
sign_release = load("sign_release")


def test_key_written_then_signing_verifies(tmp_path):
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "k.pem"
    keys_file = tmp_path / "update_keys.txt"
    make_key.write_key(key, key_path, b"correct horse")
    make_key.append_public_key(make_key.public_key_b64(key), keys_file)
    make_key.append_public_key(make_key.public_key_b64(key), keys_file)  # idempotent

    lines = [l for l in keys_file.read_text().splitlines() if l and not l.startswith("#")]
    assert len(lines) == 1

    sig = sign_release.sign_bytes(b"sums", key_path, b"correct horse")
    assert updater.verify_signature(b"sums", sig, lines)


def test_wrong_passphrase_raises(tmp_path):
    import pytest

    key_path = tmp_path / "k.pem"
    make_key.write_key(Ed25519PrivateKey.generate(), key_path, b"right-one")
    with pytest.raises((ValueError, TypeError)):
        sign_release.sign_bytes(b"x", key_path, b"wrong-one")


def _write_release(folder: Path, version="1.6.0", tamper=None, drop=None):
    lines = []
    for plat in ("windows-x64.exe", "linux-x86_64"):
        name = updater.asset_name(version, plat)
        data = name.encode()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
        if name == drop:
            continue
        (folder / name).write_bytes(b"tampered" if name == tamper else data)
    return "\n".join(lines) + "\n"


def test_check_sums_ok(tmp_path):
    assert sign_release.check_sums(tmp_path, _write_release(tmp_path), "1.6.0") == []


def test_check_sums_catches_tamper_and_missing(tmp_path):
    win = updater.asset_name("1.6.0", "windows-x64.exe")
    linux = updater.asset_name("1.6.0", "linux-x86_64")
    problems = sign_release.check_sums(tmp_path, _write_release(tmp_path, tamper=win, drop=linux), "1.6.0")
    assert any("doesn't match" in p for p in problems)
    assert any("wasn't in the release" in p for p in problems)


def test_check_sums_requires_both_platforms(tmp_path):
    name = updater.asset_name("1.6.0", "windows-x64.exe")
    (tmp_path / name).write_bytes(b"x")
    sums = f"{hashlib.sha256(b'x').hexdigest()}  {name}\n"
    problems = sign_release.check_sums(tmp_path, sums, "1.6.0")
    assert any("linux-x86_64" in p for p in problems)
