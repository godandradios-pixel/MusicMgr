"""Watched folders, scanning, and housekeeping.

2026-09-07 follow-up (James: "I really don't need a different block for
audio and video. Just one block to add folders and then one option to scan
all those folders") - this view used to show two entirely parallel
sections, each with its own Add/Remove/Scan now buttons, its own list, and
its own progress bar: "Watched folders" for music (`LibraryFolder`) and
"Watched video folders" for videos (`VideoFolder`). That split is gone -
see `db.models.WatchedFolder`'s docstring for the merged table it now reads
and writes. One list, one Add/Remove/Scan now row, one progress bar:
`ScanThread` below runs *both* `services.scanner.scan_folder` and
`services.video_scanner.scan_video_folder` over every watched folder,
back to back, so a folder never has to be told which kind of media it
holds - each scanner just finds whatever it understands and ignores the
rest.

This also fixed a real duplicate-folder bug that had nothing to do with
the two-block layout itself: `add_folder` used to store whatever raw
string `QFileDialog.getExistingDirectory` returned, while `scan_folder`/
`scan_video_folder` looked up (and, if not found, silently re-created) a
folder row by `str(Path(root))`. On Windows, Qt's file dialog returns
forward-slash paths ("D:/Music") but `str(Path(...))` normalizes to
backslashes ("D:\\Music") - two different strings for the same folder, so
every "add a folder, then scan" sequence quietly created a *second*,
never-scanned row for the folder just added. `add_folder` now normalizes
through `Path(...)` before ever touching the database, the same
normalization the scanners already used, so both sides always agree on one
canonical string. The one-time `WatchedFolder` migration also merges any
such duplicate rows an already-affected database was left with (see
`db.session._migrate_watched_folders`).

2026-09-07 follow-up #2 (James: renaming a file on disk to fix a stray
"01 " prefix in its title left the *old* title still showing in search,
looking like a duplicate song): every audio scan now also calls
`scanner.purge_orphaned_tracks` right after `mark_missing_files`, which
deletes any track left with no playable file and no play/playlist/chart
history to lose - see that function's docstring. This runs automatically,
with no confirmation step, by James's own choice.

2026-09-13 follow-up (James: "right now it does all on the folders. I
would like an option where you can select a folder to scan") - "Scan now"
still scans every enabled watched folder, unchanged. A new "Scan
selected" button next to it scans only the one folder highlighted in
`self.folder_list` (`scan_selected`, reusing the exact same
`scan_paths()`/`ScanThread` machinery "Scan now" already runs, just with
a one-item list) - useful for re-scanning a single folder you just added
files to without waiting on every other watched folder as well. Mirrors
`remove_folder`'s existing "act on whatever row is selected"
(`self.folder_list.current_payload()`) shape, rather than inventing a
second selection mechanism.

2026-09-16 follow-up (James, on the new artist-profile page: "Settings
option, like lyrics, to download profile") - a bulk "Download artist
profiles…" button next to "Import artist images…" below, for the same
network-fetch-a-missing-thing job `services.lyrics_downloader` already
does for lyrics, just for `Artist.profile` instead (see
`services.artist_bio_downloader`'s own docstring for why that one's a
database write rather than a sidecar file, and `BioDownloadThread` (ui/
widgets/common.py) / _on_bio_progress / _on_bio_done below for the
mechanics - the same QThread-with-a-progress-signal shape as `ScanThread`
above). Only ever
targets artists with no biography yet (`bio_dl.artists_missing_bio`) -
there's no "Scan now"-style "redo everything" mode here, matching how the
per-artist "Fetch bio" button on the artist page itself only ever offers
to fill in a *missing* bio, never to overwrite one already there.

2026-09-16 same-day follow-up (James, asked where the artist page's "Top
Tracks" ranking came from: "I was thinking more like youtube playlist
count or some authoritative source" → first picked Spotify's own artist
top-tracks endpoint after hearing the options, then - same day, once he
went to actually set it up - switched to Last.fm's equivalent instead,
after finding Spotify's Developer Mode now requires the app owner to hold
a Premium subscription and, worse, had a February 2026 change remove the
artist-top-tracks endpoint outright for a personal app's tier. See
`services.lastfm_popularity`'s own module docstring for the full story)
- two more additions here: `LastfmCredentialsDialog` below, opened from a
new "Last.fm API key…" button, for the one free API key
`services.lastfm_popularity` needs (no OAuth, no login, no Premium - just
a key from last.fm/api/account/create); and a bulk "Update track
popularity from Last.fm…" button next to "Download artist profiles…",
which - unlike that one - deliberately targets *every* artist with a
release, not just ones missing something, since popularity is meant to be
refreshed periodically rather than fetched once and left alone (see
`lastfm_popularity.all_artist_ids_with_releases`).

2026-09-16 follow-up (James: "let's have settings be rows in a table. All
those 'buttons' at bottom are cramed and I can't read their text") - the
nine maintenance actions above (Import artist images… through Purge
missing files…) used to sit in one `QHBoxLayout`, packed edge to edge with
no wrap - fine at the width it was designed at, unreadable the moment the
window was any narrower, since a button's own label just clips instead of
the row wrapping. `_ToolRow` (below) turns each one into its own row - a
title, a short description of what it actually does, and the button - laid
out one per line in a Card frame. See `_ToolRow`'s own docstring for the
rest of it.

2026-09-16 same-day follow-up #2 (James, on the table above: "we don't
need the line between the rows. Also can you put the action buttons to
the left of the text. It looks strange having them hanging way out to the
right") - dropped the `divider()` row separator (the Card frame's own
outline plus the row spacing already reads as one grouped list without
it), and swapped `_ToolRow`'s internal order so the button sits on the
left with the title/description filling the rest of the row - see
`_ToolRow` itself for the new layout.

2026-09-22 follow-up (James: "how do I stop a process, Download Artist
Profiles from running", followed a moment later by "add a real cancel
button that stops any process running within Settings") - this page's
nine background threads (everything `_busy_with` above enumerates) had no
way to stop once started short of closing the whole app. A single
`self.cancel_btn`, next to `self.progress_label`, now covers eight of
them - `_active_cancellable_thread`/`_show_cancel`/`_hide_cancel` right
after `_busy_with` below - by requesting `QThread.requestInterruption()`
on whichever one is currently running; each underlying service-layer loop
(`download_bios_for_artists`, `update_popularity_for_artists`,
`search_artwork_for_releases`, `import_artist_images`,
`scanner.scan_folder`/`mark_missing_files`,
`video_scanner.scan_video_folder`/`mark_missing_videos`,
`charts.rematch_all_charts`/`rematch_chart`,
`metadata_health.scan_missing_metadata`) grew a matching `should_stop`
parameter, checked once per item and always via a clean `break` rather
than a raise, so whatever's already been committed (or, for the handful
that only flush once at the end, already processed in memory) is never
rolled back - see each function's own should_stop docstring note for why
that specific loop is safe to interrupt this way. `MigrationThread` (the
ninth - "Move data location…") is the deliberate exception:
`data_migration.migrate` copies files to a new location in several
discrete steps with no per-step undo, so `_CANCELLABLE_THREAD_ATTRS`
leaves it out entirely and Cancel simply never appears while it's
running - see move_data's own confirmation dialog text for how that's
explained up front instead.
"""

from __future__ import annotations

import logging
import sys

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from ... import config
from ...version import APP_VERSION, RELEASE_VERSION
from ...db.models import WatchedFolder
from ...db.session import reset_database, session_scope
from ...services import artist_bio_downloader as bio_dl
from ...services import artist_images as art_svc
from ...services import artwork_downloader as artwork_dl
from ...services import artwork_names
from ...services import data_migration
from ...services import library as lib
from ...services import scanner
from ...services import lastfm_popularity as popularity_dl
from ...services import metadata_health
from ...services import updater
from ...services import library_state
from ...services import usb_sync
from ...services import video_scanner
from ...services import videos as vid_svc
from ...services.library import format_duration
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import (
    ArtistImagesImportThread,
    ArtworkBulkThread,
    BioDownloadThread,
    MetadataScanThread,
    MissingMetadataDialog,
    PopularityDownloadThread,
    RematchChartsThread,
    SearchBar,
    TouchButton,
    clear_art_cache,
    TouchList,
    VerifyFilesThread,
    dim_label,
)
from ..widgets.usb_sync import (
    UsbCompareThread,
    UsbPairsDialog,
    UsbSyncDialog,
    UsbSyncThread,
    human_size,
)
from .base import BaseView


