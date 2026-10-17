"""Preferences and the Radio page's tuner, shared through the USB drive
(2026-10-17 - James chose these from a list of what the USB sync didn't
carry yet: "Please proceed with 2., 3., and 4.").

Two checkboxes in Settings → USB sync, each a flat `{key: value}` dict in
library-state.json.gz, merged three-way key by key like the rest (see
library_state.py):

- **Playback & look** (off unless switched on - each PC may well want its
  own, the Mint PC on other speakers say): crossfade, crossfading within an
  album, volume levelling and preamp (services/playback_prefs.py), whether
  Now Playing shows the record player, and the skin and finishes from
  `data/appearance.json` (ui/skins.py). A new look shows the next time
  MusicMgr starts, as when it's picked by hand.
- **Tuner**: the band showing, the static-between-stations switch, and
  which station or show the dial was left on - keyed portably (a station
  by its stream address, a show by its folder's name), never by database
  id. A station or show this PC doesn't have leaves its own dial where it
  is, without that counting as a change to send back.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import RadioShow, RadioStation, Setting
from . import playback_prefs

log = logging.getLogger(__name__)

#: settings carried as they are by "Playback & look"
SETTING_KEYS = (
    playback_prefs.KEY_CROSSFADE,
    playback_prefs.KEY_FADE_SAME_ALBUM,
    playback_prefs.KEY_LEVELLING,
    playback_prefs.KEY_PREAMP,
    "nowplaying_turntable",          # ui/views/nowplaying.py:PREF_TURNTABLE
)
_SETTING = "setting:"
_LOOK = "look:"

#: the tuner's own settings (ui/views/tuner.py, services/tuner.py)
BAND_KEY = "tuner_band"
STATIC_KEY = "tuner_static"
LAST_TUNED_KEY = "tuner_last"
#: what the last sync put in tuner_last, and the portable value it stood
#: for - so a dial left untouched since reads back as that value
PREF_TUNER_PLACED = "sync_tuner_last"


def _get(db: Session, key: str) -> Optional[str]:
    row = db.get(Setting, key)
    return row.value if row is not None else None


def _put(db: Session, key: str, value: Optional[str]) -> None:
    row = db.get(Setting, key)
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()


# --------------------------------------------------------------------------
# playback & look
# --------------------------------------------------------------------------


def _appearance_file() -> Path:
    from ..ui import skins

    return skins.appearance_file()


def _read_look() -> dict[str, str]:
    """The saved skin and finishes, only as far as appearance.json has them
    (a PC that never chose keeps the defaults without sending them)."""
    from ..ui import skins

    try:
        saved = json.loads(_appearance_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("couldn't read the appearance choice: %s", exc)
        return {}
    if not isinstance(saved, dict):
        return {}
    return {kind: saved[kind] for kind, table in skins.TABLES.items() if saved.get(kind) in table}


def read_prefs(db: Session) -> dict[str, str]:
    out = {}
    for key in SETTING_KEYS:
        value = _get(db, key)
        if value is not None:
            out[_SETTING + key] = value
    for kind, name in _read_look().items():
        out[_LOOK + kind] = name
    return out


def apply_prefs(db: Session, merged: dict, local: dict) -> tuple[int, bool]:
    """Put the drive's preferences in place here. Returns (how many changed,
    whether the look did - it shows once MusicMgr restarts)."""
    from ..ui import skins

    changed, look = 0, {}
    for key in set(merged) | set(local):
        value = merged.get(key)
        if value == local.get(key):
            continue
        if key.startswith(_SETTING) and key[len(_SETTING):] in SETTING_KEYS:
            _put(db, key[len(_SETTING):], value)
            changed += 1
        elif key.startswith(_LOOK) and key[len(_LOOK):] in skins.TABLES:
            look[key[len(_LOOK):]] = value
    if look:
        choice = dict(skins.DEFAULTS)
        choice.update(_read_look())
        for kind, name in look.items():
            choice[kind] = name if name in skins.TABLES[kind] else skins.DEFAULTS[kind]
        try:
            skins.save_choice(choice, _appearance_file())
            changed += len(look)
        except OSError as exc:
            log.warning("couldn't save the appearance choice: %s", exc)
            look = {}
    return changed, bool(look)


# --------------------------------------------------------------------------
# tuner
# --------------------------------------------------------------------------


def _portable_last(db: Session, raw: Optional[str]) -> Optional[str]:
    """tuner_last ("station|12") as it travels ("station|<stream url>")."""
    if not raw or "|" not in raw:
        return None
    kind, ident = raw.split("|", 1)
    if kind == "onair":
        return "onair|0"
    try:
        ident_id = int(ident)
    except ValueError:
        return None
    if kind == "station":
        st = db.get(RadioStation, ident_id)
        url = (st.stream_url or "").strip() if st is not None else ""
        return f"station|{url}" if url else None
    if kind == "show":
        show = db.get(RadioShow, ident_id)
        return f"show|{Path(show.folder).name}" if show is not None else None
    return None


def _local_last(db: Session, portable: Optional[str]) -> Optional[str]:
    """The other way round; None when this PC hasn't got it."""
    if not portable or "|" not in portable:
        return None
    kind, ident = portable.split("|", 1)
    if kind == "onair":
        return "onair|0"
    if kind == "station":
        for st in db.scalars(select(RadioStation)):
            if (st.stream_url or "").strip() == ident:
                return f"station|{st.id}"
    if kind == "show":
        for show in db.scalars(select(RadioShow)):
            if Path(show.folder).name == ident:
                return f"show|{show.id}"
    return None


def _placed(db: Session) -> dict:
    try:
        value = json.loads(_get(db, PREF_TUNER_PLACED) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def read_tuner(db: Session) -> dict[str, str]:
    out = {}
    for key in (BAND_KEY, STATIC_KEY):
        value = _get(db, key)
        if value is not None:
            out[key] = value
    raw = _get(db, LAST_TUNED_KEY)
    placed = _placed(db)
    if placed and raw == placed.get("local"):
        # untouched since the last sync put it there (or couldn't: this
        # PC hasn't the station) - it still stands for what the drive said
        last = placed.get("portable")
    else:
        last = _portable_last(db, raw)
    if last:
        out[LAST_TUNED_KEY] = last
    return out


def apply_tuner(db: Session, merged: dict, local: dict) -> int:
    """Put the drive's band, static switch and dial in place here. Returns
    how many of the three changed."""
    changed = 0
    for key in (BAND_KEY, STATIC_KEY):
        value = merged.get(key)
        if value != local.get(key) and value is not None:
            _put(db, key, value)
            changed += 1
    portable = merged.get(LAST_TUNED_KEY)
    if portable != local.get(LAST_TUNED_KEY) and portable is not None:
        target = _local_last(db, portable)
        if target is not None:
            _put(db, LAST_TUNED_KEY, target)
            changed += 1
        _put(db, PREF_TUNER_PLACED,
             json.dumps({"local": _get(db, LAST_TUNED_KEY), "portable": portable}))
    return changed
