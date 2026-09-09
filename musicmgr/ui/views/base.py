"""Base class for the main content views."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..context import AppContext
from ..widgets.common import title_label


class BaseView(QWidget):
    title_text = ""

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(20, 18, 20, 12)
        self._root.setSpacing(14)

        self.header = QHBoxLayout()
        self.header.setSpacing(12)
        self.title = title_label(self.title_text)
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        self._root.addLayout(self.header)

    def body(self) -> QVBoxLayout:
        return self._root

    def refresh(self) -> None:
        """Called whenever the view becomes visible or the library changes."""
