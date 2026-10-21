"""musicmgr/launch.py - the splash goes up before the rest of the app loads
(2026-10-21)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from musicmgr import config, launch

REPO = Path(__file__).resolve().parents[1]


def test_launch_module_does_not_load_the_app():
    """Everything launch.py imports at module level runs before the splash
    is up - it must not pull in ui/app.py and its pages."""
    code = (
        "import sys; import musicmgr.launch; "
        "heavy = [m for m in ('musicmgr.ui.app', 'musicmgr.ui.views.library', 'sqlalchemy') "
        "if m in sys.modules]; print(heavy)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_splash_uses_the_640_picture_pyinstaller_shows(qapp):
    assert config.SPLASH_SMALL_IMAGE.is_file()
    splash = launch.build_splash()
    try:
        assert splash is not None
        assert splash.pixmap().width() == 640
    finally:
        splash.close()


def test_closing_the_bootloader_splash_is_harmless_from_source():
    launch.close_bootloader_splash()  # no pyi_splash module here - must not raise
