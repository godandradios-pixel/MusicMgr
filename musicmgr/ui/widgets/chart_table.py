"""Sortable, filterable, multi-select chart table for the Charts screen.

Charts is admin work done at a desk to seed data you'll later turn into
playlists, so unlike every other list in this touch-first app this one is
deliberately NOT sized for a finger. Rows are dense, columns are real
(Name / Type / Coverage, click-to-sort), a search box filters by name, and
rows multi-select for bulk Move-to-folder or Delete.

**Source is a tooltip, not a column.** Name is the one column you read
every row by and the sole Stretch column; Source (the CSV basename a chart
was imported from) lives in the Name cell's tooltip so it doesn't squeeze
Name. `ChartLeafNode.source` still carries the value.

**Three tree levels: Folder -> Chart -> Edition**, all in this one widget.
A chart with editions gets the same chevron/whole-row-expands treatment a
folder has; one with none renders as a plain leaf.

**Folder and edition rows are one spanned line**
(`setFirstColumnSpanned(True)`), e.g. "31 Dec 2024 · 83/100 in library",
so no column boundary runs through them. `setFirstColumnSpanned()` is a
silent no-op on an item not yet attached to a `treeWidget()`, so it's
called from the attachment sites (`_add_level`, `_populate_chart_children`)
right after `addTopLevelItem`/`addChild`, not in the `_make_*_item`
builders. `_SpanGuardDelegate` skips painting columns 1+ of a spanned row
(stylesheet painting otherwise draws empty selection boxes there), and
`QTreeWidget::branch:selected` is styled in theme.py so a selected row's
indentation isn't native highlight blue.

**Editions are populated lazily, on first expand.** A weekly chart can
have thousands of editions, and Charts refreshes on any library change, so
`ChartTable` never receives edition data up front; it takes an
`issues_provider` callback (`chart_id -> Sequence[ChartIssueNode]`) and
calls it only for a chart that is actually expanded (persisted expand state
included). `services/charts.py:issue_coverage_by_chart` keeps that one
query cheap. The search filter never force-expands a chart's editions
(filter text never matches a date, and it would reintroduce the cost) -
only a chart's own name/folder membership decides its visibility.

Folders keep the "whole row toggles expansion" and chevron-in-text
behaviour `TouchTree` uses elsewhere (see ui/widgets/common.py) and stay
selectable - a chart folder is itself something you Rename/Move/Delete, so
it needs a real payload and selection. Sorting only reorders chart
*leaves* within their folder (editions stay in the newest-first order
`services/charts.py:list_issues` returns); folders always sort
alphabetically, as in `VideoTable` - see that module's docstring for why
manual sort/rebuild replaces Qt's native `setSortingEnabled`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ...services.search_text import fold

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QScroller,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
)

#: node payload lives in column 0's UserRole+4, matching TouchTree's ROLE_NODE
#: so the two widgets stay drop-in-similar for callers/tests.
ROLE_NODE = Qt.UserRole + 4

COLUMNS = (
    ("name", "Name"),
    ("type", "Type"),
    ("coverage", "Coverage"),
)
COL_NAME = 0
COL_TYPE = 1
COL_COVERAGE = 2


@dataclass
class ChartFolderNode:
    id: int
    name: str
    parent_id: Optional[int]


@dataclass
class ChartLeafNode:
    id: int
    name: str
    parent_id: Optional[int]
    kind: str  # Chart.kind, carried through in the payload for callers that need it
    shape: str  # "Year-End" / "Weekly" / "—" - see services/charts.py:chart_shape
    source: str  # display text - already basename'd by the caller, or "—"
    editions: int  # not a column - decides whether this chart gets an expand
    # chevron, and shown in its Name-column tooltip
    coverage_pct: Optional[int]  # 0-100, or None when the chart has no entries at all
    coverage_label: str  # what the Coverage column shows - "44%" or "—"


@dataclass
class ChartIssueNode:
    id: int
    chart_id: int
    label: str  # display date, e.g. "31 Dec 2024" - already formatted by the caller
    coverage_pct: Optional[int]
    detail: str  # trailing text for the spanned row, e.g. "83/100 in library"


class _SpanGuardDelegate(QStyledItemDelegate):
    """Qt's QSS-based item painting doesn't honor setFirstColumnSpanned() the
    way the native (non-stylesheet) style does: the spanned column 0 rect is
    correctly widened to cover the whole row, but the app's `::item:selected`
    rule still draws its own rounded box for columns 1 and 2 - two empty
    "ghost" boxes beside any selected folder or edition row. Columns 1+ of a
    spanned row have nothing to show, so they're simply not painted."""

    def __init__(self, tree: "ChartTable", parent=None) -> None:
        super().__init__(parent)
        self._tree = tree

    def paint(self, painter, option, index) -> None:  # noqa: D102 - Qt override
        if index.column() != 0:
            item = self._tree.itemFromIndex(index)
            if item is not None and item.isFirstColumnSpanned():
                return
        super().paint(painter, option, index)


