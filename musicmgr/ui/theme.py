"""Dark, high-contrast theme sized for fingers rather than mouse pointers."""

from __future__ import annotations

from ..config import TOUCH

COLORS = {
    "bg": "#101216",
    "surface": "#181b21",
    "surface_alt": "#1f232b",
    "surface_hi": "#2a2f39",
    "border": "#2e333d",
    "text": "#f2f4f8",
    "text_dim": "#98a0ad",
    "accent": "#e8563f",
    "accent_dim": "#b5402c",
    "accent_soft": "#3a1f1b",
    "good": "#4fb286",
    # scrollbar handles need more contrast than ordinary chrome: on a touch
    # panel the handle is also the visual cue for how far down a long list you
    # are, often read at arm's length
    "scroll_handle": "#4a5364",
    "scroll_handle_hi": "#5d6880",
    "warn": "#e0a458",
    # -- Jukebox (2026-09-06): an "authentic" cream-paper/chrome/key-color
    # palette lives apart from the rest of the (dark, neutral) theme on
    # purpose - it's meant to look like a physical object under glass, not
    # like another app panel. The key color (jukebox_key/_hi/_pressed) was
    # originally a bright red - James, 2026-09-07 sixth follow-up, looking
    # at the board: it read as an alert color against the rest of the app's
    # warm brown/gold branding (the MusicMgr logo). Retinted to a walnut-
    # brown family instead - every consumer (ui/widgets/jukebox_strip.py's
    # banner border/code-box/currently-playing fill, and QFrame#
    # JukeboxArtistLine below) reads these three tokens rather than a
    # hardcoded hex, so retheming here was the only change needed.
    "jukebox_chrome_light": "#e7eaee",
    "jukebox_chrome_dark": "#8b909a",
    "jukebox_paper": "#f4e6bd",
    "jukebox_paper_shadow": "#e6d29f",
    "jukebox_paper_border": "#c9b788",
    "jukebox_ink": "#2b2013",
    "jukebox_ink_dim": "#6b5a3a",
    "jukebox_key": "#7a4a1f",
    "jukebox_key_hi": "#a56b34",
    "jukebox_key_pressed": "#4a2e14",
}

#: 2026-09-07, an eighth same-day follow-up - James, looking at the board
#: after the genre chips shipped: "Let's also change the red color on the
#: Now Playing, Add to Jukebox and the Play button at the bottom to the
#: same MusicMgr color palett[e]." These three widgets are styled through
#: `accent`/`accent_dim` (the app's one shared red-orange highlight color,
#: also used for the nav-rail selection bar, focus rings, scrollbars,
#: sliders, and every *other* page's "primary" button - Settings' "Add
#: folder…", Playlists' "New playlist"/"Play", Charts' "Import chart CSV…"
#: /"Play owned", the artist/track panels' "Play all"/"Play everything")
#: - retinting `accent` itself would recolor all of those too, well beyond
#: what James pointed at. Retinted just the three named widgets instead,
#: reusing the same walnut-brown tokens the jukebox key badges already
#: use (`jukebox_key`/`jukebox_key_hi`/`jukebox_key_pressed`) rather than
#: inventing a fourth brown - see QLabel#JukeboxNowPlayingText,
#: QPushButton#PrimaryWarm (ui/views/jukebox.py's "+ Add to jukebox"
#: button, the one non-generic `Primary` id in the app), and
#: QPushButton#TransportMain below. TransportMain is the one board-wide
#: control here rather than jukebox-specific - it's both the main player
#: bar's central Play/Pause and the fullscreen video overlay's (see
#: ui/widgets/common.py's TouchButton), so retinting it recolors that too;
#: there's no way to scope "the play button at the bottom" to the Jukebox
#: page alone, since it's the same persistent widget on every page.
#:
#: 2026-09-07, a ninth same-day follow-up - James: "make the pills not red
#: but the brown color" (the genre chips themselves, added the same day as
#: the above). Same reasoning as the paragraph above: `QPushButton#Chip`'s
#: checked state is shared by every chip row in the app (cover_grid.py's
#: sort chips, Now Playing's "Up next"/"Lyrics", track_panel.py's/
#: video_table.py's sort/group chips), so retinting `#Chip:checked`
#: itself would have turned all of those brown too. See
#: QPushButton#ChipWarm below - a duplicate of `#Chip`'s pill shape with
#: only the checked-state color swapped, applied to just the Jukebox
#: page's genre chips (ui/views/jukebox.py overrides each chip's object
#: name from ChipButton's default "Chip" to "ChipWarm").


