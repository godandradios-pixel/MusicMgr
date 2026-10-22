"""Settings › Files and data (verify, back up / restore, locate, move) (split out of settings.py, 2026-10-22)."""

from __future__ import annotations

import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
from .... import config
from ....services import data_migration
from ....services import library as lib
from ....services import updater
from ....services import library_backup
from ....services import videos as vid_svc
from ...widgets.common import VerifyFilesThread
from ...widgets.backups import RestoreBackupDialog
from .parts import (
    MigrationThread,
)


class FilesSection:
    """Settings › Files and data (verify, back up / restore, locate, move): a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def verify_files(self) -> None:
        """2026-09-07 follow-up: checks video files too now, not just audio -
        the same "one unified thing" this whole view's folder merge is
        about (see module docstring); there's no reason "Verify files"
        would only mean half of what's watched.

        2026-09-22 - James: "does the progress bar also work on ... verify
        files" (it didn't - this used to run both checks entirely
        synchronously, right here, with no feedback). Threaded the same
        way as every other bulk action on this page now - see
        VerifyFilesThread (ui/widgets/common.py), which runs both the
        audio and video checks in the two phases this used to be."""
        if self._verify_thread is not None and self._verify_thread.isRunning():
            self.ctx.notify("Already verifying files")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Starting…")
        self._verify_thread = VerifyFilesThread(parent=self)
        self._verify_thread.progress.connect(self._on_verify_progress)
        self._verify_thread.finished_with.connect(self._on_verify_done)
        self._verify_thread.start()
        self._show_cancel()

    def _on_verify_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            self.progress_label.setText(name or "Verifying…")
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress_label.setText(f"{done}/{total}")

    def _on_verify_done(self, missing: int) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._verify_thread)
        # 2026-10-18 - the newly-flagged count alone read as "nothing is
        # missing" while Purge found 133 flagged by earlier scans; show
        # the running total too.
        with self.ctx.session() as session:
            total = lib.library_stats(session)["missing"]
            total += vid_svc.video_counts(session)["video_missing"]
        self.ctx.notify(
            f"{missing} file(s) newly marked missing, {total} missing in total"
        )
        self.refresh()

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

    def restore_library(self) -> None:
        backups = library_backup.list_backups(config.DB_BACKUP_DIR)
        if not backups:
            self.ctx.notify("There are no backups yet - use Back up to make one")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        dialog = RestoreBackupDialog(backups, self)
        if dialog.exec() != QDialog.Accepted or dialog.chosen is None:
            return
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
        QMessageBox.information(
            self,
            "Data locations",
            f"Database:\n{config.DB_PATH}\n\n"
            f"Artwork cache:\n{config.ART_DIR}\n\n"
            "Set the MUSICMGR_HOME environment variable to move them, or use "
            "\"Move data location…\" below to do it from here.",
        )

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
