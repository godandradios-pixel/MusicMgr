"""Skins and finishes - user-selectable colour schemes, applied at
restart. The jukebox, radio and turntable each get their own finish
choice.

Two kinds of choice, both saved per PC in `data/appearance.json` and read
once, when ui/theme.py is first imported - before any screen is built or
any module computes a colour from the theme - so a change takes effect the
next time MusicMgr starts (Settings › Appearance has a "Restart now"):

- **Skin** - the app itself: backgrounds, cards, text, borders, scroll
  bars and the accent (primary buttons, the selected menu item, chips,
  sliders, progress bars, stars, the Play button...). `SKINS` below; each
  is a set of values for ui/theme.py's COLORS.
- **Finishes** - the three things drawn to look like real equipment keep
  their own look whatever the skin: the jukebox's title strips and chrome
  (`JUKEBOX_FINISHES`, also COLORS values), the radio's cabinet, knobs,
  keys and trim (`RADIO_FINISHES`, read by ui/widgets/radio_dial.py) and
  the record player's console, deck and felt (`TURNTABLE_FINISHES`, read
  by ui/widgets/turntable.py).

The first entry of each table is today's look, and what a PC gets until
something else is chosen. Setting MUSICMGR_DEFAULT_LOOK=1 ignores the
saved choice (the test suite does, so it always sees the defaults).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# skins - values for ui/theme.py's COLORS
# --------------------------------------------------------------------------
#
# `accent` / `accent_hi` / `accent_pressed` are the skin's highlight colour
# in three strengths: the fill of a primary button, a brighter tone for
# small text and outlines on the dark background, and the pressed state.
# (The app also calls these jukebox_key / _hi / _pressed; theme.py keeps
# those as the same values.)

SKINS: dict[str, dict] = {
    "walnut": {
        "label": "Walnut (brown)",
        "bg": "#101216", "surface": "#181b21", "surface_alt": "#1f232b",
        "surface_hi": "#2a2f39", "border": "#2e333d",
        "text": "#f2f4f8", "text_dim": "#98a0ad", "text_disabled": "#5a6270",
        "scroll_handle": "#4a5364", "scroll_handle_hi": "#5d6880",
        "accent": "#7a4a1f", "accent_hi": "#a56b34", "accent_pressed": "#4a2e14",
        "on_accent": "#ffffff", "good": "#4fb286", "warn": "#e0a458",
    },
    "midnight": {
        "label": "Midnight blue",
        "bg": "#0d1117", "surface": "#151b26", "surface_alt": "#1b2331",
        "surface_hi": "#263042", "border": "#2b3647",
        "text": "#eef2f8", "text_dim": "#93a1b5", "text_disabled": "#56637a",
        "scroll_handle": "#44526a", "scroll_handle_hi": "#56688a",
        "accent": "#2f5fa8", "accent_hi": "#5b8fe0", "accent_pressed": "#1f3f72",
        "on_accent": "#ffffff", "good": "#4fb286", "warn": "#e0a458",
    },
    "forest": {
        "label": "Forest green",
        "bg": "#0f1411", "surface": "#161d18", "surface_alt": "#1c251f",
        "surface_hi": "#26322a", "border": "#2c382f",
        "text": "#eef4ef", "text_dim": "#98a89c", "text_disabled": "#56645a",
        "scroll_handle": "#4a5c4f", "scroll_handle_hi": "#5d735f",
        "accent": "#2e6b47", "accent_hi": "#55a372", "accent_pressed": "#1e472f",
        "on_accent": "#ffffff", "good": "#6fcf97", "warn": "#e0a458",
    },
    "jukebox_red": {
        "label": "Jukebox red",
        "bg": "#121012", "surface": "#1b1719", "surface_alt": "#231d20",
        "surface_hi": "#2f272b", "border": "#3a3035",
        "text": "#f6f1f2", "text_dim": "#ab9ea2", "text_disabled": "#655a5e",
        "scroll_handle": "#5b4b51", "scroll_handle_hi": "#735e66",
        "accent": "#b0302a", "accent_hi": "#e8563f", "accent_pressed": "#7a1f1b",
        "on_accent": "#ffffff", "good": "#4fb286", "warn": "#e0a458",
    },
    "cream": {
        "label": "Cream (light)",
        "bg": "#f4efe4", "surface": "#fbf8f1", "surface_alt": "#ece5d6",
        "surface_hi": "#e0d6c2", "border": "#d2c6ad",
        "text": "#2a2118", "text_dim": "#6e6153", "text_disabled": "#a89c8a",
        "scroll_handle": "#b9ab90", "scroll_handle_hi": "#9c8d72",
        "accent": "#7a4a1f", "accent_hi": "#8f5726", "accent_pressed": "#5a3516",
        "on_accent": "#ffffff", "good": "#2e8b5f", "warn": "#b7791f",
    },
}

# --------------------------------------------------------------------------
# finishes
# --------------------------------------------------------------------------

#: the jukebox board's title strips (paper and ink) and chrome trim - more
#: COLORS values (the board has always read these by name)
JUKEBOX_FINISHES: dict[str, dict] = {
    "classic": {
        "label": "Classic cream",
        "jukebox_chrome_light": "#e7eaee", "jukebox_chrome_dark": "#8b909a",
        "jukebox_strip_border": "#5b5f66",
        "jukebox_paper": "#f4e6bd", "jukebox_paper_shadow": "#e6d29f",
        "jukebox_paper_border": "#c9b788",
        "jukebox_ink": "#2b2013", "jukebox_ink_dim": "#6b5a3a",
    },
    "white": {
        "label": "White card",
        "jukebox_chrome_light": "#eef0f3", "jukebox_chrome_dark": "#9aa0a8",
        "jukebox_strip_border": "#5b5f66",
        "jukebox_paper": "#fbfaf6", "jukebox_paper_shadow": "#ecebe5",
        "jukebox_paper_border": "#c8c6bd",
        "jukebox_ink": "#1f2328", "jukebox_ink_dim": "#5c6168",
    },
    "aged": {
        "label": "Aged paper",
        "jukebox_chrome_light": "#dcdcd6", "jukebox_chrome_dark": "#7d7f7f",
        "jukebox_strip_border": "#55575a",
        "jukebox_paper": "#e8cf95", "jukebox_paper_shadow": "#d6b878",
        "jukebox_paper_border": "#b0925a",
        "jukebox_ink": "#3a2510", "jukebox_ink_dim": "#6e5530",
    },
    "gold": {
        "label": "Gold trim",
        "jukebox_chrome_light": "#f3e3b0", "jukebox_chrome_dark": "#a88a4a",
        "jukebox_strip_border": "#6a5530",
        "jukebox_paper": "#f4e6bd", "jukebox_paper_shadow": "#e6d29f",
        "jukebox_paper_border": "#c9b788",
        "jukebox_ink": "#2b2013", "jukebox_ink_dim": "#6b5a3a",
    },
}

#: the radio set (ui/widgets/radio_dial.py). Colours are "#rrggbb" or
#: (r, g, b[, a]). The dial glass stays dark, and the red pointer and the
#: green magic eye stay as they are in every finish - that's what the real
#: sets had.
RADIO_FINISHES: dict[str, dict] = {
    "walnut": {
        "label": "Walnut",
        "wood": ("#6e3c1e", "#5a2f16", "#431f0d"), "grain": (25, 10, 3),
        "edge": "#1d0c04", "edge_hi": (255, 220, 170, 38),
        "bezel": "#2a1a0a", "trim": "#b08a4a", "trim_hi": (255, 236, 190, 110),
        "knob": ("#6a3a1c", "#2c1308", "#120602"), "knob_edge": "#0a0301",
        "knob_hi": (255, 210, 160, 45), "knob_cap": ("#7a4524", "#2a1207"),
        "knob_mark": "#f1e3bd", "knob_label": "#f1e3bd",
        "key": "#efe4c8", "key_border": "#5c4a2c", "key_text": "#4a2a12",
    },
    "oak": {
        "label": "Blonde oak",
        "wood": ("#c9a06a", "#b58950", "#9a713c"), "grain": (90, 60, 25),
        "edge": "#5a3e1c", "edge_hi": (255, 240, 210, 60),
        "bezel": "#3a2812", "trim": "#c9a24a", "trim_hi": (255, 240, 200, 120),
        "knob": ("#8a5a32", "#4a2a12", "#2a1608"), "knob_edge": "#1a0c04",
        "knob_hi": (255, 220, 170, 50), "knob_cap": ("#9a6a3a", "#3a1e0a"),
        "knob_mark": "#f6ead0", "knob_label": "#3a2510",
        "key": "#f3ead4", "key_border": "#7a6440", "key_text": "#4a2a12",
    },
    "black": {
        "label": "Black bakelite",
        "wood": ("#2a2a2c", "#1c1c1e", "#111112"), "grain": (0, 0, 0),
        "edge": "#050505", "edge_hi": (255, 255, 255, 30),
        "bezel": "#1a1a1a", "trim": "#c0c4c8", "trim_hi": (255, 255, 255, 120),
        "knob": ("#3a3a3c", "#151516", "#060606"), "knob_edge": "#000000",
        "knob_hi": (255, 255, 255, 40), "knob_cap": ("#4a4a4c", "#121213"),
        "knob_mark": "#e8e8e8", "knob_label": "#e8e8e8",
        "key": "#f0ede4", "key_border": "#555555", "key_text": "#222222",
    },
    "ivory": {
        "label": "Ivory bakelite",
        "wood": ("#efe6d0", "#e2d6b8", "#cfc09c"), "grain": (120, 100, 60),
        "edge": "#8a7a58", "edge_hi": (255, 255, 255, 70),
        "bezel": "#6a5530", "trim": "#c9a24a", "trim_hi": (255, 245, 210, 120),
        "knob": ("#6a3a1c", "#2c1308", "#120602"), "knob_edge": "#0a0301",
        "knob_hi": (255, 210, 160, 45), "knob_cap": ("#7a4524", "#2a1207"),
        "knob_mark": "#f1e3bd", "knob_label": "#3a2510",
        "key": "#fbf6ea", "key_border": "#8a7a58", "key_text": "#4a2a12",
    },
}

#: the record player on Now Playing (ui/widgets/turntable.py). The record,
#: tone arm and lamps are the same in every finish.
TURNTABLE_FINISHES: dict[str, dict] = {
    "walnut": {
        "label": "Walnut console",
        "cabinet": ("#6b3b1f", "#5a2f17", "#4a2512"), "cabinet_edge": "#2a140a",
        "grain": (30, 12, 4),
        "deck": ("#4a2a17", "#2e180c", "#1f1008"), "deck_edge": "#140903",
        "deck_mottle": ("#6a3d22", "#170a04", "#553018"), "rim_hi": (255, 220, 180, 40),
        "hole": "#120702", "felt": "#5b5249", "felt_mottle": ("#7a7065", "#3a332c"),
        "panel": "#3d2210", "panel_edge": "#110802", "lever": "#1a0d06",
        "lamp_off": "#3a2a1e",
    },
    "oak": {
        "label": "Blonde oak",
        "cabinet": ("#c9a06a", "#b58950", "#9a713c"), "cabinet_edge": "#5a3e1c",
        "grain": (90, 60, 25),
        "deck": ("#4a2a17", "#2e180c", "#1f1008"), "deck_edge": "#140903",
        "deck_mottle": ("#6a3d22", "#170a04", "#553018"), "rim_hi": (255, 220, 180, 40),
        "hole": "#120702", "felt": "#5b5249", "felt_mottle": ("#7a7065", "#3a332c"),
        "panel": "#3d2210", "panel_edge": "#110802", "lever": "#1a0d06",
        "lamp_off": "#3a2a1e",
    },
    "black": {
        "label": "Modern black",
        "cabinet": ("#2b2b2d", "#1f1f21", "#151516"), "cabinet_edge": "#0a0a0a",
        "grain": (0, 0, 0),
        "deck": ("#222224", "#18181a", "#101011"), "deck_edge": "#050505",
        "deck_mottle": ("#2e2e30", "#0c0c0d", "#262628"), "rim_hi": (255, 255, 255, 30),
        "hole": "#050505", "felt": "#3d3d3f", "felt_mottle": ("#4d4d50", "#2a2a2c"),
        "panel": "#1a1a1b", "panel_edge": "#050505", "lever": "#0d0d0e",
        "lamp_off": "#2a2a2a",
    },
    "white": {
        "label": "White",
        "cabinet": ("#f2f0ea", "#e6e3db", "#d6d2c8"), "cabinet_edge": "#a8a49a",
        "grain": (120, 110, 95),
        "deck": ("#e9e6de", "#dcd8ce", "#cfcabd"), "deck_edge": "#9a958a",
        "deck_mottle": ("#f5f2ea", "#c9c4b8", "#e0dbd0"), "rim_hi": (255, 255, 255, 80),
        "hole": "#8a857a", "felt": "#4a4a4c", "felt_mottle": ("#5a5a5c", "#38383a"),
        "panel": "#d6d2c8", "panel_edge": "#8a857a", "lever": "#6e6a62",
        "lamp_off": "#8a857a",
    },
}

DEFAULTS = {"skin": "walnut", "jukebox": "classic", "radio": "walnut", "turntable": "walnut"}
TABLES = {"skin": SKINS, "jukebox": JUKEBOX_FINISHES, "radio": RADIO_FINISHES,
          "turntable": TURNTABLE_FINISHES}


# --------------------------------------------------------------------------
# the saved choice
# --------------------------------------------------------------------------


def appearance_file() -> Path:
    from .. import config
    return config.DATA_DIR / "appearance.json"


def load_choice(path: Optional[Path] = None) -> dict:
    """The saved skin and finishes, each checked against what exists (an
    unknown name - say from a newer version - falls back to the default)."""
    choice = dict(DEFAULTS)
    if os.environ.get("MUSICMGR_DEFAULT_LOOK"):
        return choice
    path = path or appearance_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return choice
    except Exception as exc:  # a damaged file must never stop the app starting
        log.warning("couldn't read %s: %s", path, exc)
        return choice
    if isinstance(saved, dict):
        for kind, table in TABLES.items():
            if saved.get(kind) in table:
                choice[kind] = saved[kind]
    return choice


def save_choice(choice: dict, path: Optional[Path] = None) -> None:
    path = path or appearance_file()
    clean = {kind: choice.get(kind, DEFAULTS[kind]) for kind in TABLES}
    for kind, name in clean.items():
        if name not in TABLES[kind]:
            clean[kind] = DEFAULTS[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")


#: what this run of the app is using - read once, at import (see the
#: module docstring); Settings compares a new choice with it to say
#: whether a restart is needed
ACTIVE = load_choice()


def colors_for(choice: dict) -> dict:
    """The COLORS values a choice sets: the skin, with the accent under its
    older jukebox_key names too, and the jukebox finish."""
    skin = SKINS[choice["skin"]]
    out = {k: v for k, v in skin.items() if k != "label"}
    out["jukebox_key"] = skin["accent"]
    out["jukebox_key_hi"] = skin["accent_hi"]
    out["jukebox_key_pressed"] = skin["accent_pressed"]
    out.update({k: v for k, v in JUKEBOX_FINISHES[choice["jukebox"]].items() if k != "label"})
    return out


def radio_finish() -> dict:
    return RADIO_FINISHES[ACTIVE["radio"]]


def turntable_finish() -> dict:
    return TURNTABLE_FINISHES[ACTIVE["turntable"]]
