"""In-app updates from GitHub Releases (2026-09-23).

James: "I would like to have the MusicMgr app have a settings option that
checks for updates to the program... connect to the github to check...
Realizing the if the MusicMgr.EXE is open, it cannot download a new one and
write over itself when open... works for both updates on Windows 11 and
Linux." Decisions (claude/2026-09-23-auto-update-plan.md): Linux Mint as
the Linux target, *always ask before installing*, a fresh public repo
(config.UPDATE_REPO), and signed releases from day one.

Headless on purpose (no Qt import anywhere in this module) so every piece is
unit-testable; ui/views/settings.py owns the threads and dialogs.

The pieces, in the order an update actually uses them:

* **check_for_update** - asks the GitHub API for the newest release and
  returns an `UpdateInfo` if it's newer than this build *and* has a binary
  for this platform plus a signed checksum file.
* **download_update** - downloads SHA256SUMS.txt + its .sig first, verifies
  the Ed25519 signature against the public keys compiled into the app
  (config.update_public_keys), then streams the binary next to the running
  exe as `<exe>.download`, hashing as it goes. Anything that fails to verify
  is deleted and nothing else happens.
* **install_downloaded** - the "rename, don't overwrite" swap. Windows won't
  let a running .exe be written to or deleted, but it *does* allow a rename
  (James confirmed this on his own Win 11 PC); Linux doesn't care either
  way. So: running `MusicMgr.exe` -> `MusicMgr.exe.old`, then
  `MusicMgr.exe.download` -> `MusicMgr.exe`. The running session keeps
  going from the renamed file; the next launch is the new version.
* **relaunch** / **wait_for_pid_exit** - "Restart now": start the new exe
  with `--after-update --wait-pid <us>`, then the old one closes normally;
  the new one waits for it to be gone before touching library.db.
* **rollback** - swaps `.old` back in.
* **backup_database_if_version_changed** - called at startup before the
  schema migration runs; copies library.db to data/backups/ the first time
  a new version opens it.

Why rollback doesn't restore the database backup: every schema change this
app has ever made is an additive ALTER TABLE ADD COLUMN
(db/session.py:_COLUMN_ADDITIONS), which an older build simply ignores - so
rolling the *program* back is safe on its own, and restoring an older
database copy would throw away everything done since the update. The
backups are there for a manual restore if a future migration is ever not
additive.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import platform
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = "SHA256SUMS.txt.sig"
DOWNLOAD_SUFFIX = ".download"
OLD_SUFFIX = ".old"
CHUNK = 256 * 1024
#: an automatic check runs at most this often (a manual "Check now" ignores it)
AUTO_CHECK_INTERVAL_S = 24 * 60 * 60
#: how many pre-upgrade database copies to keep in data/backups/
KEEP_DB_BACKUPS = 3

# Setting-table keys (db.models.Setting) - same key/value store the Last.fm
# key and Discogs token already use.
PREF_AUTO_CHECK = "update_auto_check"
PREF_INCLUDE_PRERELEASE = "update_include_prerelease"
PREF_LAST_CHECK = "update_last_check"
PREF_SKIPPED_VERSION = "update_skipped_version"
PREF_PREVIOUS_VERSION = "update_previous_version"


class UpdateError(Exception):
    """Anything that stops an update, with a message fit for James to read."""


@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: int = 0


@dataclass
class UpdateInfo:
    version: str
    tag: str
    title: str
    notes: str
    html_url: str
    published_at: str
    prerelease: bool
    binary: ReleaseAsset
    sums: ReleaseAsset
    sig: ReleaseAsset


@dataclass
class CheckResult:
    """`update` is set when there's something to install. `message` is
    always a one-line human summary; `error` is True when the check itself
    failed (network, rate limit, unsigned release...)."""

    current: str
    update: Optional[UpdateInfo] = None
    latest: Optional[str] = None
    message: str = ""
    error: bool = False


# -- versions & platforms --------------------------------------------------


def parse_version(text: str) -> Optional[Version]:
    text = (text or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    try:
        return Version(text)
    except InvalidVersion:
        return None


def is_newer(candidate: str, current: str) -> bool:
    cand, cur = parse_version(candidate), parse_version(current)
    if cand is None or cur is None:
        return False
    return cand > cur


def platform_tag(system: Optional[str] = None, machine: Optional[str] = None) -> Optional[str]:
    """The asset-name suffix this platform's build is published under, or
    None if there's no build for it (anything but Windows x64 / Linux
    x86_64 today)."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "windows" and machine in ("amd64", "x86_64"):
        return "windows-x64.exe"
    if system == "linux" and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    return None


