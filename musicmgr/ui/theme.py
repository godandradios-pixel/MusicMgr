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
    # the accent colour comes from the skin (see the skins note below);
    # jukebox_key/_hi/_pressed below are its Walnut values
    "good": "#4fb286",
    # scrollbar handles need more contrast than ordinary chrome: on a touch
    # panel the handle is also the visual cue for how far down a long list you
    # are, often read at arm's length
    "scroll_handle": "#4a5364",
    "scroll_handle_hi": "#5d6880",
    "warn": "#e0a458",
    # -- Jukebox: a cream-paper/chrome/key-color palette kept apart from the
    # rest of the (dark, neutral) theme on purpose - it should look like a
    # physical object under glass. The key color (jukebox_key/_hi/_pressed)
    # is a walnut brown matching the app's branding; ui/widgets/
    # jukebox_strip.py reads these tokens rather than hardcoded hex.
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

# Skins: the values above are the Walnut skin; the skin and jukebox finish
# chosen in Settings › Appearance replace them here, once, when this module
# is first imported - before any screen is built or any module works out a
# colour from COLORS - so a new choice shows after a restart. See
# ui/skins.py. The accent is `accent` / `accent_hi` / `accent_pressed`; the
# older jukebox_key names hold the same values, so their readers need no
# change.
from . import skins as _skins  # noqa: E402

COLORS.update(_skins.colors_for(_skins.ACTIVE))

#: Jukebox-specific rules: QPushButton#PrimaryWarm (the "+ Add to jukebox"
#: button) and QPushButton#ChipWarm (the Jukebox genre chips - `#Chip`'s
#: pill shape with only the checked colour swapped; ui/views/jukebox.py sets
#: that object name) use the jukebox_key tokens, so they can be styled
#: apart from the generic #Primary / #Chip rules shared across the app.
#: QLabel#JukeboxNowPlayingText and QPushButton#TransportMain (the player
#: bar's and video overlay's Play/Pause - one widget on every page) use
#: them too.


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
    /* Settings page only (its only user). Uses jukebox_key_hi rather
       than accent: the brighter walnut tone JukeboxNowPlayingText uses for
       readable text at this size (plain jukebox_key reads too dark here). */
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
    /* border-radius: 0 here is load-bearing, not decorative. Without it
       this inherits the generic QPushButton rule's 10px further down, which
       rounds every corner including the ones behind border-left, and Qt
       then draws the colored left edge curving in at top and bottom instead
       of as a flat bar. Checked/hover below only round the two right
       corners, so the flat left edge (and its accent bar) never curves, in
       any state. */
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
       reads as a distinct, elevated state rather than looking like whatever
       you last hovered over. The border is jukebox_key_hi, the same
       brighter walnut tone #BigNumber and JukeboxNowPlayingText use for
       legibility on a dark background; plain jukebox_key reads too dark
       this thin. */
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
    /* icon + wordmark header row */
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
/* make_compact() below (artist page) - a property
   selector, so it layers over #Primary's colors rather than replacing them. */
QPushButton[compact="true"] {{
    min-height: {t['compact_button_height']}px;
    max-height: {t['compact_button_height']}px;
    padding: 0 16px;
    border-radius: 8px;
}}
/* Colour roles. The app's accent is the walnut-brown jukebox_key family
   (the skin replaces these values; see ui/skins.py). Two roles, two tokens:
   jukebox_key for a filled background (dark enough for white button text to
   read clearly on top), and jukebox_key_hi, the brighter tone, for a colour
   standing on its own against the dark background - a focus ring, a small
   badge, an icon - where the darker jukebox_key reads as muddy. */
QPushButton:pressed {{ background: {c['jukebox_key_pressed']}; border-color: {c['jukebox_key']}; }}
QPushButton:disabled {{ color: {c['text_disabled']}; background: {c['surface']}; }}
QPushButton#Primary {{
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    color: {c['on_accent']};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {c['jukebox_key_pressed']}; }}
/* The Jukebox page's "+ Add to jukebox" button. Identical to #Primary;
   kept as its own object name so the Jukebox can be styled apart from the
   rest of the app. */
