"""Is this file in the old-time radio folder? (2026-10-13)

James keeps his old-time radio shows in D:\\Radio (~/Radio on the Mint
PCs) - the Radio page's own folder (services/otr.py). They aren't music:
the music scan leaves that folder out (services/scanner.py) and the artist
profile download skips anyone whose files are only there
(services/artist_bio_downloader.py). Both ask here so they agree.

A Radio folder is any of:
  * the Radio page's folder, wherever it's set (Settings key "otr_root"),
  * a folder called Radio at the top of a drive (D:\\Radio),
  * ~/Radio.
A song or album that only has "Radio" in its name (Queen's "Radio Ga Ga",
a "Radio" album folder inside D:\\Music) is not one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

_DRIVE_RADIO = re.compile(r"^[a-z]:/radio/")


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower() + "/"


def roots(session) -> list[str]:
    """The normalised Radio folders for this PC (the drive-top one is
    matched by pattern, see is_radio_path)."""
    from .otr import get_root

    out = [_norm(str(Path.home() / "Radio"))]
    try:
        otr_root = get_root(session)
    except Exception:  # pragma: no cover - never let this break a scan
        otr_root = None
    if otr_root:
        out.append(_norm(otr_root))
    return out


def is_radio_path(path: str, radio_roots: Sequence[str]) -> bool:
    p = _norm(path)
    return bool(_DRIVE_RADIO.match(p)) or any(p.startswith(r) for r in radio_roots)


def without_radio(paths: Iterable, radio_roots: Sequence[str]) -> list:
    return [p for p in paths if not is_radio_path(str(p), radio_roots)]
