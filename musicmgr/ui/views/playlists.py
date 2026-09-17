"""Playlist browser: manual lists, smart rules, and frozen chart snapshots -
organised into a folder tree, the same shape MusicBee's playlist pane uses.
"""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...db.models import Playlist, PlaylistFolder
from ...services import playlists as pl_svc
from ...services.library import format_duration
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import FolderPickerDialog, TouchButton, TouchList, TouchTree, dim_label
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


class PlaylistsView(BaseView):
    title_text = "Playlists"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._selected_type: Optional[str] = None  # "folder" | "playlist"
        self._selected_id: Optional[int] = None
        self._expanded_folder_ids: set[int] = set()
        self._tracks: list = []

        new_btn = TouchButton("New playlist", primary=True)
        new_btn.clicked.connect(self.create_manual)
        smart_btn = TouchButton("New smart playlist")
        smart_btn.clicked.connect(self.create_smart)
        folder_btn = TouchButton("New folder")
        folder_btn.clicked.connect(self.create_folder)
        self.header.addWidget(new_btn)
        self.header.addWidget(smart_btn)
        self.header.addWidget(folder_btn)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        lbl = QLabel("Your playlists")
        lbl.setObjectName("Crumb")
        ll.addWidget(lbl)
        self.playlist_tree = TouchTree()
        self.playlist_tree.itemActivatedPayload.connect(self._on_node_selected)
        self.playlist_tree.itemExpanded.connect(self._on_expand_changed)
        self.playlist_tree.itemCollapsed.connect(self._on_expand_changed)
        ll.addWidget(self.playlist_tree, 1)

        tree_actions = QHBoxLayout()
        tree_actions.setSpacing(8)
        rename_btn = TouchButton("Rename")
        rename_btn.clicked.connect(self.rename_selected)
        move_btn = TouchButton("Move to folder…")
        move_btn.clicked.connect(self.move_selected)
        delete_node_btn = TouchButton("Delete")
        delete_node_btn.clicked.connect(self.delete_selected)
        for b in (rename_btn, move_btn, delete_node_btn):
            tree_actions.addWidget(b)
        ll.addLayout(tree_actions)
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        head = QHBoxLayout()
        self.detail_title = QLabel("Select a playlist")
        self.detail_title.setStyleSheet("font-size: 19px; font-weight: 600;")
        head.addWidget(self.detail_title)
        head.addStretch(1)
        self.detail_meta = dim_label("")
        head.addWidget(self.detail_meta)
        rl.addLayout(head)

        self.detail_desc = QLabel("")
        self.detail_desc.setObjectName("Subtitle")
        self.detail_desc.setWordWrap(True)
        rl.addWidget(self.detail_desc)

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
        self.remove_btn = TouchButton("Remove track")
        self.remove_btn.clicked.connect(self._remove_selected)
        for b in (self.play_btn, self.shuffle_btn, self.queue_btn, self.edit_btn, self.remove_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        rl.addLayout(actions)

        self.track_list = TouchList()
        self.track_list.itemActivatedPayload.connect(self._on_track_tapped)
        rl.addWidget(self.track_list, 1)
        splitter.addWidget(right)
        # James: give the "Your playlists" column more room - the built-in
        # "Most Played - This Week"/"Most Played - This Month" names (see
        # services/playlists.py's MOST_PLAYED_DEFS) were the longest
        # leaves in this tree and didn't fit 340px next to TouchTree's own
        # fixed 56px count column, eliding down to "Most Played - T..."
        # for both.
        #
        # 2026-09-17 follow-up (James: still truncated at 420px) - "Most
        # Played - All Time" (23 chars) fit there, but the two-chars-
        # longer "This Week"/"This Month" variants didn't quite, by a
        # handful of pixels this sandbox can't measure directly (no way
        # to run the real Qt layout here - see the module's other verify-
        # by-in-memory-DB workarounds). Rather than nudge the number again
        # and risk the same near-miss, 500px leaves real headroom past the
        # longest current name instead of just clearing it. Kept the same
        # 1240 total so the right-hand track list doesn't end up any
        # narrower than before - just a differently split QSplitter
        # (still user-draggable further either way, via the handle
        # between the two panes).
        splitter.setSizes([500, 740])
        self.body().addWidget(splitter, 1)

        ctx.playlistsChanged.connect(self.refresh)
        ctx.libraryChanged.connect(self.refresh)

    # -- loading -------------------------------------------------------------

    def refresh(self) -> None:
        playlists_by_folder: dict[Optional[int], list[Playlist]] = {}
        track_counts: dict[int, int] = {}
        with self.ctx.session() as session:
            pl_svc.ensure_default_playlists(session)
            pl_svc.ensure_builtin_playback_playlists(session)
            folders = pl_svc.list_folders(session)
            playlists = pl_svc.list_playlists(session)
            for playlist in playlists:
                playlists_by_folder.setdefault(playlist.folder_id, []).append(playlist)
                track_counts[playlist.id] = len(pl_svc.playlist_tracks(session, playlist.id))

        self.playlist_tree.blockSignals(True)
        self.playlist_tree.clear()
        self._add_tree_level(self.playlist_tree, None, folders, playlists_by_folder, track_counts)
        self.playlist_tree.blockSignals(False)

        folder_ids = {f.id for f in folders}
        playlist_ids = {p.id for p in playlists}
        still_there = (
            (self._selected_type == "folder" and self._selected_id in folder_ids)
            or (self._selected_type == "playlist" and self._selected_id in playlist_ids)
        )
        if still_there:
            found = self.playlist_tree.select_node(
                lambda n: n.get("type") == self._selected_type
                and n.get("id") == self._selected_id
            )
        else:
            found = self.playlist_tree.select_node(lambda n: n.get("type") == "playlist")
            if not found:
                found = self.playlist_tree.select_node(lambda n: n.get("type") == "folder")
            if found:
                payload = self.playlist_tree.current_payload() or {}
                self._selected_type = payload.get("type")
                self._selected_id = payload.get("id")
        if not found:
            self._selected_type = None
            self._selected_id = None
        self._load_detail()

    def _add_tree_level(
        self,
        parent_widget,
        parent_id: Optional[int],
        folders: list[PlaylistFolder],
        playlists_by_folder: dict[Optional[int], list[Playlist]],
        track_counts: dict[int, int],
    ) -> None:
        children = sorted(
            (f for f in folders if f.parent_id == parent_id), key=lambda f: f.name.lower()
        )
        for folder in children:
            item = self.playlist_tree.add_folder(
                parent_widget,
                folder.name,
                {"id": folder.id},
                expanded=folder.id in self._expanded_folder_ids,
            )
            self._add_tree_level(item, folder.id, folders, playlists_by_folder, track_counts)
        for playlist in sorted(
            playlists_by_folder.get(parent_id, []),
            key=lambda p: (not p.is_pinned, p.name.lower()),
        ):
            label = ("▶ " if playlist.kind == Playlist.KIND_PLAYBACK else "") + playlist.name
            self.playlist_tree.add_leaf(
                parent_widget,
                label,
                {"type": "playlist", "id": playlist.id, "kind": playlist.kind},
                meta=str(track_counts.get(playlist.id, 0)),
            )

    def _on_expand_changed(self, item) -> None:
        payload = self.playlist_tree.payload_of(item) or {}
        if payload.get("type") != "folder":
            return
        if item.isExpanded():
            self._expanded_folder_ids.add(payload.get("id"))
        else:
            self._expanded_folder_ids.discard(payload.get("id"))

    def _on_node_selected(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        self._selected_type = payload.get("type")
        self._selected_id = payload.get("id")
        self._load_detail()

    def _current_folder_context(self) -> Optional[int]:
        """Where a newly created playlist or folder should land: alongside
        whatever's currently selected."""
        if self._selected_type == "folder":
            return self._selected_id
        if self._selected_type == "playlist":
            with self.ctx.session() as session:
                playlist = session.get(Playlist, self._selected_id)
                return playlist.folder_id if playlist is not None else None
        return None

    def _load_detail(self) -> None:
        self._tracks = []
        if self._selected_type == "folder" and self._selected_id is not None:
            self._load_folder_detail(self._selected_id)
        elif self._selected_type == "playlist" and self._selected_id is not None:
            self._load_playlist_detail(self._selected_id)
        else:
            self.detail_title.setText("Select a playlist")
            self.detail_desc.setText("")
            self.detail_meta.setText("")
            self.track_list.set_rows([])
            self._set_playlist_actions_enabled(False)

    def _load_folder_detail(self, folder_id: int) -> None:
        with self.ctx.session() as session:
            folder = session.get(PlaylistFolder, folder_id)
            if folder is None:
                self._selected_type = self._selected_id = None
                self._load_detail()
                return
            name = folder.name
            playlists, subfolders = pl_svc.folder_counts(session, folder_id)
        self.detail_title.setText(name)
        self.detail_desc.setText("Folder")
        self.detail_meta.setText(
            f"{playlists} playlist{'s' if playlists != 1 else ''} · "
            f"{subfolders} subfolder{'s' if subfolders != 1 else ''}"
        )
        self.track_list.set_rows([])
        self._set_playlist_actions_enabled(False)

    def _load_playlist_detail(self, playlist_id: int) -> None:
        rows = []
        with self.ctx.session() as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None:
                self._selected_type = self._selected_id = None
                self._load_detail()
                return
            self.detail_title.setText(playlist.name)
            self.detail_desc.setText(playlist.description or "")
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
                rows.append({
                    "lead": str(idx),
                    "primary": track.title,
                    "secondary": track.artist_display or "",
                    "trail": trail,
                    "color": COLORS["text_dim"] if (mf is None or mf.is_missing)
                    else COLORS["text"],
                    "key": track.id,
                })
            self.detail_meta.setText(
                f"{len(tracks)} tracks · {format_duration(total)} · "
                f"{KIND_LABEL.get(playlist.kind, playlist.kind)}"
            )
            kind = playlist.kind
        self.track_list.set_rows(rows)
        self._set_playlist_actions_enabled(
            True, editable=kind == Playlist.KIND_MANUAL, smart=kind == Playlist.KIND_SMART
        )

    def _set_playlist_actions_enabled(
        self, enabled: bool, editable: bool = False, smart: bool = False
    ) -> None:
        self.play_btn.setEnabled(enabled)
        self.shuffle_btn.setEnabled(enabled)
        self.queue_btn.setEnabled(enabled)
        self.edit_btn.setEnabled(smart)
        self.remove_btn.setEnabled(editable)

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
        if self._selected_type != "playlist" or self._selected_id is None:
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, self._selected_id)
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
                playlist = session.get(Playlist, self._selected_id)
                playlist.name = new_name or playlist.name
                playlist.rules = json.dumps(spec, indent=2)
        except Exception as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.refresh()

    def _remove_selected(self) -> None:
        payload = self.track_list.current_payload()
        if payload is None or self._selected_type != "playlist" or self._selected_id is None:
            return
        with self.ctx.session() as session:
            playlist = session.get(Playlist, self._selected_id)
            if playlist is None or playlist.kind != Playlist.KIND_MANUAL:
                self.ctx.notify("Only manual playlists can be edited by hand")
                return
            for item in playlist.items:
                if item.track_id == payload.get("key"):
                    session.delete(item)
                    break
        self._load_detail()

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
        self.ctx.play_tracks(self._tracks, start=start, source=f"playlist:{self._selected_id}")

    def _shuffle(self) -> None:
        if not self._tracks:
            return
        self.ctx.player.set_shuffle(True)
        self.ctx.play_tracks(self._tracks, start=0, source=f"playlist:{self._selected_id}")

    # -- folder actions -------------------------------------------------------------

    def create_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        parent_id = self._current_folder_context()
        with self.ctx.session() as session:
            pl_svc.create_folder(session, name.strip(), parent_id=parent_id)
        if parent_id is not None:
            self._expanded_folder_ids.add(parent_id)
        self.ctx.notify(f"Created folder “{name.strip()}”")
        self.refresh()

    def rename_selected(self) -> None:
        if self._selected_type == "folder" and self._selected_id is not None:
            with self.ctx.session() as session:
                folder = session.get(PlaylistFolder, self._selected_id)
                current = folder.name if folder else ""
            name, ok = QInputDialog.getText(self, "Rename folder", "Folder name:", text=current)
            if not ok or not name.strip():
                return
            with self.ctx.session() as session:
                pl_svc.rename_folder(session, self._selected_id, name.strip())
            self.refresh()
        elif self._selected_type == "playlist" and self._selected_id is not None:
            with self.ctx.session() as session:
                playlist = session.get(Playlist, self._selected_id)
                current = playlist.name if playlist else ""
            name, ok = QInputDialog.getText(self, "Rename playlist", "Playlist name:", text=current)
            if not ok or not name.strip():
                return
            with self.ctx.session() as session:
                pl_svc.rename_playlist(session, self._selected_id, name.strip())
            self.refresh()
        else:
            self.ctx.notify("Select a playlist or folder first")

    def move_selected(self) -> None:
        if self._selected_type not in ("folder", "playlist") or self._selected_id is None:
            self.ctx.notify("Select a playlist or folder first")
            return
        with self.ctx.session() as session:
            folders = pl_svc.list_folders(session)
            if self._selected_type == "folder":
                current_folder_id = session.get(PlaylistFolder, self._selected_id).parent_id
                exclude_ids = pl_svc.folder_and_descendant_ids(session, self._selected_id)
            else:
                playlist = session.get(Playlist, self._selected_id)
                current_folder_id = playlist.folder_id if playlist else None
                exclude_ids = None

        dialog = FolderPickerDialog(self, folders, current_folder_id, exclude_ids)
        if dialog.exec() != QDialog.Accepted:
            return
        target_id = dialog.selected_folder_id()
        with self.ctx.session() as session:
            if self._selected_type == "folder":
                try:
                    pl_svc.move_folder(session, self._selected_id, target_id)
                except ValueError as exc:
                    QMessageBox.warning(self, "Can't move folder", str(exc))
                    return
            else:
                pl_svc.move_playlist(session, self._selected_id, target_id)
        if target_id is not None:
            self._expanded_folder_ids.add(target_id)
        self.refresh()

    def delete_selected(self) -> None:
        if self._selected_type == "folder" and self._selected_id is not None:
            confirm = QMessageBox.question(
                self,
                "Delete folder",
                "Delete this folder? Playlists and subfolders inside it move up "
                "to the level above - nothing inside is deleted.",
            )
            if confirm != QMessageBox.Yes:
                return
            with self.ctx.session() as session:
                pl_svc.delete_folder(session, self._selected_id)
            self._selected_type = self._selected_id = None
            self.refresh()
        elif self._selected_type == "playlist" and self._selected_id is not None:
            with self.ctx.session() as session:
                playlist = session.get(Playlist, self._selected_id)
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
                playlist = session.get(Playlist, self._selected_id)
                if playlist is not None:
                    session.delete(playlist)
            self._selected_type = self._selected_id = None
            self.refresh()
        else:
            self.ctx.notify("Select a playlist or folder first")