QPushButton#PrimaryWarm {{
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    color: {c['on_accent']};
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
    /* The main player bar's central Play/Pause and the fullscreen video
       overlay's (both build a TransportMain button - see
       ui/widgets/common.py's TouchButton): one shared control, not scoped
       to the Jukebox page. */
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    border-radius: 34px;
    min-width: 68px;
    max-width: 68px;
    min-height: 68px;
    max-height: 68px;
    padding: 0;
    font-size: 24px;
    color: {c['on_accent']};
}}
QPushButton#TransportMain:hover {{ background: {c['jukebox_key_pressed']}; }}
QPushButton#Chip {{
    border-radius: 18px;
    min-height: 44px;
    padding: 0 18px;
    background: {c['surface']};
}}
QPushButton#Chip:checked {{
    /* Every chip row in the app (sort chips, Now Playing's "Up
       next"/"Lyrics", the track and video tables' sort/group chips) - the
       same colour as #ChipWarm below. */
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    color: {c['on_accent']};
}}
/* The Jukebox page's genre chips: the same pill shape as #Chip
   (duplicated rather than shared, since object-name selectors don't inherit
   from one another). Visually identical to #Chip; ui/views/jukebox.py sets
   the "ChipWarm" object name so the genre chips can be styled apart. */
QPushButton#ChipWarm {{
    border-radius: 18px;
    min-height: 44px;
    padding: 0 18px;
    background: {c['surface']};
}}
QPushButton#ChipWarm:checked {{
    background: {c['jukebox_key']};
    border-color: {c['jukebox_key']};
    color: {c['on_accent']};
}}
/* The text box a genre chip (or the trailing "+" chip) turns into while
   renaming or adding (ui/views/jukebox.py's _InlineGenreEdit): the same
   pill radius/height/padding as #ChipWarm right above so swapping one for
   the other doesn't make the row jump, plus a border in the jukebox key
   colour so it reads as "being edited" rather than a plain QLineEdit. */
QLineEdit#GenreChipEditor {{
    border-radius: 18px;
    min-height: 44px;
    padding: 0 18px;
    background: {c['surface']};
    border: 2px solid {c['jukebox_key']};
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
/* The Jukebox page's own pager, next to "+ Add to jukebox" in the page
   header. Its own object name rather than a #RowPageArrow size change,
   since that shared style also backs the horizontal-row pagers in
   cover_grid.py. 48px - between #RowPageArrow's 32px and
   #CoverflowNavArrow's 56px - lines it up with the 52px-tall "+ Add to
   jukebox" button beside it. */
QPushButton#JukeboxPageArrow {{
    background: {c['surface_alt']};
    border-radius: 24px;
    min-width: 48px;
    max-width: 48px;
    min-height: 48px;
    max-height: 48px;
    padding: 0;
    font-size: 20px;
    color: {c['text']};
}}
QPushButton#JukeboxPageArrow:hover {{ background: {c['surface_hi']}; }}
QPushButton#JukeboxPageArrow:disabled {{
    background: {c['surface']};
    color: {c['text_dim']};
}}
QPushButton#CoverflowNavArrow {{
    background: {c['surface_alt']};
    border-radius: 28px;
    min-width: 56px;
    max-width: 56px;
    min-height: 56px;
    max-height: 56px;
    padding: 0;
    font-size: 22px;
    color: {c['text']};
}}
QPushButton#CoverflowNavArrow:hover {{ background: {c['surface_hi']}; }}
QPushButton#CoverflowNavArrow:disabled {{
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
    /* Without this, the striping colour of alternating rows (the Charts
       table) comes from the desktop theme's palette, which on Linux Mint is
       a light one - every other row showed near-white. */
    alternate-background-color: {c['surface_alt']};
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
/* TrackDetailsTable (Title Details) is the one QTableView in the app.
   Without this, a selected cell falls through to the platform style's
   default (thin per-cell border lines, not a filled background), since
   QTableView isn't in the shared rule above. Deliberately no matching
   QTableView::item rule (border-radius/margin, the way list and tree items
   get one): the whole row should read as one solid highlighted bar, and a
   per-cell margin/radius would break it into separate chips. */
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
    /* Without this, a selected nested row (anything at indentation level
       1+ - a subfolder, a chart's edition) shows the platform's native
       highlight blue in its indent/branch strip, since the plain
       QTreeWidget::branch rule above only covers the unselected state.
       Matching it to the row's own selection colour keeps the whole row
       reading as one highlight. */
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
/* jukebox_key_hi, not jukebox_key: a small bar with no text on it, being
   dragged, needs to read clearly against the dark background (see the
   colour roles above). */
QScrollBar::handle:vertical:pressed {{ background: {c['jukebox_key_hi']}; }}

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
QScrollBar::handle:horizontal:pressed {{ background: {c['jukebox_key_hi']}; }}  /* see the vertical rule's comment above */

/* no arrow buttons - they are far too small to hit and just steal track space */
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ---------------- inputs ---------------- */
/* selection-background-color pairs with the default (near-white) text
   colour, like a filled button, so it takes jukebox_key as #Primary's
   background does; the focus border is a thin ring on the dark surface_alt
   background with no text of its own, so it takes jukebox_key_hi like the
   scrollbar's pressed handle above. */
