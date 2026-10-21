"""Fast first frame (2026-10-21 - James: "determine if the splash screen is
appearing when it should").

It wasn't. Timed on PC1's library, a launch spent about a second loading
MusicMgr's own code - every page of the app, imported by ui/app.py - with
nothing on screen, and the splash only appeared after that. Before even
that, the single-file MusicMgr.exe unpacks itself to a temp folder on every
launch (PyInstaller --onefile), again with nothing on screen.

Two splashes now cover that, showing the same picture in the same place so
the hand-over is invisible:

1. PyInstaller's own splash (MusicMgr.spec, Windows build) appears the
   moment the exe starts, before it unpacks - drawn by the exe itself, no
   Python involved.
2. `main` below - the program's entry point (run.py) - creates Qt and shows
   the Qt splash first, closes PyInstaller's splash, and only then imports
   ui/app.py and the rest of the app behind it.

Keep this module light: anything imported at the top of it delays the
first frame. Only `config` (stdlib only) and PySide6 are loaded before the
splash is up.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from . import config


def build_splash():
    """The Qt splash, shown and painted - or None if the picture is missing
    (a from-source checkout without musicmgr/assets/ still starts)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QSplashScreen

    if config.SPLASH_SMALL_IMAGE.is_file():
        # the same 640-pixel picture PyInstaller's splash shows, so the
        # switch from one to the other doesn't jump
        pixmap = QPixmap(str(config.SPLASH_SMALL_IMAGE))
    elif config.SPLASH_IMAGE.is_file():
        pixmap = QPixmap(str(config.SPLASH_IMAGE))
        if not pixmap.isNull():
            pixmap = pixmap.scaledToWidth(640, Qt.SmoothTransformation)
    else:
        return None
    if pixmap.isNull():
        return None
    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
    splash.show()
    return splash


def close_bootloader_splash() -> None:
    """Close PyInstaller's splash, if this is a build that has one. The exe
    sets _PYI_SPLASH_IPC when its splash is up; without it, importing
    pyi_splash (which PyInstaller bundles anyway once it sees the import)
    prints a traceback, so it isn't imported at all."""
    if "_PYI_SPLASH_IPC" not in os.environ:
        return
    try:
        import pyi_splash  # type: ignore[import-not-found]

        pyi_splash.close()
    except Exception:  # not frozen, a build without a splash, or already closed
        pass


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv)

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(argv)
    splash = build_splash()
    if splash is not None:
        app.processEvents()
    close_bootloader_splash()

    from .ui import app as app_module  # the slow import: every page of the app

    return app_module.main(argv, splash=splash)
