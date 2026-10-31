"""Settings › Library health - the check thread, the fix thread and the
results dialog (2026-10-25; see services/library_health.py)."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...db.session import session_scope
from ...services import library_health as lh
from .common import TouchButton, dim_label


class LibraryHealthThread(QThread):
    """Runs services.library_health.check_library off the UI thread."""

    progress = Signal(int, int, str)
    finished_with = Signal(object)  # HealthReport

    def run(self) -> None:  # pragma: no cover - exercised interactively
        with session_scope() as session:
            report = lh.check_library(
                session,
                progress=lambda done, total, name: self.progress.emit(done, total, name),
                should_stop=self.isInterruptionRequested,
            )
        self.finished_with.emit(report)


class HealthFixThread(QThread):
    """Backs the library up, then runs one finding's fix and commits.
    `finished_with(message, error)` - error is "" on success."""

    finished_with = Signal(str, str)

    def __init__(self, finding: lh.Finding, parent=None) -> None:
        super().__init__(parent)
        self.finding = finding

    def run(self) -> None:
        try:
            lh.backup_before_fix()
        except Exception as exc:
            self.finished_with.emit("", f"Nothing was changed - the library couldn't be "
                                        f"backed up first ({exc})")
            return
        try:
            with session_scope() as session:
                message = lh.apply_fix(session, self.finding)
                session.commit()
        except Exception as exc:
            self.finished_with.emit("", f"The fix failed ({exc}). A backup was saved first "
                                        "in the backups folder.")
            return
        self.finished_with.emit(message, "")


class _FindingCard(QFrame):
    """One check: its name and count, what it means, the examples behind a
    Show button, and its fix (or where to fix it)."""

    fix_requested = Signal(object)

    def __init__(self, finding: lh.Finding, parent=None) -> None:
        super().__init__(parent)
        self.finding = finding
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)

        top = QHBoxLayout()
        title = QLabel(finding.title)
        title.setStyleSheet("font-weight: 600;")
        top.addWidget(title, 1)
        self.status = QLabel("")
        top.addWidget(self.status)
        layout.addLayout(top)
        detail = dim_label(finding.detail)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        self.examples = QListWidget()
        self.examples.setMaximumHeight(180)
        self.examples.addItems(finding.examples)
        if finding.count > len(finding.examples):
            self.examples.addItem(f"… and {finding.count - len(finding.examples):,} more")
        self.examples.setVisible(False)
        layout.addWidget(self.examples)

        buttons = QHBoxLayout()
        self.show_btn = TouchButton(f"Show ({len(finding.examples):,})")
        self.show_btn.clicked.connect(self._toggle)
        buttons.addWidget(self.show_btn)
        self.fix_btn: Optional[TouchButton] = None
        if finding.fix_label:
            self.fix_btn = TouchButton(finding.fix_label, primary=True)
            self.fix_btn.clicked.connect(lambda: self.fix_requested.emit(self))
            buttons.addWidget(self.fix_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.hint = dim_label(finding.hint)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.done = dim_label("")
        self.done.setWordWrap(True)
        self.done.setVisible(False)
        layout.addWidget(self.done)
        self._refresh()

    def _refresh(self) -> None:
        bad = not self.finding.ok
        self.status.setText(f"{self.finding.count:,} found" if bad else "✓ None")
        self.status.setStyleSheet("font-weight: 600;" if bad else "")
        self.show_btn.setVisible(bad and bool(self.finding.examples))
        if self.fix_btn is not None:
            self.fix_btn.setVisible(bad)
        self.hint.setVisible(bad and bool(self.finding.hint))

    def _toggle(self) -> None:
        on = not self.examples.isVisible()
        self.examples.setVisible(on)
        self.show_btn.setText("Hide" if on else f"Show ({len(self.finding.examples):,})")

    def set_busy(self, busy: bool) -> None:
        if self.fix_btn is not None:
            self.fix_btn.setEnabled(not busy)
            self.fix_btn.setText("Working…" if busy else self.finding.fix_label)

    def mark_fixed(self, message: str) -> None:
        self.done.setText(f"Done: {message}. Check again to see what's left.")
        self.done.setVisible(True)
        if self.fix_btn is not None:
            self.fix_btn.setVisible(False)
        self.status.setText("Fixed")
        self.status.setStyleSheet("font-weight: 600;")


class LibraryHealthDialog(QDialog):
    """Every check's result, problems first, with its fix."""

    #: done(..) code asking Settings to run the checks again
    RECHECK = 2
    #: a fix changed the library: (finding key, message)
    fixed = Signal(str, str)

    #: findings whose fix runs in Settings rather than apply_fix, because
    #: it asks its own questions (see `remove_missing` below)
    SETTINGS_FIXES = ("missing_files",)

    def __init__(self, report: lh.HealthReport, parent=None,
                 remove_missing: Optional[Callable[[], str]] = None) -> None:
        """`remove_missing` runs the "Records whose file is gone" fix - the
        Settings purge, which confirms (and backs nothing up: it only
        removes records of files that are gone) - and returns what it did,
        or "" if nothing was done. 2026-10-31: replaces the old Verify
        files row."""
        super().__init__(parent)
        self.report = report
        self._remove_missing = remove_missing
        self._fix_thread: Optional[HealthFixThread] = None
        self.setWindowTitle("Library health")
        self.resize(900, 760)
        layout = QVBoxLayout(self)

        n = report.problems()
        head = QLabel("Everything checks out." if n == 0 else
                      f"{n} thing{'s' if n != 1 else ''} to look at.")
        head.setStyleSheet("font-weight: 600; font-size: 15px;")
        layout.addWidget(head)
        layout.addWidget(dim_label(
            f"Checked in {report.seconds:,.0f} s. Every fix saves a copy of your library "
            "first (backups folder, library-before-health-fix-…)."
            + (" Stopped early - these results are partial." if report.cancelled else "")))

        body = QWidget()
        cards = QVBoxLayout(body)
        cards.setSpacing(8)
        self.cards: list[_FindingCard] = []
        for finding in sorted(report.findings, key=lambda f: f.ok):
            card = _FindingCard(finding)
            card.fix_requested.connect(self._fix)
            cards.addWidget(card)
            self.cards.append(card)
        cards.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.recheck_btn = TouchButton("Check again")
        self.recheck_btn.clicked.connect(lambda: self.done(self.RECHECK))
        self.close_btn = TouchButton("Close", primary=True)
        self.close_btn.clicked.connect(self.accept)
        buttons.addWidget(self.recheck_btn)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

    def _confirm(self, finding: lh.Finding) -> bool:
        return QMessageBox.question(
            self, finding.title,
            f"{finding.fix_label}? ({finding.count:,} found)\n\n"
            "A copy of your library is saved in the backups folder first.",
        ) == QMessageBox.Yes

    def _fix(self, card: _FindingCard) -> None:
        if self._fix_thread is not None and self._fix_thread.isRunning():
            return
        if card.finding.key in self.SETTINGS_FIXES:
            if self._remove_missing is None:
                return
            message = self._remove_missing()  # asks its own questions
            if message:
                card.mark_fixed(message)
                self.fixed.emit(card.finding.key, message)
            return
        if not self._confirm(card.finding):
            return
        card.set_busy(True)
        self.recheck_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self._fix_thread = HealthFixThread(card.finding, self)
        self._fix_thread.finished_with.connect(
            lambda message, error, card=card: self._fixed(card, message, error))
        self._fix_thread.start()

    def _fixed(self, card: _FindingCard, message: str, error: str) -> None:
        if self._fix_thread is not None:
            self._fix_thread.wait()
        self._fix_thread = None
        card.set_busy(False)
        self.recheck_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        if error:
            QMessageBox.warning(self, card.finding.title, error)
            return
        card.mark_fixed(message)
        self.fixed.emit(card.finding.key, message)

    def reject(self) -> None:
        if self._fix_thread is not None and self._fix_thread.isRunning():
            return  # let the fix finish
        super().reject()