QLineEdit, QComboBox, QSpinBox, QDateEdit {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 0 16px;
    min-height: {t['button_height'] - 12}px;
    selection-background-color: {c['jukebox_key']};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {c['jukebox_key_hi']}; }}
QComboBox::drop-down {{ width: 40px; border: none; }}
QComboBox QAbstractItemView {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    selection-background-color: {c['jukebox_key']};
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
    /* The seek/scrub bar fill, used by every slider in the app. */
    background: {c['jukebox_key']};
    border-radius: 4px;
}}
QSlider::handle:horizontal {{
    background: #ffffff;
    width: 24px;
    height: 24px;
    margin: -9px 0;
    border-radius: 12px;
}}
/* The player bar's volume slider (player_bar.py sets the "VolumeSlider"
   object name). The same colour as the generic slider fill above; kept as
   its own rule so the volume control can be styled apart. */
QSlider#VolumeSlider::sub-page:horizontal {{
    background: {c['jukebox_key']};
    border-radius: 4px;
}}

/* ---------------- jukebox ---------------- */
QLabel#JukeboxNowPlayingText {{
    /* The jukebox key palette's brighter tone: jukebox_key itself reads
       too dark for body text at this size. */
    color: {c['jukebox_key_hi']};
    font-weight: 700;
    font-size: {t['font_base'] + 2}px;
}}
QFrame#JukeboxStrip {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {c['jukebox_chrome_light']},
        stop:0.5 {c['jukebox_chrome_dark']},
        stop:1 {c['jukebox_chrome_light']});
    border: 2px solid {c['jukebox_strip_border']};
    border-radius: 16px;
}}
/* A card being dragged over (drag-and-drop reordering,
   ui/widgets/jukebox_strip.py) highlights its border in the same
   jukebox_key_hi used for the currently-playing banner on this page, so a
   valid drop target reads clearly without a second, unrelated colour.
   JukeboxStripWidget toggles this dynamic property on `card` (not on
   itself) via unpolish/polish - see `_set_drop_highlight`. */
QFrame#JukeboxStrip[dropTarget="true"] {{
    border: 3px solid {c['jukebox_key_hi']};
}}
/* The card's inside is the two painted _ChevronBanner buttons plus the
   artist badge below - both draw their own look in code, so there's no QSS
   for them. */
QLabel#JukeboxArtistBadge {{
    background: {c['jukebox_paper']};
    color: {c['jukebox_ink']};
    /* Kept to a thin border and padding so the artist-badge row stays
       short, along with the rest of the card. */
    border: 1px solid {c['jukebox_ink']};
    border-radius: 4px;
    padding: 0px 10px;
    font-weight: 700;
    font-size: {t['font_base'] - 2}px;
}}
/* `_build_artist_row`'s `row` container (ui/widgets/jukebox_strip.py) is
   a bare QWidget with no rule of its own, so it would inherit the blanket
   QWidget background rule above and paint near-black blocks at both ends of
   the artist badge. A transparent background lets the card's own light
   chrome gradient (QFrame#JukeboxStrip) show through, as it does beside the
   two chevron banners. */
QWidget#JukeboxArtistRow {{
    background: transparent;
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
/* Collapsible groups (ui/widgets/common.py CollapsibleSection) - the
   Settings page's folding headers. */
QFrame#SectionHeader {{
    background: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QFrame#SectionHeader:hover {{ background: {c['surface_hi']}; }}
QLabel#SectionTitle {{
    font-size: {t['font_base'] + 2}px;
    font-weight: 600;
    background: transparent;
}}
QLabel#SectionChevron {{
    color: {c['jukebox_key_hi']};
    font-size: {t['font_base'] + 2}px;
    background: transparent;
}}
QProgressBar {{
    background: {c['surface_hi']};
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: transparent;
}}
/* Charts' progress bar and the release panel's lyrics-download bar,
   among others. */
QProgressBar::chunk {{ background: {c['jukebox_key']}; border-radius: 6px; }}
/* Settings page's scan/move progress bar. The same colour as the generic
   rule above; kept as its own object name so it can be styled apart. */
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


def make_compact(button):
    """Shorter button (TOUCH["compact_button_height"], 40px vs the usual
    52px) for pages where vertical space matters more than a jukebox-panel-
    sized touch target (the artist page's Play everything, say). A dynamic
    property rather than another objectName, so it layers over #Primary
    instead of replacing it (see the `QPushButton[compact="true"]` rule in
    stylesheet()). Returns the button for inline use."""
    button.setProperty("compact", True)
    button.setMinimumHeight(TOUCH["compact_button_height"])
    return button
