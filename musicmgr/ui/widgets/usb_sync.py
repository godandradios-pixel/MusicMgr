"""USB sync UI pieces (2026-09-23) - the threads and dialogs behind
Settings → USB sync. All the real work is in services/usb_sync.py (headless,
tested there); see its module docstring and
claude/2026-09-23-usb-sync-plan.md for the design."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from ...db.session import session_scope
from ...services import usb_sync as us
from .common import TouchButton, dim_label

log = logging.getLogger(__name__)


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit in ("B", "KB") else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------
# threads
# --------------------------------------------------------------------------


class UsbCompareThread(QThread):
    """Walks both sides of every enabled pair and builds the plan. Only
    reads files; the database gets this PC's id and any new default pairs.
    `finished_with(plan_or_None, error_or_None)`."""

    progress = Signal(str)
    finished_with = Signal(object, object)

    def __init__(self, drive: us.Drive, auto: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.drive = drive
        self.auto = auto

    def run(self) -> None:  # pragma: no cover - exercised interactively / service tests
        try:
            with session_scope() as db:
                plan = us.build_plan(
                    db, self.drive, progress=self.progress.emit,
                    should_stop=self.isInterruptionRequested,
                )
        except Exception as exc:
            log.exception("USB compare failed")
            self.finished_with.emit(None, str(exc))
            return
        self.finished_with.emit(plan, None)


class UsbSyncThread(QThread):
    """Applies the checked rows, then imports what arrived and marks what
    left. `finished_with(result_or_None, library_message, error_or_None)`."""

    progress = Signal(int, int, str)
    finished_with = Signal(object, str, object)

    def __init__(self, plan: us.ComparePlan, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan

    def run(self) -> None:  # pragma: no cover - exercised interactively / service tests
        try:
            with session_scope() as db:
                result = us.apply(
                    db, self.plan,
                    progress=lambda done, total, name: self.progress.emit(done, total, name),
                    should_stop=self.isInterruptionRequested,
                )
            with session_scope() as db:
                message = us.update_library(db, result)
            if not result.cancelled:
                message = self._sync_library_state(result, message)
        except Exception as exc:
            log.exception("USB sync failed")
            self.finished_with.emit(None, "", str(exc))
            return
        self.finished_with.emit(result, message, None)

    def _sync_library_state(self, result, message: str) -> str:
        """Step 3 (2026-09-24): plays, ratings, playlists and the Jukebox
        board, merged through the drive - see services/library_state.py.
        A failure here never undoes the file sync; it's just reported."""
        from ...services import library_state

        try:
            with session_scope() as db:
                notes = library_state.sync_library_state(db, self.plan.drive, us.get_pc_id(db))
        except Exception as exc:
            log.exception("library data sync failed")
            return (message + " · " if message else "") + f"Library data sync failed: {exc}"
        result.library_notes = notes
        text = notes.summary()
        return f"{message} · {text}" if message else text


# --------------------------------------------------------------------------
# review dialog
# --------------------------------------------------------------------------

_ROLE = Qt.UserRole + 1


