"""Sortable, filterable, multi-select chart table - the PC-density answer to
Charts' old touch-sized `TouchTree` list.

James's framing (2026-08-30): Charts is admin work done at a desk to seed
data you'll later turn into playlists, not something you tap through mid-
listen the way the rest of this touch-first app is - so unlike every other
list here, this one is deliberately NOT sized for a finger. Rows are dense,
columns are real (Name / Type / Coverage, click-to-sort), there's a search
box to filter by name, and rows multi-select so a bulk Move-to-folder or
Delete doesn't mean one chart at a time.

**Source dropped as a column (revised again, same day):** it started as a
fourth real column, but a folder-and-chart tree only has so much width to
give a Fixed column before the Stretch column (Name - the one thing you
actually read every row by) gets squeezed, and that got worse once editions
became a third tree level (see below) with its own indentation eating into
that same shared budget. James flagged the result directly - "look at all
the blank spots" - after Coverage got clipped once already and got fixed,
then Name itself started rendering blank on the newly-added edition rows.
Source (the CSV basename a chart was imported from) is useful sometimes but
not something you read every row for the way Name is, so it moved to a
tooltip on the Name cell instead of a column - freeing real width for what
actually needs it. `ChartLeafNode.source` still carries the value; it's
just not one of `COLUMNS` any more.

**Three tree levels, not two (revised 2026-08-30, same day):** the first cut
put a chart's individual dated editions in a completely separate middle
pane. James looked at the result and asked why editions weren't "in the main
column with just another subfolder" - fair: this screen already has folders
that expand to reveal charts, so charts expanding to reveal their editions
the same way is the more consistent shape, and it means one browse tree
instead of a tree plus a second list widget. So: Folder -> Chart -> Edition,
all in this one widget, and a chart gets the same chevron/whole-row-expands
treatment a folder already has - if it has any editions at all.

**Editions render as one spanned line, like a folder, not as their own
columns.** They never had a Type or Source to show anyway (only a date and
a coverage figure), so splitting them across the same Name/Type/Coverage
columns a chart uses left two of the three cells blank on every single
edition row - exactly the "blank spots" James was pointing at. An edition
now shows one line, `setFirstColumnSpanned(True)` same as a folder header:
"31 Dec 2024 · 83/100 in library" - immune to column-width squeezing
because there's no column boundary running through it at all.

**That spanning didn't actually work until later the same day - it was a
silent no-op the whole time.** `QTreeWidgetItem.setFirstColumnSpanned()`
only takes effect once the item already has a `treeWidget()` - called on a
freestanding item before it's attached to its parent (which is what both
`_make_folder_item` and `_make_issue_item` did, since they hand a finished
item back to the caller to attach), it silently does nothing, and calling
it later doesn't retroactively fix it either. So every "spanned" row here
was, invisibly, still three real columns the whole time - it just *looked*
like one line because the other two cells were empty and blended into the
unselected background. James caught it from a screenshot of a selected
edition row - "what is that blue box" - where selection colored those two
empty cells enough to show as their own boxes, plus a stray native-blue
highlight in the indentation strip. Fix: `setFirstColumnSpanned(True)` now
runs from the attachment call sites (`_add_level` for folders,
`_populate_chart_children` for editions), immediately after `addTopLevelItem`/
`addChild`, not inside the `_make_*_item` builders. Two more defenses went
in alongside it, since spanning had clearly been silently failing before
without anyone (including tests) catching it: `_SpanGuardDelegate` skips
painting columns 1+ outright for any item where `isFirstColumnSpanned()` is
true (Qt's QSS-based item painting doesn't suppress those columns on its
own, even when spanning genuinely is in effect), and
`QTreeWidget::branch:selected` is styled explicitly in theme.py instead of
falling back to the platform's native highlight blue for a selected row's
indentation area.

**Editions are populated lazily, on first expand, not eagerly with
everything else.** This isn't just tidiness: James's real Billboard Hot 100
Weekly chart has 3,393 editions. Building all of them as tree items (and
computing per-edition library coverage for each) on every single Charts
refresh - which happens on any library change, not just when you're looking
at that one chart - would be real, felt slowness for no benefit, since
almost every refresh nobody's even looking at that chart's editions. So
`ChartTable` never receives edition data up front the way it receives
folders and charts; it takes an `issues_provider` callback instead
(`chart_id -> Sequence[ChartIssueNode]`) and calls it only for a chart that
is actually expanded - persisted expand state included, so a chart you left
open before a refresh gets re-queried once (still just that one chart), not
every chart. `services/charts.py:issue_coverage_by_chart` is what makes even
that one query cheap regardless of edition count - see its docstring. A
chart's editions are also never force-expanded by the search filter the way
folders are (filter text never matches a date anyway, and force-expanding
every filtered chart's potentially-thousands of editions on every keystroke
would reintroduce the exact cost this design avoids) - only a chart's own
name/folder-membership decides whether it stays visible while filtering.

Folders keep the same "whole row toggles expansion" and chevron-in-text
behaviour `TouchTree` uses elsewhere (see ui/widgets/common.py), and stay
selectable - unlike `VideoTable`'s pure-divider group headers, a chart
folder is itself a first-class thing you Rename/Move/Delete, so it needs a
real payload and real selection, not just `Qt.ItemIsEnabled`. Charts get the
same expand/collapse chevron once they have at least one edition; a chart
with none renders as a plain leaf, same as before. Sorting only ever
reorders chart *leaves* within their folder (never editions, which always
stay in the newest-first order `services/charts.py:list_issues` already
returns); folders themselves always sort alphabetically, same rule
`VideoTable` uses for its artist groups - see that module's docstring for
why manual sort/rebuild replaces Qt's native `setSortingEnabled`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

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
    editions: int  # not its own column any more (see module docstring) - used
    # only to decide whether this chart gets an expand chevron, and shown in
    # its Name-column tooltip
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
    rule still fires its own separate rounded box for columns 1 and 2 too -
    visible as two empty "ghost" boxes next to any selected folder or edition
    row (both span column 0). James flagged this on an edition row ("what is
    that blue box"), but it's really two things: this one (present on folder
    rows too, since 2026-08-30's first pass at spanning, just less obvious at
    the top level) and the branch-guide color below. Fix here is to just not
    paint columns 1+ at all for a spanned row - there's nothing to show
    there anyway, so skipping the paint call removes the box outright rather
    than trying to recolor it to blend in."""

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
        # is drawn. Trimmed down now that there are three levels, not two.
        self.setIndentation(14)
        # Stylesheet painting doesn't suppress columns 1+ on a spanned row on
        # its own (see _SpanGuardDelegate) - without this, a selected folder
        # or edition row shows two empty "ghost" boxes where Type/Coverage
        # would be.
        self.setItemDelegate(_SpanGuardDelegate(self, self))
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # A real horizontal scrollbar, not AlwaysOff, is the safety net for
        # the bug fixed 2026-08-30: with it off and two Stretch columns
        # sharing whatever space the other Fixed columns didn't already
        # claim, a narrow pane (the Charts screen's browse column is ~300px
        # by default) squeezed Name down to a couple of pixels - showing
        # only its chevron - and silently clipped Coverage off the right
        # edge entirely, with no way to recover either. Now only Name
        # stretches (see below), and setMinimumSectionSize is the hard floor
        # that keeps every column readable even if the pane gets this narrow
        # again; scrolling only kicks in if that floor itself doesn't fit.
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
        # 80px used to clip "Year-End"/"Year End" to "Year-..." - the item's
        # own padding+margin (theme.py: 6px padding + 4px margin per side)
        # eats ~20px of the nominal column width before any text is drawn,
        # leaving less than the ~65px "Year End"/"Weekly" actually need.
        # James called the truncated result out directly: "The 'Year-....'
        # offers no value." 96px clears both labels with room to spare.
        header.resizeSection(COL_TYPE, 96)
        header.resizeSection(COL_COVERAGE, 84)
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
        needle = self._filter_text.lower()
        matched_folder_ids: set[int] = set()
        for chart in self._charts:
            if needle and needle not in chart.name.lower():
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
            # Must come AFTER the item is attached to the tree - called on a
            # freestanding item it's a silent no-op (no treeWidget() yet for
            # it to record the span against), which is exactly what made
            # every "spanned" row here quietly not-actually-spanned since
            # this was first added - see the module docstring's note on
            # James's "what is that blue box" report for how that surfaced.
            item.setFirstColumnSpanned(True)
            item.setExpanded(expanded)
            self._add_level(item, folder.id, visible_folder_ids)

        charts_here = [c for c in self._charts if c.parent_id == parent_id]
        needle = self._filter_text.lower()
        if needle:
            charts_here = [c for c in charts_here if needle in c.name.lower()]
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