def asset_name(version: str, tag: str) -> str:
    """`MusicMgr-1.5.0-windows-x64.exe` / `MusicMgr-1.5.0-linux-x86_64` -
    the contract .github/workflows/release.yml publishes to."""
    return f"MusicMgr-{version}-{tag}"


# -- GitHub ------------------------------------------------------------------


def _headers(current_version: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        # GitHub rejects API calls with no User-Agent
        "User-Agent": f"MusicMgr/{current_version}",
    }


def fetch_release(
    http: requests.Session, repo: str, current_version: str, include_prerelease: bool = False
) -> Optional[dict]:
    """The newest published release as GitHub's JSON, or None if the repo
    has none yet. `/releases/latest` already skips drafts and pre-releases;
    with `include_prerelease` the list endpoint is used instead and the
    first non-draft entry (newest first) wins."""
    if include_prerelease:
        url = f"{API_BASE}/repos/{repo}/releases?per_page=10"
    else:
        url = f"{API_BASE}/repos/{repo}/releases/latest"
    try:
        response = http.get(url, headers=_headers(current_version), timeout=15)
    except requests.RequestException as exc:
        raise UpdateError(f"Couldn't reach GitHub ({exc.__class__.__name__})") from exc
    if response.status_code == 404:
        return None
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        raise UpdateError("GitHub's hourly check limit was reached - try again later")
    if response.status_code != 200:
        raise UpdateError(f"GitHub answered {response.status_code}")
    data = response.json()
    if include_prerelease:
        for release in data or []:
            if not release.get("draft"):
                return release
        return None
    return data


def _find_asset(release: dict, name: str) -> Optional[ReleaseAsset]:
    for asset in release.get("assets") or []:
        if asset.get("name") == name:
            return ReleaseAsset(name, asset.get("browser_download_url", ""), int(asset.get("size") or 0))
    return None


def select_update(release: dict, current_version: str, plat: Optional[str]) -> CheckResult:
    tag = release.get("tag_name") or ""
    parsed = parse_version(tag)
    if parsed is None:
        return CheckResult(current_version, message=f"Latest release tag {tag!r} isn't a version", error=True)
    latest = str(parsed)
    if not is_newer(latest, current_version):
        return CheckResult(current_version, latest=latest, message=f"You're up to date ({current_version})")
    if plat is None:
        return CheckResult(
            current_version, latest=latest, error=True,
            message=f"{latest} is out, but there's no build for this computer",
        )
    binary = _find_asset(release, asset_name(latest, plat))
    sums = _find_asset(release, SUMS_NAME)
    sig = _find_asset(release, SIG_NAME)
    if binary is None:
        return CheckResult(
            current_version, latest=latest, error=True,
            message=f"{latest} is out, but its {plat} download is missing",
        )
    if sums is None or sig is None:
        return CheckResult(
            current_version, latest=latest, error=True,
            message=f"{latest} is out, but it isn't signed - not installing it",
        )
    info = UpdateInfo(
        version=latest,
        tag=tag,
        title=release.get("name") or f"MusicMgr {latest}",
        notes=release.get("body") or "",
        html_url=release.get("html_url") or "",
        published_at=(release.get("published_at") or "")[:10],
        prerelease=bool(release.get("prerelease")),
        binary=binary,
        sums=sums,
        sig=sig,
    )
    return CheckResult(current_version, update=info, latest=latest, message=f"MusicMgr {latest} is available")