class _ItemTree(QTreeWidget):
    """One tab: pair → top folder (usually the artist) → files, with
    tri-state checkboxes."""

    COLS = ["File", "Size", "Detail"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(self.COLS)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.entries: list[us.SyncItem] = []
        #: the PairPlan each entry came from (same index as `entries`)
        self.entry_pairs: list[us.PairPlan] = []

    def populate(self, plan: us.ComparePlan, keep, detail) -> int:
        self.clear()
        self.entries = []
        self.entry_pairs = []
        count = 0
        for pp in plan.pairs:
            items = [i for i in pp.items if keep(i)]
            if not items:
                continue
            top = QTreeWidgetItem([pp.label, "", ""])
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            groups: dict[str, QTreeWidgetItem] = {}
            group_sizes: dict[str, int] = {}
            for item in items:
                head, _, rest = item.rel.partition("/")
                group_key = head if rest else "(top folder)"
                group = groups.get(group_key)
                if group is None:
                    group = QTreeWidgetItem([group_key, "", ""])
                    group.setFlags(group.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                    top.addChild(group)
                    groups[group_key] = group
                    group_sizes[group_key] = 0
                leaf = QTreeWidgetItem([rest or head, human_size(item.size), detail(item)])
                leaf.setFlags(leaf.flags() | Qt.ItemIsUserCheckable)
                leaf.setCheckState(0, Qt.Checked if item.selected else Qt.Unchecked)
                leaf.setData(0, _ROLE, len(self.entries))
                leaf.setToolTip(0, item.rel)
                self.entries.append(item)
                self.entry_pairs.append(pp)
                group.addChild(leaf)
                group_sizes[group_key] += item.size
                count += 1
            for key, group in groups.items():
                group.setText(1, human_size(group_sizes[key]))
                n = group.childCount()
                group.setText(2, f"{n:,} file{'' if n == 1 else 's'}")
            n = len(items)
            top.setText(0, f"{pp.label}  ·  {n:,} file{'' if n == 1 else 's'}")
            self.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)
            top.setExpanded(True)
            if len(groups) <= 20:
                for group in groups.values():
                    group.setExpanded(True)
        return count

    def leaves(self):
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            node = stack.pop()
            if node.childCount():
                stack.extend(node.child(i) for i in range(node.childCount()))
            else:
                idx = node.data(0, _ROLE)
                if idx is not None:
                    yield node, self.entries[idx]

    def read_back(self) -> None:
        for node, item in self.leaves():
            item.selected = node.checkState(0) == Qt.Checked

    def refresh_details(self, detail) -> None:
        for node, item in self.leaves():
            node.setText(2, detail(item))

    def checked_items(self) -> list[us.SyncItem]:
        return [item for node, item in self.leaves() if node.checkState(0) == Qt.Checked]

    def current_entry(self) -> Optional[tuple[us.SyncItem, us.PairPlan]]:
        node = self.currentItem()
        idx = node.data(0, _ROLE) if node is not None else None
        if idx is None:
            return None
        return self.entries[idx], self.entry_pairs[idx]


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


class _ConflictPreview(QWidget):
    """Both versions of a conflicting picture side by side (2026-09-24,
    artwork sync) - for an image, "which one?" is best answered by looking.
    Hidden for anything that isn't an image."""

    SIZE = 180

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 0)
        self.local = QLabel()
        self.usb = QLabel()
        for title, label in (("This PC", self.local), ("USB", self.usb)):
            col = QVBoxLayout()
            col.addWidget(dim_label(title))
            label.setFixedSize(self.SIZE, self.SIZE)
            label.setAlignment(Qt.AlignCenter)
            col.addWidget(label)
            row.addLayout(col)
        row.addStretch(1)
        self.setVisible(False)

    def show_item(self, entry) -> None:
        from PySide6.QtGui import QPixmap

        if entry is None:
            self.setVisible(False)
            return
        item, pp = entry
        rel_local = item.local_rel or item.rel
        rel_usb = item.usb_rel or item.rel
        if Path(item.rel).suffix.lower() not in _IMAGE_EXTS:
            self.setVisible(False)
            return
        for label, path in ((self.local, pp.local_root / rel_local), (self.usb, pp.usb_root / rel_usb)):
            pix = QPixmap(str(path))
            if pix.isNull():
                label.setText("(can't show)")
            else:
                label.setPixmap(pix.scaled(self.SIZE, self.SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.setVisible(True)


def _conflict_detail(item: us.SyncItem) -> str:
    keep = "keep this PC's" if item.direction == us.TO_USB else "keep the USB's"
    return f"both changed — {keep} (other kept as a backup)"


def _delete_detail(item: us.SyncItem) -> str:
    gone_from = "the USB" if item.remaining_side == "local" else "this PC"
    still_on = "this PC" if item.remaining_side == "local" else "the USB"
    if item.restore:
        action = f"restore to {gone_from}"
    else:
        action = "delete here → Recycle Bin" if item.remaining_side == "local" else "delete from USB → _deleted"
    note = " · changed since last sync" if item.changed_since else ""
    return f"deleted on {gone_from}, still on {still_on} — {action}{note}"


def _copy_detail(item: us.SyncItem) -> str:
    if item.local and item.usb:
        return "changed"
    return "new"


def _rename_detail(item: us.SyncItem) -> str:
    side = "USB" if item.direction == us.TO_USB else "this PC"
    return f"rename on {side} from {item.old_rel}"


class UsbSyncDialog(QDialog):
    """Review what a USB check found, then Sync. Nothing is copied or
    deleted until Sync is pressed; deletions start unchecked."""

    def __init__(self, plan: us.ComparePlan, local_free=None, usb_free=None, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("USB sync")
        self.setMinimumSize(860, 600)
        self._usb_free = usb_free if usb_free is not None else us.free_space(plan.drive.root)
        self._local_free = local_free
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel(f"{plan.drive.describe()}")
        title.setObjectName("Crumb")
        layout.addWidget(title)
        self.totals = dim_label("")
        self.totals.setWordWrap(True)
        layout.addWidget(self.totals)
        for pp in plan.pairs:
            notes = [f"⚠ {err}" for err in pp.errors]
            if pp.baseline_source == "none" and pp.items:
                notes.append(
                    f"First sync for {pp.label} — files on only one side are copied across; "
                    "nothing is treated as deleted yet."
                )
            for note in notes:
                label = dim_label(note)
                label.setWordWrap(True)
                layout.addWidget(label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.to_usb = _ItemTree()
        self.to_local = _ItemTree()
        self.conflicts = _ItemTree()
        self.deletions = _ItemTree()
        self.renames = _ItemTree()
        spec = [
            (self.to_local, "From USB", lambda i: i.kind == us.COPY and i.direction == us.TO_LOCAL, _copy_detail),
            (self.to_usb, "To USB", lambda i: i.kind == us.COPY and i.direction == us.TO_USB, _copy_detail),
            (self.conflicts, "Conflicts", lambda i: i.kind == us.CONFLICT, _conflict_detail),
            (self.deletions, "Deletions", lambda i: i.kind == us.DELETE, _delete_detail),
            (self.renames, "Renames", lambda i: i.kind == us.RENAME, _rename_detail),
        ]
        for tree, name, keep, detail in spec:
            n = tree.populate(plan, keep, detail)
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 6, 0, 0)
            page_layout.addWidget(tree, 1)
            if tree is self.conflicts and n:
                self.conflict_preview = _ConflictPreview()
                tree.currentItemChanged.connect(
                    lambda *_: self.conflict_preview.show_item(self.conflicts.current_entry())
                )
                page_layout.addWidget(self.conflict_preview)
                row = QHBoxLayout()
                a = TouchButton("Keep this PC's version")
                a.clicked.connect(lambda: self._set_direction(us.TO_USB))
                b = TouchButton("Keep the USB's version")
                b.clicked.connect(lambda: self._set_direction(us.TO_LOCAL))
                row.addWidget(dim_label("For checked rows:"))
                row.addWidget(a)
                row.addWidget(b)
                row.addStretch(1)
                page_layout.addLayout(row)
            if tree is self.deletions and n:
                page_layout.addWidget(dim_label(
                    "Checked rows are applied. Deleted files are never lost: this PC's go to the "
                    "Recycle Bin, the USB's to MusicMgr\\sync\\_deleted. Unchecked rows are asked "
                    "about again next time."
                ))
                row = QHBoxLayout()
                d = TouchButton("Delete checked")
                d.clicked.connect(lambda: self._set_restore(False))
                r = TouchButton("Restore checked instead")
                r.clicked.connect(lambda: self._set_restore(True))
                row.addWidget(d)
                row.addWidget(r)
                row.addStretch(1)
                page_layout.addLayout(row)
            idx = self.tabs.addTab(page, f"{name} ({n:,})")
            self.tabs.setTabEnabled(idx, n > 0)
            tree.itemChanged.connect(self._schedule_totals)
        for i in range(self.tabs.count()):
            if self.tabs.isTabEnabled(i):
                self.tabs.setCurrentIndex(i)
                break

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = TouchButton("Close")
        self.cancel_btn.clicked.connect(self.reject)
        self.sync_btn = TouchButton("Sync", primary=True)
        self.sync_btn.setObjectName("PrimaryWarm")
        self.sync_btn.clicked.connect(self._on_sync)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.sync_btn)
        layout.addLayout(buttons)

        self._totals_timer = QTimer(self)
        self._totals_timer.setSingleShot(True)
        self._totals_timer.setInterval(120)
        self._totals_timer.timeout.connect(self._update_totals)
        self._update_totals()

    def _trees(self):
        return (self.to_usb, self.to_local, self.conflicts, self.deletions, self.renames)

    def _schedule_totals(self, *_):
        self._totals_timer.start()

    def _read_back(self) -> None:
        for tree in self._trees():
            tree.read_back()

    def _set_direction(self, direction: str) -> None:
        for item in self.conflicts.checked_items():
            item.direction = direction
        self.conflicts.refresh_details(_conflict_detail)
        self._update_totals()

    def _set_restore(self, restore: bool) -> None:
        for item in self.deletions.checked_items():
            item.restore = restore
        self.deletions.refresh_details(_delete_detail)
        self._update_totals()

    def _update_totals(self) -> None:
        self._read_back()
        items = [i for i in self.plan.all_items() if i.selected]
        need = us.bytes_needed(self.plan)
        n_up = sum(1 for i in items if i.kind in (us.COPY, us.CONFLICT) and i.direction == us.TO_USB)
        n_down = sum(1 for i in items if i.kind in (us.COPY, us.CONFLICT) and i.direction == us.TO_LOCAL)
        n_del = sum(1 for i in items if i.kind == us.DELETE)
        parts = [
            f"↓ {n_down:,} file{'' if n_down == 1 else 's'} · {human_size(need[us.TO_LOCAL])} to this PC",
            f"↑ {n_up:,} file{'' if n_up == 1 else 's'} · {human_size(need[us.TO_USB])} to USB",
        ]
        if n_del:
            parts.append(f"{n_del:,} deletions/restores")
        text = "   ".join(parts)
        problems = []
        if self._usb_free is not None and need[us.TO_USB] > self._usb_free:
            problems.append(f"the USB has only {human_size(self._usb_free)} free")
        local_free = self._local_free
        if local_free is None and self.plan.pairs:
            local_free = us.free_space(self.plan.pairs[0].local_root)
        if local_free is not None and need[us.TO_LOCAL] > local_free:
            problems.append(f"this PC has only {human_size(local_free)} free")
        if problems:
            text += "\n⚠ Not enough room: " + "; ".join(problems)
        self.totals.setText(text)
        self.sync_btn.setEnabled(bool(items) and not problems)

    def _on_sync(self) -> None:
        self._read_back()
        deletions = [i for i in self.plan.all_items() if i.selected and i.kind == us.DELETE and not i.restore]
        if deletions:
            confirm = QMessageBox.question(
                self,
                "Apply deletions",
                f"Delete {len(deletions):,} file{'s' if len(deletions) != 1 else ''}? "
                "This PC's go to the Recycle Bin; the USB's are moved to "
                "MusicMgr\\sync\\_deleted, so both can be recovered.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        self.accept()


# --------------------------------------------------------------------------
# folder pairs
# --------------------------------------------------------------------------


class UsbPairsDialog(QDialog):
    """Folder pairs for one drive: turn them on/off, change either folder,
    remove one, or add one for a folder MusicMgr doesn't watch (e.g.
    MusicMP3). Nothing is saved until OK.

    2026-09-24 - James: "how do I edit a folder pair, or delete?" (the first
    version could only switch pairs on/off and add new ones). An artwork
    pair's folder on this PC can't be changed: it is always this library's
    own data\\artwork / data\\artists - to sync D:\\MusicMgr\\data, run the
    MusicMgr that uses D:\\MusicMgr\\data."""

    def __init__(self, drive: us.Drive, parent=None) -> None:
        super().__init__(parent)
        self.drive = drive
        self.setWindowTitle("USB folder pairs")
        self.setMinimumSize(760, 460)
        layout = QVBoxLayout(self)
        intro = dim_label(
            f"Folders kept in two-way sync with {drive.describe()}. Watched folders are paired "
            "automatically with the USB folder of the same name. Artwork pairs always use this "
            "library's own data folder."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.add_btn = TouchButton("Add pair…")
        self.add_btn.clicked.connect(self._add_pair)
        self.local_btn = TouchButton("Change PC folder…")
        self.local_btn.clicked.connect(self._change_local)
        self.usb_btn = TouchButton("Change USB folder…")
        self.usb_btn.clicked.connect(self._change_usb)
        self.remove_btn = TouchButton("Remove")
        self.remove_btn.clicked.connect(self._remove)
        for b in (self.add_btn, self.local_btn, self.usb_btn, self.remove_btn):
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        #: the working copy - dicts with id (None = new), local, rel, kind,
        #: enabled, removed, orig_local, orig_rel
        self.rows: list[dict] = []
        with session_scope() as db:
            for pair in us.ensure_default_pairs(db, drive):
                if us.is_removed(pair):
                    continue
                self.rows.append({
                    "id": pair.id, "local": pair.local_path, "rel": pair.usb_rel_path,
                    "kind": pair.kind, "enabled": pair.enabled, "removed": False,
                    "orig_local": pair.local_path, "orig_rel": pair.usb_rel_path,
                })
        self._render()

    # -- view -----------------------------------------------------------------

    def _visible(self) -> list[dict]:
        return [r for r in self.rows if not r["removed"]]

    def _render(self, select: Optional[int] = None) -> None:
        self._read_checks()
        self.list.clear()
        for r in self._visible():
            label = f"{r['local']}  ↔  USB\\{r['rel'].replace('/', chr(92))}"
            if r["id"] is None:
                label += "  (new)"
            elif (r["local"], r["rel"]) != (r["orig_local"], r["orig_rel"]):
                label += "  (changed)"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if r["enabled"] else Qt.Unchecked)
            self.list.addItem(item)
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)
        self._update_buttons()

    def _read_checks(self) -> None:
        visible = self._visible()
        for i in range(min(self.list.count(), len(visible))):
            visible[i]["enabled"] = self.list.item(i).checkState() == Qt.Checked

    def _current(self) -> Optional[dict]:
        row = self.list.currentRow()
        visible = self._visible()
        return visible[row] if 0 <= row < len(visible) else None

    def _update_buttons(self) -> None:
        cur = self._current()
        self.local_btn.setEnabled(cur is not None and cur["kind"] != "artwork")
        self.usb_btn.setEnabled(cur is not None)
        self.remove_btn.setEnabled(cur is not None)

    # -- folder pickers (overridable seams for tests) ---------------------------

    def _pick_local(self, start: str = "") -> str:  # pragma: no cover - file dialog
        return QFileDialog.getExistingDirectory(self, "Folder on this PC", start)

    def _pick_usb(self, start: str = "") -> str:  # pragma: no cover - file dialog
        return QFileDialog.getExistingDirectory(self, "Folder on the USB drive", start or str(self.drive.root))

    def _usb_rel(self, usb: str) -> Optional[str]:
        try:
            rel = Path(usb).resolve().relative_to(self.drive.root.resolve())
        except ValueError:
            QMessageBox.warning(self, "USB folder pairs", "Pick a folder on the MusicMgr USB drive.")
            return None
        if not str(rel) or str(rel) == ".":
            QMessageBox.warning(self, "USB folder pairs", "Pick a folder inside the drive, not the drive itself.")
            return None
        return str(rel).replace("\\", "/")

    def _taken(self, local: str, skip: Optional[dict] = None) -> bool:
        key = str(Path(local)).casefold()
        return any(r is not skip and str(Path(r["local"])).casefold() == key for r in self._visible())

    # -- actions ----------------------------------------------------------------

    def _add_pair(self) -> None:
        local = self._pick_local()
        if not local:
            return
        if self._taken(local):
            QMessageBox.warning(self, "USB folder pairs", f"{local} is already paired.")
            return
        rel = self._usb_rel(self._pick_usb())
        if not rel:
            return
        # a folder removed earlier comes back as that same pair
        revived = next((r for r in self.rows if r["removed"]
                        and str(Path(r["local"])).casefold() == str(Path(local)).casefold()), None)
        if revived is not None:
            revived.update(removed=False, rel=rel, enabled=True)
        else:
            self.rows.append({"id": None, "local": str(Path(local)), "rel": rel, "kind": "media",
                              "enabled": True, "removed": False, "orig_local": None, "orig_rel": None})
        self._render(select=len(self._visible()) - 1)

    def _change_local(self) -> None:
        cur = self._current()
        if cur is None or cur["kind"] == "artwork":
            return
        local = self._pick_local(cur["local"])
        if not local or self._taken(local, skip=cur):
            if local:
                QMessageBox.warning(self, "USB folder pairs", f"{local} is already paired.")
            return
        cur["local"] = str(Path(local))
        self._render(select=self.list.currentRow())

    def _change_usb(self) -> None:
        cur = self._current()
        if cur is None:
            return
        rel = self._usb_rel(self._pick_usb(str(self.drive.root / cur["rel"])))
        if not rel:
            return
        cur["rel"] = rel
        self._render(select=self.list.currentRow())

    def _remove(self) -> None:
        cur = self._current()
        if cur is None:
            return
        row = self.list.currentRow()
        self._read_checks()
        if cur["id"] is None:
            self.rows.remove(cur)
        else:
            cur["removed"] = True
        self._render(select=min(row, len(self._visible()) - 1))

    def _save(self) -> None:
        self._read_checks()
        changed = [r for r in self.rows if r["id"] is not None and not r["removed"]
                   and (r["local"], r["rel"]) != (r["orig_local"], r["orig_rel"])]
        removed = [r for r in self.rows if r["id"] is not None and r["removed"]]
        if changed or removed:
            lines = [f"Stop syncing {r['orig_local']}" for r in removed]
            lines += [f"Change {r['orig_local']} ↔ USB\\{r['orig_rel']}  →  {r['local']} ↔ USB\\{r['rel']}"
                      for r in changed]
            confirm = QMessageBox.question(
                self, "USB folder pairs",
                "\n".join(lines) + "\n\nNo files are moved or deleted. A changed pair starts "
                "fresh: its next check copies files that are only on one side and treats nothing "
                "as deleted.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        with session_scope() as db:
            for r in self.rows:
                if r["id"] is None:
                    if not r["removed"]:
                        pair = us.add_pair(db, self.drive, r["local"], r["rel"])
                        us.set_pair_enabled(db, pair.id, r["enabled"])
                    continue
                if r["removed"]:
                    us.remove_pair(db, r["id"])
                    continue
                us.update_pair(db, r["id"], local_path=r["local"], usb_rel_path=r["rel"])
                us.set_pair_enabled(db, r["id"], r["enabled"])
        self.accept()
