#!/usr/bin/env python3
"""Create the Ed25519 key pair that signs MusicMgr releases. Run ONCE.

    python tools/make_signing_key.py

What it does:

1. Asks for a passphrase (twice) and writes the password-encrypted PRIVATE
   key to ~/.musicmgr-signing/release_key.pem (on Windows that's
   C:\\Users\\<you>\\.musicmgr-signing\\release_key.pem). It refuses to
   overwrite an existing key - losing or replacing the key means installed
   copies can no longer auto-update (see below).
2. Appends the PUBLIC key to musicmgr/assets/update_keys.txt, which is
   committed and bundled into every build. Commit that file.

Back up the private key file AND its passphrase somewhere offline (USB
stick, password manager). Never commit it and never put it in a GitHub
secret: the whole point is that someone who takes over the GitHub account
still can't produce a release the app will install.

If the key is ever lost: generate a new one with --force, commit the new
public key, and install that next build by hand once on each PC - installed
copies only trust the keys they were built with.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[1]
KEYS_FILE = REPO_ROOT / "musicmgr" / "assets" / "update_keys.txt"
DEFAULT_KEY_PATH = Path.home() / ".musicmgr-signing" / "release_key.pem"


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def write_key(private_key: Ed25519PrivateKey, path: Path, passphrase: bytes) -> None:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)


def append_public_key(pub_b64: str, keys_file: Path = KEYS_FILE) -> None:
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    existing = keys_file.read_text(encoding="utf-8") if keys_file.exists() else (
        "# Ed25519 public keys trusted to sign MusicMgr releases (base64, one per line).\n"
        "# Written by tools/make_signing_key.py - see config.UPDATE_KEYS_FILE.\n"
    )
    if pub_b64 in existing:
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    keys_file.write_text(existing + pub_b64 + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY_PATH, help="where to write the private key")
    parser.add_argument("--force", action="store_true", help="overwrite an existing private key (dangerous)")
    args = parser.parse_args(argv)

    if args.key.exists() and not args.force:
        print(f"A signing key already exists at {args.key} - refusing to overwrite it.", file=sys.stderr)
        print("Use --force only if the old key is truly lost (see this script's docstring).", file=sys.stderr)
        return 1

    passphrase = getpass.getpass("Passphrase for the new signing key: ")
    if len(passphrase) < 8:
        print("Use at least 8 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat passphrase: ") != passphrase:
        print("Passphrases didn't match.", file=sys.stderr)
        return 1

    key = Ed25519PrivateKey.generate()
    write_key(key, args.key, passphrase.encode("utf-8"))
    pub = public_key_b64(key)
    append_public_key(pub)

    print(f"Private key : {args.key}")
    print(f"Public key  : {pub}")
    print(f"Added to    : {KEYS_FILE.relative_to(REPO_ROOT)}  <- commit this file")
    print("Now back up the private key file and its passphrase somewhere offline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
