"""Watched folders, scanning, and housekeeping.

One watched-folder list (`db.models.WatchedFolder`) for audio and video:
`ScanThread` runs both `services.scanner.scan_folder` and
`services.video_scanner.scan_video_folder` over every folder, so a folder
never has to say which kind of media it holds. "Scan now" scans every
enabled folder; "Scan selected" scans only the row highlighted in
`folder_list`, through the same `scan_paths()`/`ScanThread` machinery.

`add_folder` normalizes the path through `Path(...)` before storing it -
the same normalization the scanners use - so Qt's forward-slash paths on
Windows never create a second row for the same folder (an older database's
duplicates are merged by `db.session._migrate_watched_folders`).

Every audio scan also calls `scanner.purge_orphaned_tracks` right after
`mark_missing_files`, with no confirmation step: it deletes tracks left
with no playable file and no play/playlist/chart history (e.g. the old
title after a file is renamed on disk) - see that function's docstring.

Maintenance actions are `_ToolRow` rows (button on the left, title and a
short description beside it) in a Card frame. Among them:

- "Download artist profiles…" fills `Artist.profile` for artists with no
  biography yet (`bio_dl.artists_missing_bio`), never overwriting one -
  see `services.artist_bio_downloader` and `BioDownloadThread`
  (ui/widgets/common.py).
- "Last.fm API key…" (`LastfmCredentialsDialog`) stores the free key
  `services.lastfm_popularity` needs, and "Update track popularity from
  Last.fm…" refreshes *every* artist with a release
  (`lastfm_popularity.all_artist_ids_with_releases`), since popularity is
  meant to be refreshed periodically. See `services.lastfm_popularity` for
  why Last.fm rather than Spotify.

`cancel_btn`, next to `progress_label`, stops whichever background thread
is running (`_active_cancellable_thread`/`_show_cancel`/`_hide_cancel`) via
`QThread.requestInterruption()`. Each service loop it drives takes a
`should_stop` callable, checked once per item with a clean `break`, so
work already committed is never rolled back - see each function's
docstring. `MigrationThread` ("Move data location…") is deliberately left
out of `_CANCELLABLE_THREAD_ATTRS`: `data_migration.migrate` copies in
steps with no per-step undo, so Cancel never appears while it runs.
"""

# The page is a package with one module per section (library.py, usb.py,
# ...); this module re-exports everything other modules and tests import
# from `musicmgr.ui.views.settings`.

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
