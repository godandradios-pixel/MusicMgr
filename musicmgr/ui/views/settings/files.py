"""Settings › Files and data (backups, data location)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
from .... import config
from ....services import data_migration
from ....services import library as lib
from ....services import updater
from ....services import library_backup
from ....services import videos as vid_svc
from ...widgets.backups import RestoreBackupDialog
from .parts import (
    MigrationThread,
)


class FilesSection:
    """Settings › Files and data (backups, data location): a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def back_up_library(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = library_backup.make_daily_backup(config.DB_PATH, config.DB_BACKUP_DIR, force=True)
        finally:
            QApplication.restoreOverrideCursor()
        if result.error:
            QMessageBox.warning(self, "Back up library", result.error)
        else:
            self.ctx.notify(f"Library backed up ({result.path.name})")

    def _back_up_from_window(self) -> tuple[str, Optional[list]]:
        """The Backups window's Back up now button (widgets/backups.py)."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = library_backup.make_daily_backup(config.DB_PATH, config.DB_BACKUP_DIR, force=True)
        finally:
            QApplication.restoreOverrideCursor()
        if result.error:
            return result.error, None
        return (f"Library backed up ({result.path.name})",
                library_backup.list_backups(config.DB_BACKUP_DIR))

    def restore_library(self) -> None:
        """Settings › Backups…: the Backups window - Back up now, or pick a
        backup to restore. 2026-10-31: the old Back up library and Restore
        library rows in one."""
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        backups = library_backup.list_backups(config.DB_BACKUP_DIR)
        dialog = RestoreBackupDialog(backups, self, back_up=self._back_up_from_window)
        if dialog.exec() != QDialog.Accepted or dialog.chosen is None:
            return
        # Back up now may have added backups since the window opened
        backups = library_backup.list_backups(config.DB_BACKUP_DIR)
        chosen = next(b for b in backups if b.path == dialog.chosen)
        exe = self._current_exe()
        restart = ("MusicMgr will restart to do this." if exe is not None
                   else "Close MusicMgr and start it again to finish.")
        confirm = QMessageBox.question(
            self,
            "Restore library",
            f"Replace your library with the backup from {chosen.when:%A %B %d, %I:%M %p}?\n\n"
            "Anything changed since then - playlists, plays, chart matches, jukebox, "
            "settings - goes back to how it was. Your current library is saved first as a "
            f"\"Before restore\" backup, so this can be undone.\n\n{restart}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        problem = library_backup.schedule_restore(chosen.path, config.RESTORE_MARKER)
        if problem:
            QMessageBox.warning(self, "Restore library", problem)
            return
        if exe is None:
            # running from source (python run.py) - there's no exe to start
            self.ctx.notify("Close MusicMgr and start it again to finish restoring")
            return
        try:
            updater.relaunch(exe, sys.argv, after_update=False)
        except OSError as exc:
            QMessageBox.warning(self, "Restore library", f"Couldn't restart ({exc}). "
                                "Close MusicMgr and open it again to finish restoring.")
            return
        self.window().close()

    def show_paths(self) -> None:
        """Settings › Data location: where the database and artwork live,
        with a Move… button (2026-10-31: the old "Move data" row)."""
        box = QMessageBox(self)
        box.setWindowTitle("Data location")
        box.setIcon(QMessageBox.Information)
        box.setText(
            f"Database:\n{config.DB_PATH}\n\n"
            f"Artwork cache:\n{config.ART_DIR}\n\n"
            "Use Move… to copy them somewhere else (or set the MUSICMGR_HOME "
            "environment variable)."
        )
        move_btn = box.addButton("Move…", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        if self._exec_paths_box(box) is move_btn:
            self.move_data()

    def _exec_paths_box(self, box: QMessageBox):
        """Show the Data location box and return the clicked button - its
        own method so tests can answer it."""
        box.exec()
        return box.clickedButton()

    def move_data(self) -> None:
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A move is already running")
            return
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is running — wait for it to finish before moving data")
            return
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify(
                "Artist profiles are downloading — wait for it to finish before moving data"
            )
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify(
                "Track popularity is updating — wait for it to finish before moving data"
            )
            return
        if self._artwork_thread is not None and self._artwork_thread.isRunning():
            self.ctx.notify(
                "Album artwork is downloading — wait for it to finish before moving data"
            )
            return

        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to move your data", str(config.PROJECT_ROOT)
        )
        if not folder:
            return
        dest = Path(folder).expanduser().resolve()
        if dest == config.DATA_DIR:
            # Not a move - but this is exactly where someone lands after
            # copying data\ to a new PC by hand: the files are already in
            # the right folder, but cover_path inside library.db can still
            # remember the old machine's paths (see data_migration.repair's
            # own docstring). Offer the fix instead of a dead end.
            confirm = QMessageBox.question(
                self,
                "Move data location",
                "Your data already lives in this folder.\n\n"
                "Would you like to repair any cover art paths that still "
                "point somewhere else instead - for example, left over from "
                "copying this folder here by hand on another PC?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm == QMessageBox.Yes:
                result = data_migration.repair(dest)
                QMessageBox.information(self, "Move data location", result.summary())
            return

        confirm = QMessageBox.question(
            self,
            "Move data location",
            f"Copy your database and artwork cache to:\n{dest}\n\n"
            f"Your current data at {config.DATA_DIR} is left in place — nothing "
            "is deleted here. You'll need to fully close and restart MusicMgr "
            "afterwards for it to actually use the new location (and, unless "
            "the new folder is your portable data\\ folder, set the "
            "MUSICMGR_HOME environment variable to it first).\n\n"
            "For the safest result, avoid scanning or importing anything in "
            "MusicMgr until this finishes. Unlike every other action on this "
            "page, this one has no Cancel button — it copies files in "
            "several steps with no safe way to stop partway through, so "
            "once started it's best just left to finish. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Starting move…")
        self._migration_thread = MigrationThread(dest, self)
        self._migration_thread.progress.connect(self.progress_label.setText)
        self._migration_thread.finished_with.connect(self._on_move_done)
        self._migration_thread.start()

    def _on_move_done(self, result: data_migration.MigrationResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        QMessageBox.information(self, "Move data location", result.summary())
