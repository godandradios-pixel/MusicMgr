"""App version string, for the Settings page's About footer.

MusicMgr has no formal release process - James is the only user, there
are no version-numbered releases, no tags (see `git tag` - empty), and
nothing else in the codebase tracks a version at all. Rather than hand-
maintain a number that's guaranteed to go stale the moment someone
forgets to bump it, "version" here just means "exactly which commit is
this" - a short git hash plus the date it was committed, which is always
accurate for free.

Two sources, tried in order:

1. Ask git directly (`git log -1`) against config.PROJECT_ROOT. This is
   the live, always-correct answer whenever this *is* a git checkout -
   i.e. every time the app is run from source (`python run.py`). It's
   tried first so a stray leftover VERSION file (see below) from a
   previous local build can never shadow the commit actually running.

2. A VERSION file baked into musicmgr/assets/ at build time. This is the
   *only* source available once frozen into the PyInstaller .exe - a
   packaged build has no .git folder to ask (git -C <exe's temp extract
   dir> fails harmlessly and falls through to here). build.ps1 writes
   this file, in the same "%h - %cd" format as source 1 above, right
   before invoking PyInstaller, so it always names the commit that was
   actually packaged. Not committed to git itself (see .gitignore) since
   it's a build artifact that goes stale the moment another commit
   lands - regenerated fresh by every build.ps1 run instead.

Falls back to "dev build" rather than raising if neither source works
(git not on PATH, a shallow/export checkout with no .git, and no baked-in
VERSION file either) - this is a cosmetic label, not worth crashing the
app over.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from . import config

VERSION_FILE = config.ASSETS_DIR / "VERSION"


def _from_git() -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(config.PROJECT_ROOT),
                "log", "-1", "--format=%h - %cd", "--date=short",
            ],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _from_file() -> Optional[str]:
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def get_version() -> str:
    return _from_git() or _from_file() or "dev build"


#: computed once at import time - a version string can't change while
#: the app is running, and every caller (just the Settings page today)
#: wants the same value.
APP_VERSION = get_version()
