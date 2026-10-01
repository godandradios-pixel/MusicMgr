"""Playback preferences - crossfade and volume levelling (2026-10-01).

Stored in the generic `settings` key/value table like every other
preference in this app (see services/charts.py:LAST_BROWSE_DIR_KEY), set on
the Settings page's "Playback" card, read by `PlayerController.reload_prefs`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db.models import Setting

LEVELLING_OFF = "off"
LEVELLING_TRACK = "track"
LEVELLING_ALBUM = "album"
#: album gain while an album is playing in order, track gain otherwise
LEVELLING_AUTO = "auto"
LEVELLING_MODES = (LEVELLING_OFF, LEVELLING_AUTO, LEVELLING_TRACK, LEVELLING_ALBUM)

MAX_CROSSFADE_S = 12
MIN_PREAMP_DB, MAX_PREAMP_DB = -12.0, 6.0

KEY_CROSSFADE = "playback_crossfade_s"
KEY_FADE_SAME_ALBUM = "playback_crossfade_same_album"
KEY_LEVELLING = "playback_levelling"
KEY_PREAMP = "playback_preamp_db"


@dataclass
class PlaybackPrefs:
    #: 0 = no crossfade: tracks hand off back to back (gapless)
    crossfade_s: int = 0
    #: crossfade between consecutive tracks of the same album too. Off by
    #: default, so live albums and segued records still play gaplessly.
    crossfade_same_album: bool = False
    levelling: str = LEVELLING_AUTO
    preamp_db: float = 0.0


def _get(session: Session, key: str):
    row = session.get(Setting, key)
    return row.value if row is not None else None


def _set(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def load(session: Session) -> PlaybackPrefs:
    prefs = PlaybackPrefs()
    raw = _get(session, KEY_CROSSFADE)
    if raw is not None:
        try:
            prefs.crossfade_s = max(0, min(MAX_CROSSFADE_S, int(raw)))
        except ValueError:
            pass
    raw = _get(session, KEY_FADE_SAME_ALBUM)
    if raw is not None:
        prefs.crossfade_same_album = raw == "1"
    raw = _get(session, KEY_LEVELLING)
    if raw in LEVELLING_MODES:
        prefs.levelling = raw
    raw = _get(session, KEY_PREAMP)
    if raw is not None:
        try:
            prefs.preamp_db = max(MIN_PREAMP_DB, min(MAX_PREAMP_DB, float(raw)))
        except ValueError:
            pass
    return prefs


def save(session: Session, prefs: PlaybackPrefs) -> None:
    _set(session, KEY_CROSSFADE, str(int(prefs.crossfade_s)))
    _set(session, KEY_FADE_SAME_ALBUM, "1" if prefs.crossfade_same_album else "0")
    _set(session, KEY_LEVELLING, prefs.levelling)
    _set(session, KEY_PREAMP, f"{prefs.preamp_db:g}")
    session.flush()
