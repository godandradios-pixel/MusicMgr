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
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
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
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select

from ... import config
from ...version import APP_VERSION
from ...db.models import WatchedFolder
from ...db.session import session_scope
from ...services import artist_bio_downloader as bio_dl
from ...services import data_migration
from ...services import library as lib
from ...services import scanner
from ...services import lastfm_popularity as popularity_dl
from ...services import video_scanner
from ...services import videos as vid_svc
from ...services.library import format_duration
from ..context import AppContext
from ..widgets.common import (
    BioDownloadThread,
    PopularityDownloadThread,
    SearchBar,
    TouchButton,
    TouchList,
    dim_label,
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
    about which row it belongs to."""

    progress = Signal(int, int, str, str)
    finished_with = Signal(str)

    def __init__(self, folders: list[str], parent=None) -> None:
        super().__init__(parent)
        self.folders = folders

    def run(self) -> None:  # pragma: no cover - exercised interactively
        audio_result = scanner.ScanResult()
        video_result = video_scanner.ScanResult()
        try:
            with session_scope() as session:
                for folder in self.folders:
                    scanner.scan_folder(
                        session,
                        folder,
                        progress=lambda done, total, name, folder=folder: self.progress.emit(
                            done, total, folder, f"Audio: {name}"
                        ),
                        result=audio_result,
                    )
                audio_result.missing = scanner.mark_missing_files(session)
                audio_result.removed = scanner.purge_orphaned_tracks(session)
            with session_scope() as session:
                for folder in self.folders:
                    video_scanner.scan_video_folder(
                        session,
                        folder,
                        progress=lambda done, total, name, folder=folder: self.progress.emit(
                            done, total, folder, f"Video: {name}"
                        ),
                        result=video_result,
                    )
                video_result.missing = video_scanner.mark_missing_videos(session)
            self.finished_with.emit(
                f"Audio: {audio_result.summary()} · Video: {video_result.summary()}"
            )
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
        self.progress_label = dim_label("")
        body.addWidget(self.progress_label)

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
        # 2026-09-16 same-day follow-up (James, on where "Top Tracks"
        # ranking comes from - ended up on Last.fm, see the module
        # docstring for why) - see update_track_popularity/
        # open_lastfm_credentials below.
        add_tool_row(
            "Last.fm API key",
            "Set the free API key track popularity needs to fetch Last.fm charts.",
            "Set key…",
            self.open_lastfm_credentials,
        )
        add_tool_row(
            "Track popularity",
            "Refresh Last.fm popularity for every artist with a release.",
            "Update…",
            self.update_track_popularity,
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
        _size_tool_row_buttons(tool_rows)

        body.addWidget(tools_card)

        # ---- about ----
        # James: "is there a way I can put the version of MusicMgr
        # somewhere in the App. Maybe on the settings screen somewhere" -
        # there's never been a version number anywhere in this app (no
        # tags, no version file - see version.py's own docstring for why
        # it's the commit hash/date instead), so a quiet line at the very
        # bottom of this page, past every actual setting, is the whole
        # feature: something to glance at or read off when comparing "is
        # this the build I just pushed" against another machine, not
        # something that needs its own Card/row treatment like the
        # Maintenance tools above.
        version_label = dim_label(f"{config.APP_NAME} {APP_VERSION}")
        version_label.setAlignment(Qt.AlignRight)
        body.addWidget(version_label)

        ctx.libraryChanged.connect(self.refresh)
        ctx.videosChanged.connect(self.refresh)

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
        self.scan_paths(paths)

    def scan_selected(self) -> None:
        payload = self.folder_list.current_payload()
        if not payload:
            self.ctx.notify("Select a folder in the list first, then Scan selected")
            return
        self.scan_paths([payload["path"]])

    def scan_paths(self, paths: list[str]) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is already running")
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
        # 2026-09-08 follow-up - James: "have the progress bar appear on
        # the line with the folder." Give every folder about to be scanned
        # an immediate row-level state rather than leaving its stale "last
        # scan: …" text sitting there until the first progress tick - see
        # _on_progress for the live updates once scanning actually starts.
        for path in paths:
            self.folder_list.update_row(
                path, "path", secondary="Waiting to scan…", progress=0.0
            )
        self._thread = ScanThread(paths, self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished_with.connect(self._on_scan_done)
        self._thread.start()

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
        self.ctx.notify(summary)
        self.ctx.libraryChanged.emit()
        self.ctx.videosChanged.emit()

    def import_artist_images(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose your folder of artist images", str(Path.home())
        )
        if not folder:
            return
        from ...services import artist_images as art_svc

        with self.ctx.session() as session:
            result = art_svc.import_artist_images(session, folder)
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
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is running — wait for it to finish first")
            return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish first")
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            self.ctx.notify("Track popularity is updating — wait for it to finish first")
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

    def _on_bio_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_bio_done(self, result: bio_dl.BioDownloadResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        lines = [result.summary()]
        if result.errors:
            lines.append("")
            lines += [f"  ! {err}" for err in result.errors[:8]]
        QMessageBox.information(self, "Artist profiles", "\n".join(lines))

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
        if self._thread is not None and self._thread.isRunning():
            self.ctx.notify("A scan is running — wait for it to finish first")
            return
        if self._migration_thread is not None and self._migration_thread.isRunning():
            self.ctx.notify("A data move is running — wait for it to finish first")
            return
        if self._bio_thread is not None and self._bio_thread.isRunning():
            self.ctx.notify("Artist profiles are downloading — wait for it to finish first")
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

    def _on_popularity_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_popularity_done(self, result: popularity_dl.PopularityResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        lines = [result.summary()]
        if result.errors:
            lines.append("")
            lines += [f"  ! {err}" for err in result.errors[:8]]
        QMessageBox.information(self, "Track popularity", "\n".join(lines))
        self.ctx.libraryChanged.emit()

    def verify_files(self) -> None:
        # 2026-09-07 follow-up: checks video files too now, not just audio -
        # the same "one unified thing" this whole view's folder merge is
        # about (see module docstring); there's no reason "Verify files"
        # would only mean half of what's watched.
        with self.ctx.session() as session:
            missing = scanner.mark_missing_files(session)
            missing += video_scanner.mark_missing_videos(session)
        self.ctx.notify(f"{missing} file(s) newly marked missing")
        self.refresh()

    def rematch_all(self) -> None:
        from ...db.models import Chart
        from ...services import charts as chart_svc

        total = 0
        with self.ctx.session() as session:
            for chart in session.scalars(
                select(Chart).where(Chart.kind == Chart.KIND_EXTERNAL)
            ):
                total += chart_svc.rematch_chart(session, chart.id)
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
            "MusicMgr until this finishes. Continue?",
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