def check_for_update(
    current_version: str,
    *,
    repo: str,
    include_prerelease: bool = False,
    http: Optional[requests.Session] = None,
    plat: Optional[str] = "auto",
) -> CheckResult:
    """Never raises - failures come back as `CheckResult(error=True)`."""
    http = http or requests.Session()
    if plat == "auto":
        plat = platform_tag()
    try:
        release = fetch_release(http, repo, current_version, include_prerelease)
    except UpdateError as exc:
        return CheckResult(current_version, message=str(exc), error=True)
    except ValueError:
        return CheckResult(current_version, message="GitHub sent an unreadable answer", error=True)
    if release is None:
        return CheckResult(current_version, message="No releases published yet")
    return select_update(release, current_version, plat)


# -- signature & checksums ---------------------------------------------------


def verify_signature(data: bytes, signature_text: str, public_keys: Iterable[str]) -> bool:
    """True if `signature_text` (base64 of a raw 64-byte Ed25519 signature,
    what tools/sign_release.py writes) is a valid signature of `data` by
    any of `public_keys` (base64 raw 32-byte keys)."""
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    for key_text in public_keys:
        try:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_text.strip(), validate=True))
        except (binascii.Error, ValueError):
            continue
        try:
            key.verify(signature, data)
            return True
        except InvalidSignature:
            continue
    return False


def parse_sums(text: str) -> dict[str, str]:
    """`sha256sum` output ("<hex>  <name>", or "<hex> *<name>") -> {name: hex}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        name = name.lstrip("*").strip()
        if len(digest) == 64:
            out[name] = digest.lower()
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_small(http: requests.Session, asset: ReleaseAsset, current_version: str) -> bytes:
    try:
        response = http.get(asset.url, headers={"User-Agent": f"MusicMgr/{current_version}"}, timeout=30)
    except requests.RequestException as exc:
        raise UpdateError(f"Couldn't download {asset.name}") from exc
    if response.status_code != 200:
        raise UpdateError(f"Couldn't download {asset.name} ({response.status_code})")
    return response.content


# -- download ------------------------------------------------------------------


def download_update(
    info: UpdateInfo,
    dest: Path,
    *,
    current_version: str,
    public_keys: Iterable[str],
    http: Optional[requests.Session] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[Path]:
    """Download and fully verify `info`'s binary to `dest`. Returns `dest`,
    or None if `should_stop` cancelled it. Raises UpdateError on anything
    that doesn't verify; a partial or unverified file is always removed.

    Verification order matters: the signature is checked *before* the big
    download, so an unsigned or tampered release costs a few hundred bytes,
    not a 100 MB download."""
    http = http or requests.Session()
    keys = list(public_keys)
    if not keys:
        raise UpdateError("This build has no update signing key, so it can't verify updates")

    sums_bytes = _get_small(http, info.sums, current_version)
    sig_text = _get_small(http, info.sig, current_version).decode("ascii", "replace")
    if not verify_signature(sums_bytes, sig_text, keys):
        raise UpdateError(f"The {info.version} release's signature didn't verify - not installing it")
    expected = parse_sums(sums_bytes.decode("utf-8", "replace")).get(info.binary.name)
    if not expected:
        raise UpdateError(f"{info.binary.name} isn't listed in the signed checksums")

    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    done = 0
    try:
        with http.get(
            info.binary.url, headers={"User-Agent": f"MusicMgr/{current_version}"},
            stream=True, timeout=60,
        ) as response:
            if response.status_code != 200:
                raise UpdateError(f"Download failed ({response.status_code})")
            total = int(response.headers.get("Content-Length") or info.binary.size or 0)
            with open(dest, "wb") as fh:
                for chunk in response.iter_content(CHUNK):
                    if should_stop is not None and should_stop():
                        raise _Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except _Cancelled:
        _remove_quietly(dest)
        return None
    except requests.RequestException as exc:
        _remove_quietly(dest)
        raise UpdateError("The download was interrupted") from exc
    except BaseException:
        _remove_quietly(dest)
        raise

    if h.hexdigest() != expected:
        _remove_quietly(dest)
        raise UpdateError("The downloaded file didn't match its signed checksum - discarded")
    if os.name != "nt":
        os.chmod(dest, 0o755)
    return dest


class _Cancelled(Exception):
    pass


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# -- where am I, and can I replace myself? -------------------------------------


def current_executable() -> Optional[Path]:
    """The running MusicMgr binary, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def download_path_for(exe: Path) -> Path:
    return exe.with_name(exe.name + DOWNLOAD_SUFFIX)


