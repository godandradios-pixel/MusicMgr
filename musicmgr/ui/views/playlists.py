"""Playlist browser: manual lists, smart rules, and frozen chart snapshots -
organised into a folder tree, the same shape MusicBee's playlist pane uses.
"""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
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


class SmartPlaylistDialog(QDialog):
    """Minimal JSON rule editor - powerful without a giant rule builder UI."""

    TEMPLATE = {
        "match": "all",
        "order_by": "play_count_desc",
        "limit": 100,
        "rules": [
            {"field": "genre", "op": "is", "value": "Soul"},
            {"field": "year", "op": "between", "value": [1965, 1979]},
        ],
    }

    def __init__(self, parent=None, name: str = "", rules: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Smart playlist")
        self.setMinimumSize(620, 520)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        form.addRow("Name", self.name_edit)
        layout.addLayout(form)

        help_text = QLabel(
            "Fields: title, artist, album, genre, style, year, play_count, rating, "
            "bpm, duration_ms, played_within_days, not_played_within_days.\n"
            "Ops: is, contains, startswith, gte, lte, gt, lt, between.\n"
            "order_by: title, artist, added_desc, play_count_desc, "
            "last_played_desc, rating_desc, random."
        )
        help_text.setObjectName("Subtitle")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        self.editor = QPlainTextEdit(rules or json.dumps(self.TEMPLATE, indent=2))
        self.editor.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.editor, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, dict]:
        return self.name_edit.text().strip(), json.loads(self.editor.toPlainText())


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
