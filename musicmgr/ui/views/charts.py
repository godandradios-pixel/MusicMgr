"""Charts: imported Billboard-style rankings, organised into a folder tree,
the same shape the playlist pane uses.

The built-in "Most Played" playback charts that used to live here (a window
chip bar switching between today/week/month/etc.) moved to the Playlists
view on 2026-08-30 - see ui/views/playlists.py and
services/playlists.py's "playback playlists" section.

**Admin density, not touch (added 2026-08-30):** James's framing - Charts is
where you sit down and curate imported data before turning it into
playlists, not something you tap through mid-listen the way the rest of
this app is - so the browse column deliberately breaks from touch sizing.
It's `ChartTable` (ui/widgets/chart_table.py): a dense, sortable,
multi-select table (Name/Type/Source/Coverage) with a search box, replacing
the old single-column `TouchTree`. See that module's docstring for the
widget's own reasoning.

**Two panes, not three (revised 2026-08-30, same day):** the first cut had
a chart's editions in their own middle pane, separate from the browse tree.
James asked why editions weren't "in the main column with just another
subfolder" - a fair question, since folders already expand to reveal
charts the same way. So editions moved into the tree itself as a third
level (Folder -> Chart -> Edition, see `ChartTable`'s own docstring for how
that's kept cheap even for a 3,393-edition real chart), the middle pane is
gone, and this view is now two panes: the tree on the left, and the
currently-selected edition's positions on the right. Selecting a chart
(rather than one specific edition under it) still shows its most recent
edition, same as before - you just get there by clicking the chart instead
of it happening automatically in a pane that no longer exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...db.models import Chart, ChartFolder, ChartIssue, Track
from ...services import charts as chart_svc
from ..context import AppContext
from ..theme import COLORS
from ..widgets.chart_table import ChartFolderNode, ChartIssueNode, ChartLeafNode, ChartTable
from ..widgets.common import FolderPickerDialog, TouchButton, TouchList, dim_label
from .base import BaseView


class ChartsView(BaseView):
    title_text = "Charts"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._selected_type: Optional[str] = None  # "folder" | "chart" | "issue"
        self._selected_id: Optional[int] = None
        self._selected_payloads: list[dict] = []  # every currently multi-selected node
        self._chart_id: Optional[int] = None
        self._issue_id: Optional[int] = None
        self._tracks: list[Track] = []

        import_btn = TouchButton("Import chart CSV…", primary=True)
        import_btn.clicked.connect(self.import_csv)
        folder_btn = TouchButton("New folder")
        folder_btn.clicked.connect(self.create_folder)
        self.header.addWidget(import_btn)
        self.header.addWidget(folder_btn)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        lbl = QLabel("Charts")
        lbl.setObjectName("Crumb")
        ll.addWidget(lbl)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter by name…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_filter_changed)
        ll.addWidget(self.search_box)

        self.chart_tree = ChartTable(issues_provider=self._issues_for_chart)
        self.chart_tree.itemActivatedPayload.connect(self._on_node_selected)
        self.chart_tree.selectionChangedPayloads.connect(self._on_selection_changed)
        ll.addWidget(self.chart_tree, 1)

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

        splitter.addWidget(self._entry_pane())
        # The Charts pane got noticeably wider on 2026-08-30, alongside the
        # ChartTable rebuild: real columns (Name/Type/Source/Coverage) don't
        # fit readably in the 300px this pane used to get when it was a
        # single-column TouchTree - see chart_table.py's minimum-section-
        # size fix for the crushed-to-unreadable bug this caused before the
        # pane was widened to match. Two panes now, not three - editions
        # moved into the tree itself the same day (see module docstring),
        # so the entries pane picks up the width the old middle pane used.
        splitter.setSizes([620, 900])
        self.body().addWidget(splitter, 1)

        ctx.libraryChanged.connect(self.refresh)

    def _entry_pane(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self.entry_title = QLabel("Pick a chart")
        self.entry_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        head.addWidget(self.entry_title)
        head.addStretch(1)
        self.coverage_label = dim_label("")
        head.addWidget(self.coverage_label)
        layout.addLayout(head)

        self.coverage = QProgressBar()
        self.coverage.setRange(0, 100)
        self.coverage.setValue(0)
        layout.addWidget(self.coverage)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.play_btn = TouchButton("Play owned", primary=True)
        self.play_btn.clicked.connect(lambda: self._play(0))
        self.queue_btn = TouchButton("Queue")
        self.queue_btn.clicked.connect(lambda: self.ctx.enqueue_tracks(self._tracks))
        self.save_btn = TouchButton("Save as playlist")
        self.save_btn.clicked.connect(self._save_playlist)
        self.rematch_btn = TouchButton("Re-match library")
        self.rematch_btn.clicked.connect(self._rematch)
        # Moved here from the old middle pane (removed 2026-08-30) - it acts
        # on whichever edition is currently shown below, same as before.
        self.delete_issue_btn = TouchButton("Delete edition")
        self.delete_issue_btn.clicked.connect(self.delete_selected_issue)
        for b in (self.play_btn, self.queue_btn, self.save_btn, self.rematch_btn, self.delete_issue_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.entry_list = TouchList(row_height=76)
        self.entry_list.itemActivatedPayload.connect(self._on_entry_tapped)
        layout.addWidget(self.entry_list, 1)
        return box

    # -- loading -------------------------------------------------------------

    def _issues_for_chart(self, chart_id: int) -> Sequence[ChartIssueNode]:
        """`ChartTable`'s `issues_provider` - called only for a chart the
        user has actually expanded (see that widget's docstring for why),
        so this can afford two real queries per call regardless of how many
        editions the chart has."""
        nodes: list[ChartIssueNode] = []
        with self.ctx.session() as session:
            issues = chart_svc.list_issues(session, chart_id)
            coverage = chart_svc.issue_coverage_by_chart(session, chart_id)
            for issue in issues:
                owned, total = coverage.get(issue.id, (0, 0))
                pct = int(100 * owned / total) if total else None
                nodes.append(
                    ChartIssueNode(
                        id=issue.id,
                        chart_id=chart_id,
                        label=issue.chart_date.strftime("%d %b %Y"),
                        coverage_pct=pct,
                        detail=f"{owned}/{total} in library" if total else "no entries",
                    )
                )
        return nodes

    def refresh(self) -> None:
        folder_nodes: list[ChartFolderNode] = []
        chart_nodes: list[ChartLeafNode] = []
        with self.ctx.session() as session:
            folders = chart_svc.list_folders(session)
            charts = chart_svc.list_charts(session)
            folder_nodes = [
                ChartFolderNode(id=f.id, name=f.name, parent_id=f.parent_id) for f in folders
            ]
            for chart in charts:
                owned, total = chart_svc.chart_coverage(session, chart.id)
                pct = int(100 * owned / total) if total else None
                chart_nodes.append(
                    ChartLeafNode(
                        id=chart.id,
                        name=chart.name,
                        parent_id=chart.folder_id,
                        kind=chart.kind,
                        shape=chart_svc.chart_shape(chart),
                        source=Path(chart.source).name if chart.source else "—",
                        editions=chart_svc.chart_edition_count(session, chart.id),
                        coverage_pct=pct,
                        coverage_label=f"{pct}%" if pct is not None else "—",
                    )
                )

        self.chart_tree.blockSignals(True)
        self.chart_tree.set_data(folder_nodes, chart_nodes)
        self.chart_tree.blockSignals(False)

        if self._selected_type == "issue" and self._selected_id is not None:
            # editions are only ever rendered while their parent chart is
            # expanded (ChartTable's lazy third level - see its docstring),
            # so a still-valid edition under a currently-collapsed chart
            # simply won't be found here; that's fine, _load_detail() below
            # re-verifies it against the database independently and is the
            # real source of truth for whether it still exists.
            self.chart_tree.select_node(
                lambda n: n.get("type") == "issue" and n.get("id") == self._selected_id
            )
        else:
            folder_ids = {f.id for f in folders}
            chart_ids = {c.id for c in charts}
            still_there = (
                (self._selected_type == "folder" and self._selected_id in folder_ids)
                or (self._selected_type == "chart" and self._selected_id in chart_ids)
            )
            if still_there:
                found = self.chart_tree.select_node(
                    lambda n: n.get("type") == self._selected_type
                    and n.get("id") == self._selected_id
                )
            else:
                found = self.chart_tree.select_node(lambda n: n.get("type") == "chart")
                if not found:
                    found = self.chart_tree.select_node(lambda n: n.get("type") == "folder")
                if found:
                    payload = self.chart_tree.current_payload() or {}
                    self._selected_type = payload.get("type")
                    self._selected_id = payload.get("id")
            if not found:
                self._selected_type = None
                self._selected_id = None
        self._selected_payloads = self.chart_tree.selected_payloads()
        self._load_detail()

    def _on_filter_changed(self, text: str) -> None:
        self.chart_tree.set_filter_text(text)

    def _on_selection_changed(self, payloads: list) -> None:
        self._selected_payloads = list(payloads)

    def _on_node_selected(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        self._selected_type = payload.get("type")
        self._selected_id = payload.get("id")
        self._load_detail()

    def _current_folder_context(self) -> Optional[int]:
        """Where a newly created chart or folder should land: alongside
        whatever's currently selected."""
        if self._selected_type == "folder":
            return self._selected_id
        if self._selected_type == "chart":
            with self.ctx.session() as session:
                chart = session.get(Chart, self._selected_id)
                return chart.folder_id if chart is not None else None
        return None

    def _load_detail(self) -> None:
        self._tracks = []
        if self._selected_type == "folder" and self._selected_id is not None:
            self._load_folder_detail(self._selected_id)
            self._issue_id = None
        elif self._selected_type == "chart" and self._selected_id is not None:
            with self.ctx.session() as session:
                chart = session.get(Chart, self._selected_id)
                if chart is None:
                    self._selected_type = self._selected_id = None
                    self._load_detail()
                    return
                self._chart_id = chart.id
                issues = chart_svc.list_issues(session, chart.id)
            self._set_chart_actions_enabled(True)
            if issues:
                # a chart selected as a whole (not one specific edition
                # under it) shows its most recent edition - same behaviour
                # as before the middle pane was removed, just reached by
                # clicking the chart instead of it auto-selecting there
                self._issue_id = issues[0].id
                self._load_entries()
            else:
                self._issue_id = None
                self.entry_title.setText("No editions imported yet")
                self.coverage.setValue(0)
                self.coverage_label.setText("")
                self.entry_list.set_rows([])
        elif self._selected_type == "issue" and self._selected_id is not None:
            with self.ctx.session() as session:
                issue = session.get(ChartIssue, self._selected_id)
                if issue is None:
                    self._selected_type = self._selected_id = None
                    self._load_detail()
                    return
                self._chart_id = issue.chart_id
            self._set_chart_actions_enabled(True)
            self._issue_id = self._selected_id
            self._load_entries()
        else:
            self.entry_title.setText("Pick a chart")
            self.coverage.setValue(0)
            self.coverage_label.setText("")
            self.entry_list.set_rows([])
            self._issue_id = None
            self._set_chart_actions_enabled(False)
        self.delete_issue_btn.setEnabled(self._issue_id is not None)

    def _load_folder_detail(self, folder_id: int) -> None:
        with self.ctx.session() as session:
            folder = session.get(ChartFolder, folder_id)
            if folder is None:
                self._selected_type = self._selected_id = None
                self._load_detail()
                return
            name = folder.name
            charts, subfolders = chart_svc.folder_counts(session, folder_id)
        self.entry_title.setText(name)
        self.coverage.setValue(0)
        self.coverage_label.setText(
            f"{charts} chart{'s' if charts != 1 else ''} · "
            f"{subfolders} subfolder{'s' if subfolders != 1 else ''}"
        )
        self.entry_list.set_rows([])
        self._set_chart_actions_enabled(False)

    def _set_chart_actions_enabled(self, enabled: bool) -> None:
        self.play_btn.setEnabled(enabled)
        self.queue_btn.setEnabled(enabled)
        self.save_btn.setEnabled(enabled)
        self.rematch_btn.setEnabled(enabled)

    def _load_entries(self) -> None:
        if self._issue_id is None:
            return
        rows = []
        self._tracks = []
        with self.ctx.session() as session:
            entries = chart_svc.issue_entries(session, self._issue_id)
            owned, total = chart_svc.issue_coverage(session, self._issue_id)
            for entry in entries:
                movement, colour = self._movement(entry.rank, entry.last_week)
                stats = []
                if entry.peak_pos:
                    stats.append(f"peak {entry.peak_pos}")
                if entry.weeks_on_chart:
                    stats.append(f"{entry.weeks_on_chart} wks")
                owned_track = entry.track
                if owned_track is not None:
                    self._tracks.append(owned_track)
                rows.append({
                    "lead": str(entry.rank),
                    "lead_bold": True,
                    # 2026-09-13 follow-up (see ui/theme.py's #Primary
                    # comment for this whole cleanup) - top-10 highlight,
                    # walnut brown now instead of red-orange.
                    "lead_color": COLORS["jukebox_key_hi"] if entry.rank <= 10 else COLORS["text_dim"],
                    "primary": entry.title,
                    "secondary": " · ".join(
                        x for x in (entry.artist_name, ", ".join(stats)) if x
                    ),
                    "trail": movement,
                    "trail_color": colour,
                    "color": COLORS["text"] if owned_track else COLORS["text_dim"],
                    "key": entry.track_id,
                })
        pct = int(100 * owned / total) if total else 0
        self.entry_title.setText(f"{len(rows)} positions")
        self.coverage.setValue(pct)
        self.coverage_label.setText(f"{owned} of {total} in your library ({pct}%)")
        self.entry_list.set_rows(rows)

    @staticmethod
    def _movement(rank: int, last_week: Optional[int]) -> tuple[str, str]:
        if not last_week:
            # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for
            # this whole cleanup) - "NEW" badge, walnut brown now instead
            # of red-orange.
            return "NEW", COLORS["jukebox_key_hi"]
        delta = last_week - rank
        if delta > 0:
            return f"▲ {delta}", COLORS["good"]
        if delta < 0:
            return f"▼ {abs(delta)}", COLORS["warn"]
        return "=", COLORS["text_dim"]

    # -- actions -------------------------------------------------------------

    def _on_entry_tapped(self, payload: Optional[dict]) -> None:
        if not payload or payload.get("key") is None:
            self.ctx.notify("That title is not in your library yet")
            return
        for idx, track in enumerate(self._tracks):
            if track.id == payload["key"]:
                self._play(idx)
                return

    def _play(self, start: int) -> None:
        if not self._tracks:
            self.ctx.notify("Nothing in this chart is in your library yet")
            return
        self.ctx.player.set_shuffle(False)
        self.ctx.play_tracks(self._tracks, start=start, source=f"chart:{self._chart_id}")

    def _save_playlist(self) -> None:
        if self._issue_id is None:
            return
        with self.ctx.session() as session:
            playlist = chart_svc.snapshot_to_playlist(session, self._issue_id)
            name = playlist.name
        self.ctx.playlistsChanged.emit()
        self.ctx.notify(f"Saved playlist “{name}”")

    def delete_selected_issue(self) -> None:
        """Removes just the selected edition - the fix for a single bad
        import (a mis-parsed date collapsing many rows into one edition,
        say) without deleting and re-importing the whole chart's history."""
        if self._issue_id is None:
            self.ctx.notify("Select an edition first")
            return
        confirm = QMessageBox.question(
            self,
            "Delete edition",
            "Delete this one edition? Its entries go with it - the rest of "
            "this chart's editions are untouched.",
        )
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            chart_svc.delete_issue(session, self._issue_id)
        # fall back to the parent chart (which re-selects its now-latest
        # remaining edition, if any) rather than leaving nothing selected
        self._selected_type = "chart"
        self._selected_id = self._chart_id
        self._issue_id = None
        self.refresh()

    def _rematch(self) -> None:
        if self._chart_id is None:
            return
        with self.ctx.session() as session:
            matched = chart_svc.rematch_chart(session, self._chart_id)
        self.ctx.notify(f"Matched {matched} chart entries to your library")
        self._load_entries()

    def import_csv(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose chart CSV file(s)", str(Path.home()), "CSV files (*.csv)"
        )
        if not paths:
            return
        name, ok = QInputDialog.getText(
            self,
            "Chart name",
            "Name this chart series (leave blank to use the file name):",
            text="Billboard Hot 100",
        )
        if not ok:
            return
        folder_id = self._current_folder_context()
        summaries = []
        imported_chart_ids: set[int] = set()
        with self.ctx.session() as session:
            for path in paths:
                result = chart_svc.import_chart_csv(
                    session, path, chart_name=name.strip() or None, folder_id=folder_id
                )
                summaries.append(result.summary())
                if result.errors:
                    summaries.append("  ! " + "; ".join(result.errors[:3]))
                if result.chart_id is not None:
                    imported_chart_ids.add(result.chart_id)
        QMessageBox.information(self, "Import complete", "\n".join(summaries))
        self._chart_id = None
        for chart_id in imported_chart_ids:
            self._expand_chart(chart_id)
        self.refresh()
        self.ctx.libraryChanged.emit()

    # -- folder actions -------------------------------------------------------------

    def create_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        parent_id = self._current_folder_context()
        with self.ctx.session() as session:
            chart_svc.create_folder(session, name.strip(), parent_id=parent_id)
        if parent_id is not None:
            self._expand_folder(parent_id)
        self.ctx.notify(f"Created folder “{name.strip()}”")
        self.refresh()

    def _expand_folder(self, folder_id: int) -> None:
        ids = self.chart_tree.expanded_folder_ids
        ids.add(folder_id)
        self.chart_tree.set_expanded_folder_ids(ids)

    def _expand_chart(self, chart_id: int) -> None:
        ids = self.chart_tree.expanded_chart_ids
        ids.add(chart_id)
        self.chart_tree.set_expanded_chart_ids(ids)

    def _notify_no_valid_target(self, verb: str) -> None:
        """Rename/Move/Delete all act on folders and charts, never on an
        edition - a selected edition isn't "nothing selected," so it gets
        its own message instead of the generic one, which used to read as
        confusing/wrong when James had clearly selected *something*, just
        not a chart or folder."""
        if self._selected_payloads and all(
            p.get("type") == "issue" for p in self._selected_payloads
        ):
            self.ctx.notify(f"Editions can't be {verb} - select the chart itself")
        else:
            self.ctx.notify("Select a chart or folder first")

    def rename_selected(self) -> None:
        if len(self._selected_payloads) > 1:
            self.ctx.notify("Select exactly one chart or folder to rename")
            return
        if self._selected_type == "folder" and self._selected_id is not None:
            with self.ctx.session() as session:
                folder = session.get(ChartFolder, self._selected_id)
                current = folder.name if folder else ""
            name, ok = QInputDialog.getText(self, "Rename folder", "Folder name:", text=current)
            if not ok or not name.strip():
                return
            with self.ctx.session() as session:
                chart_svc.rename_folder(session, self._selected_id, name.strip())
            self.refresh()
        elif self._selected_type == "chart" and self._selected_id is not None:
            with self.ctx.session() as session:
                chart = session.get(Chart, self._selected_id)
                current = chart.name if chart else ""
            name, ok = QInputDialog.getText(self, "Rename chart", "Chart name:", text=current)
            if not ok or not name.strip():
                return
            with self.ctx.session() as session:
                chart_svc.rename_chart(session, self._selected_id, name.strip())
            self.refresh()
        else:
            self._notify_no_valid_target("renamed")

    def _bulk_targets(self) -> list[dict]:
        """Every currently-selected chart/folder node - the table multi-
        selects (added 2026-08-30), so Move and Delete act on all of them at
        once rather than whatever single node happened to be tapped last.
        Editions are excluded here on purpose - deleting one is a dedicated
        action ("Delete edition" in the entry pane), not part of the
        folder/chart bulk actions."""
        return [
            t for t in self._selected_payloads
            if t.get("type") in ("folder", "chart") and t.get("id") is not None
        ]

    def move_selected(self) -> None:
        targets = self._bulk_targets()
        if not targets:
            self._notify_no_valid_target("moved")
            return
        with self.ctx.session() as session:
            folders = chart_svc.list_folders(session)
            current_folder_id = None
            exclude_ids: set[int] = set()
            if len(targets) == 1:
                t = targets[0]
                if t["type"] == "folder":
                    folder = session.get(ChartFolder, t["id"])
                    current_folder_id = folder.parent_id if folder else None
                    exclude_ids = chart_svc.folder_and_descendant_ids(session, t["id"])
                else:
                    chart = session.get(Chart, t["id"])
                    current_folder_id = chart.folder_id if chart else None
            else:
                # moving several folders at once: none of them (or their own
                # subtrees) can be offered as a destination for the batch
                for t in targets:
                    if t["type"] == "folder":
                        exclude_ids |= chart_svc.folder_and_descendant_ids(session, t["id"])

        dialog = FolderPickerDialog(self, folders, current_folder_id, exclude_ids or None)
        if dialog.exec() != QDialog.Accepted:
            return
        target_id = dialog.selected_folder_id()
        moved = 0
        with self.ctx.session() as session:
            for t in targets:
                if t["type"] == "folder":
                    try:
                        chart_svc.move_folder(session, t["id"], target_id)
                        moved += 1
                    except ValueError as exc:
                        QMessageBox.warning(
                            self, "Can't move folder", f"“{t.get('name', 'That folder')}”: {exc}"
                        )
                else:
                    chart_svc.move_chart(session, t["id"], target_id)
                    moved += 1
        if target_id is not None:
            self._expand_folder(target_id)
        if len(targets) > 1:
            self.ctx.notify(f"Moved {moved} item{'s' if moved != 1 else ''}")
        self.refresh()

    def delete_selected(self) -> None:
        targets = self._bulk_targets()
        if not targets:
            self._notify_no_valid_target("deleted")
            return
        folders = [t for t in targets if t["type"] == "folder"]
        charts = [t for t in targets if t["type"] == "chart"]

        if len(targets) == 1:
            single = targets[0]
            if single["type"] == "folder":
                title, msg = "Delete folder", (
                    "Delete this folder? Charts and subfolders inside it move up "
                    "to the level above - nothing inside is deleted."
                )
            else:
                title, msg = "Delete chart", (
                    "Delete this chart? All its imported editions go with it."
                )
        else:
            parts = []
            if folders:
                parts.append(f"{len(folders)} folder{'s' if len(folders) != 1 else ''}")
            if charts:
                parts.append(f"{len(charts)} chart{'s' if len(charts) != 1 else ''}")
            title = "Delete selected"
            msg = (
                f"Delete {' and '.join(parts)}? Each chart's imported editions go with "
                "it; a deleted folder's own contents move up to the level above rather "
                "than being deleted."
            )
        confirm = QMessageBox.question(self, title, msg)
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            for t in folders:
                chart_svc.delete_folder(session, t["id"])
            for t in charts:
                chart_svc.delete_chart(session, t["id"])
        self._selected_type = self._selected_id = None
        self._selected_payloads = []
        self.refresh()
