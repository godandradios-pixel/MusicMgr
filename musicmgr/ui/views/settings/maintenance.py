"""Settings › Maintenance tools (artwork, artist info, metadata, duplicates, charts)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QLabel, QMessageBox, QVBoxLayout,
)
from ....services import artist_bio_downloader as bio_dl
from ....services import artist_images as art_svc
from ....services import artwork_downloader as artwork_dl
from ....services import artwork_names
from ....services import lastfm_popularity as popularity_dl
from ....services import metadata_health
from ...widgets.common import (
    ArtistImagesImportThread,
    ArtworkBulkThread,
    BioDownloadThread,
    MetadataScanThread,
    MissingMetadataDialog,
    PopularityDownloadThread,
    RematchChartsThread,
    TouchButton,
    clear_art_cache,
    dim_label,
)
from .parts import (
    DiscogsCredentialsDialog,
    LastfmCredentialsDialog,
    _ToolRow,
    _size_tool_row_buttons,
)


class MaintenanceSection:
    """Settings › Maintenance tools (artwork, artist info, metadata, duplicates, charts): a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_maintenance(self, section) -> None:
        """Build the Maintenance group."""
        # ---- maintenance ----
        # one row per tool (see _ToolRow), each with its own button and a
        # one-line description, inside Card frames with no dividers, in
        # three groups - artwork and track info, files and data, online
        # services.
        tool_rows: list[_ToolRow] = []
        tools: dict[str, QVBoxLayout] = {}

        def tools_group(title: str, key: str) -> None:
            tools_card = QFrame()
            tools_card.setObjectName("Card")
            tools_layout = QVBoxLayout(tools_card)
            tools_layout.setContentsMargins(20, 2, 20, 2)
            tools_layout.setSpacing(0)
            section(title, key).addWidget(tools_card)
            tools["layout"] = tools_layout

        def add_tool_row(title: str, description: str, button_text: str, handler) -> TouchButton:
            row = _ToolRow(title, description, button_text)
            row.button.clicked.connect(handler)
            tools["layout"].addWidget(row)
            tool_rows.append(row)
            return row.button

        tools_group("Artwork & track info", "info")
        # first in the list, nearest `self.progress`/`self.progress_label`,
        # which is what lights up while its scan runs (see
        # view_missing_metadata). 2026-10-31: the Artist images, Artist
        # profiles, Track popularity and Album artwork rows live inside its
        # window now (MissingMetadataDialog.ACTION), with "Fill in
        # everything" to run them all - see fill_in_everything.
        add_tool_row(
            "Missing info",
            "See what's missing cover art, a biography, popularity data or lyrics, and fill "
            "it in - one kind at a time, artist photos from a folder, or everything at once.",
            "Open…",
            self.view_missing_metadata,
        )
        # 2026-10-31: no Relink artwork row - it runs by itself after a USB
        # sync and at the end of Missing info › Fill in everything
        # (_finish_fill). Volume levels › Analyze… moved to Playback, beside
        # the Volume levelling setting it feeds (settings/playback.py).
        # 2026-10-31: one row - re-matching goes on to offer the chart
        # playlists update (the old "Chart playlists" row), see
        # _on_rematch_done and services/charts.py:refresh_chart_playlists
        add_tool_row(
            "Charts",
            "Re-match every chart entry against your current library, then update the "
            "playlists saved from charts to match.",
            "Re-match…",
            self.rematch_all,
        )

        tools_group("Files & data", "files")
        # one check-up of the library against the disk and itself, with a
        # fix beside each finding - see services/library_health.py.
        # 2026-10-31: the old Verify files row is its "Records whose file is
        # gone" card, whose Remove them runs purge_missing_files
        add_tool_row(
            "Library health",
            "Check for missing files (and remove them), files never imported, broken links, "
            "chart songs linked to the wrong song, out-of-date chart playlists and charts "
            "with gaps.",
            "Check…",
            self.check_library_health,
        )
        # identical-file duplicates, and moving playlist songs off chart-folder
        # copies onto album copies - see services/duplicates.py
        add_tool_row(
            "Duplicates",
            "Move playlist and chart songs off Billboard, Hot Country and Oldies copies onto "
            "album copies, and list files that hold the identical recording.",
            "Find…",
            self.find_duplicates,
        )
        # 2026-10-31: one row - the window it opens has the Move… button
        # (the old "Move data" row), see show_paths
        add_tool_row(
            "Data location",
            "See where your library database and media files live, or move them.",
            "Show…",
            self.show_paths,
        )
        # daily library backups, local only, keep 7 - see
        # services/library_backup.py. 2026-10-31: one row - Back up now and
        # Restore are both in the window it opens (widgets/backups.py)
        add_tool_row(
            "Backups",
            "MusicMgr saves a copy of your library the first time it closes each day, keeping "
            "the last 7. Back up now, or put your library back to a saved copy.",
            "Backups…",
            self.restore_library,
        )
        # wipes library.db and starts fresh. Deliberately last in this list -
        # the most destructive action here, see NukeConfirmDialog/nuke_library.
        add_tool_row(
            "Nuke library",
            "Permanently erase your entire library database and start completely fresh.",
            "Nuke…",
            self.nuke_library,
        )

        tools_group("Online services", "services")
        # every key MusicMgr uses, kept together here rather than beside the
        # tools that use them: Last.fm (track popularity), Discogs (album
        # artwork), and - 2026-10-31, moved from Phone remote - Google AI
        # Studio and Anthropic (the phone remote's Ask box)
        add_tool_row(
            "Last.fm API key",
            "Set the free API key track popularity needs to fetch Last.fm charts.",
            "Set key…",
            self.open_lastfm_credentials,
        )
        # see services/artwork_downloader.py's module docstring for why the
        # bulk search auto-applies the top match (the release page's own
        # "Search artwork" button always shows a picker first)
        add_tool_row(
            "Discogs API token",
            "Set the free personal access token album artwork search needs.",
            "Set token…",
            self.open_discogs_credentials,
        )
        # the Ask keys stay plain fields that save as you leave them, as they
        # did on Phone remote - built and loaded by _build_remote, which runs
        # first (view.py), placed here
        ask_keys = QGridLayout()
        ask_keys.setContentsMargins(0, 12, 0, 12)
        ask_keys.setHorizontalSpacing(18)
        ask_keys.setVerticalSpacing(10)
        ask_note = dim_label("For the phone remote's Ask box - the model is chosen under Phone remote.")
        ask_note.setWordWrap(True)
        ask_keys.addWidget(ask_note, 0, 0, 1, 2)
        ask_keys.addWidget(QLabel("Google AI Studio key"), 1, 0)
        ask_keys.addWidget(self.ask_gemini_key_edit, 1, 1)
        ask_keys.addWidget(QLabel("Anthropic API key"), 2, 0)
        ask_keys.addWidget(self.ask_key_edit, 2, 1)
        ask_keys.setColumnStretch(1, 1)
        tools["layout"].addLayout(ask_keys)

        _size_tool_row_buttons(tool_rows)

    def _relink_pictures(self) -> str:
        """Point every release/artist at its name-based picture
        (artwork\\<Artist> - <Title>.jpg, artists\\<Artist>.jpg) and
        return what changed, or "" if nothing did. The USB sync does this by
        itself after pictures arrive; for files dropped in by hand it runs
        at the end of Fill in everything (2026-10-31: replaced the Relink
        artwork row)."""
        with self.ctx.session() as db:
            result = artwork_names.relink(db)
        clear_art_cache()
        return result.summary() if (result.covers or result.artist_images) else ""

    def import_artist_images(self) -> None:
        """Import artist images from a chosen folder on a background thread
        with the page's progress bar - see ArtistImagesImportThread
        (ui/widgets/common.py)."""
        folder = QFileDialog.getExistingDirectory(
            self, "Choose your folder of artist images", str(Path.home())
        )
        if not folder:
            return
        # importing MusicBee's "Artist Pictures\Thumb" folder should fill
        # gaps, not replace photos already chosen, unless the user says so
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

        all_ids = bio_dl.artists_missing_bio()
        if not all_ids:
            QMessageBox.information(
                self, "Artist profiles", "Every artist already has a saved biography."
            )
            return
        # artists already looked up with no luck (or a non-music bio) are
        # remembered (Artist.profile_checked_at), so the run can be limited
        # to artists never looked up before
        new_ids = bio_dl.artists_missing_bio(new_only=True)
        choice = self._ask_profile_scope(len(new_ids), len(all_ids))
        if choice is None:
            return
        artist_ids = new_ids if choice == "new" else all_ids
        self._start_bio_thread(artist_ids)

    def _start_bio_thread(self, artist_ids: list) -> None:
        self.progress.setVisible(True)
        self.progress.setRange(0, len(artist_ids))
        self.progress_label.setText("Starting…")
        self._bio_thread = BioDownloadThread(artist_ids, parent=self)
        self._bio_thread.progress.connect(self._on_bio_progress)
        self._bio_thread.finished_with.connect(self._on_bio_done)
        self._bio_thread.start()
        self._show_cancel()

    def _ask_profile_scope(self, new_count: int, all_count: int) -> Optional[str]:
        """"new" (only artists never looked up), "all" (also retry every
        artist looked up before with no luck, plus non-music bios), or None
        to cancel. Kept separate so tests can answer it."""
        retry_count = all_count - new_count

        def artists(n: int) -> str:
            return f"{n:,} artist{'s' if n != 1 else ''}"

        box = QMessageBox(self)
        box.setWindowTitle("Download artist profiles")
        box.setIcon(QMessageBox.Question)
        lines = []
        if new_count:
            lines.append(f"{artists(new_count)} with no biography haven't been looked up yet.")
        if retry_count:
            lines.append(
                f"{artists(retry_count)} were looked up before and Wikipedia had "
                "nothing (or only a page that isn't about the music)."
            )
        lines.append("")
        lines.append("Needs an internet connection. Retrying everyone can take a "
                     "long time on a big library.")
        box.setText("\n".join(lines))
        new_btn = all_btn = None
        if new_count:
            new_btn = box.addButton(f"New artists only ({new_count:,})", QMessageBox.AcceptRole)
        if retry_count:
            label = (f"Retry all ({all_count:,})" if new_count
                     else f"Retry them ({all_count:,})")
            all_btn = box.addButton(label, QMessageBox.ActionRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(new_btn or cancel)
        box.exec()
        clicked = box.clickedButton()
        if new_btn is not None and clicked is new_btn:
            return "new"
        if all_btn is not None and clicked is all_btn:
            return "all"
        return None

    def _on_bio_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total}")

    def _on_bio_done(self, result: bio_dl.BioDownloadResult) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._bio_thread)
        self._last_metadata_scan = None
        if self._fill_step_done("Artist profiles", result, self._bio_thread):
            return
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
        self._start_popularity_thread(artist_ids)

    def _start_popularity_thread(self, artist_ids: list) -> None:
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
        self._last_metadata_scan = None
        if self._fill_step_done("Track popularity", result, self._popularity_thread):
            return
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
        self._start_artwork_thread(release_ids)

    def _start_artwork_thread(self, release_ids: list) -> None:
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
        self._last_metadata_scan = None
        if self._fill_step_done("Album artwork", result, self._artwork_thread):
            return
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
        """One screen listing everything missing cover art, a bio, lyrics,
        or popularity data - see services/metadata_health.py's module
        docstring.

        All four categories run inside MetadataScanThread (even the fast
        queries add up to a multi-second freeze on a real library), and
        `self.progress` is shown in busy mode (`setRange(0, 0)`) before the
        thread starts, so tapping "View…" gives immediate feedback.

        Browse-and-jump only: picking a row navigates to the artist/release
        page (as Now Playing's tappable artist/album does,
        ui/views/nowplaying.py), where the fix buttons live.

        A completed scan in `self._last_metadata_scan` is reused rather than
        rerun, so reopening after fixing one item is instant (the lyrics
        check isn't cheap). `self._metadata_visited` is only ever added to,
        and MissingMetadataDialog marks those rows as already picked, so a
        long list keeps its place across reopens."""
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
        # a cancelled scan's `result` holds only the categories reached
        # before the stop (see scan_missing_metadata's should_stop), so it's
        # shown once but NOT cached - reopening re-scans from scratch
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
        if code == MissingMetadataDialog.ACTION:
            handler = {
                "artwork": self.search_missing_artwork,
                "profiles": self.download_artist_profiles,
                "popularity": self.update_track_popularity,
                "artist_images": self.import_artist_images,
                "fill_all": self.fill_in_everything,
            }.get(dialog.action())
            if handler is not None:
                handler()
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
        if kind == "artist":
            self.ctx.openArtistFromMissingMetadataRequested.emit(item_id)
        else:
            self.ctx.openReleaseFromMissingMetadataRequested.emit(item_id)

    # ---- Missing info › Fill in everything (2026-10-31) ----

    def fill_in_everything(self) -> None:
        """Missing info › "Fill in everything": biographies for artists never
        looked up, then Discogs covers for releases with none, then Last.fm
        popularity - the three bulk jobs that need no answers along the way,
        one after another, with one confirmation up front and one summary at
        the end - and finally relinking pictures in the artwork and artists
        folders. A job whose key/token isn't saved is skipped and named, and
        Cancel stops the whole run (what finished stays). Artist photos need
        a folder chosen, so they stay their own button."""
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        steps: list[tuple[str, list]] = []
        will: list[str] = []
        skipped: list[str] = []

        def n(count: int, word: str) -> str:
            return f"{count:,} {word}{'s' if count != 1 else ''}"

        bios = bio_dl.artists_missing_bio(new_only=True)
        if bios:
            steps.append(("profiles", bios))
            will.append(f"download a biography for {n(len(bios), 'artist')} never looked up")
        if artwork_dl.has_api_token():
            covers = artwork_dl.releases_missing_cover()
            if covers:
                steps.append(("artwork", covers))
                will.append(f"search Discogs for a cover for {n(len(covers), 'release')}")
        else:
            skipped.append("album covers - no Discogs token saved (Online services)")
        if popularity_dl.has_api_key():
            artists = popularity_dl.all_artist_ids_with_releases()
            if artists:
                steps.append(("popularity", artists))
                will.append(f"update Last.fm popularity for {n(len(artists), 'artist')}")
        else:
            skipped.append("track popularity - no Last.fm key saved (Online services)")
        skip_text = ("\n\nSkipped:\n" + "\n".join(f"  {s}" for s in skipped)) if skipped else ""
        if not steps:
            # still worth relinking pictures dropped into the folders by hand
            relinked = self._relink_pictures()
            if relinked:
                self.ctx.libraryChanged.emit()
            QMessageBox.information(
                self, "Fill in everything",
                (f"Pictures: {relinked}\n\n" if relinked else "")
                + "Nothing else to fill in right now." + skip_text)
            return
        if not self._confirm_fill_in_everything(
            "This will:\n" + "\n".join(f"  • {w}" for w in will) + skip_text
            + "\n\nNeeds an internet connection and can take a long time on a big "
            "library - you can Cancel at any point and keep what's done."
        ):
            return
        self._fill_steps = steps
        self._fill_results = []
        self._fill_next()

    def _confirm_fill_in_everything(self, text: str) -> bool:
        """Its own method so tests can answer it."""
        return QMessageBox.question(
            self, "Fill in everything", text, QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes

    def _fill_next(self) -> None:
        if not self._fill_steps:
            self._finish_fill()
            return
        kind, ids = self._fill_steps.pop(0)
        {
            "profiles": self._start_bio_thread,
            "artwork": self._start_artwork_thread,
            "popularity": self._start_popularity_thread,
        }[kind](ids)

    def _fill_step_done(self, title: str, result, thread) -> bool:
        """Called by each bulk job's done handler: True if the job was one
        step of a Fill in everything run, which takes over from there (the
        next step, or the end-of-run summary) instead of the job's own
        result box."""
        if self._fill_steps is None:
            return False
        line = f"{title}: {result.summary()}"
        errors = getattr(result, "errors", None) or []
        if errors:
            line += f" ({len(errors)} couldn't be fetched)"
        self._fill_results.append(line)
        if thread is not None and thread.isInterruptionRequested():
            self._fill_steps = []  # Cancel stops the rest of the run
        self._fill_next()
        return True

    def _finish_fill(self) -> None:
        # pick up pictures put in the artwork/artists folders by hand too
        relinked = self._relink_pictures()
        if relinked:
            self._fill_results.append(f"Pictures: {relinked}")
        results = self._fill_results or ["Nothing was filled in."]
        self._fill_steps = None
        self._fill_results = []
        self._last_metadata_scan = None
        self.ctx.libraryChanged.emit()
        QMessageBox.information(self, "Fill in everything", "\n\n".join(results))

    def find_duplicates(self) -> None:
        """Settings › Duplicates - see services/duplicates.py. Read-only
        until "Switch … to album copies" is pressed in the results."""
        from ...widgets.duplicates import DuplicateScanThread

        if self._duplicates_thread is not None and self._duplicates_thread.isRunning():
            self.ctx.notify("Already searching for duplicates")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Reading the library…")
        self._duplicates_thread = DuplicateScanThread(parent=self)
        self._duplicates_thread.progress.connect(self._on_duplicates_progress)
        self._duplicates_thread.finished_with.connect(self._on_duplicates_done)
        self._duplicates_thread.start()
        self._show_cancel()

    def _on_duplicates_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.progress_label.setText(f"{name}  ({done:,}/{total:,})" if total else name)

    def _on_duplicates_done(self, report) -> None:
        from ...widgets.duplicates import DuplicatesDialog

        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._duplicates_thread)
        dialog = DuplicatesDialog(report, self)
        dialog.switched.connect(self._on_duplicates_switched)
        dialog.charts_switched.connect(self._on_duplicate_charts_switched)
        dialog.exec()

    def _on_duplicate_charts_switched(self, changed: int) -> None:
        self.ctx.notify(f"{changed:,} chart rows now use album copies")
        self.ctx.libraryChanged.emit()

    def _on_duplicates_switched(self, changed: int) -> None:
        self.ctx.notify(f"{changed} playlist song{'s' if changed != 1 else ''} now use album copies")
        self.ctx.playlistsChanged.emit()

    def check_library_health(self) -> None:
        """Settings › Library health - see services/library_health.py.
        Read-only until a fix is pressed in the results; every fix backs the
        library up first."""
        from ...widgets.library_health import LibraryHealthThread

        if self._health_thread is not None and self._health_thread.isRunning():
            self.ctx.notify("Already checking the library")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Checking your library…")
        self._health_thread = LibraryHealthThread(parent=self)
        self._health_thread.progress.connect(self._on_health_progress)
        self._health_thread.finished_with.connect(self._on_health_done)
        self._health_thread.start()
        self._show_cancel()

    def _on_health_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.progress_label.setText(f"{name}  ({done:,}/{total:,})" if total else name)

    def _on_health_done(self, report) -> None:
        from ...widgets.library_health import LibraryHealthDialog

        self.progress.setVisible(False)
        self.progress_label.setText("")
        thread = self._health_thread
        self._hide_cancel(thread)
        if thread is not None:
            thread.wait()
        self._health_thread = None
        dialog = LibraryHealthDialog(
            report, self, remove_missing=lambda: self.purge_missing_files(recheck=True))
        dialog.fixed.connect(self._on_health_fixed)
        if dialog.exec() == LibraryHealthDialog.RECHECK:
            self.check_library_health()

    def _on_health_fixed(self, key: str, message: str) -> None:
        if key != "missing_files":  # the purge already said what it did
            self.ctx.notify(message)
        self._last_metadata_scan = None
        self.ctx.libraryChanged.emit()
        self.ctx.playlistsChanged.emit()
        if key == "not_imported":
            self.ctx.videosChanged.emit()

    def rematch_all(self) -> None:
        """Re-match every external chart on a background thread with the
        page's progress bar - see RematchChartsThread (ui/widgets/common.py)
        and services.charts.rematch_all_charts."""
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

    def refresh_chart_playlists(self, check_busy: bool = True) -> None:
        """Rebuild every chart-saved playlist from its edition's current
        matches (services.charts.refresh_chart_playlists). Quick - one
        playlist per saved edition - so it runs here rather than on a
        thread, behind a wait cursor.

        2026-10-31: no button of its own any more - Charts › Re-match…
        offers it once re-matching finishes (_on_rematch_done), passing
        check_busy=False since the re-match thread may not have quite
        wound down when its result arrives."""
        if check_busy:
            busy = self._busy_with()
            if busy:
                self.ctx.notify(busy)
                return
        if not self._confirm_refresh_chart_playlists():
            return
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from ....services import charts as chart_svc

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with self.ctx.session() as session:
                result = chart_svc.refresh_chart_playlists(session)
        finally:
            QApplication.restoreOverrideCursor()
        self.ctx.playlistsChanged.emit()
        if not result.checked:
            self.ctx.notify("No playlists have been saved from a chart yet")
            return
        message = f"Updated {result.updated} of {result.checked} chart playlists"
        if result.chart_fixed:
            message += f", re-matched {result.chart_fixed} chart songs from them"
        if result.unlinked:
            names = ", ".join(f"“{n}”" for n in result.unlinked[:5])
            more = f" and {len(result.unlinked) - 5} more" if len(result.unlinked) > 5 else ""
            message += f" - couldn't tell which chart {names}{more} came from, so left them alone"
        self.ctx.notify(message)
        if result.chart_fixed:
            self.ctx.libraryChanged.emit()

    def _confirm_refresh_chart_playlists(self) -> bool:
        """Its own method so tests can answer it."""
        return QMessageBox.question(
            self,
            "Update chart playlists",
            "Rebuild every playlist saved from a chart so its songs match the "
            "chart's current matches?\n\nEach playlist keeps its name and "
            "folder, but songs you added or reordered in it by hand are "
            "replaced - the same as “Save as playlist” on the Charts page.",
        ) == QMessageBox.Yes

    def _on_rematch_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            self.progress_label.setText(name or "Re-matching…")
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress_label.setText(f"{done}/{total}")

    def _on_rematch_done(self, total: int) -> None:
        thread = self._rematch_thread
        cancelled = thread is not None and thread.isInterruptionRequested()
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(thread)
        self.ctx.notify(f"{total} chart entries now point at tracks you own")
        self.ctx.libraryChanged.emit()
        # 2026-10-31: re-matching and updating the chart playlists are one
        # button now - offer the update (it asks first) when there are
        # playlists saved from charts, unless the re-match was cancelled
        if not cancelled and self._chart_playlist_count():
            self.refresh_chart_playlists(check_busy=False)

    def _chart_playlist_count(self) -> int:
        from sqlalchemy import func, select

        from ....db.models import Playlist

        with self.ctx.session() as session:
            return session.scalar(
                select(func.count()).select_from(Playlist)
                .where(Playlist.kind == Playlist.KIND_CHART)) or 0
