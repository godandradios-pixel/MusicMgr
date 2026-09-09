"""Now Playing's biography pane - shows the currently-playing track's album
artist's profile text (Artist.profile: a free-text column already in the
schema, same shape as a Discogs bio, but with no importer wired up yet -
see the note in nowplaying.py). Until an artist's profile is filled in this
just shows an empty state, mirroring LyricsPanel's pattern for a track with
no .lrc sidecar."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from .common import EmptyState


class BioPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._empty = EmptyState(
            "No biography on file",
            "This artist doesn't have a saved biography yet.",
        )

        self._text = QLabel("")
        self._text.setObjectName("BioText")
        self._text.setWordWrap(True)
        self._text.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidget(self._text)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)  # 0
        self._stack.addWidget(self._scroll)  # 1
        layout.addWidget(self._stack)

    def set_bio(self, profile: Optional[str]) -> None:
        text = (profile or "").strip()
        self._text.setText(text)
        if text:
            self._scroll.verticalScrollBar().setValue(0)
            self._stack.setCurrentWidget(self._scroll)
        else:
            self._stack.setCurrentWidget(self._empty)
