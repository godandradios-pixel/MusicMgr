"""SettingsView's "Updates" card (2026-09-23 in-app updates).

The network and the real threads are never exercised here -
services/updater.py's own tests cover check/download/verify/swap. These
cover the UI decisions: what's shown when, the always-ask-first flow, the
once-a-day auto check, skip-this-version, and what a finished download does
to the running exe (on dummy files in tmp_path).
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from musicmgr.services import updater
from musicmgr.ui.views import settings as settings_module
from musicmgr.ui.views.settings import SettingsView, UpdateAvailableDialog


def make_info(version="1.6.0"):
    asset = updater.ReleaseAsset(f"MusicMgr-{version}-windows-x64.exe", "https://dl/x", 10)
    return updater.UpdateInfo(
        version=version, tag=f"v{version}", title=f"MusicMgr {version}",
        notes="## New\n- **faster**", html_url="https://github.com/x/releases/tag/v1.6.0",
        published_at="2026-09-30", prerelease=False,
        binary=asset, sums=asset, sig=asset,
    )


@pytest.fixture
def view(ctx):
    return SettingsView(ctx)


@pytest.fixture
def frozen_view(ctx, tmp_path, monkeypatch):
    """A SettingsView that believes it's the packaged exe at tmp_path."""
    exe = tmp_path / "MusicMgr.exe"
    exe.write_bytes(b"OLD")
    monkeypatch.setattr(SettingsView, "_current_exe", lambda self: exe)
    monkeypatch.setattr(updater, "platform_tag", lambda *a, **k: "windows-x64.exe")
    v = SettingsView(ctx)
    v._test_exe = exe
    return v


class TestCard:
    def test_shows_version_and_source_mode(self, view):
        assert settings_module.RELEASE_VERSION in view.version_label.text()
        assert "source" in view.update_status.text()
        assert view.update_check_btn.isEnabled()
        assert view.update_rollback_btn.isHidden()
        assert view.update_restart_btn.isHidden()
        assert view.update_install_btn.isHidden()

    def test_no_startup_check_option_and_prerelease_off(self, view):
        # 2026-09-24: no update check at startup, so no checkbox for one
        assert not hasattr(view, "update_auto_cb")
        assert not hasattr(view, "auto_check_for_updates")
        assert not view.update_prerelease_cb.isChecked()

    def test_prefs_persist(self, ctx):
        first = SettingsView(ctx)
        first.update_prerelease_cb.setChecked(True)
        second = SettingsView(ctx)
        assert second.update_prerelease_cb.isChecked()

    def test_rollback_offered_when_old_exists(self, ctx, tmp_path, monkeypatch):
        exe = tmp_path / "MusicMgr.exe"
        exe.write_bytes(b"NEW")
        updater.old_path_for(exe).write_bytes(b"OLD")
        monkeypatch.setattr(SettingsView, "_current_exe", lambda self: exe)
        v = SettingsView(ctx)
        v._save_update_pref(updater.PREF_PREVIOUS_VERSION, "1.4.0")
        v._refresh_update_controls()
        assert not v.update_rollback_btn.isHidden()
        assert "1.4.0" in v.update_rollback_btn.text()


class TestNoStartupNetwork:
    def test_main_schedules_no_update_check(self):
        """2026-09-24 - James: "I should never be checking for updates or
        anything that relies on an internet connection at startup"."""
        import inspect

        from musicmgr.ui import app

        src = inspect.getsource(app.main) + inspect.getsource(app.bootstrap_database)
        assert "check_for_update" not in src and "auto_check" not in src


class TestCheckResult:
    def test_update_found_is_offered(self, frozen_view, monkeypatch):
        offered = []
        monkeypatch.setattr(frozen_view, "offer_update", offered.append)
        info = make_info()
        frozen_view._on_update_check_done(updater.CheckResult("1.5.0", update=info, message="avail"), manual=False)
        assert offered == [info]
        assert frozen_view._get_update_pref(updater.PREF_LAST_CHECK)
        assert not frozen_view.update_install_btn.isHidden()
        assert "1.6.0" in frozen_view.update_install_btn.text()

    def test_skipped_version_not_offered_automatically(self, frozen_view, monkeypatch):
        frozen_view._save_update_pref(updater.PREF_SKIPPED_VERSION, "1.6.0")
        offered = []
        monkeypatch.setattr(frozen_view, "offer_update", offered.append)
        frozen_view._on_update_check_done(updater.CheckResult("1.5.0", update=make_info(), message="m"), manual=False)
        assert offered == []

    def test_skipped_version_still_offered_on_check_now(self, frozen_view, monkeypatch):
        frozen_view._save_update_pref(updater.PREF_SKIPPED_VERSION, "1.6.0")
        offered = []
        monkeypatch.setattr(frozen_view, "offer_update", offered.append)
        frozen_view._on_update_check_done(updater.CheckResult("1.5.0", update=make_info(), message="m"), manual=True)
        assert len(offered) == 1

    def test_manual_error_is_shown(self, frozen_view, monkeypatch):
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
        frozen_view._on_update_check_done(
            updater.CheckResult("1.5.0", message="Couldn't reach GitHub", error=True), manual=True)
        assert warned == ["Couldn't reach GitHub"]
        assert not frozen_view._get_update_pref(updater.PREF_LAST_CHECK)

    def test_automatic_error_is_silent(self, frozen_view, monkeypatch):
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(1))
        frozen_view._on_update_check_done(
            updater.CheckResult("1.5.0", message="down", error=True), manual=False)
        assert warned == []


