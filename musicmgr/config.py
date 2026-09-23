"""Application paths and defaults."""

from __future__ import annotations

import os
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
    #: actually land at runtime. This is NOT the same folder as the .exe -
    #: PyInstaller 6+'s default --onedir layout nests everything except the
    #: exe itself under a `_internal/` folder to keep dist/MusicMgr/ tidy,
    #: so PROJECT_ROOT / "musicmgr" / "assets" silently doesn't exist there
    #: (this is why the splash banner and app icon weren't showing up in
    #: the built .exe - James, 2026-09-08 - the code ran fine, ASSETS_DIR
    #: just pointed nowhere and both guards fail quiet-and-safe by design).
    #: sys._MEIPASS is PyInstaller's own answer to "where did my data files
    #: go" in both --onedir and --onefile builds, so use it instead of
    #: assuming datas sit next to the exe. Falls back to PROJECT_ROOT if
    #: _MEIPASS is somehow unset, matching the old (wrong-for-6+) behavior
    #: rather than crashing.
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
APP_ICON = ASSETS_DIR / "app_icon.ico"

#: -- in-app updater (services/updater.py, 2026-09-23) --------------------
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
#: the release version that last opened this data folder
LAST_RUN_VERSION_FILE = DATA_DIR / "last_run_version.txt"

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga",
    ".opus", ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv",
}

#: Touch UI sizing. Bump these if your panel is very high-DPI or very small.
TOUCH = {
    "row_height": 72,
    "button_height": 64,
    #: nav rail items only (see theme.py's #NavButton/#NavQuit) - separate
    #: from button_height so shrinking the sidebar's own row height doesn't
    #: shrink every other button in the app. James, 2026-09-07: "make the
    #: sidebar space between items smaller" - was implicitly button_height
    #: (64) before this key existed.
    #: There used to be a "nav_width" key here (James, 2026-09-07: "make
    #: the width of the sidebar to be smaller since the short word menu
    #: items are not that long" - went 220 -> 176 -> back up to 200 the
    #: same day once the brand icon grew and needed the extra room
    #: alongside the wordmark). A 2026-09-13 follow-up (James: this app
    #: runs on a small touchscreen jukebox panel, so "let's make the rail
    #: a bit smaller ... where I think space may be saved") removed the
    #: fixed constant entirely rather than hand-picking yet another
    #: number - by the time it was raised again, the brand icon had long
    #: since moved to its own stacked row (see ui/app.py's
    #: `_build_brand_row` docstring), so the icon-needs-room constraint
    #: that produced 200 didn't even apply anymore. `MainWindow.
    #: _nav_rail_width` in ui/app.py now measures the real nav buttons'
    #: own `sizeHint()` instead, so the rail always hugs whatever labels
    #: are actually on it rather than a guess that goes stale the next
    #: time one changes.
    "nav_item_height": 48,
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
