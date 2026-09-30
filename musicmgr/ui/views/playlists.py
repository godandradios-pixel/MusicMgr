"""Playlist browser: manual lists, smart rules, and frozen chart snapshots,
organised into folders.

2026-09-26: shown as a drill-down tile grid (folders and playlists as
tappable artwork tiles, a breadcrumb to walk back out) rather than the
MusicBee-style folder tree it started as - see PlaylistsView's docstring.
"""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import config
from ...db.models import Playlist, PlaylistFolder
from ...services import playlists as pl_svc
from ...services import tracknum
from ...services.library import format_duration
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import (
    Breadcrumb,
    EmptyState,
    FolderPickerDialog,
    TouchButton,
    TouchList,
    dim_label,
)
from .jukebox import JukeboxPickerDialog
from ..widgets.playlist_grid import (
    KIND_FOLDER,
    KIND_PLAYLIST,
    PlaylistGrid,
    PlaylistTile,
    tile_pixmap,
)
from .base import BaseView

KIND_LABEL = {
    Playlist.KIND_MANUAL: "Manual",
    Playlist.KIND_SMART: "Smart",
    Playlist.KIND_CHART: "Chart snapshot",
    Playlist.KIND_PLAYBACK: "Most Played",
}


# --------------------------------------------------------------------------
# smart playlist rule schema (mirrors services/playlists.py:_rule_clause -
# every (field, op) pairing offered below is one `_rule_clause` actually
# implements, nothing more, so the form built from these tables can never
# produce a spec `resolve_smart` would reject)
# --------------------------------------------------------------------------

_FIELD_LABELS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("comment", "Comment"),
    ("genre", "Genre"),
    ("style", "Style"),
    ("year", "Year"),
    ("play_count", "Play count"),
    ("rating", "Rating"),
    ("bpm", "BPM"),
    ("duration_ms", "Duration"),
    ("played_within_days", "Played within"),
    ("not_played_within_days", "Not played within"),
]

#: which kind of value control a field needs. "duration" is its own kind
#: rather than lumped in with "int" - the column it maps to
#: (Track.duration_ms) is milliseconds, but nobody thinks in milliseconds,
#: so this kind's spin boxes show/accept seconds and _RuleRow converts at
#: the edges (see _rule_kind_widgets/to_dict/set_from_dict).
_FIELD_KIND = {
    "title": "text", "artist": "text", "album": "text", "comment": "text",
    "genre": "tag", "style": "tag",
    "year": "int", "play_count": "int", "rating": "int", "bpm": "int",
    "duration_ms": "duration",
    "played_within_days": "days", "not_played_within_days": "days",
}

#: per-field spin box range for the "int"/"duration" kinds (seconds for
#: duration, see above) - just enough to keep a stray keystroke from
#: producing an absurd value, not a hard domain limit.
_INT_RANGE = {
    "year": (1900, 2100),
    "play_count": (0, 100000),
    "rating": (0, 5),
    "bpm": (0, 400),
    "duration_ms": (0, 36000),  # 10 hours, in seconds
}

#: small unit hints on the spin box itself so "3" reads as "3 plays" rather
#: than needing the field label to carry that on its own - empty string
#: for fields (year) that are self-explanatory without one. duration_ms's
#: is handled separately in _rebuild_value_widgets since it's the one
#: field where the *kind* (not just the field) picks the suffix.
_INT_SUFFIX = {
    "play_count": " plays",
    "rating": " / 5",
    "bpm": " bpm",
}

_OPS_BY_KIND = {
    "text": [("is", "is"), ("contains", "contains"), ("startswith", "starts with")],
    #: "not" isn't a documented op in `_rule_clause` - anything outside
    #: {"is", "eq", "contains"} falls through to its `else ~clause` branch,
    #: which is exactly "is not"; "not" just names that branch explicitly
    #: for this dropdown rather than reusing a word that would otherwise
    #: read as a positive match.
    "tag": [("is", "is"), ("not", "is not")],
    "int": [
        ("is", "is"), ("gte", "at least"), ("lte", "at most"),
        ("gt", "more than"), ("lt", "less than"), ("between", "between"),
    ],
    #: same op set as "int" - a duration rule is still a numeric comparison,
    #: just in seconds instead of raw milliseconds.
    "duration": [
        ("is", "is"), ("gte", "at least"), ("lte", "at most"),
        ("gt", "more than"), ("lt", "less than"), ("between", "between"),
    ],
    #: `_rule_clause` never reads `op` for these two fields at all - one
    #: choice keeps every row the same shape rather than special-casing
    #: the op dropdown away entirely.
    "days": [("is", "is")],
}

_ORDER_BY_LABELS = [
    ("title", "Title (A-Z)"),
    ("artist", "Artist (A-Z)"),
    ("added_desc", "Date added (newest first)"),
    ("play_count_desc", "Play count (most first)"),
    ("last_played_desc", "Last played (most recent first)"),
    ("rating_desc", "Rating (highest first)"),
    #: James: "I need an option where it can be manual so that I can sort
    #: them by ranking of the chart" - not a *manual*, drag-to-reorder
    #: order (a smart playlist has no stored position for anything, it
    #: always resolves fresh from these rules - only a KIND_MANUAL
    #: playlist's PlaylistItem.position has that), but the effect he's
    #: after: a chart-tagged playlist (built via "Comment contains
    #: [Billboard]"-style rules) reading in the same countdown order the
    #: chart itself lists it in. See resolve_smart's own "chart_rank"
    #: comment in services/playlists.py for how ties/unmatched tracks
    #: are handled.
    ("chart_rank", "Chart ranking"),
    ("random", "Random"),
]