def stylesheet() -> str:
    c = COLORS
    t = TOUCH
    return f"""
* {{
    font-family: "Inter", "Segoe UI", "Helvetica Neue", "DejaVu Sans", sans-serif;
    font-size: {t['font_base']}px;
    outline: none;
}}

QWidget {{
    background: {c['bg']};
    color: {c['text']};
}}

QLabel {{
    background: transparent;
}}
QLabel#Title {{
    font-size: {t['font_title']}px;
    font-weight: 600;
    padding: 4px 0;
}}
QLabel#Subtitle {{
    color: {c['text_dim']};
    font-size: {t['font_base'] - 1}px;
}}
QLabel#Crumb {{
    color: {c['text_dim']};
    font-size: {t['font_base']}px;
}}
QLabel#CrumbCurrent {{
    color: {c['text']};
    font-size: {t['font_base']}px;
    font-weight: 600;
}}
QLabel#BioText {{
    font-size: {t['font_base']}px;
    padding: 2px 4px;
}}
QLabel#BigNumber {{
    /* Settings page only (its only user) - James: "change all that bright
       orange/red color on this settings page to the brown pallet." Reuses
       jukebox_key_hi rather than accent, same walnut-brown token
       JukeboxNowPlayingText already uses for readable text at this size
       (plain jukebox_key reads too dark/muddy here - see that rule's own
       comment below). */
    font-size: 30px;
    font-weight: 700;
    color: {c['jukebox_key_hi']};
}}
QLabel#Dim {{ color: {c['text_dim']}; }}

/* ---------------- navigation rail ---------------- */
QFrame#NavRail {{
    background: {c['surface']};
    border-right: 1px solid {c['border']};
}}
QPushButton#NavButton {{
    /* border-radius: 0 here is load-bearing, not decorative - without it,
       this inherits the generic QPushButton rule's 10px further down,
       which rounds every corner including the ones behind border-left.
       Qt then draws that colored left edge curving in at top and bottom
       instead of as a flat bar - the "red left parenthesis" James spotted
       next to the Settings label. Checked/hover below only round the two
       *right* corners, so the flat left edge (and its accent bar) never
       curves again, in any state. */
    background: transparent;
    border: none;
    border-left: 4px solid transparent;
    border-radius: 0;
    color: {c['text_dim']};
    text-align: left;
    padding: 0 18px;
    min-height: {t['nav_item_height']}px;
    font-size: {t['font_base'] + 2}px;
    font-weight: 500;
}}
QPushButton#NavButton:hover {{
    background: {c['surface_alt']};
    color: {c['text']};
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
}}
QPushButton#NavButton:checked {{
    /* surface_hi (not hover's dimmer surface_alt) so the selected page
       reads as a distinct, elevated state rather than looking like
       whatever you last happened to hover over. Brown pallet (James,
       2026-09-08: "bring it into the brown palette") rather than accent -
       jukebox_key_hi is the same brighter walnut tone #BigNumber/
       JukeboxNowPlayingText already use for legibility against a dark
       background; plain jukebox_key reads too dark/muddy this thin. */
    background: {c['surface_hi']};
    color: {c['text']};
    border-left: 5px solid {c['jukebox_key_hi']};
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
}}
QPushButton#NavQuit {{
    background: transparent;
    border: none;
    border-left: 4px solid transparent;
    border-radius: 0;  /* see #NavButton's comment above - same fix */
    color: {c['text_dim']};
    text-align: left;
    padding: 0 18px;
    min-height: {t['nav_item_height']}px;
    font-size: {t['font_base'] + 2}px;
    font-weight: 500;
}}
QPushButton#NavQuit:hover {{
    /* jukebox_key_pressed as the wash - same dark/muted role accent_soft
       played, just brown instead of red - under the brighter jukebox_key_hi
       text/border, same pairing #NavButton:checked uses above. */
    background: {c['jukebox_key_pressed']};
    color: {c['jukebox_key_hi']};
    border-left: 4px solid {c['jukebox_key_hi']};
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
}}
QFrame#NavBrand {{
    /* icon + wordmark header row - replaces the old plain-text QLabel
       (2026-09-07, James: "put the ico image in the upper left area") */
    border-bottom: 1px solid {c['border']};
}}
QLabel#NavBrandText {{
    font-size: 19px;
    font-weight: 700;
    color: {c['text']};
}}

/* ---------------- buttons ---------------- */
QPushButton {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 0 20px;
    min-height: {t['button_height'] - 12}px;
    color: {c['text']};
    font-weight: 500;
}}
QPushButton:hover {{ background: {c['surface_hi']}; }}
QPushButton:pressed {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
QPushButton:disabled {{ color: #5a6270; background: {c['surface']}; }}
QPushButton#Primary {{
    background: {c['accent']};
    border-color: {c['accent']};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {c['accent_dim']}; }}
/* the Jukebox page's "+ Add to jukebox" button only (2026-09-07 follow-up -
   see the COLORS dict's comment above) - everywhere else in the app a
   TouchButton(primary=True) keeps the ordinary #Primary red-orange above.
   Same shape/padding/radius as #Primary (inherited from the base
   QPushButton rule); only the color swaps to the jukebox key palette. */
QPushButton#PrimaryWarm {{
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#PrimaryWarm:hover {{ background: {c['jukebox_key_pressed']}; }}
QPushButton#Transport {{
    background: {c['surface_alt']};
    border-radius: 28px;
    min-width: 56px;
    max-width: 56px;
    min-height: 56px;
    max-height: 56px;
    padding: 0;
    font-size: 20px;
}}
QPushButton#TransportWide {{
    background: {c['surface_alt']};
    border-radius: 28px;
    min-width: 88px;
    max-width: 88px;
    min-height: 56px;
    max-height: 56px;
    padding: 0;
    font-size: 16px;
}}
QPushButton#TransportMain {{
    /* 2026-09-07 follow-up (see the COLORS dict's comment above) -
       retinted from accent red-orange to the jukebox key palette, same
       as the two widgets below. This is the main player bar's central
       Play/Pause *and* the fullscreen video overlay's (both build a
       TransportMain button - see ui/widgets/common.py's TouchButton) -
       one shared control, not scoped to the Jukebox page. */
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    border-radius: 34px;
    min-width: 68px;
    max-width: 68px;
    min-height: 68px;
    max-height: 68px;
    padding: 0;
    font-size: 24px;
    color: #ffffff;
}}
QPushButton#TransportMain:hover {{ background: {c['jukebox_key_pressed']}; }}
QPushButton#Chip {{
    border-radius: 18px;
    min-height: 44px;
    padding: 0 18px;
    background: {c['surface']};
}}
QPushButton#Chip:checked {{
    background: {c['accent']};
    border-color: {c['accent']};
    color: #fff;
}}
/* the Jukebox page's own genre chips only (2026-09-07 follow-up - James:
   "make the pills not red but the brown color") - same pill shape as
   #Chip (duplicated rather than shared, since object-name selectors don't
   inherit from one another); only the checked-state color swaps to the
   jukebox key palette, so every other chip row in the app (cover_grid.py's
   sort chips, Now Playing's "Up next"/"Lyrics", track_panel.py's/
   video_table.py's sort/group chips) keeps the ordinary red #Chip:checked
   look above. See ui/views/jukebox.py, which overrides ChipButton's own
   "Chip" object name to "ChipWarm" for these genre chips specifically. */
QPushButton#ChipWarm {{
    border-radius: 18px;
    min-height: 44px;
    padding: 0 18px;
    background: {c['surface']};
}}
QPushButton#ChipWarm:checked {{
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    color: #fff;
}}
QPushButton#RowPageArrow {{
    background: {c['surface_alt']};
    border-radius: 16px;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
    font-size: 16px;
    color: {c['text']};
}}
QPushButton#RowPageArrow:hover {{ background: {c['surface_hi']}; }}
QPushButton#RowPageArrow:disabled {{
    background: {c['surface']};
    color: {c['text_dim']};
}}
QLabel#CrumbLink {{
    background: transparent;
    padding: 6px 2px;
    color: {c['text_dim']};
    font-size: {t['font_base']}px;
    font-weight: 500;
}}
QLabel#CrumbLink:hover {{ color: {c['text']}; }}
QPushButton#LetterKey {{
    background: transparent;
    border: none;
    color: {c['text_dim']};
    font-size: {t['letter_bar_font']}px;
    font-weight: 600;
    padding: 0;
    min-height: {t['letter_bar_height'] - 8}px;
    min-width: 34px;
}}
QPushButton#LetterKey:hover {{
    background: {c['surface_hi']};
    border-radius: 10px;
    color: {c['text']};
}}
/* no :disabled rule - every letter is always tappable (it always types
   into the search box, whether or not it currently has anything to
   scroll to), so none of them are ever actually disabled. See
   JumpBar.set_available() in cover_grid.py. */
QListWidget#Document {{
    background: {c['bg']};
    border: none;
    border-radius: 0;
    padding: 0;
}}
QListWidget#Document::item {{ margin: 0; border-radius: 0; }}
QPushButton#KeyCap {{
    min-height: 52px;
    min-width: 46px;
    padding: 0;
    border-radius: 8px;
    font-size: 17px;
}}

/* ---------------- lists ---------------- */
QListWidget, QListView, QTreeWidget, QTableWidget {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 12px;
    padding: 4px;
}}
QListWidget::item, QListView::item, QTreeWidget::item {{
    border-radius: 8px;
    margin: 2px 4px;
}}
QListWidget::item:selected, QListView::item:selected, QTreeWidget::item:selected {{
    background: {c['surface_hi']};
    color: {c['text']};
}}
/* TrackDetailsTable (Title Details, 2026-09-06 follow-up) is the one
   QTableView in the app - it never had selection enabled before, so this
   selector was never needed until James asked to be able to select a
   title. Without it, a selected cell fell through to the platform style's
   own default (thin per-cell border lines, not a filled background) since
   QTableView isn't in the shared rule above - the same class of "what is
   that blue box" gap QTreeWidget::branch:selected exists to close, below.
   Deliberately no matching QTableView::item rule (border-radius/margin,
   the way QListWidget/QTreeWidget items get one) - James wants the whole
   row reading as one solid highlighted bar, and a per-cell margin/radius
   would break it into a row of separated chips instead. */
QTableView::item:selected {{
    background: {c['surface_hi']};
    color: {c['text']};
}}
QListWidget#LyricsList::item {{
    padding: 7px 10px;
    font-size: 16px;
}}
QTreeWidget::item {{
    min-height: {t['tree_row_height']}px;
    padding: 0 6px;
}}
QTreeWidget::branch {{
    background: transparent;
    border: none;
}}
QTreeWidget::branch:selected {{
    /* Without this, a selected nested row (anything at indentation level 1+
       - a subfolder, a chart's edition) shows the platform's native
       highlight blue in its indent/branch strip, since the plain
       QTreeWidget::branch rule above only covers the unselected state -
       spotted 2026-08-30 as "what is that blue box" on a selected edition
       row. Matching it to the same color the row's own selection uses
       keeps the whole row reading as one consistent highlight. */
    background: {c['surface_hi']};
}}

/* VideoTable is the one QTreeWidget in the app used as a genuine multi-
   column table with a visible header, rather than a hidden-header nav tree
   (TouchTree) - it gets the same row height as every other touch list
   (row_height, not the shorter tree_row_height) and its own header styling,
   since QHeaderView has no other visible use in this app to style for. */
QTreeWidget#VideoTable::item {{
    min-height: {t['row_height']}px;
    padding: 0 10px;
}}
QHeaderView::section {{
    background: {c['surface_alt']};
    color: {c['text_dim']};
    border: none;
    border-bottom: 1px solid {c['border']};
    padding: 0 10px;
    min-height: {t['button_height'] - 12}px;
    font-weight: 600;
}}
QHeaderView::section:hover {{ background: {c['surface_hi']}; color: {c['text']}; }}
QHeaderView {{ background: {c['surface_alt']}; border: none; }}

/* Scrollbars are sized for fingers, not pointers. The handle is inset by only
   2px of margin and 2px of border, so the visible bar is nearly as wide as the
   grabbable track - a slim-looking handle on a touch panel reads as "don't
   touch me" however wide its hit area actually is. */
QScrollBar:vertical {{
    background: {c['surface']};
    width: {t['scrollbar']}px;
    margin: 2px;
    border-radius: {t['scrollbar'] // 2}px;
}}
QScrollBar::handle:vertical {{
    background: {c['scroll_handle']};
    border: 2px solid {c['surface']};
    border-radius: {t['scrollbar'] // 2}px;
    min-height: {t['scrollbar_min_handle']}px;
}}
QScrollBar::handle:vertical:hover {{ background: {c['scroll_handle_hi']}; }}
QScrollBar::handle:vertical:pressed {{ background: {c['accent']}; }}

QScrollBar:horizontal {{
    background: {c['surface']};
    height: {t['scrollbar']}px;
    margin: 2px;
    border-radius: {t['scrollbar'] // 2}px;
}}
QScrollBar::handle:horizontal {{
    background: {c['scroll_handle']};
    border: 2px solid {c['surface']};
    border-radius: {t['scrollbar'] // 2}px;
    min-width: {t['scrollbar_min_handle']}px;
}}
QScrollBar::handle:horizontal:hover {{ background: {c['scroll_handle_hi']}; }}
QScrollBar::handle:horizontal:pressed {{ background: {c['accent']}; }}

/* no arrow buttons - they are far too small to hit and just steal track space */
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ---------------- inputs ---------------- */
QLineEdit, QComboBox, QSpinBox, QDateEdit {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 0 16px;
    min-height: {t['button_height'] - 12}px;
    selection-background-color: {c['accent']};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
QComboBox::drop-down {{ width: 40px; border: none; }}
QComboBox QAbstractItemView {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    selection-background-color: {c['accent']};
}}

QTextEdit, QPlainTextEdit {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 10px;
}}

/* ---------------- sliders ---------------- */
QSlider::groove:horizontal {{
    height: 8px;
    background: {c['surface_hi']};
    border-radius: 4px;
}}
QSlider::sub-page:horizontal {{
    background: {c['accent']};
    border-radius: 4px;
}}
QSlider::handle:horizontal {{
    background: #ffffff;
    width: 24px;
    height: 24px;
    margin: -9px 0;
    border-radius: 12px;
}}
/* the player bar's volume slider only (2026-09-08 follow-up - James: "make
   that volume control bar brown to fit pallete") - object-name scoped
   (see player_bar.py: self.volume.setObjectName("VolumeSlider")) rather
   than retinting QSlider::sub-page generally, since that generic rule is
   shared with the seek/scrub bar here and video_panel.py's own seek bar,
   neither of which James asked to change. Same jukebox_key brown
   ProgressWarm/ChipWarm already use, groove/handle left as the shared
   QSlider rules above set them. */
QSlider#VolumeSlider::sub-page:horizontal {{
    background: {c['jukebox_key']};
    border-radius: 4px;
}}

/* ---------------- jukebox ---------------- */
QLabel#JukeboxNowPlayingText {{
    /* 2026-09-07 follow-up - retinted from accent red-orange to the
       jukebox key palette's brighter tone (jukebox_key itself reads too
       dark/muddy for body text at this size) - see the COLORS dict's
       comment above. */
    color: {c['jukebox_key_hi']};
    font-weight: 700;
    font-size: {t['font_base'] + 2}px;
}}
QFrame#JukeboxStrip {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {c['jukebox_chrome_light']},
        stop:0.5 {c['jukebox_chrome_dark']},
        stop:1 {c['jukebox_chrome_light']});
    border: 2px solid #5b5f66;
    border-radius: 16px;
}}
/* 2026-09-07 redesign (match James's reference mockup): the card's inside
   is the two painted _ChevronBanner buttons plus the artist badge/line
   below - both draw their own look in code, so there's no QSS for them.
   The old cream-paper "JukeboxPaper"/row/title/subtitle/indicator/round-
   key rules this replaced are gone along with the widgets they styled. */
QLabel#JukeboxArtistBadge {{
    background: {c['jukebox_paper']};
    color: {c['jukebox_ink']};
    border: 2px solid {c['jukebox_ink']};
    border-radius: 4px;
    padding: 1px 10px;
    font-weight: 700;
    font-size: {t['font_base'] - 2}px;
}}
QFrame#JukeboxArtistLine {{
    background: {c['jukebox_key']};
    min-height: 2px;
    max-height: 2px;
}}

/* ---------------- misc ---------------- */
QFrame#Card {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 14px;
}}
QFrame#PlayerBar {{
    background: {c['surface']};
    border-top: 1px solid {c['border']};
}}
QFrame#Divider {{ background: {c['border']}; max-height: 1px; }}
QProgressBar {{
    background: {c['surface_hi']};
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 6px; }}
/* Settings page's scan/move progress bar only - QProgressBar is also used
   on Charts and the release panel's lyrics download, which keep the
   ordinary accent fill above; only Settings' bar gets its objectName set
   to ProgressWarm (see ui/views/settings.py), matching the same brown
   pallet as #BigNumber/#PrimaryWarm on this page. */
QProgressBar#ProgressWarm::chunk {{ background: {c['jukebox_key']}; border-radius: 6px; }}
QTabBar::tab {{
    background: {c['surface']};
    padding: 14px 26px;
    border: 1px solid {c['border']};
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    color: {c['text_dim']};
}}
QTabBar::tab:selected {{ background: {c['surface_alt']}; color: {c['text']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 12px; top: -1px; }}
QToolTip {{
    background: {c['surface_hi']};
    color: {c['text']};
    border: 1px solid {c['border']};
    padding: 6px;
}}
QMessageBox, QDialog {{ background: {c['bg']}; }}
"""