def old_path_for(exe: Path) -> Path:
    return exe.with_name(exe.name + OLD_SUFFIX)


def self_update_blocker(exe: Optional[Path], plat: Optional[str] = "auto") -> Optional[str]:
    """None if this install can update itself in place, else why not (shown
    in Settings, with a link to the release page instead)."""
    if plat == "auto":
        plat = platform_tag()
    if exe is None:
        return "Running from source - update with git pull"
    if plat is None:
        return "There's no published build for this computer"
    if not os.access(exe.parent, os.W_OK):
        return f"MusicMgr's folder ({exe.parent}) isn't writable"
    return None


# -- swap, rollback, cleanup ---------------------------------------------------


def install_downloaded(downloaded: Path, exe: Path) -> None:
    """Swap a verified download in for the running binary. See the module
    docstring for why a rename (not a copy/overwrite) is what makes this
    possible while MusicMgr is still open. Either fully succeeds or leaves
    the original exe in place."""
    old = old_path_for(exe)
    if old.exists():
        try:
            old.unlink()
        except OSError as exc:
            raise UpdateError(
                "The previous version is still running - restart MusicMgr to "
                "finish the last update, then try again"
            ) from exc
    try:
        os.replace(exe, old)
    except OSError as exc:
        raise UpdateError(f"Couldn't move the current MusicMgr aside ({exc.strerror or exc})") from exc
    try:
        os.replace(downloaded, exe)
    except OSError as exc:
        os.replace(old, exe)  # put things back exactly as they were
        raise UpdateError(f"Couldn't put the new version in place ({exc.strerror or exc})") from exc


def can_roll_back(exe: Optional[Path]) -> bool:
    return exe is not None and old_path_for(exe).is_file()


def rollback(exe: Path) -> None:
    """Swap `.old` back in; the version being rolled away from becomes the
    new `.old` (so a mistaken rollback can itself be rolled back)."""
    old = old_path_for(exe)
    if not old.is_file():
        raise UpdateError("There's no previous version to roll back to")
    parked = exe.with_name(exe.name + ".rollback")
    _remove_quietly(parked)
    try:
        os.replace(exe, parked)
    except OSError as exc:
        raise UpdateError(f"Couldn't move the current MusicMgr aside ({exc.strerror or exc})") from exc
    try:
        os.replace(old, exe)
    except OSError as exc:
        os.replace(parked, exe)
        raise UpdateError(f"Couldn't restore the previous version ({exc.strerror or exc})") from exc
    try:
        os.replace(parked, old)
    except OSError:
        # parked is the exe we're running from (Windows); harmless to leave -
        # cleanup_leftovers removes it on a later start
        pass


def cleanup_leftovers(exe: Optional[Path]) -> None:
    """Startup housekeeping: a half-finished `.download` (app closed
    mid-download) or a parked `.rollback` from an earlier session."""
    if exe is None:
        return
    for leftover in (download_path_for(exe), exe.with_name(exe.name + ".rollback")):
        if leftover.exists():
            _remove_quietly(leftover)


