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
# ...). This module re-exports what the app and the tests import from
# `musicmgr.ui.views.settings`: the page itself, two helpers the tests
# use, and the Qt dialogs/config the tests patch through it (2026-11-08:
# trimmed from ~100 leftover imports from before the split).

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox, QTextBrowser

from .... import config
from ....version import RELEASE_VERSION
from . import files, library, maintenance
from .parts import UpdateAvailableDialog, _ToolRow
from .view import SettingsView

__all__ = [
    "SettingsView",
    "UpdateAvailableDialog",
    "_ToolRow",
    "QDesktopServices",
    "QFileDialog",
    "QMessageBox",
    "QTextBrowser",
    "config",
    "RELEASE_VERSION",
    "files",
    "library",
    "maintenance",
]
