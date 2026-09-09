#!/usr/bin/env python3
"""Launch MusicMgr.

    python run.py                 windowed
    python run.py --fullscreen    kiosk mode for a dedicated touch panel
    python run.py --maximized     windowed, but maximized on launch - for a
                                   Windows Startup shortcut (2026-09-08)
"""

import sys

from musicmgr.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