class TestAskFirst:
    def test_skip_is_remembered(self, frozen_view, monkeypatch):
        monkeypatch.setattr(UpdateAvailableDialog, "exec", lambda self: UpdateAvailableDialog.SKIP)
        frozen_view.offer_update(make_info())
        assert frozen_view._get_update_pref(updater.PREF_SKIPPED_VERSION) == "1.6.0"

    def test_later_does_nothing(self, frozen_view, monkeypatch):
        installs = []
        monkeypatch.setattr(UpdateAvailableDialog, "exec", lambda self: UpdateAvailableDialog.LATER)
        monkeypatch.setattr(frozen_view, "install_update", installs.append)
        frozen_view.offer_update(make_info())
        assert installs == [] and frozen_view._get_update_pref(updater.PREF_SKIPPED_VERSION) is None

    def test_install_starts_install(self, frozen_view, monkeypatch):
        installs = []
        monkeypatch.setattr(UpdateAvailableDialog, "exec", lambda self: UpdateAvailableDialog.INSTALL)
        monkeypatch.setattr(frozen_view, "install_update", installs.append)
        frozen_view.offer_update(make_info())
        assert len(installs) == 1

    def test_source_checkout_opens_release_page_instead(self, view, monkeypatch):
        opened = []
        monkeypatch.setattr(UpdateAvailableDialog, "exec", lambda self: UpdateAvailableDialog.INSTALL)
        monkeypatch.setattr(settings_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
        view.offer_update(make_info())
        assert opened == ["https://github.com/x/releases/tag/v1.6.0"]

    def test_dialog_renders_notes(self, qapp):
        dialog = UpdateAvailableDialog(make_info(), "1.5.0", can_install=True)
        assert dialog.install_btn.text() == "Install now"
        assert "faster" in dialog.findChild(settings_module.QTextBrowser).toPlainText()
        cant = UpdateAvailableDialog(make_info(), "1.5.0", can_install=False)
        assert cant.install_btn.text() == "Open download page"


class TestAfterDownload:
    def test_success_swaps_and_offers_restart(self, frozen_view, monkeypatch):
        exe = frozen_view._test_exe
        dl = updater.download_path_for(exe)
        dl.write_bytes(b"NEW")
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
        frozen_view._on_update_downloaded(make_info(), str(dl), None)
        assert exe.read_bytes() == b"NEW"
        assert updater.old_path_for(exe).read_bytes() == b"OLD"
        assert frozen_view._installed_pending == "1.6.0"
        assert not frozen_view.update_restart_btn.isHidden()
        assert not frozen_view.update_check_btn.isEnabled()
        assert frozen_view._get_update_pref(updater.PREF_PREVIOUS_VERSION) == settings_module.RELEASE_VERSION

    def test_yes_restarts(self, frozen_view, monkeypatch):
        dl = updater.download_path_for(frozen_view._test_exe)
        dl.write_bytes(b"NEW")
        restarts = []
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        monkeypatch.setattr(frozen_view, "restart_now", lambda: restarts.append(1))
        frozen_view._on_update_downloaded(make_info(), str(dl), None)
        assert restarts == [1]

    def test_verify_error_leaves_exe_alone(self, frozen_view, monkeypatch):
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
        frozen_view._on_update_downloaded(make_info(), None, "signature didn't verify")
        assert frozen_view._test_exe.read_bytes() == b"OLD"
        assert warned == ["signature didn't verify"]
        assert frozen_view._installed_pending is None

    def test_cancelled(self, frozen_view):
        frozen_view._on_update_downloaded(make_info(), None, None)
        assert "cancelled" in frozen_view.update_status.text()
        assert frozen_view._test_exe.read_bytes() == b"OLD"

    def test_restart_relaunches_and_closes(self, frozen_view, monkeypatch):
        launched = []
        monkeypatch.setattr(updater, "relaunch", lambda exe, argv: launched.append(exe))
        frozen_view.restart_now()
        assert launched == [frozen_view._test_exe]


class TestRollback:
    def test_rollback_swaps_and_restarts(self, frozen_view, monkeypatch):
        exe = frozen_view._test_exe
        exe.write_bytes(b"NEW")
        updater.old_path_for(exe).write_bytes(b"OLD")
        frozen_view._save_update_pref(updater.PREF_PREVIOUS_VERSION, "1.4.0")
        restarts = []
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        monkeypatch.setattr(frozen_view, "restart_now", lambda: restarts.append(1))
        frozen_view.roll_back_update()
        assert exe.read_bytes() == b"OLD"
        assert restarts == [1]
        # don't immediately re-offer the version just rolled away from
        assert frozen_view._get_update_pref(updater.PREF_SKIPPED_VERSION) == settings_module.RELEASE_VERSION