class _RuleRow(QWidget):
    """One "field / op / value" line in SmartPlaylistDialog's rule list.

    field/op pick from `_FIELD_LABELS`/`_OPS_BY_KIND` above; the value
    control(s) swap to match - a text box, a "days" spin box, or (for
    "between") two spin boxes with an "and" between them - same shape as
    MusicBee's own auto-playlist row, minus its "[.]" regex toggle (this
    app's rule matching has no regex mode to toggle).
    """

    removeRequested = Signal(object)  # self
    addRequested = Signal(object)  # self - a new blank row belongs after this one

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value_widgets: list[QWidget] = []

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.field_combo = QComboBox()
        for key, label in _FIELD_LABELS:
            self.field_combo.addItem(label, key)
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        row.addWidget(self.field_combo)

        self.op_combo = QComboBox()
        self.op_combo.currentIndexChanged.connect(self._on_op_changed)
        row.addWidget(self.op_combo)

        self.value_box = QWidget()
        self.value_layout = QHBoxLayout(self.value_box)
        self.value_layout.setContentsMargins(0, 0, 0, 0)
        self.value_layout.setSpacing(6)
        row.addWidget(self.value_box, 1)

        remove_btn = TouchButton("−")
        remove_btn.setFixedWidth(44)
        remove_btn.clicked.connect(lambda: self.removeRequested.emit(self))
        row.addWidget(remove_btn)

        add_btn = TouchButton("+")
        add_btn.setFixedWidth(44)
        add_btn.clicked.connect(lambda: self.addRequested.emit(self))
        row.addWidget(add_btn)

        self._on_field_changed()  # populate op_combo + value widgets for the initial field

    def _kind(self) -> str:
        return _FIELD_KIND[self.field_combo.currentData()]

    def _on_field_changed(self, *_args) -> None:
        kind = self._kind()
        self.op_combo.blockSignals(True)
        self.op_combo.clear()
        for key, label in _OPS_BY_KIND[kind]:
            self.op_combo.addItem(label, key)
        self.op_combo.setVisible(kind != "days")
        self.op_combo.blockSignals(False)
        self._rebuild_value_widgets()

    def _on_op_changed(self, *_args) -> None:
        self._rebuild_value_widgets()

    def _rebuild_value_widgets(self) -> None:
        while self.value_layout.count():
            item = self.value_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._value_widgets = []

        kind = self._kind()
        op = self.op_combo.currentData()

        if kind in ("text", "tag"):
            edit = QLineEdit()
            self.value_layout.addWidget(edit)
            self._value_widgets = [edit]
        elif kind == "days":
            spin = QSpinBox()
            spin.setRange(0, 36500)
            spin.setSuffix(" days")
            self.value_layout.addWidget(spin)
            self._value_widgets = [spin]
        elif kind in ("int", "duration"):
            field = self.field_combo.currentData()
            lo, hi = _INT_RANGE[field]
            suffix = " sec" if kind == "duration" else _INT_SUFFIX.get(field, "")
            spin = QSpinBox()
            spin.setRange(lo, hi)
            spin.setSuffix(suffix)
            self.value_layout.addWidget(spin)
            self._value_widgets = [spin]
            if op == "between":
                spin2 = QSpinBox()
                spin2.setRange(lo, hi)
                spin2.setSuffix(suffix)
                self.value_layout.addWidget(dim_label("and"))
                self.value_layout.addWidget(spin2)
                self._value_widgets.append(spin2)

    def is_blank(self) -> bool:
        """True for a text/tag row with nothing typed in yet - the state
        an extra row lands in right after "+" is clicked. Never true for
        a numeric row: a spin box always holds a real value (even 0, which
        can be a meaningful rule - "play_count is 0" - not an empty one)."""
        kind = self._kind()
        if kind in ("text", "tag"):
            return not self._value_widgets[0].text().strip()
        return False

    def to_dict(self) -> dict:
        field = self.field_combo.currentData()
        kind = self._kind()
        if kind == "days":
            return {"field": field, "op": "is", "value": self._value_widgets[0].value()}
        op = self.op_combo.currentData()
        if kind in ("text", "tag"):
            return {"field": field, "op": op, "value": self._value_widgets[0].text().strip()}
        # int/duration
        scale = 1000 if kind == "duration" else 1
        if op == "between":
            lo = self._value_widgets[0].value() * scale
            hi = self._value_widgets[1].value() * scale
            return {"field": field, "op": op, "value": sorted((lo, hi))}
        return {"field": field, "op": op, "value": self._value_widgets[0].value() * scale}

    def set_from_dict(self, d: dict) -> None:
        field = d.get("field", "title")
        idx = self.field_combo.findData(field)
        self.field_combo.setCurrentIndex(idx if idx >= 0 else 0)
        kind = self._kind()

        op = d.get("op", "is")
        if kind != "days":
            op_idx = self.op_combo.findData(op)
            self.op_combo.setCurrentIndex(op_idx if op_idx >= 0 else 0)

        value = d.get("value")
        if kind in ("text", "tag"):
            self._value_widgets[0].setText("" if value is None else str(value))
        elif kind == "days":
            self._value_widgets[0].setValue(int(value) if value is not None else 0)
        else:  # int/duration
            scale = 1000 if kind == "duration" else 1
            if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
                self._value_widgets[0].setValue(int(value[0]) // scale)
                self._value_widgets[1].setValue(int(value[1]) // scale)
            else:
                self._value_widgets[0].setValue(int(value) // scale if value is not None else 0)


class SmartPlaylistDialog(QDialog):
    """Smart playlist rule builder - a row of field/op/value dropdowns per
    rule, with "+"/"-" to grow or shrink the rule list, modeled on
    MusicBee's own Auto-Playlist editor (James: "that scripting window is
    confusing", pointing at the raw-JSON box this replaces). Reads and
    writes the exact same JSON spec services/playlists.py has always
    stored and resolved (see that module's own docstring) - only how it's
    edited changed, so the three built-in smart playlists (and anything
    saved through the old JSON box) still load and edit here unchanged.

    Unlike the JSON box, this can never produce an invalid spec - every
    field/op/value combination the form offers is one `_rule_clause`
    already implements (see the schema tables above) - so there's nothing
    left for services/playlists.py's own validation to actually reject.
    """

    def __init__(self, parent=None, name: str = "", rules: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Smart playlist")
        self.setMinimumSize(700, 560)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        form.addRow("Name", self.name_edit)
        layout.addLayout(form)

        match_row = QHBoxLayout()
        match_row.addWidget(dim_label("match"))
        self.match_combo = QComboBox()
        self.match_combo.addItem("all", "all")
        self.match_combo.addItem("any", "any")
        match_row.addWidget(self.match_combo)
        match_row.addWidget(dim_label("of the following rules:"))
        match_row.addStretch(1)
        layout.addLayout(match_row)

        self._rows_box = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_box)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        self._rows: list[_RuleRow] = []

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._rows_box)
        layout.addWidget(scroll, 1)

        limit_row = QHBoxLayout()
        self.limit_check = QCheckBox("limit to")
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 100000)
        self.limit_spin.setValue(100)
        self.limit_spin.setEnabled(False)
        self.limit_check.toggled.connect(self.limit_spin.setEnabled)
        limit_row.addWidget(self.limit_check)
        limit_row.addWidget(self.limit_spin)
        limit_row.addWidget(dim_label("tracks"))
        limit_row.addStretch(1)
        layout.addLayout(limit_row)

        order_row = QHBoxLayout()
        order_row.addWidget(dim_label("order by"))
        self.order_combo = QComboBox()
        for key, label in _ORDER_BY_LABELS:
            self.order_combo.addItem(label, key)
        order_row.addWidget(self.order_combo)
        order_row.addStretch(1)
        layout.addLayout(order_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load(json.loads(rules) if rules else {})

    def _load(self, spec: dict) -> None:
        match_idx = self.match_combo.findData(spec.get("match", "all"))
        self.match_combo.setCurrentIndex(match_idx if match_idx >= 0 else 0)

        for rule in spec.get("rules") or [{}]:
            self._add_row(rule)

        limit = spec.get("limit")
        if limit is not None:
            self.limit_check.setChecked(True)
            self.limit_spin.setValue(int(limit))

        order_idx = self.order_combo.findData(spec.get("order_by", "title"))
        self.order_combo.setCurrentIndex(order_idx if order_idx >= 0 else 0)

    def _add_row(self, initial: dict, after: Optional[_RuleRow] = None) -> None:
        row = _RuleRow()
        row.removeRequested.connect(self._remove_row)
        row.addRequested.connect(self._insert_row_after)
        if initial:
            row.set_from_dict(initial)
        if after is None:
            self._rows_layout.addWidget(row)
            self._rows.append(row)
        else:
            pos = self._rows.index(after) + 1
            self._rows_layout.insertWidget(pos, row)
            self._rows.insert(pos, row)

    def _insert_row_after(self, row: _RuleRow) -> None:
        self._add_row({}, after=row)

    def _remove_row(self, row: _RuleRow) -> None:
        if len(self._rows) <= 1:
            # Always keep at least one row rather than letting the list go
            # empty - an empty `rules: []` already means "match everything"
            # to resolve_smart, and a blank row (see is_blank/values below)
            # gets you exactly that, so there's no missing capability here,
            # just one row that never disappears.
            return
        self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    def values(self) -> tuple[str, dict]:
        spec = {
            "match": self.match_combo.currentData(),
            "order_by": self.order_combo.currentData(),
            "rules": [row.to_dict() for row in self._rows if not row.is_blank()],
        }
        if self.limit_check.isChecked():
            spec["limit"] = self.limit_spin.value()
        return self.name_edit.text().strip(), spec


#: touch-sized rows for the tile menu (long-press / right-click) - the
#: default QMenu row is a mouse-sized ~24px
_TOUCH_MENU_STYLE = "QMenu::item { padding: 14px 32px; font-size: 16px; }"


class PlaylistsView(BaseView):
    """Playlists as a drill-down tile grid - 2026-09-26 (James: "I would
    like playlist to have larger touch areas like Artist, Album or
    Jukebox... any ideas of how we can have a better touch screen interface
    for Playlists"). Replaces the MusicBee-style folder tree + side panel.

    Two pages in `self.stack`:
      * browse - one folder level as tiles (`PlaylistGrid`): subfolders
        first, then playlists. Tapping a folder drills into it; the
        breadcrumb above walks back out ("Playlists › Billboard Hot 100 ›
        2020-29"), so nesting depth never costs screen space.
      * playlist - a full-width page for one playlist, like the album
        page: artwork, name, big Play / Shuffle / Queue, then the tracks.

    Rename / Move / Choose image / Delete live on each tile's menu (long
    press or right-click) and on the playlist page's "More…" button,
    instead of the old fixed button row under the tree. New playlists and
    folders are created inside whichever folder is showing.

    See ui/widgets/playlist_grid.py for how tile artwork is chosen (a
    chosen image, else a 2x2 mosaic of album covers, else a placeholder).
    """

    title_text = "Playlists"

    PAGE_BROWSE, PAGE_PLAYLIST = 0, 1

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        #: the folder the grid is showing (None = top level)
        self._folder_id: Optional[int] = None
        #: the playlist page's playlist, or None while browsing
        self._playlist_id: Optional[int] = None
        self._tracks: list = []
        #: index-matched to the breadcrumb's crumbs (see _set_breadcrumb)
        self._crumb_actions: list = []
        #: [(folder_id, name), ...] root-first, for the folder showing
        self._chain: list[tuple[int, str]] = []

        new_btn = TouchButton("New playlist", primary=True)
        new_btn.clicked.connect(self.create_manual)
        smart_btn = TouchButton("New smart playlist")
        smart_btn.clicked.connect(self.create_smart)
        folder_btn = TouchButton("New folder")
        folder_btn.clicked.connect(self.create_folder)
        self.header.addWidget(new_btn)
        self.header.addWidget(smart_btn)
        self.header.addWidget(folder_btn)

        self.breadcrumb = Breadcrumb()
        self.breadcrumb.crumbActivated.connect(self._on_crumb)
        self.body().addWidget(self.breadcrumb)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_browse_page())  # PAGE_BROWSE
        self.stack.addWidget(self._build_playlist_page())  # PAGE_PLAYLIST
        self.body().addWidget(self.stack, 1)

        ctx.playlistsChanged.connect(self.refresh)
        ctx.libraryChanged.connect(self.refresh)

    # -- construction ------------------------------------------------------

    def _build_browse_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.grid = PlaylistGrid()
        self.grid.tileActivated.connect(self._on_tile_activated)
        self.grid.contextRequested.connect(self._on_tile_context)
        self.grid_empty = EmptyState(
            "This folder is empty",
            "Use New playlist or New folder above to add something here.",
        )
        self.browse_stack = QStackedWidget()
        self.browse_stack.addWidget(self.grid)  # 0
        self.browse_stack.addWidget(self.grid_empty)  # 1
        layout.addWidget(self.browse_stack, 1)
        return page

    def _build_playlist_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(18)
        self.detail_art = QLabel()
        self.detail_art.setFixedSize(DETAIL_ART, DETAIL_ART)
        head.addWidget(self.detail_art, 0, Qt.AlignTop)

        meta = QVBoxLayout()
        meta.setSpacing(4)
        self.detail_title = QLabel("")
        self.detail_title.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.detail_title.setWordWrap(True)
        meta.addWidget(self.detail_title)
        self.detail_meta = dim_label("")
        meta.addWidget(self.detail_meta)
        self.detail_desc = QLabel("")
        self.detail_desc.setObjectName("Subtitle")
        self.detail_desc.setWordWrap(True)
        meta.addWidget(self.detail_desc)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.play_btn = TouchButton("Play", primary=True)
        self.play_btn.clicked.connect(lambda: self._play(0))
        self.shuffle_btn = TouchButton("Shuffle")
        self.shuffle_btn.clicked.connect(self._shuffle)
        self.queue_btn = TouchButton("Queue")
        self.queue_btn.clicked.connect(lambda: self.ctx.enqueue_tracks(self._tracks))
        self.edit_btn = TouchButton("Edit rules")
        self.edit_btn.clicked.connect(self.edit_smart)
        # 2026-09-27 - James: "how can I edit a playlist by replacing a
        # song with a search for another song" / "please build both". Both
        # open the Jukebox's artist + title picker (JukeboxPickerDialog,
        # genre box hidden) over pl_svc.search_tracks.
        self.add_btn = TouchButton("Add songs…")
        self.add_btn.clicked.connect(self._add_songs)
        self.replace_btn = TouchButton("Replace song…")
        self.replace_btn.clicked.connect(self._replace_selected)
        self.remove_btn = TouchButton("Remove track")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.more_btn = TouchButton("More…")
        self.more_btn.clicked.connect(self._on_more_clicked)
        for b in (self.play_btn, self.shuffle_btn, self.queue_btn, self.edit_btn,
                  self.add_btn, self.replace_btn, self.remove_btn, self.more_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        meta.addSpacing(6)
        meta.addLayout(actions)
        head.addLayout(meta, 1)
        layout.addLayout(head)

        self.track_list = TouchList()
        self.track_list.itemActivatedPayload.connect(self._on_track_tapped)
        layout.addWidget(self.track_list, 1)
        return page

    # -- loading -------------------------------------------------------------

    def refresh(self) -> None:
        cache: dict[int, list] = {}
        with self.ctx.session() as session:
            pl_svc.ensure_default_playlists(session)
            pl_svc.ensure_builtin_playback_playlists(session)
            folders = pl_svc.list_folders(session)
            playlists = pl_svc.list_playlists(session)

            if self._folder_id is not None and self._folder_id not in {f.id for f in folders}:
                self._folder_id = None
            if self._playlist_id is not None and self._playlist_id not in {p.id for p in playlists}:
                self._playlist_id = None

            tiles = self._build_tiles(session, folders, playlists, cache)
            chain = [(f.id, f.name) for f in pl_svc.folder_path(session, self._folder_id)]

        self.grid.set_tiles(tiles)
        self.browse_stack.setCurrentIndex(0 if tiles else 1)
        self._chain = chain
        if self._playlist_id is not None:
            self._load_playlist_detail(self._playlist_id)
            self.stack.setCurrentIndex(self.PAGE_PLAYLIST)
        else:
            self._tracks = []
            self.stack.setCurrentIndex(self.PAGE_BROWSE)
            self._set_breadcrumb()

    def _tracks_for(self, session, playlist_id: int, cache: dict) -> list:
        if playlist_id not in cache:
            cache[playlist_id] = pl_svc.playlist_tracks(session, playlist_id)
        return cache[playlist_id]

    def _build_tiles(self, session, folders, playlists, cache) -> list[PlaylistTile]:
        """Tiles for the folder currently showing: its subfolders (A-Z),
        then its playlists (pinned first, then A-Z - the tree's old order)."""
        by_parent: dict[Optional[int], list[PlaylistFolder]] = {}
        for folder in folders:
            by_parent.setdefault(folder.parent_id, []).append(folder)
        playlists_by_folder: dict[Optional[int], list[Playlist]] = {}
        for playlist in playlists:
            playlists_by_folder.setdefault(playlist.folder_id, []).append(playlist)

        tiles: list[PlaylistTile] = []
        for folder in sorted(by_parent.get(self._folder_id, []), key=lambda f: f.name.lower()):
            n_lists = len(playlists_by_folder.get(folder.id, []))
            n_sub = len(by_parent.get(folder.id, []))
            parts = []
            if n_sub:
                parts.append(f"{n_sub} folder{'s' if n_sub != 1 else ''}")
            if n_lists:
                parts.append(f"{n_lists} playlist{'s' if n_lists != 1 else ''}")
            tiles.append(PlaylistTile(
                kind=KIND_FOLDER,
                id=folder.id,
                title=folder.name,
                subtitle=" · ".join(parts) or "Empty",
                cover_paths=self._folder_covers(
                    session, folder.id, by_parent, playlists_by_folder, cache
                ),
                custom_path=folder.cover_path,
            ))
        for playlist in sorted(
            playlists_by_folder.get(self._folder_id, []),
            key=lambda p: (not p.is_pinned, p.name.lower()),
        ):
            tracks = self._tracks_for(session, playlist.id, cache)
            n = len(tracks)
            tiles.append(PlaylistTile(
                kind=KIND_PLAYLIST,
                id=playlist.id,
                title=playlist.name,
                subtitle=f"{n} song{'s' if n != 1 else ''}",
                cover_paths=pl_svc.cover_paths_for_tracks(tracks),
                custom_path=playlist.cover_path,
                playlist_kind=playlist.kind,
            ))
        return tiles

    def _folder_covers(self, session, folder_id, by_parent, playlists_by_folder, cache) -> list[str]:
        """Up to four different covers for a folder's mosaic: from the
        playlists directly inside it first, then its subfolders' (breadth
        first), stopping as soon as four are found - a folder holding a
        hundred chart snapshots only ever resolves the first few."""
        paths: list[str] = []
        queue = [folder_id]
        seen: set[int] = set()
        while queue and len(paths) < pl_svc.MOSAIC_COVERS:
            fid = queue.pop(0)
            if fid in seen:
                continue
            seen.add(fid)
            for playlist in sorted(
                playlists_by_folder.get(fid, []), key=lambda p: (not p.is_pinned, p.name.lower())
            ):
                if playlist.cover_path:
                    candidates = [playlist.cover_path]
                else:
                    candidates = pl_svc.cover_paths_for_tracks(
                        self._tracks_for(session, playlist.id, cache)
                    )
                for path in candidates:
                    if path not in paths:
                        paths.append(path)
                        if len(paths) >= pl_svc.MOSAIC_COVERS:
                            return paths
            queue.extend(
                f.id for f in sorted(by_parent.get(fid, []), key=lambda f: f.name.lower())
            )
        return paths

    def _set_breadcrumb(self, playlist_name: Optional[str] = None) -> None:
        """"Playlists › folder › subfolder [› playlist]" - hidden entirely
        at the top level, where the page title already says "Playlists"."""
        labels = ["Playlists"] + [name for _fid, name in self._chain]
        actions = [lambda: self._go_to_folder(None)] + [
            (lambda fid=fid: self._go_to_folder(fid)) for fid, _name in self._chain
        ]
        if playlist_name is not None:
            labels.append(playlist_name)
            actions.append(lambda: None)
        if len(labels) == 1:
            labels, actions = [], []
        self._crumb_actions = actions
        self.breadcrumb.set_path(labels)

    def _on_crumb(self, index: int) -> None:
        if 0 <= index < len(self._crumb_actions):
            self._crumb_actions[index]()

    def _go_to_folder(self, folder_id: Optional[int]) -> None:
        self._folder_id = folder_id
        self._playlist_id = None
        self.refresh()

    def _on_tile_activated(self, payload: dict) -> None:
        if payload.get("type") == KIND_FOLDER:
            self._go_to_folder(payload.get("id"))
        elif payload.get("type") == KIND_PLAYLIST:
            self._open_playlist(payload.get("id"))

    def _open_playlist(self, playlist_id: int) -> None:
        self._playlist_id = playlist_id
        self._load_playlist_detail(playlist_id)
        if self._playlist_id is not None:
            self.stack.setCurrentIndex(self.PAGE_PLAYLIST)

    def _current_folder_context(self) -> Optional[int]:
        """Where a newly created playlist or folder should land: the folder
        the grid is showing (the open playlist's folder, on its page)."""
        return self._folder_id

    def _load_playlist_detail(self, playlist_id: int) -> None:
        rows = []
        with self.ctx.session() as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None:
                self._playlist_id = None
                self.stack.setCurrentIndex(self.PAGE_BROWSE)
                self._set_breadcrumb()
                return
            # a playlist opened from elsewhere, or just moved, may live in a
            # different folder than the one the grid was showing
            if playlist.folder_id != self._folder_id:
                self._folder_id = playlist.folder_id
                self._chain = [
                    (f.id, f.name) for f in pl_svc.folder_path(session, self._folder_id)
                ]
            name = playlist.name
            self.detail_title.setText(name)
            self.detail_desc.setText(playlist.description or "")
            self.detail_desc.setVisible(bool(playlist.description))
            is_playback = playlist.kind == Playlist.KIND_PLAYBACK
            total = 0
            if is_playback:
                # show play counts (what "most played" is ranked by) instead
                # of duration - the same trailing info the old built-in
                # playback charts showed
                plays_by_track: dict[int, int] = {}
                tracks = []
                for track, plays, _ms in pl_svc.resolve_playback(session, playlist):
                    tracks.append(track)
                    plays_by_track[track.id] = plays
            else:
                tracks = pl_svc.playlist_tracks(session, playlist_id)
                plays_by_track = {}
            self._tracks = list(tracks)
            for idx, track in enumerate(tracks, start=1):
                total += track.duration_ms or 0
                mf = track.primary_file
                trail = (
                    f"{plays_by_track[track.id]} play{'s' if plays_by_track[track.id] != 1 else ''}"
                    if is_playback
                    else format_duration(track.duration_ms)
                )
                # album and side on every row (2026-09-30 - James's 78s:
                # "the granularity of Album and then Track #")
                detail = [track.artist_display or ""]
                if track.release is not None and track.release.title:
                    detail.append(track.release.title)
                if tracknum.is_side(track.position):
                    detail.append(f"Side {track.position}")
                rows.append({
                    "lead": str(idx),
                    "primary": track.title,
                    "secondary": " · ".join(d for d in detail if d),
                    "trail": trail,
                    "color": COLORS["text_dim"] if (mf is None or mf.is_missing)
                    else COLORS["text"],
                    "key": track.id,
                })
            self.detail_meta.setText(
                f"{len(tracks)} songs · {format_duration(total)} · "
                f"{KIND_LABEL.get(playlist.kind, playlist.kind)}"
            )
            kind = playlist.kind
            tile = PlaylistTile(
                kind=KIND_PLAYLIST,
                id=playlist.id,
                title=name,
                cover_paths=pl_svc.cover_paths_for_tracks(tracks),
                custom_path=playlist.cover_path,
                playlist_kind=kind,
            )
        self.detail_art.setPixmap(_rounded(tile_pixmap(tile, DETAIL_ART)))
        self.track_list.set_rows(rows)
        has_tracks = bool(self._tracks)
        self.play_btn.setEnabled(has_tracks)
        self.shuffle_btn.setEnabled(has_tracks)
        self.queue_btn.setEnabled(has_tracks)
        # only the actions that apply to this kind of playlist are shown at
        # all, rather than sitting there disabled
        self.edit_btn.setVisible(kind == Playlist.KIND_SMART)
        self.remove_btn.setVisible(kind == Playlist.KIND_MANUAL)
        self.add_btn.setVisible(kind == Playlist.KIND_MANUAL)
        self.replace_btn.setVisible(kind == Playlist.KIND_MANUAL)
        self.replace_btn.setEnabled(has_tracks)
        self._set_breadcrumb(name)

    # -- tile / playlist menu ------------------------------------------------

    def _on_tile_context(self, payload: dict, global_pos) -> None:
        self._build_node_menu(payload).exec(global_pos)

    def _on_more_clicked(self) -> None:
        if self._playlist_id is None:
            return
        menu = self._build_node_menu({"type": KIND_PLAYLIST, "id": self._playlist_id}, on_page=True)
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def _build_node_menu(self, payload: dict, on_page: bool = False) -> QMenu:
        """The tile menu (long press / right-click) - also the playlist
        page's "More…". Split out from exec() so tests can trigger an
        action by its text, same as the Jukebox chip/card menus."""
        node_type, node_id = payload.get("type"), payload.get("id")
        menu = QMenu(self)
        menu.setStyleSheet(_TOUCH_MENU_STYLE)
        with self.ctx.session() as session:
            if node_type == KIND_FOLDER:
                node = session.get(PlaylistFolder, node_id)
                kind = ""
            else:
                node = session.get(Playlist, node_id)
                kind = node.kind if node is not None else ""
            has_image = bool(node is not None and node.cover_path)
        if node is None:
            return menu

        if node_type == KIND_FOLDER:
            menu.addAction("Open", lambda: self._go_to_folder(node_id))
        elif not on_page:
            menu.addAction("Play", lambda: self._play_node(node_id, shuffle=False))
            menu.addAction("Shuffle", lambda: self._play_node(node_id, shuffle=True))
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self._rename(node_type, node_id))
        menu.addAction("Move to folder…", lambda: self._move(node_type, node_id))
        menu.addAction("Choose image…", lambda: self._choose_image(node_type, node_id))
        if has_image:
            menu.addAction("Use automatic image", lambda: self._clear_image(node_type, node_id))
        if node_type == KIND_PLAYLIST and kind == Playlist.KIND_SMART and not on_page:
            menu.addAction("Edit rules…", lambda: self._edit_smart_id(node_id))
        # 2026-09-30 - one record after another, A side before B side
        if node_type == KIND_FOLDER:
            menu.addAction("Sort playlists by album && track",
                           lambda: self._sort_album_track(node_type, node_id))
        elif kind == Playlist.KIND_MANUAL:
            menu.addAction("Sort by album && track",
                           lambda: self._sort_album_track(node_type, node_id))
        if not (node_type == KIND_PLAYLIST and kind == Playlist.KIND_PLAYBACK):
            menu.addSeparator()
            label = "Delete folder…" if node_type == KIND_FOLDER else "Delete playlist…"
            menu.addAction(label, lambda: self._delete(node_type, node_id))
        return menu

    def _sort_album_track(self, node_type: str, node_id: int) -> None:
        """Re-order a manual playlist - or every manual playlist in a folder
        - by album, then side/track (services/playlists.py)."""
        with self.ctx.session() as session:
            if node_type == KIND_FOLDER:
                changed, total = pl_svc.sort_folder_by_album_track(session, node_id)
                message = (f"Sorted {changed} of {total} playlist{'s' if total != 1 else ''} "
                           "by album and track" if changed else "Already in album and track order")
            else:
                changed = pl_svc.sort_playlist_by_album_track(session, node_id)
                message = "Sorted by album and track" if changed else "Already in album and track order"
        self.ctx.notify(message)
        if self._playlist_id is not None:
            self._load_playlist_detail(self._playlist_id)
        else:
            self.refresh()

    def _play_node(self, playlist_id: int, shuffle: bool) -> None:
        with self.ctx.session() as session:
            tracks = pl_svc.playlist_tracks(session, playlist_id)
        if not tracks:
            self.ctx.notify("That playlist is empty")
            return
        self.ctx.player.set_shuffle(shuffle)
        self.ctx.play_tracks(tracks, start=0, source=f"playlist:{playlist_id}")

    # -- playlist actions -------------------------------------------------------------

    def create_manual(self) -> None:
        name, ok = QInputDialog.getText(self, "New playlist", "Playlist name:")
        if not ok or not name.strip():
            return
        with self.ctx.session() as session:
            pl_svc.create_playlist(session, name.strip(), folder_id=self._current_folder_context())
        self.ctx.notify(f"Created “{name.strip()}”")
        self.refresh()

    def create_smart(self) -> None:
        dialog = SmartPlaylistDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            name, spec = dialog.values()
            if not name:
                raise ValueError("a name is required")
            with self.ctx.session() as session:
                pl_svc.create_smart_playlist(
                    session, name, spec, folder_id=self._current_folder_context()
                )
        except Exception as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.ctx.notify(f"Created smart playlist “{name}”")
        self.refresh()

    def edit_smart(self) -> None:
        if self._playlist_id is not None:
            self._edit_smart_id(self._playlist_id)

    def _edit_smart_id(self, playlist_id: int) -> None:
        with self.ctx.session() as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None or playlist.kind != Playlist.KIND_SMART:
                return
            name, rules = playlist.name, playlist.rules
        dialog = SmartPlaylistDialog(self, name=name, rules=rules)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            new_name, spec = dialog.values()
            with self.ctx.session() as session:
                pl_svc.resolve_smart(session, json.dumps(spec))
                playlist = session.get(Playlist, playlist_id)
                playlist.name = new_name or playlist.name
                playlist.rules = json.dumps(spec, indent=2)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.refresh()

    def _remove_selected(self) -> None:
        payload = self.track_list.current_payload()
        if payload is None or self._playlist_id is None:
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, self._playlist_id)
            if playlist is None or playlist.kind != Playlist.KIND_MANUAL:
                self.ctx.notify("Only manual playlists can be edited by hand")
                return
            for item in playlist.items:
                if item.track_id == payload.get("key"):
                    session.delete(item)
                    break
        self._load_playlist_detail(self._playlist_id)

    #: most songs "Add songs…" takes in one go
    ADD_MAX_PICKS = 25

    def _search_playlist_tracks(self, artist_query: str, track_query: str) -> list[dict]:
        """JukeboxPickerDialog's search callback for Add/Replace."""
        with self.ctx.session() as session:
            return pl_svc.search_tracks(session, artist_query, track_query)

    def _add_songs(self) -> None:
        """"Add songs…" - search and tick up to ADD_MAX_PICKS songs; they
        go on the end of the playlist in the order they were ticked. Songs
        already in the playlist are skipped."""
        if self._playlist_id is None:
            return
        dialog = JukeboxPickerDialog(
            self,
            self._search_playlist_tracks,
            max_picks=self.ADD_MAX_PICKS,
            title="Add songs to playlist",
            show_genre=False,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        picked = [track_id for track_id, _artist_id in dialog.selected_picks()]
        if not picked:
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, self._playlist_id)
            if playlist is None or playlist.kind != Playlist.KIND_MANUAL:
                self.ctx.notify("Only manual playlists can be edited by hand")
                return
            existing = {item.track_id for item in playlist.items}
            new_ids = [tid for tid in picked if tid not in existing]
            if new_ids:
                pl_svc.add_tracks(session, self._playlist_id, new_ids)
        skipped = len(picked) - len(new_ids)
        msg = f"Added {len(new_ids)} song{'s' if len(new_ids) != 1 else ''}"
        if skipped:
            msg += f" ({skipped} already in the playlist)"
        self.ctx.notify(msg)
        self._load_playlist_detail(self._playlist_id)

    def _replace_selected(self) -> None:
        """"Replace song…" - swap the selected song for one found by
        search, in the same spot in the list."""
        payload = self.track_list.current_payload()
        if self._playlist_id is None:
            return
        if payload is None:
            self.ctx.notify("Select the song to replace first")
            return
        old_track_id = payload.get("key")
        try:
            index = int(payload.get("lead", "0")) - 1
        except ValueError:
            index = -1
        old_title = payload.get("primary", "")
        dialog = JukeboxPickerDialog(
            self,
            self._search_playlist_tracks,
            max_picks=1,
            title=f"Replace “{old_title}”" if old_title else "Replace song",
            show_genre=False,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        picks = dialog.selected_rows()
        if not picks:
            return
        new_track_id = picks[0]["track_id"]
        new_title = picks[0]["title"]
        if new_track_id == old_track_id:
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, self._playlist_id)
            if playlist is None or playlist.kind != Playlist.KIND_MANUAL:
                self.ctx.notify("Only manual playlists can be edited by hand")
                return
            if any(item.track_id == new_track_id for item in playlist.items):
                self.ctx.notify(f"“{new_title}” is already in this playlist")
                return
            ok = pl_svc.replace_track_at(
                session, self._playlist_id, index, old_track_id, new_track_id
            )
        if not ok:
            self.ctx.notify("Couldn't find that song in the playlist any more")
        else:
            self.ctx.notify(f"Replaced with “{new_title}”")
        self._load_playlist_detail(self._playlist_id)
        # keep the replaced row selected so several swaps in a row are easy
        if 0 <= index < self.track_list.count():
            self.track_list.setCurrentRow(index)

    def _on_track_tapped(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        for idx, track in enumerate(self._tracks):
            if track.id == payload.get("key"):
                self._play(idx)
                return

    def _play(self, start: int) -> None:
        if not self._tracks:
            return
        self.ctx.player.set_shuffle(False)
        self.ctx.play_tracks(self._tracks, start=start, source=f"playlist:{self._playlist_id}")

    def _shuffle(self) -> None:
        if not self._tracks:
            return
        self.ctx.player.set_shuffle(True)
        self.ctx.play_tracks(self._tracks, start=0, source=f"playlist:{self._playlist_id}")

    # -- folder / node actions -----------------------------------------------------

    def create_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        with self.ctx.session() as session:
            pl_svc.create_folder(session, name.strip(), parent_id=self._current_folder_context())
        self.ctx.notify(f"Created folder “{name.strip()}”")
        self.refresh()

    def _rename(self, node_type: str, node_id: int) -> None:
        with self.ctx.session() as session:
            model = PlaylistFolder if node_type == KIND_FOLDER else Playlist
            node = session.get(model, node_id)
            current = node.name if node else ""
        what = "folder" if node_type == KIND_FOLDER else "playlist"
        name, ok = QInputDialog.getText(
            self, f"Rename {what}", f"{what.capitalize()} name:", text=current
        )
        if not ok or not name.strip():
            return
        with self.ctx.session() as session:
            if node_type == KIND_FOLDER:
                pl_svc.rename_folder(session, node_id, name.strip())
            else:
                pl_svc.rename_playlist(session, node_id, name.strip())
        self.refresh()

    def _move(self, node_type: str, node_id: int) -> None:
        with self.ctx.session() as session:
            folders = pl_svc.list_folders(session)
            if node_type == KIND_FOLDER:
                folder = session.get(PlaylistFolder, node_id)
                if folder is None:
                    return
                current_folder_id = folder.parent_id
                exclude_ids = pl_svc.folder_and_descendant_ids(session, node_id)
            else:
                playlist = session.get(Playlist, node_id)
                if playlist is None:
                    return
                current_folder_id = playlist.folder_id
                exclude_ids = None

        dialog = FolderPickerDialog(self, folders, current_folder_id, exclude_ids)
        if dialog.exec() != QDialog.Accepted:
            return
        target_id = dialog.selected_folder_id()
        with self.ctx.session() as session:
            if node_type == KIND_FOLDER:
                try:
                    pl_svc.move_folder(session, node_id, target_id)
                except ValueError as exc:
                    QMessageBox.warning(self, "Can't move folder", str(exc))
                    return
            else:
                pl_svc.move_playlist(session, node_id, target_id)
        self.refresh()

    def _choose_image(self, node_type: str, node_id: int) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(config.IMAGE_EXTENSIONS))
        what = "folder" if node_type == KIND_FOLDER else "playlist"
        file_path, _ = QFileDialog.getOpenFileName(
            self, f"Choose an image for this {what}", "", f"Images ({patterns})"
        )
        if not file_path:
            return
        self._set_image(node_type, node_id, file_path)

    def _clear_image(self, node_type: str, node_id: int) -> None:
        self._set_image(node_type, node_id, None)

    def _set_image(self, node_type: str, node_id: int, file_path: Optional[str]) -> None:
        try:
            with self.ctx.session() as session:
                if node_type == KIND_FOLDER:
                    pl_svc.set_folder_image(session, node_id, file_path)
                else:
                    pl_svc.set_playlist_image(session, node_id, file_path)
        except ValueError as exc:
            self.ctx.notify(f"Couldn't use that image: {exc}")
            return
        self.refresh()

    def _delete(self, node_type: str, node_id: int) -> None:
        if node_type == KIND_FOLDER:
            confirm = QMessageBox.question(
                self,
                "Delete folder",
                "Delete this folder? Playlists and subfolders inside it move up "
                "to the level above - nothing inside is deleted.",
            )
            if confirm != QMessageBox.Yes:
                return
            with self.ctx.session() as session:
                pl_svc.delete_folder(session, node_id)
            self.refresh()
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, node_id)
            if playlist is not None and playlist.kind == Playlist.KIND_PLAYBACK:
                self.ctx.notify(
                    "Built-in playback playlists can't be deleted - move "
                    "them into a folder instead"
                )
                return
        confirm = QMessageBox.question(
            self, "Delete playlist", "Delete this playlist? Tracks stay in your library."
        )
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, node_id)
            if playlist is not None:
                session.delete(playlist)
        if self._playlist_id == node_id:
            self._playlist_id = None
        self.refresh()


#: the playlist page's artwork edge, px
DETAIL_ART = 140


def _rounded(pix: QPixmap, radius: int = 12) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pix.width(), pix.height(), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out
