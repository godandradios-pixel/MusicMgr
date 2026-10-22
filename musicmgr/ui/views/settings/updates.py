"""Settings › Updates (split out of settings.py, 2026-10-22)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
)
from .... import config
from ....version import APP_VERSION, RELEASE_VERSION
from ....services import updater
from ...widgets.common import TouchButton, dim_label
from .parts import (
    UpdateAvailableDialog,
    UpdateCheckThread,
    UpdateDownloadThread,
)


class UpdatesSection:
    """Settings › Updates: a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_updates(self, section) -> None:
        """Build the Updates group (moved out of __init__, 2026-10-22)."""
        # ---- updates (2026-09-23) ----
        # See the "in-app updates" classes above and services/updater.py.
        # Replaces the old right-aligned "MusicMgr <commit>" footer that
        # used to sit here - the version now leads this card instead, since
        # "which version am I on" and "is there a newer one" are the same
        # question.
        body = section("Updates", "updates")

        updates_card = QFrame()
        updates_card.setObjectName("Card")
        updates_layout = QVBoxLayout(updates_card)
        updates_layout.setContentsMargins(20, 14, 20, 14)
        updates_layout.setSpacing(8)

        self.version_label = QLabel(f"{config.APP_NAME} {APP_VERSION}")
        self.version_label.setStyleSheet("font-weight: 600;")
        updates_layout.addWidget(self.version_label)
        self.update_status = dim_label("")
        self.update_status.setWordWrap(True)
        updates_layout.addWidget(self.update_status)

        # 2026-09-24 - James: "I should never be checking for updates or
        # anything that relies on an internet connection at startup". The
        # once-a-day startup check (and its "Check for updates when
        # MusicMgr starts" checkbox) is gone; "Check now" is the only way a
        # check ever happens.
        self.update_prerelease_cb = QCheckBox("Include pre-release versions")
        self.update_prerelease_cb.toggled.connect(
            lambda on: self._save_update_pref(updater.PREF_INCLUDE_PRERELEASE, on)
        )
        updates_layout.addWidget(self.update_prerelease_cb)

        self.update_progress = QProgressBar()
        self.update_progress.setObjectName("ProgressWarm")
        self.update_progress.setVisible(False)
        updates_layout.addWidget(self.update_progress)

        update_buttons = QHBoxLayout()
        self.update_check_btn = TouchButton("Check now", primary=True)
        self.update_check_btn.setObjectName("PrimaryWarm")
        self.update_check_btn.clicked.connect(lambda: self.check_for_updates(manual=True))
        self.update_install_btn = TouchButton("Install…")
        self.update_install_btn.clicked.connect(self._reoffer_update)
        self.update_cancel_btn = TouchButton("Cancel download")
        self.update_cancel_btn.clicked.connect(self._cancel_update_download)
        self.update_restart_btn = TouchButton("Restart now", primary=True)
        self.update_restart_btn.setObjectName("PrimaryWarm")
        self.update_restart_btn.clicked.connect(self.restart_now)
        self.update_rollback_btn = TouchButton("Roll back…")
        self.update_rollback_btn.clicked.connect(self.roll_back_update)
        for b in (
            self.update_check_btn, self.update_install_btn, self.update_cancel_btn,
            self.update_restart_btn, self.update_rollback_btn,
        ):
            update_buttons.addWidget(b)
        update_buttons.addStretch(1)
        updates_layout.addLayout(update_buttons)
        body.addWidget(updates_card)

        self._update_check_thread: Optional[UpdateCheckThread] = None
        self._update_download_thread: Optional[UpdateDownloadThread] = None
        #: the newest release found by the last check, if it's newer
        self._available_update: Optional[updater.UpdateInfo] = None
        #: version installed this session and waiting for a restart
        self._installed_pending: Optional[str] = None
        self._load_update_prefs()
        self._refresh_update_controls()

    # -- updates (2026-09-23) --------------------------------------------------
    # Overridable seams for tests (and nothing else): the running exe, and
    # where "Restart now" hands off to.
    def _current_exe(self) -> Optional[Path]:
        return updater.current_executable()

    def _load_update_prefs(self) -> None:
        with self.ctx.session() as db:
            pre = updater.get_bool_pref(db, updater.PREF_INCLUDE_PRERELEASE, False)
        for cb, value in ((self.update_prerelease_cb, pre),):
            cb.blockSignals(True)
            cb.setChecked(value)
            cb.blockSignals(False)

    def _save_update_pref(self, key: str, value) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        with self.ctx.session() as db:
            updater.set_pref(db, key, value)
            db.commit()

    def _get_update_pref(self, key: str) -> Optional[str]:
        with self.ctx.session() as db:
            return updater.get_pref(db, key)

    def _refresh_update_controls(self) -> None:
        exe = self._current_exe()
        downloading = self._update_download_thread is not None and self._update_download_thread.isRunning()
        checking = self._update_check_thread is not None and self._update_check_thread.isRunning()
        pending = self._installed_pending is not None
        self.update_check_btn.setEnabled(not (downloading or checking or pending))
        self.update_install_btn.setVisible(
            self._available_update is not None and not downloading and not pending
        )
        if self._available_update is not None:
            self.update_install_btn.setText(f"Install {self._available_update.version}…")
        self.update_cancel_btn.setVisible(downloading)
        self.update_restart_btn.setVisible(pending)
        previous = self._get_update_pref(updater.PREF_PREVIOUS_VERSION)
        self.update_rollback_btn.setVisible(updater.can_roll_back(exe) and not downloading)
        self.update_rollback_btn.setText(f"Roll back to {previous}…" if previous else "Roll back…")
        if not self.update_status.text():
            blocker = updater.self_update_blocker(exe)
            self.update_status.setText(blocker or "")

    def check_for_updates(self, manual: bool = True) -> None:
        """"Check now". (`manual=False` - a quiet check that only speaks up
        when there's something to offer - is kept for callers, but nothing
        runs one at startup any more: 2026-09-24, no network at startup.)"""
        if self._update_check_thread is not None and self._update_check_thread.isRunning():
            return
        if manual:
            self.update_status.setText("Checking GitHub for a newer version…")
        self._update_check_thread = UpdateCheckThread(self.update_prerelease_cb.isChecked(), parent=self)
        self._update_check_thread.finished_with.connect(
            lambda result: self._on_update_check_done(result, manual)
        )
        self._update_check_thread.start()
        self._refresh_update_controls()

    def _on_update_check_done(self, result: "updater.CheckResult", manual: bool) -> None:
        import time as _time

        if not result.error:
            self._save_update_pref(updater.PREF_LAST_CHECK, str(int(_time.time())))
        self._available_update = result.update
        self.update_status.setText(result.message)
        self._refresh_update_controls()
        if result.update is None:
            if manual and result.error:
                QMessageBox.warning(self, "Check for updates", result.message)
            return
        skipped = self._get_update_pref(updater.PREF_SKIPPED_VERSION)
        if not manual and skipped == result.update.version:
            self.update_status.setText(f"{result.message} (skipped)")
            return
        self.offer_update(result.update)

    def _reoffer_update(self) -> None:
        if self._available_update is not None:
            self.offer_update(self._available_update)

    def offer_update(self, info: "updater.UpdateInfo") -> None:
        """Ask first - always (James's choice). Install / Skip / Later."""
        exe = self._current_exe()
        can_install = updater.self_update_blocker(exe) is None
        dialog = UpdateAvailableDialog(info, RELEASE_VERSION, can_install, parent=self)
        choice = dialog.exec()
        if choice == UpdateAvailableDialog.SKIP:
            self._save_update_pref(updater.PREF_SKIPPED_VERSION, info.version)
            self.update_status.setText(f"Skipped {info.version} - Check now still offers it")
        elif choice == UpdateAvailableDialog.INSTALL:
            if can_install:
                self.install_update(info)
            elif info.html_url:
                QDesktopServices.openUrl(QUrl(info.html_url))

    def install_update(self, info: "updater.UpdateInfo") -> None:
        exe = self._current_exe()
        blocker = updater.self_update_blocker(exe)
        if blocker or exe is None:
            QMessageBox.information(self, "Install update", blocker or "This copy can't update itself.")
            return
        if self._update_download_thread is not None and self._update_download_thread.isRunning():
            return
        self.ctx.navigateRequested.emit("settings")
        self.update_progress.setVisible(True)
        self.update_progress.setRange(0, 0)
        self.update_status.setText(f"Downloading MusicMgr {info.version}…")
        self._update_download_thread = UpdateDownloadThread(info, updater.download_path_for(exe), parent=self)
        self._update_download_thread.progress.connect(self._on_update_progress)
        self._update_download_thread.finished_with.connect(
            lambda path, error: self._on_update_downloaded(info, path, error)
        )
        self._update_download_thread.start()
        self._refresh_update_controls()

    def _on_update_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.update_progress.setRange(0, total)
            self.update_progress.setValue(done)
            self.update_status.setText(
                f"Downloading… {done / 1_048_576:.1f} of {total / 1_048_576:.1f} MB"
            )

    def _cancel_update_download(self) -> None:
        if self._update_download_thread is not None and self._update_download_thread.isRunning():
            self._update_download_thread.requestInterruption()
            self.update_cancel_btn.setEnabled(False)
            self.update_status.setText("Cancelling download…")

    def _on_update_downloaded(self, info: "updater.UpdateInfo", path, error) -> None:
        self.update_progress.setVisible(False)
        self.update_cancel_btn.setEnabled(True)
        if error:
            self.update_status.setText(error)
            self._refresh_update_controls()
            QMessageBox.warning(self, "Update not installed", error)
            return
        if path is None:
            self.update_status.setText("Download cancelled - nothing was changed")
            self._refresh_update_controls()
            return
        exe = self._current_exe()
        try:
            updater.install_downloaded(Path(path), exe)
        except updater.UpdateError as exc:
            self.update_status.setText(str(exc))
            self._refresh_update_controls()
            QMessageBox.warning(self, "Update not installed", str(exc))
            return
        self._save_update_pref(updater.PREF_PREVIOUS_VERSION, RELEASE_VERSION)
        self._save_update_pref(updater.PREF_SKIPPED_VERSION, None)
        self._installed_pending = info.version
        self._available_update = None
        self.update_status.setText(
            f"MusicMgr {info.version} is installed. It starts the next time you open "
            "MusicMgr - or restart now."
        )
        self._refresh_update_controls()
        answer = QMessageBox.question(
            self,
            "Update installed",
            f"MusicMgr {info.version} is installed.\n\nRestart now? "
            "(Anything playing will stop.) Otherwise it starts next time you open MusicMgr.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.restart_now()

    def restart_now(self) -> None:
        exe = self._current_exe()
        if exe is None:
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        try:
            updater.relaunch(exe, sys.argv)
        except OSError as exc:
            QMessageBox.warning(self, "Restart", f"Couldn't start the new version ({exc}). "
                                                 "Close MusicMgr and open it again.")
            return
        self.window().close()

    def roll_back_update(self) -> None:
        exe = self._current_exe()
        if not updater.can_roll_back(exe):
            return
        previous = self._get_update_pref(updater.PREF_PREVIOUS_VERSION) or "the previous version"
        confirm = QMessageBox.question(
            self,
            "Roll back",
            f"Go back to {previous}? Your library isn't touched.\n\n"
            "MusicMgr will restart.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            updater.rollback(exe)
        except updater.UpdateError as exc:
            QMessageBox.warning(self, "Roll back", str(exc))
            return
        # the version we're leaving is now the one a roll-back returns to
        self._save_update_pref(updater.PREF_PREVIOUS_VERSION, RELEASE_VERSION)
        self._save_update_pref(updater.PREF_SKIPPED_VERSION, RELEASE_VERSION)
        self._installed_pending = previous
        self.update_status.setText(f"Rolled back to {previous} - restart to use it")
        self._refresh_update_controls()
        self.restart_now()
