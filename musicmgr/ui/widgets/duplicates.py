"""Settings › Duplicates - the search thread and the results dialog
(2026-10-13, see services/duplicates.py for what's found and why)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...db.session import session_scope
from ...services import duplicates as dup
from .common import TouchButton, dim_label


class DuplicateScanThread(QThread):
    """Runs services.duplicates.find_duplicates off the UI thread."""

    progress = Signal(int, int, str)
    finished_with = Signal(object)  # DuplicateReport

    def run(self) -> None:  # pragma: no cover - exercised interactively
        with session_scope() as session:
            report = dup.find_duplicates(
                session,
                progress=lambda done, total, name: self.progress.emit(done, total, name),
                should_stop=self.isInterruptionRequested,
            )
        self.finished_with.emit(report)


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


def _where(f: dup.FileInfo) -> str:
    if f.chart:
        return f"{f.chart} folder"
    return f.album


class DuplicatesDialog(QDialog):
    """Two tabs: playlist songs to move off chart copies (with the switch
    button), and sets of identical files (a list to act on - nothing is
    deleted from here)."""

    #: emitted after playlists were switched, with how many entries changed
    switched = Signal(int)

    def __init__(self, report: dup.DuplicateReport, parent=None) -> None:
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("Duplicates")
        self.resize(1100, 720)
        layout = QVBoxLayout(self)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.summary)
        if report.cancelled:
            layout.addWidget(dim_label("Stopped early - these are partial results."))

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_switch_tab(), "Playlist songs")
        self.tabs.addTab(self._build_groups_tab(), "Identical files")

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = TouchButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)
        self._refresh_switches()

    # -- playlist songs ---------------------------------------------------

    def _build_switch_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        intro = dim_label(
            "Playlist songs that point at a copy in the Billboard, Hot Country or Oldies "
            "folder, and the album copy each would move to. The identical recording is "
            "used when there is one; otherwise the same song by the same artist with "
            "about the same length. Each song keeps its place in its playlist."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Lengths may differ by up to"))
        self.length_combo = QComboBox()
        for ms in dup.LENGTH_STEPS_MS:
            label = f"{ms // 1000} seconds"
            if ms == dup.SAME_SONG_MAX_DIFF_MS:
                label += " (recommended)"
            self.length_combo.addItem(label, ms)
        self.length_combo.setCurrentIndex(
            max(0, self.length_combo.findData(dup.SAME_SONG_MAX_DIFF_MS)))
        self.length_combo.currentIndexChanged.connect(self._refresh_switches)
        row.addWidget(self.length_combo)
        row.addStretch(1)
        self.switch_btn = TouchButton("")
        self.switch_btn.setObjectName("PrimaryWarm")
        self.switch_btn.clicked.connect(self._switch)
        row.addWidget(self.switch_btn)
        v.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Playlist", "Song", "Now from", "Moves to", "Match"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        header = self.table.horizontalHeader()
        for col, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.Stretch,
                                    QHeaderView.ResizeToContents, QHeaderView.Stretch,
                                    QHeaderView.ResizeToContents)):
            header.setSectionResizeMode(col, mode)
        v.addWidget(self.table, 1)
        return page

    def _max_diff(self) -> int:
        return int(self.length_combo.currentData() or dup.SAME_SONG_MAX_DIFF_MS)

    def _current(self) -> list[dup.Switch]:
        return self.report.switches_within(self._max_diff())

    def _refresh_switches(self) -> None:
        switches = self._current()
        self.summary.setText(self.report.summary(self._max_diff()))
        self.table.setRowCount(len(switches))
        for r, s in enumerate(switches):
            match = "Identical" if s.how == "identical" else (
                "Same song" if s.diff_ms < 1000 else f"Same song, {s.diff_ms / 1000:.0f}s apart")
            cells = (
                s.playlist,
                f"{s.old.artist} - {s.old.title}",
                _where(s.old),
                f"{s.new.album}  ({s.new.artist})",
                match,
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(s.old.path if c == 2 else s.new.path if c == 3 else text)
                self.table.setItem(r, c, item)
        self.switch_btn.setText(
            f"Switch {len(switches)} song{'s' if len(switches) != 1 else ''} to album copies")
        self.switch_btn.setEnabled(bool(switches))

    def _switch(self) -> None:
        switches = self._current()
        if not switches:
            return
        playlists = len({s.playlist_id for s in switches})
        answer = QMessageBox.question(
            self, "Switch to album copies",
            f"Point {len(switches)} playlist song{'s' if len(switches) != 1 else ''} in "
            f"{playlists} playlist{'s' if playlists != 1 else ''} at their album copies?\n\n"
            "Each song keeps its place. The chart-folder files themselves aren't touched.",
        )
        if answer != QMessageBox.Yes:
            return
        with session_scope() as session:
            changed = dup.switch_playlists(session, switches)
            session.commit()
        done = {s.item_id for s in switches}
        self.report.switches = [s for s in self.report.switches if s.item_id not in done]
        self._refresh_switches()
        self.switched.emit(changed)

    # -- identical files --------------------------------------------------

    def _build_groups_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        groups = self.report.groups
        wasted = sum(g.wasted_bytes for g in groups)
        intro = dim_label(
            f"{len(groups)} set{'s' if len(groups) != 1 else ''} of files holding exactly the "
            f"same recording, whatever they're named or tagged - {_mb(wasted)} in extra copies. "
            "The first file in each set is the one MusicMgr would keep (an artist's own album "
            "over a compilation or chart folder, then the best quality). Nothing here is deleted."
            if groups else
            "No identical files found among MP3 and FLAC files"
            + (f" ({self.report.examined:,} files compared)." if self.report.examined else ".")
        )
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Song / file", "Where", "Size"])
        self.tree.setColumnWidth(0, 620)
        self.tree.setColumnWidth(1, 260)
        for g in groups:
            first = g.files[0]
            top = QTreeWidgetItem([
                f"{first.artist} - {first.title}  ·  {len(g.files)} copies",
                "", f"{_mb(g.wasted_bytes)} extra",
            ])
            for i, f in enumerate(g.files):
                child = QTreeWidgetItem([f.path, _where(f) + ("  (keep)" if i == 0 else ""), _mb(f.size)])
                child.setToolTip(0, f.path)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
        v.addWidget(self.tree, 1)
        return page
