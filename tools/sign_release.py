#!/usr/bin/env python3
"""Sign and publish a MusicMgr release that CI built as a draft.

    python tools/sign_release.py v1.5.0

The release routine is: bump musicmgr/__init__.py's __version__, commit,
`git tag v1.5.0`, `git push origin main v1.5.0`, wait for the "Release"
GitHub Action to finish (it creates a DRAFT release with the Windows and
Linux builds plus SHA256SUMS.txt), then run this script. It:

1. downloads the draft's files with the GitHub CLI (`gh`),
2. re-hashes every binary and checks it matches SHA256SUMS.txt,
3. signs SHA256SUMS.txt with your private key (asks for its passphrase),
4. double-checks the signature against the public key(s) the app itself
   ships with (musicmgr/assets/update_keys.txt) - so a key mix-up is caught
   here, not by every installed copy refusing the update,
5. uploads SHA256SUMS.txt.sig and publishes the release.

Needs the GitHub CLI, signed in as the repo owner, once per PC:
    winget install GitHub.cli        (Windows)   /   sudo apt install gh   (Mint)
    gh auth login
"""

from __future__ import annotations

import argparse
import base64
import getpass
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from musicmgr import config  # noqa: E402
from musicmgr.services import updater  # noqa: E402

DEFAULT_KEY_PATH = Path.home() / ".musicmgr-signing" / "release_key.pem"


def gh(*args: str, capture: bool = False) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=capture)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise SystemExit(f"gh {' '.join(args)} failed {detail}")
    return result.stdout if capture else ""


def sign_bytes(data: bytes, key_path: Path, passphrase: bytes) -> str:
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=passphrase)
    return base64.b64encode(key.sign(data)).decode("ascii")


def check_sums(folder: Path, sums_text: str, version: str) -> list[str]:
    """Problems found (empty list = all good)."""
    problems = []
    sums = updater.parse_sums(sums_text)
    for plat in ("windows-x64.exe", "linux-x86_64"):
        name = updater.asset_name(version, plat)
        if name not in sums:
            problems.append(f"{name} is not listed in {updater.SUMS_NAME}")
    for name, digest in sums.items():
        path = folder / name
        if not path.is_file():
            problems.append(f"{name} is listed but wasn't in the release")
        elif updater.sha256_file(path) != digest:
            problems.append(f"{name} doesn't match its checksum")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="release tag, e.g. v1.5.0")
    parser.add_argument("--repo", default=config.UPDATE_REPO)
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY_PATH)
    parser.add_argument("--no-publish", action="store_true", help="upload the signature but leave it a draft")
    args = parser.parse_args(argv)

    version = updater.parse_version(args.tag)
    if version is None:
        print(f"{args.tag!r} isn't a version tag like v1.5.0", file=sys.stderr)
        return 1
    if shutil.which("gh") is None:
        print("The GitHub CLI (gh) isn't installed - see this script's docstring.", file=sys.stderr)
        return 1
    if not args.key.is_file():
        print(f"No private key at {args.key} (tools/make_signing_key.py creates one).", file=sys.stderr)
        return 1
    public_keys = config.update_public_keys()
    if not public_keys:
        print("musicmgr/assets/update_keys.txt has no public key - run tools/make_signing_key.py.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        print(f"Downloading {args.tag} from {args.repo}...")
        gh("release", "download", args.tag, "--repo", args.repo, "--dir", str(folder))
        sums_path = folder / updater.SUMS_NAME
        if not sums_path.is_file():
            print(f"The release has no {updater.SUMS_NAME} - did the CI run finish?", file=sys.stderr)
            return 1
        sums_bytes = sums_path.read_bytes()
        problems = check_sums(folder, sums_bytes.decode("utf-8"), str(version))
        if problems:
            print("Not signing:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        print("Checksums match the downloaded builds.")

        passphrase = getpass.getpass("Signing key passphrase: ").encode("utf-8")
        try:
            signature = sign_bytes(sums_bytes, args.key, passphrase)
        except (ValueError, TypeError):
            print("Wrong passphrase (or not a valid key file).", file=sys.stderr)
            return 1
        if not updater.verify_signature(sums_bytes, signature, public_keys):
            print(
                "The signature doesn't verify against musicmgr/assets/update_keys.txt - "
                "this key isn't the one the app trusts. Nothing uploaded.",
                file=sys.stderr,
            )
            return 1

        sig_path = folder / updater.SIG_NAME
        sig_path.write_text(signature + "\n", encoding="ascii")
        gh("release", "upload", args.tag, str(sig_path), "--repo", args.repo, "--clobber")
        print(f"Uploaded {updater.SIG_NAME}.")

    if args.no_publish:
        print("Left as a draft (--no-publish).")
        return 0
    edit = ["release", "edit", args.tag, "--repo", args.repo, "--draft=false"]
    if not version.is_prerelease:
        edit.append("--latest")
    gh(*edit)
    print(f"Published {args.tag}. Installed copies will offer it on their next check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
