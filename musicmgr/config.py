"""Application paths and defaults."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_NAME = "MusicMgr"

#: the folder holding run.py — i.e. the checkout, not the installed package.
#: When frozen into a PyInstaller exe, __file__ points inside the bundled
#: temp/library tree instead, so PROJECT_ROOT would resolve to the wrong
#: place and portable mode would silently miss the data/ folder next to the
#: .exe. sys.executable is the actual exe path in both --onedir and
#: --onefile builds, so use that when frozen.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    #: Where PyInstaller's `datas` (MusicMgr.spec's musicmgr/assets entry)
    #: land at runtime. NOT the .exe's folder: PyInstaller 6+'s --onedir
    #: layout nests everything but the exe under `_internal/`, so
    #: PROJECT_ROOT / "musicmgr" / "assets" doesn't exist there (and the
    #: splash/icon guards fail quietly). sys._MEIPASS is right for both
    #: --onedir and --onefile; falls back to PROJECT_ROOT if it's unset.
    _BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    _BUNDLE_ROOT = PROJECT_ROOT


def platform_data_dir() -> Path:
    """Where the OS says application data belongs."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / APP_NAME


def _resolve_data_dir() -> Path:
    """Pick the data directory, most explicit wins.

    1. ``MUSICMGR_HOME`` environment variable
    2. portable mode: a ``data`` folder sitting next to run.py
    3. the platform application-data directory

    Portable mode keeps the database, artwork cache and code together in one
    self-contained folder you can move, back up or put on a USB stick. Create
    it by making the folder (``tools/migrate_data.py`` does this for you).
    """
    override = os.environ.get("MUSICMGR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    portable = PROJECT_ROOT / "data"
    if portable.is_dir():
        return portable
    return platform_data_dir()


def is_portable() -> bool:
    return DATA_DIR == PROJECT_ROOT / "data"


#: Branding assets shipped inside the package itself (not the user's data/
#: folder - these are part of the app, same on every install). Built from
#: _BUNDLE_ROOT (not PROJECT_ROOT) so this still finds them when frozen -
#: see the sys._MEIPASS comment above.
ASSETS_DIR = _BUNDLE_ROOT / "musicmgr" / "assets"
SPLASH_IMAGE = ASSETS_DIR / "splash_banner.png"
#: the banner at the size the splash shows it (640 px wide) - used by both
#: PyInstaller's splash (MusicMgr.spec) and the Qt one (launch.py) so they
#: match exactly. Made from splash_banner.png; remake it if that changes.
SPLASH_SMALL_IMAGE = ASSETS_DIR / "splash_640.png"
APP_ICON = ASSETS_DIR / "app_icon.ico"

#: -- in-app updater (services/updater.py) --------------------------------
#: The public GitHub repo whose Releases the updater checks. Compiled into
#: every shipped binary, so changing it strands every install still
#: pointing at the old one - see claude/2026-09-23-public-repo-setup.md.
UPDATE_REPO = "godandradios-pixel/MusicMgr"
#: Ed25519 public keys (base64, one per line, `#` comments allowed) that a
#: release's SHA256SUMS.txt.sig must verify against before anything is
#: installed. Written by tools/make_signing_key.py and bundled with the
#: rest of musicmgr/assets. A list, so a key rotation can ship old + new
#: together for one release before the old one is retired.
UPDATE_KEYS_FILE = ASSETS_DIR / "update_keys.txt"


def update_public_keys() -> list[str]:
    try:
        text = UPDATE_KEYS_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    return [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "library.db"
ART_DIR = DATA_DIR / "artwork"
#: artist portraits, imported from a folder you point the app at
ARTIST_IMG_DIR = DATA_DIR / "artists"
LOG_PATH = DATA_DIR / "musicmgr.log"
#: pre-upgrade copies of library.db, one per version change - see
#: services/updater.backup_database_if_version_changed
DB_BACKUP_DIR = DATA_DIR / "backups"
#: which backup to restore at the next start - services/library_backup.py
RESTORE_MARKER = DATA_DIR / "restore_pending.txt"
#: the release version that last opened this data folder
LAST_RUN_VERSION_FILE = DATA_DIR / "last_run_version.txt"

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga",
    ".opus", ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv",
}

#: folders a library scan never walks into: GoodSync's _gsdata_ (its
#: "_saved_" old versions would import as extra copies) and Windows' system
#: folders. Matched on the folder name, case-insensitively - see
#: is_skipped_dir.
SKIP_SCAN_DIRS = {"_gsdata_", "$recycle.bin", "system volume information"}


#: a hidden name: a dot and then a letter, "_" or "~" - ".git",
#: ".DS_Store", "._Song.mp3" (macOS), ".Trash-1000", ".~lock.x#"
_HIDDEN_NAME = re.compile(r"\.[^\W\d]|\.~")


def is_hidden_name(name: str) -> bool:
    """A dot-file or dot-folder (".git", ".DS_Store", "._Song.mp3") - but
    not a real name that just starts with a dot: the album "...But
    Seriously" (an ellipsis) or the band ".38 Special" (a dot and a digit).
    2026-10-25: three ".38 Special - ....mp4" videos in D:\\Videos had never
    been imported."""
    return bool(_HIDDEN_NAME.match(name))


def is_skipped_dir(name: str) -> bool:
    """True for a folder name a scan leaves alone - hidden (dot) folders
    and SKIP_SCAN_DIRS."""
    return is_hidden_name(name) or name.casefold() in SKIP_SCAN_DIRS


#: Windows paths ignore letter case: "Like A River.mp3" and "Like a
#: River.mp3" are one file there. Path lookups in the scanners compare
#: case-insensitively on Windows, so a file renamed only in case isn't
#: imported a second time.
CASE_INSENSITIVE_PATHS = os.name == "nt"

#: Touch UI sizing. Bump these if your panel is very high-DPI or very small.
TOUCH = {
    "row_height": 72,
    "button_height": 64,
    #: nav rail items only (see theme.py's #NavButton/#NavQuit) - separate
    #: from button_height so shrinking the sidebar's row height doesn't
    #: shrink every other button in the app. There is no width key: the
    #: rail's width comes from the nav buttons' own `sizeHint()` (see
    #: `MainWindow._nav_rail_width` in ui/app.py), so it always hugs the
    #: labels actually on it.
    "nav_item_height": 48,
    #: theme.make_compact() - the artist page's header actions and its
    #: section-header buttons (Edit bio, Fetch popularity) use it;
    #: everything else keeps button_height - 12.
    "compact_button_height": 40,
    "art_size": 168,
    "font_base": 15,
    "font_title": 22,
    #: scrollbar thickness. The handle is inset only 2px of margin + 2px of
    #: border, so what you SEE is about this wide too - on a panel the handle
    #: has to look grabbable, not just be grabbable.
    "scrollbar": 40,
    #: shortest the handle is allowed to get on a very long list, so it stays
    #: catchable even with thousands of tracks
    "scrollbar_min_handle": 90,
    #: the A-Z strip above the album/track document
    "letter_bar_height": 60,
    "letter_bar_font": 22,
    #: playlist folder tree: shorter than row_height since a row is always a
    #: single line (name, or name + a small count), never the two-line
    #: primary/secondary layout the other lists use
    "tree_row_height": 56,
    "tree_indent": 30,
}


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    ARTIST_IMG_DIR.mkdir(parents=True, exist_ok=True)