# -- restart -------------------------------------------------------------------

#: launch flags worth carrying over to the restarted instance
_CARRY_FLAGS = ("--fullscreen", "--kiosk", "--maximized")


def relaunch_args(exe: Path, pid: int, argv: Iterable[str]) -> list[str]:
    carried = [a for a in argv if a in _CARRY_FLAGS]
    return [str(exe), *carried, "--after-update", "--wait-pid", str(pid)]


def relaunch(exe: Path, argv: Iterable[str], pid: Optional[int] = None) -> subprocess.Popen:
    """Start `exe` as a fully independent process. The caller then closes
    the current window normally; the new instance waits for this pid first.

    PYINSTALLER_RESET_ENVIRONMENT (PyInstaller 6.10+) matters for the
    one-file build: without it the child inherits our `_MEI...` extraction
    folder, which gets deleted the moment we exit."""
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    args = relaunch_args(exe, pid if pid is not None else os.getpid(), argv)
    kwargs: dict = {"env": env, "close_fds": True, "cwd": str(exe.parent)}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def parse_wait_pid(argv: list[str]) -> Optional[int]:
    if "--wait-pid" in argv:
        i = argv.index("--wait-pid")
        if i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
    return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover - Windows only
        import ctypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            WAIT_TIMEOUT = 0x102
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wait_for_pid_exit(pid: int, timeout: float = 30.0, poll: float = 0.2) -> bool:
    """True once `pid` is gone (or never existed); False on timeout, in
    which case startup just carries on - SQLite copes with two readers."""
    deadline = time.monotonic() + timeout
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


# -- database safety net -------------------------------------------------------


def backup_database_if_version_changed(
    db_path: Path, backup_dir: Path, marker: Path, current_version: str, keep: int = KEEP_DB_BACKUPS
) -> Optional[Path]:
    """Call before init_engine(): the first time a new release opens this
    data folder, copy library.db to backups/library-before-<ver>.db first.
    Uses SQLite's online backup API, which includes anything still sitting
    in the -wal file, so no manual checkpoint is needed. Keeps the newest
    `keep` copies. Returns the new backup's path, or None if none was needed.

    Never raises - a failed backup is logged and startup continues, since
    refusing to open the app over a safety copy would be worse."""
    try:
        previous = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
        if previous == current_version:
            return None
        made: Optional[Path] = None
        if db_path.is_file():
            backup_dir.mkdir(parents=True, exist_ok=True)
            made = backup_dir / f"library-before-{current_version}.db"
            if made.exists():
                made = backup_dir / f"library-before-{current_version}-{int(time.time())}.db"
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(made)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            _prune_backups(backup_dir, keep)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(current_version, encoding="utf-8")
        return made
    except Exception:  # pragma: no cover - logged, never fatal
        log.exception("pre-upgrade database backup failed")
        return None


def _prune_backups(backup_dir: Path, keep: int) -> None:
    backups = sorted(
        backup_dir.glob("library-before-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for stale in backups[keep:]:
        _remove_quietly(stale)


# -- preferences (Setting table) -----------------------------------------------


def get_pref(db, key: str, default: Optional[str] = None) -> Optional[str]:
    from ..db.models import Setting

    row = db.get(Setting, key)
    return row.value if row is not None and row.value is not None else default


def set_pref(db, key: str, value: Optional[str]) -> None:
    from ..db.models import Setting

    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def get_bool_pref(db, key: str, default: bool) -> bool:
    value = get_pref(db, key)
    if value is None:
        return default
    return value == "1"


def auto_check_due(last_check: Optional[str], now: Optional[float] = None) -> bool:
    if not last_check:
        return True
    now = time.time() if now is None else now
    try:
        return now - float(last_check) >= AUTO_CHECK_INTERVAL_S
    except ValueError:
        return True
