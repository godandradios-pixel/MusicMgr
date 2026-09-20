"""Release header (Discogs-style metadata) plus its track list.

Shared by the three-pane browser and the album grid's detail page so both show
exactly the same information and actions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ... import config
from ...db.models import Release
from ...services import artwork_downloader as artwork_dl
from ...services import library as lib
from ...services import lyrics_downloader as lyrics_dl
from ...services.library import format_duration
from ..theme import COLORS
from .common import (
    ArtworkPickerDialog,
    ArtworkSearchThread,
    Breadcrumb,
    CoverArt,
    LyricsDownloadThread,
    TouchButton,
    TouchList,
    dim_label,
)

FIELDS = ["Label", "Catalog #", "Country", "Released", "Format", "Genre", "Style", "Files"]

_ARTWORK_STATUS_MESSAGES = {
    "not_configured": 'Add a Discogs API token in Settings first ("Discogs API token…")',
    "not_found": "Couldn't find this release on Discogs.",
    "error": "Couldn't reach Discogs just now - try again in a bit.",
}


class ReleaseDetailPanel(QWidget):
    def __init__(self, ctx, art_size: int = 150, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._release_id: Optional[int] = None
        self._tracks: list = []
        self._breadcrumb_ancestors: list[str] = []
        self._lyrics_thread: Optional[LyricsDownloadThread] = None
        self._artwork_thread: Optional[ArtworkSearchThread] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # replaces a lone "‹ Back" pill - every ancestor is a tappable link,
        # not just the immediate one, so a release opened from an artist page
        # can jump straight to the artist grid or to that one artist (see
        # LibraryView._open_release). Stays hidden (no ancestors set) for the
        # embedded three-pane-browser instance, which isn't a "page" you
        # navigate back out of.
        self.breadcrumb = Breadcrumb()
        layout.addWidget(self.breadcrumb)

        detail = QFrame()
        detail.setObjectName("Card")
        dl = QHBoxLayout(detail)
        dl.setContentsMargins(14, 14, 14, 14)
        dl.setSpacing(16)

        self.cover = CoverArt(art_size)
        dl.addWidget(self.cover, 0, Qt.AlignTop)

        info = QGridLayout()
        info.setVerticalSpacing(3)
        info.setHorizontalSpacing(12)

        self.rel_title = QLabel("Select a release")
        self.rel_title.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.rel_title.setWordWrap(True)
        self.rel_artist = QLabel("")
        self.rel_artist.setObjectName("Subtitle")
        info.addWidget(self.rel_title, 0, 0, 1, 4)
        info.addWidget(self.rel_artist, 1, 0, 1, 4)

        self._fields: dict[str, QLabel] = {}
        for idx, name in enumerate(FIELDS):
            row = 2 + idx // 2
            col = (idx % 2) * 2
            key = QLabel(name)
            key.setObjectName("Dim")
            key.setFixedWidth(78)
            val = QLabel("—")
            val.setWordWrap(True)
            val.setMinimumWidth(130)
            info.addWidget(key, row, col)
            info.addWidget(val, row, col + 1)
            self._fields[name] = val
        # fixed-width value columns with the slack pushed to a trailing spacer,
        # so the two label/value pairs stay side by side instead of drifting to
        # opposite edges on a wide panel
        info.setColumnStretch(1, 0)
        info.setColumnStretch(3, 0)
        info.setColumnMinimumWidth(1, 210)
        info.setColumnMinimumWidth(3, 210)
        info.setColumnStretch(4, 1)
        dl.addLayout(info, 1)
        layout.addWidget(detail)

        # No standalone "Play" pill here (removed 2026-09-05, see
        # architecture.md "Redundant Play button removed from the release
        # page" - James: "why do we have 2 play buttons? ... I only want the
        # play from the bar at the bottom"). Tapping any track row already
        # starts playback from there (_on_track_tapped -> self.play(idx)),
        # so tapping the first track does exactly what the removed button
        # did (self.play(0)) - nothing is lost, just one fewer orange play
        # glyph competing with the persistent PlayerBar's.
        actions = QHBoxLayout()
        actions.setSpacing(8)
        shuffle = TouchButton("Shuffle")
        shuffle.clicked.connect(self.shuffle_play)
        queue = TouchButton("Add to queue")
        queue.clicked.connect(lambda: self.ctx.enqueue_tracks(self._tracks))
        self.lyrics_btn = TouchButton("Download lyrics")
        self.lyrics_btn.clicked.connect(self.download_lyrics)
        # 2026-09-20 - James: "is there anyway we can force LRCLIB to get
        # only Synced lyrics" - per-download checkbox rather than a
        # Settings-page global, matching LyricsPanel's own copy of this for
        # the per-track button. Defaults on. Read at click time in
        # download_lyrics() and threaded through as LyricsDownloadThread's
        # save_plain (inverted - checked means save_plain=False).
        self.lyrics_synced_only = QCheckBox("Synced only")
        self.lyrics_synced_only.setChecked(True)
        # 2026-09-18 - James: "Ability to search for album artwork" - see
        # search_artwork below and services/artwork_downloader.py's module
        # docstring for why this is a search-then-pick flow rather than an
        # auto-apply, unlike the bulk Settings action for the same feature.
        self.artwork_btn = TouchButton("Search artwork")
        self.artwork_btn.clicked.connect(self.search_artwork)
        # 2026-09-19 - James: "I need the ability to select an album art
        # from a file location ... if it can't find album art I have no
        # way of getting one added" - the manual counterpart to Search
        # artwork above, for a release Discogs comes up empty on (or one
        # James would rather point at his own scan/photo for). See
        # choose_artwork_file below and artwork_downloader.
        # apply_local_image_to_release.
        self.artwork_file_btn = TouchButton("Choose from file…")
        self.artwork_file_btn.clicked.connect(self.choose_artwork_file)
        for b in (shuffle, queue, self.lyrics_btn):
            actions.addWidget(b)
        actions.addWidget(self.lyrics_synced_only)
        for b in (self.artwork_btn, self.artwork_file_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        self.track_count = dim_label("")
        actions.addWidget(self.track_count)
        layout.addLayout(actions)

        # hidden until a lyrics download is running - same embedded
        # progress-bar-plus-label shape as SettingsView's "Scan now"
        # (ui/views/settings.py::ScanThread), the only other place in the
        # app that runs a QThread with progress feedback
        self.lyrics_progress = QProgressBar()
        self.lyrics_progress.setVisible(False)
        layout.addWidget(self.lyrics_progress)
        self.lyrics_progress_label = dim_label("")
        self.lyrics_progress_label.setVisible(False)
        layout.addWidget(self.lyrics_progress_label)

        self.track_list = TouchList(two_column=True)
        self.track_list.itemActivatedPayload.connect(self._on_track_tapped)
        layout.addWidget(self.track_list, 1)

    # -- api ------------------------------------------------------------------

    @property
    def tracks(self) -> list:
        return self._tracks

    @property
    def release_id(self) -> Optional[int]:
        return self._release_id

    def set_breadcrumb_ancestors(self, labels: Sequence[str]) -> None:
        """The levels above this release, root-first (e.g. `["Albums"]` or
        `["Artists", "Dana Voss"]`) - the release's own title is appended
        automatically once loaded. Leaving this unset (the embedded
        three-pane-browser instance never calls it) keeps the breadcrumb
        hidden, since that instance isn't a "page" to navigate back out of.
        """
        self._breadcrumb_ancestors = list(labels)
        self._refresh_breadcrumb()

    def _refresh_breadcrumb(self) -> None:
        path = list(self._breadcrumb_ancestors)
        if self._release_id is not None:
            path.append(self.rel_title.text())
        self.breadcrumb.set_path(path)

    def set_release(self, release_id: Optional[int]) -> None:
        self._release_id = release_id
        self._tracks = []
        # a search still in flight for whichever release was showing before
        # is left to finish quietly in the background - _on_artwork_ready
        # discards its result once release_id no longer matches - but this
        # page's own button must not keep reading "Searching…" for a
        # different release in the meantime (same pattern BioPanel.set_artist
        # uses for its own fetch button).
        self._artwork_thread = None
        self.artwork_btn.setText("Search artwork")
        if release_id is None:
            self.rel_title.setText("Select a release")
            self.rel_artist.setText("")
            self.cover.set_source(None, "")
            for val in self._fields.values():
                val.setText("—")
            self.track_list.set_rows([])
            self.track_count.setText("")
            self.lyrics_btn.setEnabled(False)
            self.artwork_btn.setEnabled(False)
            self.artwork_file_btn.setEnabled(False)
            self._refresh_breadcrumb()
            return

        rows: list[dict] = []
        with self.ctx.session() as session:
            release = session.get(Release, release_id)
            if release is None:
                return
            self.rel_title.setText(release.title)
            self.rel_artist.setText(release.artist_display or "")
            self.cover.set_source(release.cover_path, release.title)

            fmt = ", ".join(
                " ".join(
                    x for x in (f.name, f"({f.descriptions})" if f.descriptions else "") if x
                )
                for f in release.formats
            )
            self._fields["Label"].setText(release.label.name if release.label else "—")
            self._fields["Catalog #"].setText(release.catalog_number or "—")
            self._fields["Country"].setText(release.country or "—")
            self._fields["Released"].setText(
                release.released or (str(release.year) if release.year else "—")
            )
            self._fields["Format"].setText(fmt or "—")
            self._fields["Genre"].setText(", ".join(g.name for g in release.genres) or "—")
            self._fields["Style"].setText(", ".join(s.name for s in release.styles) or "—")

            tracks = lib.list_tracks(session, release_id=release_id)
            self._tracks = list(tracks)
            total_ms = 0
            playable = 0
            for track in tracks:
                total_ms += track.duration_ms or 0
                mf = track.primary_file
                if mf is not None and not mf.is_missing:
                    playable += 1
                missing = mf is None or mf.is_missing
                rows.append({
                    "lead": track.position or (str(track.track_no) if track.track_no else ""),
                    "primary": track.title,
                    "secondary": track.artist_display or "",
                    "trail": format_duration(track.duration_ms),
                    "color": COLORS["text_dim"] if missing else COLORS["text"],
                    "key": track.id,
                })
            self._fields["Files"].setText(f"{playable} of {len(tracks)} playable")
            self.track_count.setText(f"{len(tracks)} tracks · {format_duration(total_ms)}")
            self.lyrics_btn.setEnabled(playable > 0)
            self.artwork_btn.setEnabled(True)
            self.artwork_file_btn.setEnabled(True)
        self.track_list.set_rows(rows)
        self._refresh_breadcrumb()
        # lets the persistent PlayerBar's play button start this release
        # from track 1 even before any row's been tapped - see "Redundant
        # Play button removed from the release page" follow-up in
        # architecture.md for why this exists (removing the page's own
        # "Play" pill left the bottom bar with nothing to act on until a
        # track was tapped or something was already queued).
        self.ctx.set_viewing(self._tracks, source=f"release:{release_id}")

    # -- actions ---------------------------------------------------------------

    def _on_track_tapped(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        for idx, track in enumerate(self._tracks):
            if track.id == payload.get("key"):
                self.play(idx)
                return

    def play(self, start: int) -> None:
        if not self._tracks:
            return
        self.ctx.player.set_shuffle(False)
        self.ctx.play_tracks(self._tracks, start=start, source="library")

    def shuffle_play(self) -> None:
        if not self._tracks:
            return
        self.ctx.player.set_shuffle(True)
        self.ctx.play_tracks(self._tracks, start=0, source="library")

    def download_lyrics(self) -> None:
        """Per-album counterpart to LyricsPanel's per-track button (see that
        widget's docstring for why lyrics downloading owns its own network
        call instead of going through the DB-write-reporting convention).
        Checks every track's own .lrc sidecar individually rather than
        skipping the whole album if one track already has lyrics - James
        was asked directly (AskUserQuestion, 2026-09-07) and chose
        per-track checking over the uploaded script's whole-folder
        shortcut; `LyricsDownloadThread`/`download_lyrics_for_album` never
        implement that shortcut at all, so there's nothing to opt out of
        here."""
        if self._lyrics_thread is not None or not self._tracks:
            return

        release_title = self.rel_title.text()
        release_artist = self.rel_artist.text()
        inputs: list[lyrics_dl.LyricsTrackInput] = []
        for track in self._tracks:
            mf = track.primary_file
            if mf is None or mf.is_missing:
                continue  # no file on disk to write a lyrics sidecar next to
            inputs.append(
                lyrics_dl.LyricsTrackInput(
                    audio_path=Path(mf.path),
                    title=track.title,
                    artist=track.artist_display or release_artist,
                    album=release_title,
                    duration_ms=track.duration_ms or 0,
                )
            )
        if not inputs:
            return

        self.lyrics_btn.setEnabled(False)
        self.lyrics_synced_only.setEnabled(False)
        self.lyrics_progress.setVisible(True)
        self.lyrics_progress.setRange(0, len(inputs))
        self.lyrics_progress.setValue(0)
        self.lyrics_progress_label.setVisible(True)
        self.lyrics_progress_label.setText(f"Looking up lyrics for {len(inputs)} track(s)…")

        self._lyrics_thread = LyricsDownloadThread(
            inputs, save_plain=not self.lyrics_synced_only.isChecked(), parent=self
        )
        self._lyrics_thread.progress.connect(self._on_lyrics_progress)
        self._lyrics_thread.finished_with.connect(self._on_lyrics_finished)
        self._lyrics_thread.start()

    def _on_lyrics_progress(self, done: int, total: int, name: str) -> None:
        self.lyrics_progress.setRange(0, max(total, 1))
        self.lyrics_progress.setValue(done)
        self.lyrics_progress_label.setText(f"{done}/{total} — {name}" if name else f"{done}/{total} done")

    def _on_lyrics_finished(self, result) -> None:
        self._lyrics_thread = None
        self.lyrics_progress.setVisible(False)
        self.lyrics_progress_label.setVisible(False)
        self.lyrics_btn.setEnabled(True)
        self.lyrics_synced_only.setEnabled(True)
        QMessageBox.information(self, "Download lyrics", result.summary())

    def search_artwork(self) -> None:
        """Search Discogs for this release's cover and let James pick one
        before anything is saved - see artwork_downloader.py's module
        docstring for why this is a search-then-pick flow rather than the
        bulk Settings action's auto-apply. Mirrors download_lyrics's shape
        (guard against a run already in flight, disable the button while
        it works) but ends in a picker dialog instead of a progress bar,
        since a single-release search finishes in one round trip rather
        than a whole album's worth of per-track lookups."""
        if self._release_id is None or self._artwork_thread is not None:
            return
        if not artwork_dl.has_api_token():
            self.ctx.notify(
                'Add a Discogs API token in Settings first ("Discogs API token…")'
            )
            return

        self.artwork_btn.setEnabled(False)
        self.artwork_btn.setText("Searching…")
        self._artwork_thread = ArtworkSearchThread(self._release_id, parent=self)
        self._artwork_thread.finished_with.connect(self._on_artwork_ready)
        self._artwork_thread.start()

    def _on_artwork_ready(self, outcome: artwork_dl.ArtworkSearchOutcome) -> None:
        # James may have navigated to a different release while this was in
        # flight - set_release already reset _artwork_thread and the button
        # for whatever's showing now, so there's nothing to reconcile.
        if self._artwork_thread is None or self.sender() is not self._artwork_thread:
            return
        self._artwork_thread = None
        self.artwork_btn.setEnabled(True)
        self.artwork_btn.setText("Search artwork")

        if outcome.error:
            message = _ARTWORK_STATUS_MESSAGES.get(
                outcome.error, "Couldn't search for artwork just now."
            )
            self.ctx.notify(message)
            return

        release_id = self._release_id
        dialog = ArtworkPickerDialog(outcome, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        candidate = dialog.selected_candidate()
        if candidate is None or release_id is None:
            return

        apply_outcome = artwork_dl.apply_artwork_to_release(release_id, candidate)
        if apply_outcome.status != "applied":
            self.ctx.notify(f"Couldn't save that cover: {apply_outcome.detail or apply_outcome.status}")
            return

        # only worth reloading the cover if James hasn't already navigated
        # away from this release while the picker was open
        if self._release_id == release_id:
            self.set_release(release_id)
        self.ctx.libraryChanged.emit()

    def choose_artwork_file(self) -> None:
        """Pick an image straight from disk and set it as this release's
        cover - no search, no picker, just James's own file. Synchronous
        (no QThread the way search_artwork needs one): a local file read
        plus a digest/write under config.ART_DIR is fast enough not to
        need off-UI-thread handling, unlike a network round trip."""
        if self._release_id is None:
            return
        release_id = self._release_id
        patterns = " ".join(f"*{ext}" for ext in sorted(config.IMAGE_EXTENSIONS))
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Choose album artwork", "", f"Images ({patterns})"
        )
        if not file_path:
            return

        outcome = artwork_dl.apply_local_image_to_release(release_id, file_path)
        if outcome.status != "applied":
            self.ctx.notify(f"Couldn't use that image: {outcome.detail or outcome.status}")
            return

        # James may have navigated to a different release while the native
        # file dialog was open (it's modal, but belt-and-braces to match
        # the same guard _on_artwork_ready uses)
        if self._release_id == release_id:
            self.set_release(release_id)
        self.ctx.libraryChanged.emit()
