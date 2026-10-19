"""Settings › Files & data › "Restore library" picker (2026-10-19) - see
services/library_backup.py."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidgetItem, QVBoxLayout

from ...services.library_backup import BackupFile
from .common import ROLE_PAYLOAD, TouchButton, TouchList, dim_label


class RestoreBackupDialog(QDialog):
    """Pick one library backup, newest first. After Ok, `chosen` is its path."""

    def __init__(self, backups: list[BackupFile], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Restore library")
        self.setMinimumSize(640, 480)
        self.chosen: Optional[Path] = None
        layout = QVBoxLayout(self)
        head = QLabel("Restore your library from a backup")
        head.setObjectName("Title")
        layout.addWidget(head)
        note = dim_label(
            "Your playlists, play history, charts, jukebox and settings go back to how they "
            "were in the backup. Music files aren't touched. Your current library is saved "
            "first, as a \"Before restore\" backup, so this can be undone."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.list = TouchList()
        for backup in backups:
            item = QListWidgetItem(backup.label)
            item.setData(ROLE_PAYLOAD, str(backup.path))
            self.list.addItem(item)
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.list.itemDoubleClicked.connect(lambda *_: self._accept())
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = TouchButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.restore_btn = TouchButton("Restore…", primary=True)
        self.restore_btn.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.restore_btn)
        layout.addLayout(buttons)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._sync()

    def _sync(self) -> None:
        self.restore_btn.setEnabled(self.list.currentItem() is not None)

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self.chosen = Path(item.data(ROLE_PAYLOAD))
        self.accept()