class LastfmCredentialsDialog(QDialog):
    """A free Last.fm API key - see services/lastfm_popularity.py's module
    docstring for why the "Update track popularity from Last.fm…" button
    below needs this at all. Just the one field, unlike the short-lived
    Spotify attempt this replaced (SpotifyCredentialsDialog, gone - Spotify
    needed a client ID *and* secret; Last.fm's key-only auth needs only
    this). A `SearchBar` (the same touch-keyboard-equipped field the app
    already uses for every other text entry - see ui/widgets/cover_grid.py:
    JumpBar/ui/views/videos.py's search row) rather than a plain QLineEdit,
    since this is a touch panel with no physical keyboard to type a key
    on otherwise. Masked (SearchBar's `password=True`) - a small courtesy
    even for a value typed once during setup on what might be a
    shared/kiosk screen.
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
    shape as LastfmCredentialsDialog just above, for the same reason
    (Discogs' token-only auth needs just the one value, no OAuth flow)."""

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
    below. Every other destructive action in this app (see
    purge_missing_files' own docstring) is one Yes/No QMessageBox, but
    none of those permanently destroy the *entire* library with no way
    back the way this does - James was asked whether to keep a
    timestamped backup first and explicitly chose not to (see
    db.session.reset_database's own docstring). Requiring the exact word
    NUKE typed out before Ok even enables is deliberate extra friction
    against a mis-tap on a touchscreen kiosk, on top of that choice, not
    instead of it.
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
    other, and reports one combined summary - see the module docstring for
    why there's no longer a separate video scan thread/button. Progress
    messages are prefixed "Audio:"/"Video:" so it's clear which pass is
    running.

    2026-09-08 follow-up - James: "have the progress bar appear on the line
    with the folder." `progress` grew a `folder` argument (the exact
    `WatchedFolder.path` string the caller passed to scan_folder/
    scan_video_folder) alongside the existing (done, total, filename), so
    SettingsView can route each tick to that one folder's row in the list
    instead of a single page-wide bar - see SettingsView._on_progress and
    ui/widgets/common.py's TouchList.update_row. Only one folder is ever
    "current" at a time either way (both passes scan self.folders one at a
    time, never concurrently), so a per-row readout is never ambiguous
    about which row it belongs to.

    2026-09-18 follow-up (James: "The orginal scan was not pulling the
    comments field from a file tag and putting that into the database") -
    an ordinary scan only reads tags for a file it hasn't seen before, or
    one whose size/mtime changed (see scanner.py:_needs_import), so fixing
    a tag-reading bug (like the comment one just above) never touches a
    file already sitting in the library. `force` (surfaced as the "Re-read
    all tags (takes more time)" checkbox in SettingsView, next to the
    folder buttons) threads a `force=True` into both scan calls below,
    which makes every watched file's tags get re-read regardless of
    whether the file itself has changed - the only way to back-fill a tag
    fix onto an already-scanned library. Off by default since re-reading
    every file on every routine scan would make scanning a large library
    far slower for no normal benefit."""

    progress = Signal(int, int, str, str)
    finished_with = Signal(str)

    def __init__(self, folders: list[str], force: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.folders = folders
        # 2026-09-17 follow-up (James: "still blank after a rescan", chasing
        # the MP3 Comment-tag fix - see scanner.py's `import_file` docstring
        # for why an ordinary rescan alone could never have picked it up) -
        # the "Force full re-read" checkbox below sets this, threaded
        # through both the audio pass (`scanner.scan_folder`) and the video
        # pass (`video_scanner.scan_video_folder`) below.
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
                audio_result.missing = scanner.mark_missing_files(session)
                audio_result.removed = scanner.purge_orphaned_tracks(session)
            # 2026-09-22 - James: "add a real cancel button that stops any
            # process running within Settings" - a cancel mid-audio-pass
            # skips the video pass entirely rather than starting a second
            # multi-folder walk right after being asked to stop; the audio
            # side above still gets its own mark_missing_files/
            # purge_orphaned_tracks bookkeeping either way, same as any
            # other scan, cancelled or not.
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
    is a sharper risk than anything the CLI's closed-app --remove-source
    faces. The originals are always left in place; SettingsView's
    completion dialog (data_migration.MigrationResult.summary()) tells
    James which files are now safe to delete by hand, once he's restarted
    and confirmed the new location looks right.
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


# -- in-app updates (2026-09-23) ----------------------------------------------
# James: "a settings option that checks for updates to the program... connect
# to the github to check... I would like the update to be automatic", then
# "always ask first" when offered the choice - so the only thing that happens
# on its own is a once-a-day *check*; downloading and installing always wait
# for a tap. All the real work is in services/updater.py (headless, tested
# there); these are just its threads and its one dialog.


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
    the rest of the row. 2026-09-16 follow-up (James: "let's have settings
    be rows in a table. All those 'buttons' at bottom are cramed and I
    can't read their text") - replaces a single `QHBoxLayout` that packed
    nine `TouchButton`s edge to edge (`add_folder`/`remove_folder` and up
    through `purge_missing_files` below), which read fine at the width it
    was built at but clipped every button's own label the moment the
    window was any narrower - there was nowhere left to shrink to once the
    buttons themselves started truncating. A vertical list of rows has no
    such ceiling: each button keeps its full label, and the description
    text is somewhere for James to actually read what a button does rather
    than guessing from a name he might not be able to see all of.

    2026-09-16 same-day follow-up #2 (James, seeing the first version of
    this table: "put the action buttons to the left of the text. It looks
    strange having them hanging way out to the right") - on a wide window
    the text column's own stretch factor pushed each button out to the far
    right edge of the card, a long way from the title/description it
    belongs to; putting the button first reads as one unit with the text
    immediately next to it regardless of how wide the card is.

    2026-09-16 same-day follow-up #3 (James, on that same screenshot:
    "extend the buttons so that the text can be displayed. Also make the
    button the brown pallete colors") - the left-hand column's *first*
    attempt gave every button one arbitrary fixed width (120px), which
    turned out narrower than several of the actual labels ("Download…",
    "Set key…") once TouchButton's touch-scaled font was accounted for, so
    those still clipped. `_size_tool_row_buttons` below (called once from
    SettingsView.__init__, after every row exists) replaces that guess with
    each button's own `sizeHint()`, so the column is always exactly as wide
    as its longest real label - no more guessing a pixel number that has to
    be revisited every time a button's text changes. `primary=True` (the
    walnut-brown `#Primary`/`#PrimaryWarm` fill - see theme.py's COLORS
    comment for that palette's own history) is now this row's default
    rather than opt-in, matching the rest of the page's brown-palette
    buttons (add_btn's "Add folder…" above) instead of the plain gray
    TouchButton default.
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
        text_col.addWidget(dim_label(description))
        row.addLayout(text_col, 1)


def _size_tool_row_buttons(rows: list["_ToolRow"]) -> None:
    """Give every `_ToolRow` button in `rows` the same fixed width - the
    widest one's own `sizeHint()`, plus a little breathing room - so the
    Maintenance table's left-hand button column lines up into a straight
    edge instead of each button hugging its own (differently-sized) label.
    See `_ToolRow`'s docstring for why this replaced an earlier hardcoded
    guess."""
    if not rows:
        return
    width = max(row.button.sizeHint().width() for row in rows) + 12
    for row in rows:
        row.button.setFixedWidth(width)


class SettingsView(BaseView):
    title_text = "Library & Settings"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._thread: Optional[ScanThread] = None
        self._migration_thread: Optional[MigrationThread] = None
        self._bio_thread: Optional[BioDownloadThread] = None
        self._popularity_thread: Optional[PopularityDownloadThread] = None
        self._artwork_thread: Optional[ArtworkBulkThread] = None
        self._metadata_scan_thread: Optional[MetadataScanThread] = None
        #: 2026-09-22 - James: "does the progress bar also work on the
        #: import of artist images and the other settings options that
        #: scan" (it didn't - "Artist images"/"Verify files"/"Charts" all
        #: used to run entirely on the UI thread with no feedback). Same
        #: QThread-with-a-progress-signal shape as everything above.
        self._artist_images_thread: Optional[ArtistImagesImportThread] = None
        self._verify_thread: Optional[VerifyFilesThread] = None
        self._rematch_thread: Optional[RematchChartsThread] = None
        #: last "Missing metadata" scan, reused so reopening the dashboard
        #: (e.g. right after fixing one item) doesn't rerun the whole thing
        #: - see view_missing_metadata's own docstring. Cleared (forcing a
        #: fresh scan) whenever a bulk action below changes the data it
        #: reports on, and by the dialog's own "Rescan" button.
        self._last_metadata_scan: Optional[metadata_health.ScanResult] = None
        #: (kind, id) pairs picked from the dashboard at least once - purely
        #: a "you've already been here" marker (MissingMetadataDialog), so
        #: it's additive only, never cleared by a rescan.
        self._metadata_visited: set = set()
        # 2026-09-22 follow-up (James: "is there any way we can have a back
        # button when you go from missing metadata to the album and then
        # back") - the artist/release page's own "Missing metadata"
        # breadcrumb crumb (LibraryView._return_to_missing_metadata) lands
        # back here by emitting this rather than by calling anything on
        # Settings directly, the same "Library and Settings only talk
        # through ctx" convention every other cross-view hand-off in this
        # app already follows.
        ctx.missingMetadataBackRequested.connect(self.view_missing_metadata)

        # 2026-09-16 follow-up (same one that added the row-per-tool
        # Maintenance table below) - this page's content used to fit
        # `BaseView`'s own plain (non-scrolling) body every time, because
        # the old cramped-buttons "tools" row was short enough that the
        # whole page's total height rarely exceeded a real window. Turning
        # those buttons into nine full rows (see _ToolRow) made the page
        # tall enough that it regularly doesn't - and unlike
        # `ArtistDetailPanel` (ui/widgets/artist_panel.py), which has
        # wrapped its own content in a QScrollArea since it grew a
        # biography and Top Tracks section, `BaseView.body()` is a plain
        # QVBoxLayout directly on the view widget with nothing scrollable
        # about it: a view taller than the window it's given doesn't
        # scroll, it just gets squeezed - every row's label and button
        # compressed toward zero height rather than clipped cleanly, which
        # is what actually happened (not a rendering bug in the new rows
        # themselves - James's screenshot on a normal-height window showed
        # everything crushed into illegibility). Wrapping this page's own
        # body in its own scroll area - the same fix ArtistDetailPanel
        # already uses, just applied here instead of changing `BaseView`
        # for every other page - means a short window scrolls this page
        # instead of crushing it. `body` (a local, shadowing but distinct
        # from self.body()) is what every line below now builds into,
        # instead of `self.body()` directly - only the page's own title
        # (added by BaseView.__init__ above, before this) stays outside the
        # scroll area, always visible.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        scroll.setWidget(content)
        self.body().addWidget(scroll, 1)

        # ---- stats card ----
        card = QFrame()
        card.setObjectName("Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(20, 18, 20, 18)
        grid.setHorizontalSpacing(36)
        self._stat_labels: dict[str, QLabel] = {}
        for idx, name in enumerate(
            ["Artists", "Releases", "Tracks", "Files", "Videos", "Playlists", "Plays"]
        ):
            value = QLabel("0")
            value.setObjectName("BigNumber")
            key = QLabel(name)
            key.setObjectName("Dim")
            grid.addWidget(value, 0, idx, alignment=Qt.AlignHCenter)
            grid.addWidget(key, 1, idx, alignment=Qt.AlignHCenter)
            self._stat_labels[name] = value
        self.total_time = dim_label("")
        grid.addWidget(self.total_time, 2, 0, 1, 6, alignment=Qt.AlignHCenter)
        body.addWidget(card)

        # ---- folders ----
        folder_head = QHBoxLayout()
        label = QLabel("Watched folders")
        label.setObjectName("Crumb")
        folder_head.addWidget(label)
        folder_head.addStretch(1)
        add_btn = TouchButton("Add folder…", primary=True)
        # Settings page's own brown pallet rather than the app-wide red-
        # orange #Primary - see theme.py's #PrimaryWarm/#BigNumber/
        # #ProgressWarm comments (James: "change all that bright orange/red
        # color on this settings page to the brown pallet").
        add_btn.setObjectName("PrimaryWarm")
        add_btn.clicked.connect(self.add_folder)
        remove_btn = TouchButton("Remove")
        remove_btn.clicked.connect(self.remove_folder)
        scan_selected_btn = TouchButton("Scan selected")
        scan_selected_btn.clicked.connect(self.scan_selected)
        scan_btn = TouchButton("Scan now")
        scan_btn.clicked.connect(self.scan_all)
        for b in (add_btn, remove_btn, scan_selected_btn, scan_btn):
            folder_head.addWidget(b)
        body.addLayout(folder_head)

        # 2026-09-17 follow-up (James: "still blank after a rescan", after
        # a Comment-tag reading fix landed in services/scanner.py) - an
        # ordinary scan only reads tags from a file whose mtime/size on
        # disk actually changed, so fixing a tag-reading bug does nothing
        # for a library that's already been scanned; every file in it
        # still looks "unchanged" to the scanner. This checkbox is the
        # escape hatch: checked, "Scan selected"/"Scan now" re-read every
        # file's tags regardless (see scanner.py's `import_file` `force`
        # docstring) - slower on a big library, so it defaults off and
        # stays off after each scan rather than becoming the new normal.
        self.force_rescan_cb = QCheckBox("Force full re-read (slow)")
        self.force_rescan_cb.setToolTip(
            "Re-reads every file's tags even if it hasn't changed on disk.\n"
            "Use this once after a tag-reading fix; leave it off otherwise."
        )
        force_row = QHBoxLayout()
        force_row.addWidget(self.force_rescan_cb)
        force_row.addStretch(1)
        body.addLayout(force_row)

        self.folder_list = TouchList()
        body.addWidget(self.folder_list, 1)

        # 2026-09-08 follow-up - James: "have the progress bar appear on the
        # line with the folder." A scan's progress now paints directly into
        # the relevant row of self.folder_list above (see _on_progress and
        # ui/widgets/common.py's TouchList.update_row/RowDelegate), so this
        # page-wide bar is left for "Move data location…" only - that copy
        # targets one destination folder, not a folder in this list, so it
        # has no row of its own to report progress into.
        self.progress = QProgressBar()
        self.progress.setObjectName("ProgressWarm")  # this page's brown pallet - see theme.py
        self.progress.setVisible(False)
        body.addWidget(self.progress)

        # 2026-09-22 - James: "add a real cancel button that stops any
        # process running within Settings" (asked right after being told
        # "Download Artist Profiles" had no way to stop it short of closing
        # the whole app). One button, not one per bulk action - see
        # `_busy_with`, which this reuses the same "which of the nine
        # threads is running" enumeration from (minus MigrationThread - see
        # `_active_cancellable_thread`'s own docstring for why a data move
        # can't safely take a mid-flight stop). Sits right next to
        # `progress_label` rather than in the Maintenance table below,
        # since that's the one line every bulk action already updates
        # (ScanThread is the one exception - see its own row-progress
        # notes - but still shows/hides this same button via
        # `_show_cancel`/`_hide_cancel`).
        self.progress_label = dim_label("")
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_label, 1)
        self.cancel_btn = TouchButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        progress_row.addWidget(self.cancel_btn, 0, Qt.AlignRight)
        body.addLayout(progress_row)

        # ---- maintenance ----
        # 2026-09-16 follow-up (James: "let's have settings be rows in a
        # table" - see _ToolRow's own docstring above for the full "buttons
        # cramed" complaint this replaces) - one row per tool, each with its
        # own button and a one-line description, inside a Card frame rather
        # than nine TouchButtons packed into a single QHBoxLayout with
        # nowhere left to shrink to.
        #
        # 2026-09-16 same-day follow-up #2 (James: "we don't need the line
        # between the rows") - dropped the `divider()` separator this used
        # to add between every row; the Card frame's own outline already
        # groups these into one list without it.
        tools_label = QLabel("Maintenance")
        tools_label.setObjectName("Crumb")
        body.addWidget(tools_label)

        tools_card = QFrame()
        tools_card.setObjectName("Card")
        tools_layout = QVBoxLayout(tools_card)
        tools_layout.setContentsMargins(20, 2, 20, 2)
        tools_layout.setSpacing(0)

        tool_rows: list[_ToolRow] = []

        def add_tool_row(title: str, description: str, button_text: str, handler) -> TouchButton:
            row = _ToolRow(title, description, button_text)
            row.button.clicked.connect(handler)
            tools_layout.addWidget(row)
            tool_rows.append(row)
            return row.button

        # 2026-09-22 follow-up (James: "move that missing metadata settings
        # option up closer to the progress bar") - first in this list
        # rather than after the other four enrichment-source rows, since
        # `self.progress`/`self.progress_label` just above this card is
        # exactly what lights up while its own scan runs (see
        # view_missing_metadata).
        add_tool_row(
            "Missing metadata",
            "See everything still missing cover art, a biography, popularity data, or lyrics.",
            "View…",
            self.view_missing_metadata,
        )
        add_tool_row(
            "Artist images",
            "Import missing cover art for your artists from local files.",
            "Import…",
            self.import_artist_images,
        )
        # 2026-09-16 follow-up (James, on the new artist-profile page:
        # "Settings option, like lyrics, to download profile") - see the
        # module docstring and download_artist_profiles below.
        add_tool_row(
            "Artist profiles",
            "Download a biography for every artist that doesn't have one yet.",
            "Download…",
            self.download_artist_profiles,
        )
        add_tool_row(
            "Track popularity",
            "Refresh Last.fm popularity for every artist with a release.",
            "Update…",
            self.update_track_popularity,
        )
        add_tool_row(
            "Album artwork",
            "Search Discogs for a cover for every release that doesn't have one yet.",
            "Search…",
            self.search_missing_artwork,
        )
        # 2026-09-24 (USB sync step 2) - see services/artwork_names.py
        add_tool_row(
            "Relink artwork",
            "Point albums and artists at their pictures in the artwork and artists folders "
            "(runs by itself after a USB sync).",
            "Relink",
            self.relink_artwork,
        )
        add_tool_row(
            "Verify files",
            "Check that every track's file can still be found on disk.",
            "Verify",
            self.verify_files,
        )
        add_tool_row(
            "Charts",
            "Re-match every chart entry against your current library.",
            "Re-match",
            self.rematch_all,
        )
        add_tool_row(
            "Data location",
            "See where your library database and media files live.",
            "Show",
            self.show_paths,
        )
        add_tool_row(
            "Move data",
            "Move your database and media files to a new location.",
            "Move…",
            self.move_data,
        )
        add_tool_row(
            "Missing files",
            "Remove tracks whose files can no longer be found.",
            "Purge…",
            self.purge_missing_files,
        )
        # 2026-09-22 follow-up (James: "move the 2 API key and token
        # options to the bottom just above the nuke") - Last.fm API key/
        # Discogs API token used to sit next to the button that actually
        # uses each key (Track popularity/Album artwork respectively);
        # moved here as a pair instead, right before the destructive
        # "Nuke library" row below.
        add_tool_row(
            "Last.fm API key",
            "Set the free API key track popularity needs to fetch Last.fm charts.",
            "Set key…",
            self.open_lastfm_credentials,
        )
        # 2026-09-18 - James: "Ability to search for album artwork" - see
        # services/artwork_downloader.py's module docstring for the full
        # story and why this bulk button auto-applies the top match rather
        # than reviewing each one (unlike the release page's own per-release
        # "Search artwork" button, which always shows a picker first).
        add_tool_row(
            "Discogs API token",
            "Set the free personal access token album artwork search needs.",
            "Set token…",
            self.open_discogs_credentials,
        )
        # 2026-09-19 - James: "Create for me a 'nuke' option. Where you
        # completely wipe out library.db and start with a fresh database."
        # Deliberately last in this list - the most destructive action
        # here by a wide margin, see NukeConfirmDialog/nuke_library below.
        add_tool_row(
            "Nuke library",
            "Permanently erase your entire library database and start completely fresh.",
            "Nuke…",
            self.nuke_library,
        )
        _size_tool_row_buttons(tool_rows)

        body.addWidget(tools_card)

        # ---- updates (2026-09-23) ----
        # See the "in-app updates" classes above and services/updater.py.
        # Replaces the old right-aligned "MusicMgr <commit>" footer that
        # used to sit here - the version now leads this card instead, since
        # "which version am I on" and "is there a newer one" are the same
        # question.
        updates_label = QLabel("Updates")
        updates_label.setObjectName("Crumb")
        body.addWidget(updates_label)

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

        # ---- USB sync (2026-09-23) ----
        # James: "I would like to eliminate GoodSync from my workflow...
        # a check for new music against my USB thumb drive", two-way for
        # music, videos and movies. See services/usb_sync.py and
        # claude/2026-09-23-usb-sync-plan.md.
        usb_label = QLabel("USB sync")
        usb_label.setObjectName("Crumb")
        body.addWidget(usb_label)

        usb_card = QFrame()
        usb_card.setObjectName("Card")
        usb_layout = QVBoxLayout(usb_card)
        usb_layout.setContentsMargins(20, 14, 20, 14)
        usb_layout.setSpacing(8)
        self.usb_drive_label = QLabel("")
        self.usb_drive_label.setStyleSheet("font-weight: 600;")
        usb_layout.addWidget(self.usb_drive_label)
        self.usb_pairs_label = dim_label("")
        self.usb_pairs_label.setWordWrap(True)
        usb_layout.addWidget(self.usb_pairs_label)
        self.usb_status = dim_label("")
        self.usb_status.setWordWrap(True)
        usb_layout.addWidget(self.usb_status)
        self.usb_auto_cb = QCheckBox("Check when the USB drive is plugged in")
        self.usb_auto_cb.toggled.connect(
            lambda on: self._save_update_pref(usb_sync.PREF_AUTO_CHECK, on)
        )
        usb_layout.addWidget(self.usb_auto_cb)
        # 2026-09-24 - James: "do I have the option to sync Jukebox entries
        # or to ignore?" -> "Add check boxes for all 4". Per PC; see
        # services/library_state.py:sync_library_state's `include`.
        data_row = QHBoxLayout()
        data_row.addWidget(dim_label("Also sync library data:"))
        self.usb_data_cbs: dict[str, QCheckBox] = {}
        for cat, text in (
            (library_state.PLAYS, "Plays"),
            (library_state.RATINGS, "Ratings"),
            (library_state.PLAYLISTS, "Playlists"),
            (library_state.JUKEBOX, "Jukebox"),
        ):
            cb = QCheckBox(text)
            cb.toggled.connect(lambda on, cat=cat: self._save_usb_data_choice(cat, on))
            self.usb_data_cbs[cat] = cb
            data_row.addWidget(cb)
        data_row.addStretch(1)
        usb_layout.addLayout(data_row)
        self.usb_progress = QProgressBar()
        self.usb_progress.setObjectName("ProgressWarm")
        self.usb_progress.setVisible(False)
        usb_layout.addWidget(self.usb_progress)

        usb_buttons = QHBoxLayout()
        self.usb_check_btn = TouchButton("Check USB now", primary=True)
        self.usb_check_btn.setObjectName("PrimaryWarm")
        self.usb_check_btn.clicked.connect(lambda: self.check_usb(auto=False))
        self.usb_review_btn = TouchButton("Review…", primary=True)
        self.usb_review_btn.setObjectName("PrimaryWarm")
        self.usb_review_btn.clicked.connect(self.review_usb_plan)
        self.usb_cancel_btn = TouchButton("Cancel")
        self.usb_cancel_btn.clicked.connect(self._cancel_usb)
        self.usb_setup_btn = TouchButton("Set up this drive…")
        self.usb_setup_btn.clicked.connect(self.setup_usb_drive)
        self.usb_pairs_btn = TouchButton("Folder pairs…")
        self.usb_pairs_btn.clicked.connect(self.edit_usb_pairs)
        self.usb_empty_btn = TouchButton("Empty _deleted…")
        self.usb_empty_btn.clicked.connect(self.empty_usb_deleted)
        for b in (
            self.usb_check_btn, self.usb_review_btn, self.usb_cancel_btn,
            self.usb_setup_btn, self.usb_pairs_btn, self.usb_empty_btn,
        ):
            usb_buttons.addWidget(b)
        usb_buttons.addStretch(1)
        usb_layout.addLayout(usb_buttons)
        body.addWidget(usb_card)

        self._usb_compare_thread: Optional[UsbCompareThread] = None
        self._usb_sync_thread: Optional[UsbSyncThread] = None
        #: the drive found by the last poll, if any
        self._usb_drive: Optional[usb_sync.Drive] = None
        #: False until the first poll after launch has run - see poll_usb
        self._usb_polled_once = False
        #: an auto-check's result, waiting behind "Review…"
        self._usb_pending_plan: Optional[usb_sync.ComparePlan] = None
        with self.ctx.session() as db:
            auto = updater.get_bool_pref(db, usb_sync.PREF_AUTO_CHECK, True)
            chosen = library_state.enabled_categories(db)
        self.usb_auto_cb.blockSignals(True)
        self.usb_auto_cb.setChecked(auto)
        self.usb_auto_cb.blockSignals(False)
        for cat, cb in self.usb_data_cbs.items():
            cb.blockSignals(True)
            cb.setChecked(cat in chosen)
            cb.blockSignals(False)
        #: looks for the drive every few seconds - see poll_usb
        self._usb_timer = QTimer(self)
        self._usb_timer.setInterval(5000)
        self._usb_timer.timeout.connect(self.poll_usb)
        self._usb_timer.start()
        self._refresh_usb_controls()
        QTimer.singleShot(1500, self.poll_usb)

        ctx.libraryChanged.connect(self.refresh)
        ctx.videosChanged.connect(self.refresh)

    def _busy_with(self) -> Optional[str]:
        """None if no other bulk background job is currently running, else
        the notify() message to show instead of starting a new one - every
        bulk-action handler below checks this (after its own "am I already
        running" check, which gets its own more specific message) so at
        most one such job ever touches the database at once.

        2026-09-22 follow-up (the same "does the progress bar also work
        on ... the other settings options that scan" conversation that
        added ArtistImagesImportThread/VerifyFilesThread/RematchChartsThread
        below) - one shared check replacing each handler's own repeated
        "if self._x_thread ... elif self._y_thread ..." chain, which was
        still fine at four sibling threads to cross-check but stopped being
        worth copy-pasting once there were eight."""
        for thread, busy_message in (
            (self._thread, "A scan is running — wait for it to finish first"),
            (self._migration_thread, "A data move is running — wait for it to finish first"),
            (self._bio_thread, "Artist profiles are downloading — wait for it to finish first"),
            (self._popularity_thread, "Track popularity is updating — wait for it to finish first"),
            (self._artwork_thread, "Album artwork is downloading — wait for it to finish first"),
            (self._metadata_scan_thread, "Missing metadata is scanning — wait for it to finish first"),
            (self._artist_images_thread, "Artist images are importing — wait for it to finish first"),
            (self._verify_thread, "Files are being verified — wait for it to finish first"),
            (self._rematch_thread, "Charts are re-matching — wait for it to finish first"),
            (getattr(self, "_usb_compare_thread", None), "The USB is being checked — wait for it to finish first"),
            (getattr(self, "_usb_sync_thread", None), "The USB is syncing — wait for it to finish first"),
        ):
            if thread is not None and thread.isRunning():
                return busy_message
        return None

    #: every bulk-action thread attribute that's actually safe to interrupt
    #: mid-run - the same nine minus `_migration_thread`. 2026-09-22, James:
    #: "add a real cancel button that stops any process running within
    #: Settings" - `MigrationThread` (data_migration.migrate) copies files
    #: to a new location in several discrete steps (db, artwork/, artists/,
    #: then rewriting paths) with no per-step undo; stopping it partway
    #: could leave a half-copied, half-rewritten destination that looks
    #: plausible but isn't safe to actually switch to. Every thread listed
    #: here, by contrast, either commits/flushes per-item or is read-only -
    #: see each service function's own should_stop docstring note for the
    #: specifics - so a break is always safe.
    _CANCELLABLE_THREAD_ATTRS = (
        "_thread",
        "_bio_thread",
        "_popularity_thread",
        "_artwork_thread",
        "_metadata_scan_thread",
        "_artist_images_thread",
        "_verify_thread",
        "_rematch_thread",
        "_usb_compare_thread",
        "_usb_sync_thread",
    )

    def _active_cancellable_thread(self) -> Optional[QThread]:
        """The one currently-running thread Cancel is allowed to act on, or
        None if nothing cancellable is running right now (including "the
        thing running is the migration, which Cancel can't touch")."""
        for attr in self._CANCELLABLE_THREAD_ATTRS:
            thread = getattr(self, attr, None)
            if thread is not None and thread.isRunning():
                return thread
        return None

    def _show_cancel(self) -> None:
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)

    def _hide_cancel(self, thread: Optional[QThread] = None) -> None:
        """Called from every bulk action's `_on_..._done` handler, mirroring
        the matching `self.progress.setVisible(False)` right next to each
        one. `thread` - the just-finished thread, if this action has a
        cancellable one - lets a run that was actually stopped early say so
        once, here, rather than every individual result dialog below
        needing its own "was this cancelled" wording."""
        self.cancel_btn.setVisible(False)
        if thread is not None and thread.isInterruptionRequested():
            self.ctx.notify("Cancelled — showing partial results")

    def _on_cancel_clicked(self) -> None:
        thread = self._active_cancellable_thread()
        if thread is None:
            return
        thread.requestInterruption()
        self.cancel_btn.setEnabled(False)
        self.ctx.notify("Cancelling — finishing the current item…")


    def relink_artwork(self) -> None:
        """Maintenance → Relink artwork: point every release/artist at its
        name-based picture (artwork\\<Artist> - <Title>.jpg,
        artists\\<Artist>.jpg). The USB sync does this by itself after
        pictures arrive; this is for files dropped in by hand."""
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        with self.ctx.session() as db:
            result = artwork_names.relink(db)
        clear_art_cache()
        self.ctx.notify(result.summary())
        if result.covers or result.artist_images:
            self.ctx.libraryChanged.emit()

    # -- USB sync (2026-09-23) -------------------------------------------------
    # See services/usb_sync.py. The flow: poll_usb notices the drive (every
    # 5 s), check_usb walks and compares both sides in UsbCompareThread,
    # UsbSyncDialog shows what it found, and UsbSyncThread copies what James
    # left checked and then imports just those files into the library.

    def _save_usb_data_choice(self, category: str, on: bool) -> None:
        with self.ctx.session() as db:
            library_state.set_category_enabled(db, category, on)

    def _find_usb_drives(self) -> list:
        """Overridable seam for tests."""
        return usb_sync.find_drives()

    def _usb_running(self) -> bool:
        return any(
            t is not None and t.isRunning()
            for t in (self._usb_compare_thread, self._usb_sync_thread)
        )

    def poll_usb(self) -> None:
        """Notice the drive being plugged in or pulled out. A newly seen
        drive gets a quiet check when "Check when the USB drive is plugged
        in" is on - it never copies anything without a review."""
        try:
            drives = self._find_usb_drives()
        except Exception:  # never let a poll take the page down
            logging.getLogger(__name__).exception("USB poll failed")
            drives = []
        drive = drives[0] if drives else None
        previous = self._usb_drive.drive_id if self._usb_drive else None
        current = drive.drive_id if drive else None
        self._usb_drive = drive
        first_poll = not self._usb_polled_once
        self._usb_polled_once = True
        if current == previous and not first_poll:
            return
        if current is None:
            self._usb_pending_plan = None
            self.usb_status.setText("")
        self._refresh_usb_controls()
        # The first poll after launch only notes a drive that's already in:
        # walking a terabyte at startup is exactly the slow start James
        # doesn't want (2026-09-24). Plugging the drive in while MusicMgr
        # runs still triggers the quiet check.
        if first_poll:
            return
        if drive is not None and self.usb_auto_cb.isChecked() and not self._busy_with():
            self.check_usb(auto=True)

    def _refresh_usb_controls(self) -> None:
        drive = self._usb_drive
        running = self._usb_running()
        self.usb_setup_btn.setEnabled(not running)
        self.usb_cancel_btn.setVisible(running)
        if not running:
            self.usb_cancel_btn.setEnabled(True)
        self.usb_review_btn.setVisible(self._usb_pending_plan is not None and not running)
        if drive is None:
            self.usb_drive_label.setText("No MusicMgr USB drive connected")
            self.usb_pairs_label.setText(
                "Plug in the USB drive, or choose \"Set up this drive…\" to make one."
            )
            for b in (self.usb_check_btn, self.usb_pairs_btn, self.usb_empty_btn):
                b.setEnabled(False)
            return
        self.usb_drive_label.setText(f"USB drive: {drive.describe()}")
        with self.ctx.session() as db:
            pairs = [p for p in usb_sync.ensure_default_pairs(db, drive) if p.enabled]
            names = []
            for pair in pairs:
                when = (
                    f" (last synced {pair.last_synced_at:%Y-%m-%d %H:%M})" if pair.last_synced_at else ""
                )
                names.append(f"{pair.local_path} ↔ USB\\{pair.usb_rel_path}{when}")
        self.usb_pairs_label.setText(
            "\n".join(names) if names else
            "No folder pairs yet - choose \"Folder pairs…\" to add one."
        )
        self.usb_check_btn.setEnabled(not running and bool(names))
        self.usb_pairs_btn.setEnabled(not running)
        deleted = usb_sync.usb_deleted_dir(drive)
        has_deleted = deleted.is_dir() and any(deleted.iterdir())
        self.usb_empty_btn.setEnabled(not running and has_deleted)
        self.usb_empty_btn.setVisible(has_deleted)

    def check_usb(self, auto: bool = False) -> None:
        """"Check USB now" (auto=False, opens the review straight away) or
        the plug-in check (auto=True, leaves a "Review…" button)."""
        if self._usb_running():
            if not auto:
                self.ctx.notify("The USB is already being checked")
            return
        busy = self._busy_with()
        if busy:
            if not auto:
                self.ctx.notify(busy)
            return
        if self._usb_drive is None:
            self.poll_usb()
        drive = self._usb_drive
        if drive is None:
            if not auto:
                self.ctx.notify("No MusicMgr USB drive found — plug it in, or set it up first")
            return
        self._usb_pending_plan = None
        thread = UsbCompareThread(drive, auto=auto, parent=self)
        thread.progress.connect(self.usb_status.setText)
        thread.finished_with.connect(lambda plan, error: self._on_usb_compare_done(plan, error, auto))
        self._usb_compare_thread = thread
        self.usb_status.setText("Checking the USB…")
        self.usb_progress.setRange(0, 0)
        self.usb_progress.setVisible(True)
        self._show_cancel()
        thread.start()
        self._refresh_usb_controls()

    def _on_usb_compare_done(self, plan, error, auto: bool) -> None:
        thread = self._usb_compare_thread
        self.usb_progress.setVisible(False)
        self.cancel_btn.setVisible(False)
        if thread is not None:
            thread.wait()
        self._usb_compare_thread = None
        if error:
            self.usb_status.setText(f"USB check failed: {error}")
            if not auto:
                self.ctx.notify("USB check failed")
            self._refresh_usb_controls()
            return
        if plan.cancelled:
            self.usb_status.setText("USB check cancelled")
            self._refresh_usb_controls()
            return
        problems = [e for pp in plan.pairs for e in pp.errors]
        if not plan.all_items():
            self.usb_status.setText(
                "Everything is in sync" + (f" · {'; '.join(problems)}" if problems else "")
            )
            if not auto:
                self.ctx.notify("USB: everything is in sync")
            # Still run a quiet sync: a first sync of identical folders has a
            # file list to record, and plays/ratings/playlists/the Jukebox
            # (services/library_state.py) are exchanged on every sync.
            self._start_usb_sync(plan, quiet=True)
            return
        self._usb_pending_plan = plan
        self.usb_status.setText(f"USB: {plan.summary()}")
        self._refresh_usb_controls()
        if auto:
            self.ctx.notify(f"USB: {plan.summary()} — review it in Settings → USB sync")
        else:
            self.review_usb_plan()

    def _make_usb_dialog(self, plan) -> UsbSyncDialog:
        """Overridable seam for tests."""
        return UsbSyncDialog(plan, parent=self)

    def review_usb_plan(self) -> None:
        plan = self._usb_pending_plan
        if plan is None:
            return
        if self._busy_with():
            self.ctx.notify(self._busy_with())
            return
        dialog = self._make_usb_dialog(plan)
        if dialog.exec() == QDialog.Accepted:
            self._start_usb_sync(plan)

    def _start_usb_sync(self, plan, quiet: bool = False) -> None:
        thread = UsbSyncThread(plan, parent=self)
        thread.progress.connect(self._on_usb_progress)
        thread.finished_with.connect(
            lambda result, message, error: self._on_usb_sync_done(result, message, error, quiet)
        )
        self._usb_sync_thread = thread
        self._usb_pending_plan = None
        if not quiet:
            self.usb_status.setText("Syncing…")
            self.usb_progress.setRange(0, 1000)
            self.usb_progress.setValue(0)
            self.usb_progress.setVisible(True)
            self._show_cancel()
        thread.start()
        self._refresh_usb_controls()

    def _on_usb_progress(self, done: int, total: int, name: str) -> None:
        if total > 0:
            self.usb_progress.setValue(int(1000 * min(done, total) / total))
            self.usb_status.setText(f"Copying {name} — {human_size(done)} of {human_size(total)}")

    def _on_usb_sync_done(self, result, message: str, error, quiet: bool = False) -> None:
        thread = self._usb_sync_thread
        self.usb_progress.setVisible(False)
        self.cancel_btn.setVisible(False)
        if thread is not None:
            thread.wait()
        self._usb_sync_thread = None
        if error:
            self.usb_status.setText(f"USB sync failed: {error}")
            self.ctx.notify("USB sync failed")
        elif not quiet:
            text = result.summary()
            if message:
                text += f" · {message}"
            if result.errors:
                text += "\n" + "\n".join(result.errors[:5])
            self.usb_status.setText(text)
            self.ctx.notify(f"USB sync: {result.summary()}")
        notes = getattr(result, "library_notes", None) if result is not None else None
        data_changed = notes is not None and notes.summary() != "Library data already in sync"
        if quiet and data_changed:
            self.usb_status.setText(f"Everything is in sync · {notes.summary()}")
            self.ctx.notify(f"USB: {notes.summary()}")
        if data_changed:
            self.ctx.libraryChanged.emit()
            self.ctx.playlistsChanged.emit()
        if result is not None and getattr(result, "artwork_changed", False):
            clear_art_cache()
        if result is not None and (result.new_local_paths or result.removed_local_paths):
            self.ctx.libraryChanged.emit()
            self.ctx.videosChanged.emit()
        self._refresh_usb_controls()

    def _cancel_usb(self) -> None:
        for thread in (self._usb_compare_thread, self._usb_sync_thread):
            if thread is not None and thread.isRunning():
                thread.requestInterruption()
                self.usb_cancel_btn.setEnabled(False)
                self.ctx.notify("Cancelling — finishing the current file…")

    def _pick_usb_root(self) -> str:  # pragma: no cover - file dialog
        return QFileDialog.getExistingDirectory(self, "Choose the USB drive (its top folder)")

    def setup_usb_drive(self) -> None:
        """Write the MusicMgr marker to a drive James picks, so it's found
        whatever letter it gets. Existing folders are left exactly as they
        are; watched folders get paired with same-named folders on it."""
        path = self._pick_usb_root()
        if not path:
            return
        root = Path(path)
        if root.anchor and Path(root.anchor) != root:
            confirm = QMessageBox.question(
                self,
                "Set up USB drive",
                f"{root} isn't the top folder of a drive. Use it as the sync root anyway? "
                "Folder pairs are matched by name inside it.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        try:
            drive = usb_sync.write_marker(root, "MusicMgr USB")
        except OSError as exc:
            QMessageBox.warning(self, "Set up USB drive", f"Couldn't write to {root}: {exc}")
            return
        self._usb_drive = drive
        self._refresh_usb_controls()
        self.ctx.notify(f"{drive.describe()} is set up for USB sync")

    def edit_usb_pairs(self) -> None:
        if self._usb_drive is None:
            return
        UsbPairsDialog(self._usb_drive, parent=self).exec()
        self._usb_pending_plan = None
        self._refresh_usb_controls()

    def empty_usb_deleted(self) -> None:
        if self._usb_drive is None:
            return
        folder = usb_sync.usb_deleted_dir(self._usb_drive)
        if not folder.is_dir():
            return
        size = usb_sync.folder_size(folder)
        confirm = QMessageBox.question(
            self,
            "Empty _deleted",
            f"Permanently delete the {human_size(size)} of files the USB sync set aside in "
            f"{folder}? These are files deleted from the USB (or replaced in a conflict) "
            "during earlier syncs.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        import shutil

        shutil.rmtree(folder, ignore_errors=True)
        self.ctx.notify("USB _deleted folder emptied")
        self._refresh_usb_controls()

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

    # -- loading -------------------------------------------------------------

    def refresh(self) -> None:
        rows = []
        with self.ctx.session() as session:
            stats = lib.library_stats(session)
            vstats = vid_svc.video_counts(session)
            for folder in session.scalars(select(WatchedFolder).order_by(WatchedFolder.path)):
                last = (
                    folder.last_scan_at.strftime("%d %b %Y %H:%M")
                    if folder.last_scan_at
                    else "never scanned"
                )
                rows.append({
                    "primary": folder.path,
                    "secondary": f"last scan: {last}",
                    "key": folder.id,
                    "path": folder.path,
                })
        mapping = {
            "Artists": stats["artists"], "Releases": stats["releases"],
            "Tracks": stats["tracks"], "Files": stats["files"],
            "Videos": vstats["videos"],
            "Playlists": stats["playlists"], "Plays": stats["plays"],
        }
        for name, value in mapping.items():
            self._stat_labels[name].setText(f"{value:,}")
        missing_bits = []
        if stats["missing"]:
            missing_bits.append(f"{stats['missing']} missing audio file(s)")
        if vstats["video_missing"]:
            missing_bits.append(f"{vstats['video_missing']} missing video file(s)")
        missing = f" · {' · '.join(missing_bits)}" if missing_bits else ""
        self.total_time.setText(
            f"Total run time {format_duration(stats['total_ms'])}{missing}"
        )
        self.folder_list.set_rows(rows)

    # -- actions -------------------------------------------------------------

    def add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose a folder to watch", str(Path.home())
        )
        if not path:
            return
        # normalize before it ever touches the database - see the module
        # docstring's note on the duplicate-folder bug this fixes. The
        # scanners below normalize the exact same way, so both sides
        # always agree on one canonical string for the same folder.
        path = str(Path(path).expanduser())
        with self.ctx.session() as session:
            existing = session.scalar(
                select(WatchedFolder).where(WatchedFolder.path == path)
            )
            if existing is None:
                session.add(WatchedFolder(path=path))
        self.refresh()
        self.scan_paths([path])

    def remove_folder(self) -> None:
        payload = self.folder_list.current_payload()
        if not payload:
            return
        with self.ctx.session() as session:
            folder = session.get(WatchedFolder, payload["key"])
            if folder is not None:
                session.delete(folder)
        self.ctx.notify("Folder removed. Existing tracks and videos were kept.")
        self.refresh()

    def purge_missing_files(self) -> None:
        """Permanently delete both missing tracks and missing videos in one
        action (2026-09-15 follow-up - James: audio-only video purging
        wasn't enough, this should cover "any file both audio and video").

        The two sides keep their own, already-established safety rules
        rather than being forced to match each other: `purge_orphaned_tracks`
        still skips a missing track that's on a playlist, a chart, or a
        jukebox slot, or has play history (see its own docstring) - only
        `purge_missing_videos` is unconditional, since nothing references a
        `Video` row the way those tables reference a `Track`. So "N missing"
        and "N removed" can legitimately differ for audio; the confirmation
        text below says so rather than that reading as a bug.

        Re-checks `is_missing` against disk right before purging (via
        mark_missing_files/mark_missing_videos) rather than trusting
        whatever it was left at by the last scan - this button is meant to
        give a correct answer right now, not only right after a fresh scan.
        A library carried over to a new machine (different drive letter, a
        USB stick mounted somewhere else) can show everything as "missing"
        purely because the old paths don't resolve there; the confirmation
        text says so explicitly rather than letting that read as data loss,
        and points at rescanning as the fix for a merely-moved drive rather
        than purging.
        """
        with self.ctx.session() as session:
            # discard the return values here - each only counts files newly
            # marked missing *this call*, not the total currently missing
            # (a file already flagged missing by an earlier scan wouldn't
            # count again); library_stats()/video_counts() below read the
            # real total off the now-refreshed is_missing flags instead.
            scanner.mark_missing_files(session)
            video_scanner.mark_missing_videos(session)
            missing_tracks = lib.library_stats(session)["missing"]
            missing_videos = vid_svc.video_counts(session)["video_missing"]
        if not missing_tracks and not missing_videos:
            self.ctx.notify("No missing files to remove")
            return
        confirm = QMessageBox.question(
            self,
            "Purge missing files",
            f"Permanently remove MusicMgr's record of {missing_tracks} track"
            f"{'s' if missing_tracks != 1 else ''} and {missing_videos} video"
            f"{'s' if missing_videos != 1 else ''} whose file can't currently "
            "be found?\n\n"
            "This never touches anything on disk - only MusicMgr's own "
            "records. A track on a playlist, a chart, or a jukebox slot, or "
            "with play history, is kept even if missing, so the count "
            "actually removed can be lower than the count shown here; a "
            "missing video has no such protection and is always removed.\n\n"
            "If files are only missing because a drive isn't connected "
            "right now (or a watched folder points at an old location, "
            "like a path from a different machine), reconnect it or fix "
            "the folder and scan again instead - anything purged here has "
            "to be rescanned from scratch to come back.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        with self.ctx.session() as session:
            tracks_removed = scanner.purge_orphaned_tracks(session)
            videos_removed = video_scanner.purge_missing_videos(session)
        self.ctx.notify(
            f"Removed {tracks_removed} missing track{'s' if tracks_removed != 1 else ''} "
            f"and {videos_removed} missing video{'s' if videos_removed != 1 else ''}"
        )
        self.refresh()

    def nuke_library(self) -> None:
        """Permanently erase the entire library database and start over -
        see NukeConfirmDialog's own docstring for why this needs more
        than the usual single Yes/No tap, and db.session.reset_database's
        for why no backup is kept.

        Guards against every other long-running job the same way
        download_artist_profiles/update_track_popularity/
        search_missing_artwork above do, but for the opposite reason:
        this action itself is fast enough to need no progress bar of its
        own - what matters is making sure nothing else is mid-write to
        the database it's about to erase out from under it.
        """
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is running — wait for it to finish before nuking the library")
            return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish before nuking the library")
            return
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify(
                "Artist profiles are downloading — wait for it to finish before nuking the library"
            )
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify(
                "Track popularity is updating — wait for it to finish before nuking the library"
            )
            return
        if self._artwork_thread is not None and self._artwork_thread.isRunning():
            self.ctx.notify(
                "Album artwork is downloading — wait for it to finish before nuking the library"
            )
            return

        with self.ctx.session() as session:
            stats = lib.library_stats(session)

        dialog = NukeConfirmDialog(stats, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        # a stale queue/now-playing pointed at rows that are about to stop
        # existing is worse than just stopping playback outright
        self.ctx.player.stop()
        self.ctx.player.clear_queue()

        reset_database()

        self.refresh()
        self.ctx.libraryChanged.emit()
        self.ctx.videosChanged.emit()
        QMessageBox.information(
            self,
            "Nuke library",
            "Your library database has been permanently erased.\n\n"
            "Add a watched folder and scan to rebuild it from your files.",
        )

    def scan_all(self) -> None:
        with self.ctx.session() as session:
            paths = [
                f.path
                for f in session.scalars(
                    select(WatchedFolder).where(WatchedFolder.enabled.is_(True))
                )
            ]
        if not paths:
            QMessageBox.information(
                self, "No folders", "Add a folder first, then scan."
            )
            return
        self.scan_paths(paths, force=self.force_rescan_cb.isChecked())

    def scan_selected(self) -> None:
        payload = self.folder_list.current_payload()
        if not payload:
            self.ctx.notify("Select a folder in the list first, then Scan selected")
            return
        self.scan_paths([payload["path"]], force=self.force_rescan_cb.isChecked())

    def scan_paths(self, paths: list[str], force: bool = False) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is already running")
            return
        for usb_thread in (self._usb_compare_thread, self._usb_sync_thread):
            if usb_thread is not None and usb_thread.isRunning():
                self.ctx.notify("The USB sync is running — wait for it to finish before scanning")
                return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish before scanning")
            return
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify(
                "Artist profiles are downloading — wait for it to finish before scanning"
            )
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify(
                "Track popularity is updating — wait for it to finish before scanning"
            )
            return
        if self._artwork_thread is not None and self._artwork_thread.isRunning():
            self.ctx.notify(
                "Album artwork is downloading — wait for it to finish before scanning"
            )
            return
        if self._metadata_scan_thread is not None and self._metadata_scan_thread.isRunning():
            self.ctx.notify(
                "Missing metadata is scanning — wait for it to finish before scanning"
            )
            return
        if self._artist_images_thread is not None and self._artist_images_thread.isRunning():
            self.ctx.notify(
                "Artist images are importing — wait for it to finish before scanning"
            )
            return
        if self._verify_thread is not None and self._verify_thread.isRunning():
            self.ctx.notify(
                "Files are being verified — wait for it to finish before scanning"
            )
            return
        if self._rematch_thread is not None and self._rematch_thread.isRunning():
            self.ctx.notify(
                "Charts are re-matching — wait for it to finish before scanning"
            )
            return
        # 2026-09-08 follow-up - James: "have the progress bar appear on
        # the line with the folder." Give every folder about to be scanned
        # an immediate row-level state rather than leaving its stale "last
        # scan: …" text sitting there until the first progress tick - see
        # _on_progress for the live updates once scanning actually starts.
        for path in paths:
            self.folder_list.update_row(
                path, "path", secondary="Waiting to scan…", progress=0.0
            )
        self._thread = ScanThread(paths, force=force, parent=self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_with.connect(self._on_scan_done)
        self._thread.start()
        self._show_cancel()

    def _on_progress(self, done: int, total: int, folder: str, name: str) -> None:
        fraction = done / total if total else 0.0
        self.folder_list.update_row(
            folder, "path", secondary=f"{done}/{total} — {name}", progress=fraction
        )

    def _on_scan_done(self, summary: str) -> None:
        # refresh() (triggered by the two emits below, both connected in
        # __init__) rebuilds every row fresh from the database, which is
        # what clears the in-progress look above back to an ordinary "last
        # scan: …" row - nothing else to clean up here.
        self.cancel_btn.setVisible(False)
        self.ctx.notify(summary)
        self.ctx.libraryChanged.emit()
        self.ctx.videosChanged.emit()

    def import_artist_images(self) -> None:
        """2026-09-22 - James: "does the progress bar also work on the
        import of artist images" (it didn't - this used to run entirely
        synchronously in this very method, after the folder picker, with
        no feedback for however long a big folder took). Threaded the same
        way as every other bulk action on this page now - see
        ArtistImagesImportThread (ui/widgets/common.py)."""
        folder = QFileDialog.getExistingDirectory(
            self, "Choose your folder of artist images", str(Path.home())
        )
        if not folder:
            return
        # 2026-09-24 (USB sync step 2) - importing MusicBee's
        # "Artist Pictures\Thumb" folder should fill gaps, not replace
        # photos James already chose, unless he says so.
        replace = QMessageBox.question(
            self,
            "Artist images",
            "Replace photos your artists already have?\n\n"
            "No: only artists without a photo get one (recommended for a "
            "MusicBee Artist Pictures\\Thumb folder).",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes
        if self._artist_images_thread is not None and self._artist_images_thread.isRunning():
            self.ctx.notify("Already importing artist images")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Starting…")
        self._artist_images_thread = ArtistImagesImportThread(folder, overwrite=replace, parent=self)
        self._artist_images_thread.progress.connect(self._on_artist_images_progress)
        self._artist_images_thread.finished_with.connect(self._on_artist_images_done)
        self._artist_images_thread.start()
        self._show_cancel()

    def _on_artist_images_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_artist_images_done(self, result: art_svc.ArtistImageResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._artist_images_thread)
        with self.ctx.session() as session:
            missing = art_svc.artists_without_images(session, limit=12)

        lines = [result.summary()]
        if result.fuzzy:
            lines.append("")
            lines.append("Matched by similar name (check these):")
            lines += [
                f"  {f} → {name} ({score:.0%})" for f, name, score in result.fuzzy[:8]
            ]
        if result.unmatched:
            lines.append("")
            lines.append("No artist in your library matched:")
            lines += [f"  {n}" for n in result.unmatched[:8]]
            if len(result.unmatched) > 8:
                lines.append(f"  …and {len(result.unmatched) - 8} more")
        if missing:
            lines.append("")
            lines.append("Still without a picture:")
            lines += [f"  {n}" for n in missing[:8]]
        for err in result.errors[:5]:
            lines.append(f"  ! {err}")

        QMessageBox.information(self, "Artist images", "\n".join(lines))
        self.ctx.libraryChanged.emit()

    def download_artist_profiles(self) -> None:
        """Bulk "like lyrics" biography fetch (see module docstring) -
        every artist with no `Artist.profile` yet, one Wikipedia lookup
        each, same background-thread-plus-page-wide-progress-bar shape as
        "Move data location…" (MigrationThread) below, since like that
        job this isn't scoped to any one row in self.folder_list."""
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify("Already downloading artist profiles")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        artist_ids = bio_dl.artists_missing_bio()
        if not artist_ids:
            QMessageBox.information(
                self, "Artist profiles", "Every artist already has a saved biography."
            )
            return

        confirm = QMessageBox.question(
            self,
            "Download artist profiles",
            f"Look up a short biography on Wikipedia for {len(artist_ids)} artist"
            f"{'s' if len(artist_ids) != 1 else ''} with none saved yet?\n\n"
            "This needs an internet connection and, being rate-limited to be "
            "polite to a free public API, can take a while for a large library. "
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, len(artist_ids))
        self.progress_label.setText("Starting…")
        self._bio_thread = BioDownloadThread(artist_ids, parent=self)
        self._bio_thread.progress.connect(self._on_bio_progress)
        self._bio_thread.finished_with.connect(self._on_bio_done)
        self._bio_thread.start()
        self._show_cancel()

    def _on_bio_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_bio_done(self, result: bio_dl.BioDownloadResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._bio_thread)
        lines = [result.summary()]
        if result.errors:
            lines.append("")
            lines += [f"  ! {err}" for err in result.errors[:8]]
        QMessageBox.information(self, "Artist profiles", "\n".join(lines))
        # this just filled in what the "Missing metadata" dashboard's
        # "Artist profiles" tab was reporting - a cached scan (see
        # view_missing_metadata) would still show artists just fixed here.
        self._last_metadata_scan = None

    def open_lastfm_credentials(self) -> None:
        """Opens `LastfmCredentialsDialog` pre-filled with whatever's
        already saved (empty the first time) and persists whatever comes
        back on Ok - see that dialog's own docstring and
        `services.lastfm_popularity.get_api_key`/`set_api_key` for why this
        lives in the `Setting` table rather than a config file (same
        precedent `services/jukebox.py` already set)."""
        with self.ctx.session() as session:
            api_key = popularity_dl.get_api_key(session)
        dialog = LastfmCredentialsDialog(api_key or "", parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        new_key = dialog.api_key()
        with self.ctx.session() as session:
            popularity_dl.set_api_key(session, new_key)
        self.ctx.notify("Last.fm API key saved")

    def update_track_popularity(self) -> None:
        """Bulk Last.fm popularity refresh (see module docstring) -
        deliberately every artist with a release, not just ones missing
        something, since a Last.fm playcount drifts over time and is meant
        to be periodically refreshed rather than fetched once and left
        alone - unlike download_artist_profiles above, which only ever
        fills in a *missing* biography. Same background-thread-plus-
        page-wide-progress-bar shape as that method and "Move data
        location…" (MigrationThread), since like those jobs this isn't
        scoped to any one row in self.folder_list."""
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify("Already updating track popularity")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        if not popularity_dl.has_api_key():
            confirm = QMessageBox.question(
                self,
                "Last.fm API key needed",
                "Track popularity needs a free Last.fm API key, which isn't "
                "saved yet.\n\n"
                "Open \"Last.fm API key…\" now?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm == QMessageBox.Yes:
                self.open_lastfm_credentials()
            return

        artist_ids = popularity_dl.all_artist_ids_with_releases()
        if not artist_ids:
            QMessageBox.information(
                self, "Track popularity", "No artists with any releases yet."
            )
            return

        confirm = QMessageBox.question(
            self,
            "Update track popularity",
            f"Look up Last.fm's playcount for {len(artist_ids)} artist"
            f"{'s' if len(artist_ids) != 1 else ''} and use it to rank each "
            "artist page's Top Tracks list?\n\n"
            "This needs an internet connection and, being rate-limited to be "
            "polite to Last.fm's API, can take a while for a large library. "
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, len(artist_ids))
        self.progress_label.setText("Starting…")
        self._popularity_thread = PopularityDownloadThread(artist_ids, parent=self)
        self._popularity_thread.progress.connect(self._on_popularity_progress)
        self._popularity_thread.finished_with.connect(self._on_popularity_done)
        self._popularity_thread.start()
        self._show_cancel()

    def _on_popularity_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_popularity_done(self, result: popularity_dl.PopularityResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._popularity_thread)
        lines = [result.summary()]
        if result.errors:
            lines.append("")
            lines += [f"  ! {err}" for err in result.errors[:8]]
        QMessageBox.information(self, "Track popularity", "\n".join(lines))
        self.ctx.libraryChanged.emit()
        # same reason _on_bio_done clears this - a cached "Missing
        # metadata" scan would still show artists this bulk run just fixed.
        self._last_metadata_scan = None

    def open_discogs_credentials(self) -> None:
        """Opens `DiscogsCredentialsDialog` pre-filled with whatever's
        already saved (empty the first time) and persists whatever comes
        back on Ok - see that dialog's own docstring and
        `services.artwork_downloader.get_api_token`/`set_api_token` for why
        this lives in the `Setting` table, the same precedent
        `open_lastfm_credentials` above already set for its own key."""
        with self.ctx.session() as session:
            token = artwork_dl.get_api_token(session)
        dialog = DiscogsCredentialsDialog(token or "", parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        new_token = dialog.token()
        with self.ctx.session() as session:
            artwork_dl.set_api_token(session, new_token)
        self.ctx.notify("Discogs API token saved")

    def search_missing_artwork(self) -> None:
        """Bulk "like lyrics/profiles" album artwork search (see
        services/artwork_downloader.py's module docstring) - every release
        with no cover yet, one Discogs lookup each, auto-applying the top
        match rather than reviewing each one (unlike the release page's own
        "Search artwork" button, which always shows a picker). Same
        background-thread-plus-page-wide-progress-bar shape as
        download_artist_profiles/update_track_popularity above, since like
        those jobs this isn't scoped to any one row in self.folder_list."""
        if self._artwork_thread is not None and self._artwork_thread.isRunning():
            self.ctx.notify("Already searching for album artwork")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        if not artwork_dl.has_api_token():
            confirm = QMessageBox.question(
                self,
                "Discogs API token needed",
                "Album artwork search needs a free Discogs API token, which "
                "isn't saved yet.\n\n"
                "Open \"Discogs API token…\" now?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm == QMessageBox.Yes:
                self.open_discogs_credentials()
            return

        release_ids = artwork_dl.releases_missing_cover()
        if not release_ids:
            QMessageBox.information(
                self, "Album artwork", "Every release already has a cover."
            )
            return

        confirm = QMessageBox.question(
            self,
            "Search for missing album artwork",
            f"Look up a cover on Discogs for {len(release_ids)} release"
            f"{'s' if len(release_ids) != 1 else ''} with none saved yet, "
            "applying the best match automatically?\n\n"
            "This needs an internet connection and, being rate-limited to be "
            "polite to Discogs' API, can take a while for a large library. "
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, len(release_ids))
        self.progress_label.setText("Starting…")
        self._artwork_thread = ArtworkBulkThread(release_ids, parent=self)
        self._artwork_thread.progress.connect(self._on_artwork_progress)
        self._artwork_thread.finished_with.connect(self._on_artwork_done)
        self._artwork_thread.start()
        self._show_cancel()

    def _on_artwork_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_artwork_done(self, result: artwork_dl.ArtworkSearchResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._artwork_thread)
        lines = [result.summary()]
        if result.errors:
            lines.append("")
            lines += [f"  ! {err}" for err in result.errors[:8]]
        QMessageBox.information(self, "Album artwork", "\n".join(lines))
        self.ctx.libraryChanged.emit()
        # same reason _on_bio_done clears this - a cached "Missing
        # metadata" scan would still show releases this bulk run just fixed.
        self._last_metadata_scan = None

    def view_missing_metadata(self) -> None:
        """James, 2026-09-22: "one screen listing releases missing cover
        art, bio, lyrics, or popularity data, instead of only surfacing
        that per-item" - see services/metadata_health.py's module docstring
        for the full story.

        2026-09-22 same-day follow-up (James, on the first version of this:
        "I click the option and it looks like nothing is happening") - all
        four categories, not just the slow lyrics one, now run inside
        MetadataScanThread. The original version ran the three fast ones
        right here, synchronously, before ever making the progress bar
        visible - fast enough in a unit test's empty database, but a real
        library's worth of indexed-but-not-free queries added up to a
        genuinely silent multi-second freeze. `self.progress` is now made
        visible (in indeterminate/"busy" mode - `setRange(0, 0)`, no
        fraction to show yet) immediately, before the thread is even
        started, so there's no gap at all between tapping "View…" and
        seeing *something* move.

        Browse-and-jump only (James, same conversation, asked for and chose
        this scope explicitly over the dashboard also getting its own
        "fix all" buttons) - picking a row just hands off to the same
        ctx.navigateRequested/openArtistRequested/openReleaseRequested trip
        Now Playing's own tappable artist/album already makes
        (ui/views/nowplaying.py), landing on the artist/release page where
        the actual fix button (Fetch bio, Search artwork, Fetch popularity,
        Download lyrics) already lives.

        2026-09-22 same-day follow-up (James: "is there any way ... I can
        go back and forth to fix them. Right now it's a one time click and
        then back to running the missing metadata query again") - a scan
        already sitting in `self._last_metadata_scan` is reused instead of
        rerun, so tapping "Missing metadata" again after fixing one item
        (from _open_missing_metadata_dialog below) reopens instantly rather
        than repeating the whole scan - the lyrics half in particular isn't
        cheap on a real library. `self._metadata_visited` is never cleared
        here, only added to (same method) and read by
        MissingMetadataDialog to mark rows already picked, so working
        through a long list keeps track of where you left off across
        however many reopens that takes."""
        if self._last_metadata_scan is not None:
            self._open_missing_metadata_dialog(self._last_metadata_scan)
            return
        if self._metadata_scan_thread is not None and self._metadata_scan_thread.isRunning():
            self.ctx.notify("Already scanning for missing metadata")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Starting…")
        self._metadata_scan_thread = MetadataScanThread(parent=self)
        self._metadata_scan_thread.progress.connect(self._on_metadata_scan_progress)
        self._metadata_scan_thread.finished_with.connect(self._on_metadata_scan_done)
        self._metadata_scan_thread.start()
        self._show_cancel()

    def _on_metadata_scan_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            # one of the three fast phases (album artwork/profiles/
            # popularity) - nothing to show a fraction of, `name` carries
            # the phase itself so the bar still visibly changes.
            self.progress.setRange(0, 0)
            self.progress_label.setText(name or "Scanning…")
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress_label.setText(f"Checking lyrics… {done}/{total}")

    def _on_metadata_scan_done(self, result: metadata_health.ScanResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        cancelled = self._metadata_scan_thread is not None and (
            self._metadata_scan_thread.isInterruptionRequested()
        )
        self._hide_cancel(self._metadata_scan_thread)
        # 2026-09-22 - James: "add a real cancel button..." - a cancelled
        # scan's `result` is only whatever categories were reached before
        # the stop (see scan_missing_metadata's own should_stop docstring),
        # so it's shown once but deliberately NOT cached into
        # `_last_metadata_scan` the way a completed scan is - reopening
        # "Missing metadata" after a cancelled run re-scans from scratch
        # instead of silently treating a partial result as the real one.
        if not cancelled:
            self._last_metadata_scan = result
        self._open_missing_metadata_dialog(result)

    def _open_missing_metadata_dialog(self, result: metadata_health.ScanResult) -> None:
        dialog = MissingMetadataDialog(
            result.missing_covers, result.missing_bios,
            result.missing_popularity, result.missing_lyrics,
            visited=self._metadata_visited, parent=self,
        )
        code = dialog.exec()
        if code == MissingMetadataDialog.REFRESH:
            self._last_metadata_scan = None
            self.view_missing_metadata()
            return
        picked = dialog.picked()
        if not picked:
            return
        kind, item_id = picked
        self._metadata_visited.add((kind, item_id))
        self.ctx.navigateRequested.emit("library")
        # the "FromMissingMetadata" variants (not the plain
        # openArtistRequested/openReleaseRequested Now Playing's own
        # tappable artist/album use) so the artist/release page's
        # breadcrumb leads back here instead of to the Artists/Albums grid
        # - James, 2026-09-22: "is there any way we can have a back button
        # when you go from missing metadata to the album and then back".
        if kind == "artist":
            self.ctx.openArtistFromMissingMetadataRequested.emit(item_id)
        else:
            self.ctx.openReleaseFromMissingMetadataRequested.emit(item_id)

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
        self.ctx.notify(f"{missing} file(s) newly marked missing")
        self.refresh()

    def rematch_all(self) -> None:
        """2026-09-22 - James: "does the progress bar also work on ...
        charts" (it didn't - this used to be a plain loop over every
        external chart, entirely synchronous, right here). Threaded the
        same way as every other bulk action on this page now - see
        RematchChartsThread (ui/widgets/common.py) and
        services.charts.rematch_all_charts, which now owns the loop this
        method used to."""
        if self._rematch_thread is not None and self._rematch_thread.isRunning():
            self.ctx.notify("Already re-matching charts")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Starting…")
        self._rematch_thread = RematchChartsThread(parent=self)
        self._rematch_thread.progress.connect(self._on_rematch_progress)
        self._rematch_thread.finished_with.connect(self._on_rematch_done)
        self._rematch_thread.start()
        self._show_cancel()

    def _on_rematch_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            self.progress_label.setText(name or "Re-matching…")
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress_label.setText(f"{done}/{total}")

    def _on_rematch_done(self, total: int) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._rematch_thread)
        self.ctx.notify(f"{total} chart entries now point at tracks you own")
        self.ctx.libraryChanged.emit()

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
