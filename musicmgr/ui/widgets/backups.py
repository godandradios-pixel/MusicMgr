"""Settings › Files & data › "Backups" window (2026-10-19, Back up now
added 2026-10-31) - see services/library_backup.py."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidgetItem, QVBoxLayout

from ...services.library_backup import BackupFile
from .common import ROLE_PAYLOAD, TouchButton, TouchList, dim_label

#: Back up now callback: makes a backup and returns (message, fresh backup
#: list) - the list is None when the backup failed and `message` is why.
BackUpFn = Callable[[], tuple[str, Optional[list[BackupFile]]]]


class RestoreBackupDialog(QDialog):
    """The library's backups, newest first, with Back up now and Restore.

    2026-10-31 - James: Back up library and Restore library were two
    Settings rows; they're one "Backups…" row opening this window now. Pass
    `back_up` to show the Back up now button. After Restore, `chosen` is the
    picked backup's path."""

    def __init__(self, backups: list[BackupFile], parent=None,
                 back_up: Optional[BackUpFn] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Backups")
        self.setMinimumSize(640, 480)
        self.chosen: Optional[Path] = None
        self._back_up = back_up
        layout = QVBoxLayout(self)
        head = QLabel("Library backups")
        head.setObjectName("Title")
        layout.addWidget(head)
        note = dim_label(
            "MusicMgr saves a copy of your library the first time it closes each day and "
            "keeps the last 7. Restoring puts your playlists, play history, charts, jukebox "
            "and settings back to how they were in the backup. Music files aren't touched. "
            "Your current library is saved first, as a \"Before restore\" backup, so a "
            "restore can be undone."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.list = TouchList()
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.list.itemDoubleClicked.connect(lambda *_: self._accept())
        layout.addWidget(self.list, 1)
        self.empty_label = dim_label("No backups yet - use Back up now to make one.")
        layout.addWidget(self.empty_label)
        self.status = dim_label("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.back_up_btn = TouchButton("Back up now")
        self.back_up_btn.clicked.connect(self._on_back_up)
        self.back_up_btn.setVisible(back_up is not None)
        buttons.addWidget(self.back_up_btn)
        buttons.addStretch(1)
        close = TouchButton("Close")
        close.clicked.connect(self.reject)
        self.restore_btn = TouchButton("Restore…", primary=True)
        self.restore_btn.clicked.connect(self._accept)
        buttons.addWidget(close)
        buttons.addWidget(self.restore_btn)
        layout.addLayout(buttons)
        self._fill(backups)

    def _fill(self, backups: list[BackupFile]) -> None:
        self.list.clear()
        for backup in backups:
            item = QListWidgetItem(backup.label)
            item.setData(ROLE_PAYLOAD, str(backup.path))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.empty_label.setVisible(self.list.count() == 0)
        self._sync()

    def _sync(self) -> None:
        self.restore_btn.setEnabled(self.list.currentItem() is not None)

    def _on_back_up(self) -> None:
        if self._back_up is None:
            return
        message, backups = self._back_up()
        self.status.setText(message)
        if backups is not None:
            self._fill(backups)

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self.chosen = Path(item.data(ROLE_PAYLOAD))
        self.accept()