class ChartTable(QTreeWidget):
    itemActivatedPayload = Signal(object)  # the clicked node's payload dict
    selectionChangedPayloads = Signal(list)  # every currently-selected node's payload

    def __init__(
        self,
        parent=None,
        issues_provider: Optional[Callable[[int], Sequence[ChartIssueNode]]] = None,
    ) -> None:
        super().__init__(parent)
        self._folders: list[ChartFolderNode] = []
        self._charts: list[ChartLeafNode] = []
        self._chart_by_id: dict[int, ChartLeafNode] = {}
        self._issues_provider = issues_provider
        self._expanded_folder_ids: set[int] = set()
        self._expanded_chart_ids: set[int] = set()
        self._filter_text = ""
        self._sort_col = COL_NAME
        self._sort_asc = True

        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels([label for _, label in COLUMNS])
        self.setRootIsDecorated(False)
        # Qt's default (~20px/level) eats real width from every deeper row -
        # a folder > chart > edition item loses two levels' worth from the
        # same Name-column budget before a single character of its own text
        # is drawn, so it's kept small.
        self.setIndentation(14)
        # see _SpanGuardDelegate - no "ghost" boxes on spanned rows
        self.setItemDelegate(_SpanGuardDelegate(self, self))
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # A real horizontal scrollbar is the safety net for a narrow pane
        # (the Charts browse column is ~300px by default): only Name
        # stretches, setMinimumSectionSize keeps every column readable, and
        # scrolling kicks in only if that floor itself doesn't fit.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setAlternatingRowColors(True)
        # native sorting is off on purpose - see module docstring / VideoTable
        self.setSortingEnabled(False)

        header = self.header()
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(70)
        # Name is the one column that must never be crushed - it's the only
        # thing here you actually read a chart, folder or edition by - so
        # it's the sole Stretch column and gets everything left over; the
        # rest are all Fixed and kept lean.
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_TYPE, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_COVERAGE, QHeaderView.Fixed)
        # the item's padding+margin (theme.py: 6px + 4px per side) eats ~20px
        # of the column before any text, so 96px is what fits "Year End"
        header.resizeSection(COL_TYPE, 96)
        # 100px shows the "Coverage" header whole
        header.resizeSection(COL_COVERAGE, 100)
        # Type is hidden: charts are filed Source > Genre > Type
        # (tools/organize_charts_by_source.py names each chart "Year End" or
        # "Weekly"), so the column would repeat the name. Kept so it can
        # come back with one line.
        self.setColumnHidden(COL_TYPE, True)
        for col in (COL_TYPE, COL_COVERAGE):
            self.headerItem().setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
        header.sectionClicked.connect(self._on_header_clicked)

        QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
        self.itemClicked.connect(self._on_item_clicked)
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.itemExpanded.connect(self._sync_glyph)
        self.itemCollapsed.connect(self._sync_glyph)

        self._apply_sort_indicator()

    # -- data --------------------------------------------------------------

    def set_data(
        self, folders: Sequence[ChartFolderNode], charts: Sequence[ChartLeafNode]
    ) -> None:
        self._folders = list(folders)
        self._charts = list(charts)
        self._chart_by_id = {c.id: c for c in self._charts}
        self._rebuild()

    def set_issues_provider(
        self, provider: Optional[Callable[[int], Sequence[ChartIssueNode]]]
    ) -> None:
        self._issues_provider = provider

    def set_filter_text(self, text: str) -> None:
        text = text.strip()
        if text == self._filter_text:
            return
        self._filter_text = text
        self._rebuild()

    @property
    def expanded_folder_ids(self) -> set[int]:
        return set(self._expanded_folder_ids)

    def set_expanded_folder_ids(self, ids: set[int]) -> None:
        self._expanded_folder_ids = set(ids)

    @property
    def expanded_chart_ids(self) -> set[int]:
        return set(self._expanded_chart_ids)

    def set_expanded_chart_ids(self, ids: set[int]) -> None:
        self._expanded_chart_ids = set(ids)

    # -- sorting -------------------------------------------------------------

    def _on_header_clicked(self, col: int) -> None:
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._apply_sort_indicator()
        self._rebuild()

    def _apply_sort_indicator(self) -> None:
        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        self.header().setSortIndicator(self._sort_col, order)

    def _sort_value(self, chart: ChartLeafNode):
        key = COLUMNS[self._sort_col][0]
        if key == "type":
            return (chart.shape.lower(), chart.name.lower())
        if key == "coverage":
            pct = chart.coverage_pct if chart.coverage_pct is not None else -1
            return (pct, chart.name.lower())
        return chart.name.lower()  # "name", and the fallback

    def _sorted_charts(self, charts: Sequence[ChartLeafNode]) -> list[ChartLeafNode]:
        return sorted(charts, key=self._sort_value, reverse=not self._sort_asc)

    # -- building -----------------------------------------------------------

    def _rebuild(self) -> None:
        self.setUpdatesEnabled(False)
        self.clear()

        charts_by_parent: dict[Optional[int], list[ChartLeafNode]] = {}
        needle = fold(self._filter_text)
        matched_folder_ids: set[int] = set()
        for chart in self._charts:
            if needle and needle not in fold(chart.name):
                continue
            charts_by_parent.setdefault(chart.parent_id, []).append(chart)

        if needle:
            # a folder stays visible only if a match lives somewhere under it
            # (top-level matches, keyed by parent_id None, need no folder at
            # all to stay visible, so they're simply not in this frontier)
            by_id = {f.id: f for f in self._folders}
            frontier = {fid for fid in charts_by_parent if fid is not None}
            while frontier:
                fid = frontier.pop()
                if fid in matched_folder_ids or fid not in by_id:
                    continue
                matched_folder_ids.add(fid)
                parent_id = by_id[fid].parent_id
                if parent_id is not None:
                    frontier.add(parent_id)

        self._add_level(self, None, matched_folder_ids if needle else None)
        self.setUpdatesEnabled(True)

    def _add_level(
        self,
        parent_widget,
        parent_id: Optional[int],
        visible_folder_ids: Optional[set[int]],
    ) -> None:
        children = sorted(
            (f for f in self._folders if f.parent_id == parent_id), key=lambda f: f.name.lower()
        )
        for folder in children:
            if visible_folder_ids is not None and folder.id not in visible_folder_ids:
                continue
            expanded = bool(self._filter_text) or folder.id in self._expanded_folder_ids
            item = self._make_folder_item(folder, expanded)
            if isinstance(parent_widget, QTreeWidget):
                parent_widget.addTopLevelItem(item)
            else:
                parent_widget.addChild(item)
            # must come AFTER the item is attached to the tree - on a
            # freestanding item it's a silent no-op (see module docstring)
            item.setFirstColumnSpanned(True)
            item.setExpanded(expanded)
            self._add_level(item, folder.id, visible_folder_ids)

        charts_here = [c for c in self._charts if c.parent_id == parent_id]
        needle = fold(self._filter_text)
        if needle:
            charts_here = [c for c in charts_here if needle in fold(c.name)]
        for chart in self._sorted_charts(charts_here):
            has_children = chart.editions > 0
            # never force-expand a chart's editions just because a filter is
            # active - see module docstring for why (cost, and dates aren't
            # what the filter box searches anyway)
            expanded = (
                has_children and not self._filter_text and chart.id in self._expanded_chart_ids
            )
            item = self._make_chart_item(chart, expanded, has_children)
            if isinstance(parent_widget, QTreeWidget):
                parent_widget.addTopLevelItem(item)
            else:
                parent_widget.addChild(item)
            if expanded:
                self._populate_chart_children(item, chart.id)
                item.setExpanded(True)

    def _populate_chart_children(self, item: QTreeWidgetItem, chart_id: int) -> None:
        if self._issues_provider is None:
            return
        for issue in self._issues_provider(chart_id):
            child = self._make_issue_item(issue)
            item.addChild(child)
            # Same ordering requirement as folders above - spanning a
            # freestanding item is a no-op, so this has to happen after
            # addChild, not inside _make_issue_item before it's attached.
            child.setFirstColumnSpanned(True)

    def _make_folder_item(self, folder: ChartFolderNode, expanded: bool) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [("▾ " if expanded else "▸ ") + folder.name, "", ""]
        )
        item.setData(0, ROLE_NODE, {"type": "folder", "id": folder.id, "name": folder.name})
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        # Spanning is set by the caller once this item is actually attached
        # to the tree - see _add_level.
        return item

    def _make_chart_item(
        self, chart: ChartLeafNode, expanded: bool, has_children: bool
    ) -> QTreeWidgetItem:
        prefix = ("▾ " if expanded else "▸ ") if has_children else ""
        item = QTreeWidgetItem([prefix + chart.name, chart.shape, chart.coverage_label])
        item.setTextAlignment(COL_TYPE, Qt.AlignRight | Qt.AlignVCenter)
        item.setTextAlignment(COL_COVERAGE, Qt.AlignRight | Qt.AlignVCenter)
        tooltip_lines = [chart.name]
        if has_children:
            plural = "" if chart.editions == 1 else "s"
            tooltip_lines.append(f"{chart.editions} edition{plural}")
        if chart.source and chart.source != "—":
            tooltip_lines.append(f"Source: {chart.source}")
        item.setToolTip(COL_NAME, "\n".join(tooltip_lines))
        item.setData(
            0, ROLE_NODE, {"type": "chart", "id": chart.id, "kind": chart.kind, "name": chart.name}
        )
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def _make_issue_item(self, issue: ChartIssueNode) -> QTreeWidgetItem:
        text = f"{issue.label}   ·   {issue.detail}" if issue.detail else issue.label
        item = QTreeWidgetItem([text, "", ""])
        item.setData(
            0,
            ROLE_NODE,
            {"type": "issue", "id": issue.id, "chart_id": issue.chart_id, "name": issue.label},
        )
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        # Spanning is set by the caller once this item is actually attached
        # to the tree - see _populate_chart_children.
        return item

    def _sync_glyph(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, ROLE_NODE) or {}
        ntype = payload.get("type")
        if ntype not in ("folder", "chart"):
            return
        item.setText(0, ("▾ " if item.isExpanded() else "▸ ") + payload.get("name", ""))
        ids = self._expanded_folder_ids if ntype == "folder" else self._expanded_chart_ids
        node_id = payload.get("id")
        if item.isExpanded():
            ids.add(node_id)
        else:
            ids.discard(node_id)

    # -- selection / activation ----------------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        payload = item.data(0, ROLE_NODE) or {}
        modifiers = QApplication.keyboardModifiers()
        extending = bool(modifiers & (Qt.ControlModifier | Qt.ShiftModifier))
        if not extending:
            # a chart that hasn't been expanded yet has no children in the
            # tree at all (see module docstring - editions are lazy) so they
            # have to be fetched and added before setExpanded(True) has
            # anything to reveal
            if payload.get("type") == "chart" and item.childCount() == 0 and not item.isExpanded():
                self._populate_chart_children(item, payload.get("id"))
            if item.childCount() > 0:
                item.setExpanded(not item.isExpanded())
        self.itemActivatedPayload.emit(payload)

    def _on_selection_changed(self) -> None:
        payloads = [item.data(0, ROLE_NODE) for item in self.selectedItems()]
        self.selectionChangedPayloads.emit([p for p in payloads if p])

    def payload_of(self, item: Optional[QTreeWidgetItem]) -> Optional[dict]:
        return item.data(0, ROLE_NODE) if item is not None else None

    def current_payload(self) -> Optional[dict]:
        return self.payload_of(self.currentItem())

    def selected_payloads(self) -> list[dict]:
        return [p for p in (self.payload_of(i) for i in self.selectedItems()) if p]

    def select_node(self, matches: Callable[[dict], bool]) -> bool:
        """Select and reveal the first item whose payload satisfies `matches` -
        same contract as TouchTree.select_node, used to restore selection
        across a refresh(). Only ever finds an "issue" node if its parent
        chart happens to already be expanded (rendered) - editions of a
        collapsed chart simply aren't in the tree yet, see module docstring;
        callers restoring an edition selection should treat "not found" as
        "still exists, just not visible right now," not "gone.\""""
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            payload = self.payload_of(item) or {}
            if matches(payload):
                self.setCurrentItem(item)
                self.clearSelection()
                item.setSelected(True)
                self.scrollToItem(item)
                return True
            stack.extend(item.child(i) for i in range(item.childCount()))
        return False

    # -- test / inspection helpers -------------------------------------------

    def _visible_names(self, node_type: str) -> list[str]:
        names: list[str] = []

        def walk(item) -> None:
            payload = self.payload_of(item) or {}
            if payload.get("type") == node_type:
                names.append(payload.get("name", ""))
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return names

    def visible_chart_names(self) -> list[str]:
        """Chart leaf names in the order currently rendered - respects sort,
        grouping and the active filter."""
        return self._visible_names("chart")

    def visible_folder_names(self) -> list[str]:
        return self._visible_names("folder")

    def visible_issue_labels(self) -> list[str]:
        """Edition labels currently rendered - only ever non-empty for a
        chart that's actually expanded, since editions are lazy (see module
        docstring)."""
        return self._visible_names("issue")
