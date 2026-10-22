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

# The page was one 3,700-line module until 2026-10-22; it is now a package
# with one module per section (library.py, usb.py, ...). Everything other
# modules and tests imported from `musicmgr.ui.views.settings` is still here.

import logging
import sys
from pathlib import Path
from typing import Optional
from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select
from .... import config
from ....version import APP_VERSION, RELEASE_VERSION
from ....db.models import WatchedFolder
from ....db.session import reset_database, session_scope
from ....services import artist_bio_downloader as bio_dl
from ....services import artist_images as art_svc
from ....services import artwork_downloader as artwork_dl
from ....services import artwork_names
from ....services import data_migration
from ....services import library as lib
from ....services import scanner
from ....services import lastfm_popularity as popularity_dl
from ....services import metadata_health
from ....services import playback_prefs
from ....services import replaygain
from ....services import updater
from ....services import library_backup
from ....services import library_state
from ....services import usb_sync
from ....services import video_scanner
from ....services import voice_ask, web_remote
from ....services import videos as vid_svc
from ....services.library import format_duration
from ...context import AppContext
from ...theme import COLORS
from ... import skins
from ...widgets.common import (
    CollapsibleSection,
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
    guard_wheel,
)
from ...widgets.backups import RestoreBackupDialog
from ...widgets.usb_sync import (
    UsbCompareThread,
    UsbPairsDialog,
    UsbSyncDialog,
    UsbSyncThread,
    human_size,
)
from ..base import BaseView
from .parts import (
    CROSSFADE_CHOICES,
    DiscogsCredentialsDialog,
    LEVELLING_CHOICES,
    LastfmCredentialsDialog,
    LoudnessAnalyzeThread,
    MigrationThread,
    NukeConfirmDialog,
    PREAMP_CHOICES,
    SECTIONS_OPEN_DEFAULT,
    SECTIONS_OPEN_KEY,
    ScanThread,
    UpdateAvailableDialog,
    UpdateCheckThread,
    UpdateDownloadThread,
    _KEPT_REASONS,
    _ToolRow,
    _plural,
    _purge_breakdown,
    _size_tool_row_buttons,
)
from .view import SettingsView
