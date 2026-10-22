"""Settings › Maintenance tools (artwork, artist info, metadata, duplicates, charts) (split out of settings.py, 2026-10-22)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import QDialog, QFileDialog, QFrame, QMessageBox, QVBoxLayout
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
        """Build the Maintenance group (moved out of __init__, 2026-10-22)."""
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
        # Maintenance (2026-10-01): one list of fifteen tools became three
        # groups - artwork and track info, files and data, online services.
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
        # 2026-10-01 - volume levelling, see services/replaygain.py
        add_tool_row(
            "Volume levels",
            "Read ReplayGain tags, or measure loudness, for every track without a level yet. "
            "Your music files aren't changed.",
            "Analyze…",
            self.analyze_loudness,
        )
        add_tool_row(
            "Charts",
            "Re-match every chart entry against your current library.",
            "Re-match",
            self.rematch_all,
        )

        tools_group("Files & data", "files")
        add_tool_row(
            "Verify files",
            "Check that every track's file can still be found on disk.",
            "Verify",
            self.verify_files,
        )
        add_tool_row(
            "Missing files",
            "Remove tracks whose files can no longer be found.",
            "Purge…",
            self.purge_missing_files,
        )
        # 2026-10-13 - James: "Duplicate finder ... from an identical file
        # standpoint. Playlist should point to songs in albums not chart
        # folders" - see services/duplicates.py
        add_tool_row(
            "Duplicates",
            "Move playlist and chart songs off Billboard, Hot Country and Oldies copies onto "
            "album copies, and list files that hold the identical recording.",
            "Find…",
            self.find_duplicates,
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
        # 2026-10-19 - James: daily library backups, local only, keep 7,
        # with a Restore option - see services/library_backup.py
        add_tool_row(
            "Back up library",
            "Save a copy of your library now. MusicMgr also does this by itself the first time "
            "it closes each day, keeping the last 7.",
            "Back up",
            self.back_up_library,
        )
        add_tool_row(
            "Restore library",
            "Put your playlists, play history, charts and settings back to a saved backup. "
            "Music files aren't touched.",
            "Restore…",
            self.restore_library,
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

        tools_group("Online services", "services")
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

        _size_tool_row_buttons(tool_rows)

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

        all_ids = bio_dl.artists_missing_bio()
        if not all_ids:
            QMessageBox.information(
                self, "Artist profiles", "Every artist already has a saved biography."
            )
            return
        # 2026-10-17 - James: "Is there a way I can decide to completely
        # skip an artist profile if one exists vs. always checking. This
        # option takes way too long." Artists Wikipedia had nothing for (and
        # ones whose bio isn't about music) used to be looked up again on
        # every run; now they're remembered (Artist.profile_checked_at) and
        # the run can be limited to artists never looked up before.
        new_ids = bio_dl.artists_missing_bio(new_only=True)
        choice = self._ask_profile_scope(len(new_ids), len(all_ids))
        if choice is None:
            return
        artist_ids = new_ids if choice == "new" else all_ids

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
