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
from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...db.models import Chart, ChartEntry, ChartFolder, ChartIssue, Track
from ...services import charts as chart_svc
from ...services import scanner as scanner_svc
from ..context import AppContext
from ..theme import COLORS
from ..widgets.chart_table import ChartFolderNode, ChartIssueNode, ChartLeafNode, ChartTable
from ..widgets.common import FolderPickerDialog, TouchButton, TouchList, dim_label
from .base import BaseView


class MatchTrackDialog(QDialog):
    """"Fix match..." (`ChartsView._fix_match`) - manually pick, or clear,
    which library track one chart position matches. Same button + dialog
    assignment pattern the rest of this app uses (see FolderPickerDialog in
    ui/widgets/common.py) and the same two-box ANDed artist/track search
    `services/jukebox.py:search_addable_tracks` established for
    JukeboxPickerDialog - but single-pick (a chart position has exactly one
    track, not "up to N") and backed by
    services/charts.py:search_tracks_for_match instead, which - unlike the
    jukebox search - excludes nothing and requires no resolvable album
    artist, since a chart legitimately charts various-artists compilation
    tracks.

    Opens pre-filled with the entry's own title/artist (`initial_title`/
    `initial_artist`) so the likely correct track is usually already
    sitting in the results list rather than the person having to retype
    what's already on screen. `current_track_id` only controls whether the
    "Clear match" button appears - there's nothing to clear on an entry
    that's already unmatched.

    "Browse for file..." (`import_file`, 2026-09-17 follow-up) covers the
    case search can never solve: the file is sitting right there on disk
    but was never scanned into the library, so there's no Track row for
    any search to find - James: "I would like to be able to pick a file
    from a directory and then have it added to the match chart." Picking
    a file scans just that one file in (`services/scanner.py:import_file`,
    the same per-file logic a folder scan uses) and drops the resulting
    track into the results list as the only row, already selected -
    still one Ok click away from actually being applied, same as a normal
    search pick.
    """

    def __init__(
        self,
        parent,
        search_tracks: Callable[[str, str], list[dict]],
        import_file: Callable[[str], Optional[dict]],
        get_last_dir: Callable[[], Optional[str]],
        set_last_dir: Callable[[str], None],
        initial_title: str = "",
        initial_artist: str = "",
        current_track_id: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fix match")
        self.setMinimumSize(460, 560)
        self._search_tracks = search_tracks
        self._import_file = import_file
        #: remembers the folder "Browse for file..." last opened/picked
        #: from, persisted via chart_svc's Setting-backed
        #: get_last_browse_dir/set_last_browse_dir - James works through
        #: one album/box-set folder at a time, so re-opening this dialog
        #: for the next unmatched entry should pick up where he left off
        #: rather than always starting at his home folder.
        self._get_last_dir = get_last_dir
        self._set_last_dir = set_last_dir
        #: set by _on_accept/_on_clear - see ChartsView._fix_match for how
        #: the two outcomes (pick vs. clear) are told apart after exec().
        self.chosen_track_id: Optional[int] = None
        self.clear_requested = False

        layout = QVBoxLayout(self)
        layout.addWidget(dim_label(f"Matching “{initial_title}” — {initial_artist}"))

        layout.addWidget(dim_label("Search by artist"))
        self.artist_search = QLineEdit()
        self.artist_search.setPlaceholderText("Artist name...")
        self.artist_search.setClearButtonEnabled(True)
        self.artist_search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.artist_search)

        layout.addWidget(dim_label("Search by track"))
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText("Track name...")
        self.track_search.setClearButtonEnabled(True)
        self.track_search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.track_search)

        browse_row = QHBoxLayout()
        browse_row.addWidget(dim_label("Not in your library yet?"))
        self.browse_btn = QPushButton("Browse for file…")
        self.browse_btn.clicked.connect(self._on_browse_file)
        browse_row.addWidget(self.browse_btn)
        browse_row.addStretch(1)
        layout.addLayout(browse_row)

        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.itemSelectionChanged.connect(self._on_selection_changed)
        self.results.itemDoubleClicked.connect(lambda _item: self._on_accept())
        layout.addWidget(self.results, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setEnabled(False)
        if current_track_id is not None:
            clear_btn = buttons.addButton("Clear match", QDialogButtonBox.DestructiveRole)
            clear_btn.clicked.connect(self._on_clear)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # pre-fill and search once, blocked so filling both boxes doesn't
        # fire an extra, immediately-superseded search on the first
        # setText() alone - same reasoning as JukeboxPickerDialog's own
        # initial_artist_query/initial_track_query handling
        self.artist_search.blockSignals(True)
        self.track_search.blockSignals(True)
        self.artist_search.setText(initial_artist)
        self.track_search.setText(initial_title)
        self.artist_search.blockSignals(False)
        self.track_search.blockSignals(False)
        self._on_search_changed()

    def _on_search_changed(self, _text: str = "") -> None:
        artist_query = self.artist_search.text().strip()
        track_query = self.track_search.text().strip()
        results = (
            self._search_tracks(artist_query, track_query)
            if (artist_query or track_query)
            else []
        )
        self.results.clear()
        for row in results:
            label = f"{row['title']} — {row['artist_name']}"
            if row.get("album"):
                label += f" ({row['album']})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, row["track_id"])
            self.results.addItem(item)

    def _on_browse_file(self) -> None:
        start_dir = self._get_last_dir() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the audio file", start_dir,
            "Audio files (*.mp3 *.flac *.m4a *.aac *.ogg *.wma *.wav);;All files (*)",
        )
        if not path:
            return
        self._set_last_dir(str(Path(path).parent))
        row = self._import_file(path)
        if row is None:
            QMessageBox.warning(
                self, "Couldn't add file",
                "That file's tags couldn't be read - it may not be a "
                "supported audio format.",
            )
            return
        label = f"{row['title']} — {row['artist_name']}"
        if row.get("album"):
            label += f" ({row['album']})"
        item = QListWidgetItem(f"{label}   (just added to your library)")
        item.setData(Qt.UserRole, row["track_id"])
        self.results.clear()
        self.results.addItem(item)
        self.results.setCurrentItem(item)
        item.setSelected(True)
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        self.ok_button.setEnabled(self.results.currentItem() is not None)

    def _on_accept(self) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        self.chosen_track_id = item.data(Qt.UserRole)
        self.accept()

    def _on_clear(self) -> None:
        self.clear_requested = True
        self.accept()


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
        # 2026-09-17 follow-up, James: a checkbox filter for "only the
        # positions that aren't matched yet" - the list this reads is
        # already just this edition's positions, so it's a pure client-
        # side narrowing in _load_entries(), not a new query.
        self.unmatched_only = QCheckBox("Unmatched only")
        self.unmatched_only.toggled.connect(self._load_entries)
        head.addWidget(self.unmatched_only)
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
        # Manually fix (or fill in) one position's match - acts on
        # whichever row was last clicked in entry_list below. Added
        # alongside services/charts.py:set_entry_track, since fuzzy
        # title/artist matching (match_entry) is never going to get every
        # position right.
        self.fix_match_btn = TouchButton("Fix match…")
        self.fix_match_btn.clicked.connect(self._fix_match)
        # Moved here from the old middle pane (removed 2026-08-30) - it acts
        # on whichever edition is currently shown below, same as before.
        self.delete_issue_btn = TouchButton("Delete edition")
        self.delete_issue_btn.clicked.connect(self.delete_selected_issue)
        for b in (
            self.play_btn, self.queue_btn, self.save_btn, self.rematch_btn,
            self.fix_match_btn, self.delete_issue_btn,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.entry_list = TouchList(row_height=76)
        # 2026-09-17 follow-up, James: a single click used to both select
        # AND immediately play the row (itemActivatedPayload fires off
        # TouchList's own itemClicked) - fine on its own, but it fights
        # "Fix match", which needs to select a row *without* also jumping
        # playback to it. Single click now only selects (Qt's own default
        # QAbstractItemView behaviour - nothing to wire up for that part);
        # double-click is what plays a specific position now.
        self.entry_list.itemDoubleClicked.connect(self._on_entry_double_clicked)
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
        self.fix_match_btn.setEnabled(self._issue_id is not None)
        self.unmatched_only.setEnabled(self._issue_id is not None)

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
                # Movement (NEW/up/down) used to be this row's whole trail
                # badge - 2026-09-17 follow-up, James: that badge "isn't
                # very useful" (it's "NEW" on every position of a Year-End
                # chart, which has no real last-week to compare against)
                # and the trail column itself should show the matched
                # file's location instead. Movement is still real
                # information on an actual weekly chart, so it moves into
                # the stats line below rather than disappearing outright.
                stats = []
                if entry.peak_pos:
                    stats.append(f"peak {entry.peak_pos}")
                stats.append(self._movement(entry.rank, entry.last_week))
                if entry.weeks_on_chart:
                    stats.append(f"{entry.weeks_on_chart} wks")
                owned_track = entry.track
                if owned_track is not None:
                    self._tracks.append(owned_track)
                media_file = owned_track.primary_file if owned_track is not None else None
                if media_file is not None:
                    # entry.track.files is already eager-loaded by
                    # issue_entries() (selectinload), so .primary_file
                    # costs nothing extra here.
                    file_name = Path(media_file.path).name
                    if media_file.is_missing:
                        file_name += " (missing)"
                    # The file's full containing directory (not just its
                    # immediate parent, and not the filename - that's
                    # already on the line above) - James, 2026-09-17
                    # follow-ups: expected "D:\Music\Billboard\1940-49\1946",
                    # not just "1946".
                    file_location = str(Path(media_file.path).parent)
                    file_color = COLORS["warn"] if media_file.is_missing else COLORS["text_dim"]
                elif owned_track is not None:
                    file_name, file_location, file_color = "no file on disk", "", COLORS["warn"]
                else:
                    file_name, file_location, file_color = "not in library", "", COLORS["text_dim"]
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
                    "trail": file_name,
                    "trail_color": file_color,
                    "trail2": file_location,
                    "color": COLORS["text"] if owned_track else COLORS["text_dim"],
                    "key": entry.track_id,
                    "entry_id": entry.id,
                })
        pct = int(100 * owned / total) if total else 0
        # The title/coverage line always describes the whole edition -
        # only the list below narrows when "Unmatched only" is checked,
        # so switching it on doesn't make "41 positions" read as a lie.
        self.entry_title.setText(f"{len(rows)} positions")
        self.coverage.setValue(pct)
        self.coverage_label.setText(f"{owned} of {total} in your library ({pct}%)")
        visible_rows = [r for r in rows if r["key"] is None] if self.unmatched_only.isChecked() else rows
        self.entry_list.set_rows(visible_rows)

    @staticmethod
    def _movement(rank: int, last_week: Optional[int]) -> str:
        # Used to be its own coloured trail badge - see _load_entries'
        # 2026-09-17 comment for why it folded into the plain-text stats
        # line instead, alongside peak/weeks-on-chart.
        if not last_week:
            return "NEW"
        delta = last_week - rank
        if delta > 0:
            return f"▲ {delta}"
        if delta < 0:
            return f"▼ {abs(delta)}"
        return "="

    # -- actions -------------------------------------------------------------

    def _on_entry_double_clicked(self, item) -> None:
        """Double-click plays that position - single click is reserved for
        selection only now (see entry_list's wiring comment above)."""
        payload = self.entry_list.payload_at(self.entry_list.row(item))
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

    def _fix_match(self) -> None:
        """Manually pick (or clear) which library track the currently
        selected position matches - for the fraction `match_entry`'s fuzzy
        matching gets wrong (a cover version, an unresolved "feat." credit,
        ...) or leaves unmatched entirely. Acts on whichever row is
        currently selected in entry_list - a single click there just
        selects now (see entry_list's wiring comment in _entry_pane), so
        clicking a row to line it up for "Fix match" no longer also jumps
        playback to it."""
        payload = self.entry_list.current_payload()
        entry_id = payload.get("entry_id") if payload else None
        if entry_id is None:
            self.ctx.notify("Select a position first")
            return
        with self.ctx.session() as session:
            entry = session.get(ChartEntry, entry_id)
            if entry is None:
                return
            title, artist, current_track_id = entry.title, entry.artist_name, entry.track_id
        dialog = MatchTrackDialog(
            self,
            self._search_tracks_for_match,
            self._import_and_match_file,
            self._get_last_browse_dir,
            self._set_last_browse_dir,
            initial_title=title,
            initial_artist=artist,
            current_track_id=current_track_id,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        if not dialog.clear_requested and dialog.chosen_track_id is None:
            return
        track_id = None if dialog.clear_requested else dialog.chosen_track_id
        with self.ctx.session() as session:
            chart_svc.set_entry_track(session, entry_id, track_id)
        # A pick made via "Browse for file..." may have just scanned a
        # brand-new track into the library - cheap to always emit this,
        # and every other view that cares (Tracks, Artists, search) is
        # already set up to react to it (see import_csv's own use below).
        self.ctx.libraryChanged.emit()
        self._load_entries()

    def _search_tracks_for_match(self, artist_query: str, track_query: str) -> list[dict]:
        with self.ctx.session() as session:
            return chart_svc.search_tracks_for_match(session, artist_query, track_query)

    def _get_last_browse_dir(self) -> Optional[str]:
        with self.ctx.session() as session:
            return chart_svc.get_last_browse_dir(session)

    def _set_last_browse_dir(self, directory: str) -> None:
        with self.ctx.session() as session:
            chart_svc.set_last_browse_dir(session, directory)

    def _import_and_match_file(self, path: str) -> Optional[dict]:
        """Backs MatchTrackDialog's "Browse for file..." button - for the
        case `_search_tracks_for_match` can never solve: the file exists
        on disk but was never scanned into the library at all, so there is
        no Track row for any search to find. Reuses the exact same
        per-file import a folder scan runs on every file it walks
        (`services/scanner.py:import_file`), so picking a file that's
        already known is harmless too - it just refreshes that file's
        existing row instead of creating a duplicate."""
        with self.ctx.session() as session:
            try:
                track = scanner_svc.import_file(session, Path(path), scanner_svc.ScanResult())
            except Exception:
                return None
            if track is None:
                return None
            return {
                "track_id": track.id,
                "title": track.title,
                "artist_name": track.artist_display or "",
                "album": track.release.title if track.release else "",
            }

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
