"""Settings page building blocks shared by its sections: background
threads, small dialogs, the tool-row widget and helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from .... import config
from ....version import RELEASE_VERSION
from ....db.models import WatchedFolder
from ....db.session import session_scope
from ....services import data_migration
from ....services import scanner
from ....services import playback_prefs
from ....services import replaygain
from ....services import updater
from ....services import video_scanner
from ...widgets.common import SearchBar, TouchButton, TouchList, dim_label

#: which Settings groups are open, comma separated
SECTIONS_OPEN_KEY = "settings_sections_open"
SECTIONS_OPEN_DEFAULT = ("library",)


class LoudnessAnalyzeThread(QThread):
    """Settings > Maintenance > "Volume levels" - gives every
    track without a level one: its ReplayGain tags if it has them, otherwise
    MusicMgr measures it (services/replaygain.py:analyze_library). Commits
    per file, so Cancel keeps everything finished so far."""

    progress = Signal(int, int, str)
    finished_with = Signal(object)  # replaygain.AnalyzeResult

    def run(self) -> None:  # pragma: no cover - exercised interactively
        result = replaygain.analyze_library(
            progress=lambda done, total, name: self.progress.emit(done, total, name),
            cancelled=self.isInterruptionRequested,
        )
        self.finished_with.emit(result)


#: Settings > Playback choices: (label, value)
CROSSFADE_CHOICES = [("Off — gapless", 0)] + [
    (f"{n} seconds" if n > 1 else "1 second", n) for n in (1, 2, 3, 4, 5, 6, 8, 10, 12)
]
LEVELLING_CHOICES = [
    ("Off", playback_prefs.LEVELLING_OFF),
    ("Auto — album level when an album plays in order", playback_prefs.LEVELLING_AUTO),
    ("Track — every track at the same level", playback_prefs.LEVELLING_TRACK),
    ("Album — keep each album's own dynamics", playback_prefs.LEVELLING_ALBUM),
]
PREAMP_CHOICES = [(f"{n:+d} dB" if n else "0 dB (standard)", float(n)) for n in range(6, -13, -1)]


class LastfmCredentialsDialog(QDialog):
    """A free Last.fm API key - see services/lastfm_popularity.py's module
    docstring for why the "Update track popularity from Last.fm…" button
    below needs this. Last.fm's key-only auth needs just the one field. A
    `SearchBar` (the app's touch-keyboard-equipped text field) rather than a
    plain QLineEdit, since a touch panel may have no physical keyboard;
    masked (`password=True`) since it may be a shared/kiosk screen.
    """

    def __init__(self, api_key: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Last.fm API key")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            "Create a free account at last.fm/api/account/create to get one - "
            "no app review, no login flow, no subscription needed, just an "
            "API key."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("API key"))
        self.api_key_field = SearchBar(placeholder="API key", password=True)
        self.api_key_field.setText(api_key)
        layout.addWidget(self.api_key_field)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def api_key(self) -> str:
        return self.api_key_field.text().strip()


class DiscogsCredentialsDialog(QDialog):
    """A free Discogs personal access token - see
    services/artwork_downloader.py's module docstring for why the "Search
    for missing album artwork…" button below needs this. Same one-field
    shape as LastfmCredentialsDialog (Discogs' token-only auth needs just
    the one value, no OAuth flow)."""

    def __init__(self, token: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Discogs API token")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            "Create a free account and generate a personal access token at "
            "discogs.com/settings/developers - no app review or OAuth flow "
            "needed, just a token."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("API token"))
        self.token_field = SearchBar(placeholder="API token", password=True)
        self.token_field.setText(token)
        layout.addWidget(self.token_field)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def token(self) -> str:
        return self.token_field.text().strip()


class NukeConfirmDialog(QDialog):
    """A stronger-than-usual confirmation for `SettingsView.nuke_library`
    below. Other destructive actions are one Yes/No QMessageBox, but this
    permanently destroys the entire library with no backup kept (see
    db.session.reset_database). Requiring the word NUKE typed before Ok
    enables is deliberate friction against a mis-tap on a touchscreen.
    """

    CONFIRM_WORD = "NUKE"

    def __init__(self, stats: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Nuke library")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        warning = QLabel(
            "⚠ This permanently deletes your entire library database: "
            f"{stats['artists']:,} artists, {stats['releases']:,} releases, "
            f"{stats['tracks']:,} tracks, {stats['playlists']:,} playlists, "
            f"and {stats['charts']:,} charts, plus every rating, play "
            "history entry, watched folder, and saved Discogs/Last.fm "
            "setting.\n\n"
            "Nothing on disk is touched - your actual music and video "
            "files are safe - but MusicMgr's own record of all of it is "
            "gone for good, with no backup kept. You'll need to re-add "
            "your watched folders and scan from scratch afterward.\n\n"
            f"Type {self.CONFIRM_WORD} below to confirm."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self.confirm_field = SearchBar(placeholder=f"Type {self.CONFIRM_WORD} to confirm")
        self.confirm_field.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.confirm_field)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Nuke library")
        self.ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_text_changed(self, text: str) -> None:
        self.ok_button.setEnabled(text.strip().upper() == self.CONFIRM_WORD)


class ScanThread(QThread):
    """Scans every given folder for *both* music and videos, one after the
    other, and reports one combined summary - see the settings package
    docstring for why there's one watched-folder list for both. Progress
    messages are prefixed "Audio:"/"Video:" so it's clear which pass is
    running.

    `progress` carries `(done, total, folder, message)`, `folder` being the
    exact `WatchedFolder.path`, so SettingsView can show each tick on that
    folder's row (see SettingsView._on_progress and TouchList.update_row in
    ui/widgets/common.py). Folders are scanned one at a time, so a per-row
    readout is never ambiguous.

    `force` (the "Re-read all tags (takes more time)" checkbox) passes
    `force=True` to both scans so every file's tags are re-read even if the
    file hasn't changed (see scanner.py:_needs_import) - the only way to
    back-fill a tag-reading fix. Off by default since it's much slower."""

    progress = Signal(int, int, str, str)
    finished_with = Signal(str)

    def __init__(self, folders: list[str], force: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.folders = folders
        # see the class docstring; passed to both the audio and video pass
        self.force = force

    def run(self) -> None:  # pragma: no cover - exercised interactively
        audio_result = scanner.ScanResult()
        video_result = video_scanner.ScanResult()
        try:
            with session_scope() as session:
                for folder in self.folders:
                    if self.isInterruptionRequested():
                        break
                    scanner.scan_folder(
                        session,
                        folder,
                        progress=lambda done, total, name, folder=folder: self.progress.emit(
                            done, total, folder, f"Audio: {name}"
                        ),
                        result=audio_result,
                        force=self.force,
                        should_stop=self.isInterruptionRequested,
                    )
                # old-time radio files an older scan added
                audio_result.radio_removed = scanner.remove_radio_files(session)
                audio_result.skipped_removed = scanner.remove_skipped_folder_files(session)
                audio_result.case_duplicates_removed = scanner.merge_case_duplicate_files(session)
                audio_result.missing = scanner.mark_missing_files(session)
                audio_result.removed = scanner.purge_orphaned_tracks(session)
                scanner.tidy_after_scan(session)
            # a cancel mid-audio-pass skips the video pass entirely; the
            # audio side above still gets its missing/purge bookkeeping
            if not self.isInterruptionRequested():
                with session_scope() as session:
                    for folder in self.folders:
                        if self.isInterruptionRequested():
                            break
                        video_scanner.scan_video_folder(
                            session,
                            folder,
                            progress=lambda done, total, name, folder=folder: self.progress.emit(
                                done, total, folder, f"Video: {name}"
                            ),
                            result=video_result,
                            force=self.force,
                            should_stop=self.isInterruptionRequested,
                        )
                    # videos an older scan took from _gsdata_
                    video_result.skipped_removed = video_scanner.remove_skipped_folder_videos(session)
                    video_result.missing = video_scanner.mark_missing_videos(session)
            summary = f"Audio: {audio_result.summary()} · Video: {video_result.summary()}"
            if self.isInterruptionRequested():
                summary = f"Cancelled — {summary}"
            self.finished_with.emit(summary)
        except Exception as exc:
            self.finished_with.emit(f"Scan failed: {exc}")


class MigrationThread(QThread):
    """Runs data_migration.migrate() off the UI thread for the "Move data
    location…" button - see musicmgr.services.data_migration's module
    docstring for what that actually does, and specifically why running it
    from inside a live app (rather than the closed-app case
    tools/migrate_data.py is documented for) needs its own care.

    remove_source is deliberately never passed here as True: this app's own
    database is still open against `source` (config.DATA_DIR) while this
    thread runs, so deleting that folder out from under the running process
    is riskier than the CLI's closed-app --remove-source. The originals are
    always left in place; SettingsView's completion dialog
    (data_migration.MigrationResult.summary()) says which files are safe to
    delete by hand after a restart confirms the new location.
    """

    progress = Signal(str)
    finished_with = Signal(object)  # data_migration.MigrationResult

    def __init__(self, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.dest = dest

    def run(self) -> None:  # pragma: no cover - exercised interactively
        result = data_migration.migrate(
            config.DATA_DIR,
            self.dest,
            remove_source=False,
            progress=self.progress.emit,
        )
        self.finished_with.emit(result)


# -- in-app updates ------------------------------------------------------------
# The only thing that happens on its own is a once-a-day *check* against
# GitHub; downloading and installing always wait for a tap. The real work is
# in services/updater.py (headless, tested there); these are its threads and
# its one dialog.


class UpdateCheckThread(QThread):
    """One GitHub API call - quick, so not cancellable and not part of
    `_busy_with` (it never touches the database)."""

    finished_with = Signal(object)  # updater.CheckResult

    def __init__(self, include_prerelease: bool, parent=None) -> None:
        super().__init__(parent)
        self.include_prerelease = include_prerelease

    def run(self) -> None:  # pragma: no cover - network, exercised interactively
        self.finished_with.emit(
            updater.check_for_update(
                RELEASE_VERSION,
                repo=config.UPDATE_REPO,
                include_prerelease=self.include_prerelease,
            )
        )


class UpdateDownloadThread(QThread):
    """Downloads and verifies one release next to the running exe. Emits
    `finished_with(path_or_None, error_message_or_None)` - (None, None)
    means it was cancelled."""

    progress = Signal(int, int)
    finished_with = Signal(object, object)

    def __init__(self, info: "updater.UpdateInfo", dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.dest = dest

    def run(self) -> None:  # pragma: no cover - network, exercised interactively
        try:
            path = updater.download_update(
                self.info,
                self.dest,
                current_version=RELEASE_VERSION,
                public_keys=config.update_public_keys(),
                progress=lambda done, total: self.progress.emit(done, total),
                should_stop=self.isInterruptionRequested,
            )
        except updater.UpdateError as exc:
            self.finished_with.emit(None, str(exc))
            return
        except Exception as exc:  # never let a thread die silently
            logging.getLogger(__name__).exception("update download failed")
            self.finished_with.emit(None, f"Unexpected error: {exc}")
            return
        self.finished_with.emit(path, None)


class UpdateAvailableDialog(QDialog):
    """"MusicMgr 1.6.0 is available" - the release notes (GitHub's release
    body, which is Markdown) and three choices. `result()` is one of the
    class constants below."""

    INSTALL = 1
    LATER = 0
    SKIP = 2

    def __init__(self, info: "updater.UpdateInfo", current: str, can_install: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self.setMinimumSize(620, 480)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel(f"MusicMgr {info.version} is available")
        title.setObjectName("Crumb")
        layout.addWidget(title)
        released = f" · released {info.published_at}" if info.published_at else ""
        pre = " · pre-release" if info.prerelease else ""
        layout.addWidget(dim_label(f"You have {current}{released}{pre}"))

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.notes or "_No release notes._")
        layout.addWidget(notes, 1)

        if not can_install:
            layout.addWidget(dim_label("This copy can't update itself - "
                                       "\"Open download page\" takes you to the release instead."))

        buttons = QHBoxLayout()
        self.skip_btn = TouchButton("Skip this version")
        self.skip_btn.clicked.connect(lambda: self.done(self.SKIP))
        self.later_btn = TouchButton("Later")
        self.later_btn.clicked.connect(lambda: self.done(self.LATER))
        self.install_btn = TouchButton("Install now" if can_install else "Open download page", primary=True)
        self.install_btn.setObjectName("PrimaryWarm")
        self.install_btn.clicked.connect(lambda: self.done(self.INSTALL))
        buttons.addWidget(self.skip_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.later_btn)
        buttons.addWidget(self.install_btn)
        layout.addLayout(buttons)


class _ToolRow(QWidget):
    """One row of the Settings page's "Maintenance" list: one action button
    on the left, a title and a short description of what it does filling
    the rest of the row. A vertical list of rows lets each button keep its
    full label at any window width, with a description to read.

    The button comes first so it reads as one unit with its text on a wide
    window. `_size_tool_row_buttons` (called once from SettingsView after
    every row exists) sizes the button column to the longest label's
    `sizeHint()`. `primary=True` (the walnut-brown `#Primary` fill - see
    theme.py's COLORS) is the default, matching the page's other buttons.
    """

    def __init__(
        self, title: str, description: str, button_text: str, *, primary: bool = True, parent=None
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 14, 0, 14)
        row.setSpacing(16)

        self.button = TouchButton(button_text, primary=primary)
        row.addWidget(self.button, 0, Qt.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 600;")
        text_col.addWidget(title_label)
        desc = dim_label(description)
        # wraps rather than widening the page past the window
        desc.setWordWrap(True)
        text_col.addWidget(desc)
        row.addLayout(text_col, 1)


def _size_tool_row_buttons(rows: list["_ToolRow"]) -> None:
    """Give every `_ToolRow` button in `rows` the same fixed width - the
    widest one's own `sizeHint()`, plus a little breathing room - so the
    Maintenance table's left-hand button column lines up into a straight
    edge instead of each button hugging its own label."""
    if not rows:
        return
    width = max(row.button.sizeHint().width() for row in rows) + 12
    for row in rows:
        row.button.setFixedWidth(width)



_KEPT_REASONS = {
    "playlist": "on a playlist",
    "chart": "on a chart",
    "plays": "with play history",
    "jukebox": "in a jukebox slot",
}


def _plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _purge_breakdown(summary: "scanner.MissingSummary", removing: bool = False) -> str:
    """The part of the Purge message explaining the tracks a purge leaves
    alone - see scanner.MissingSummary."""
    if not summary.kept_total:
        return ""
    reasons = ", ".join(
        f"{n:,} {_KEPT_REASONS.get(r, r)}" for r, n in summary.kept.most_common()
    )
    verb = "will be" if removing else ("is" if summary.kept_total == 1 else "are")
    return (
        f"{_plural(summary.kept_total, 'track')} with no file left {verb} "
        f"kept ({reasons})."
    )
