"""The artist page's biography pane - shows an artist's profile text
(Artist.profile: a free-text column, same shape as a Discogs bio) plus a
"Fetch bio" button when nothing's saved yet.

2026-09-16 follow-up - this widget existed before today but was never
actually wired into any page, and nothing ever wrote to Artist.profile
either (the importer this docstring used to say was "not wired up yet"
still didn't exist). James: "Add a new menu option called artist
profile...collect a brief writeup of the artist" - asked where the page
should live and where the writeup should come from, he chose the existing
Artist page (ui/widgets/artist_panel.py:ArtistDetailPanel, right below the
release row) rather than a new sidebar section, and a network fetch
"similar to lyrics" rather than manual entry only - see
services/artist_bio_downloader.py's module docstring for the full story
and why a fetched bio, unlike a downloaded lyric, is a database write. This
is the first real caller BioPanel has ever had.

Mirrors LyricsPanel's own shape almost exactly (see that widget's
docstring): downloading only ever writes to the database (Artist.profile)
and re-reads/redisplays that one row afterwards, so there's nothing for
the artist page to do with the outcome that this panel can't already do
by reloading itself - hence BioPanel taking `ctx` and running the whole
fetch end-to-end, the same deliberate departure from the
widget-just-reports/view-does-the-work convention LyricsPanel already
made for the same reason.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from ...db.models import Artist
from ...services import artist_bio_downloader as bio_dl
from .common import BioDownloadThread, EmptyState

#: restored after a failed/negative fetch attempt, since that leaves a
#: status message (_STATUS_MESSAGES below) sitting in the same label
_DEFAULT_EMPTY_DETAIL = "This artist doesn't have a saved biography yet."

_STATUS_MESSAGES = {
    "not_found": "Couldn't find a biography for this artist on Wikipedia.",
    "error": "Couldn't reach Wikipedia just now - try again in a bit.",
}


class BioPanel(QWidget):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._artist_id: Optional[int] = None
        self._artist_name: str = ""
        self._download_thread: Optional[BioDownloadThread] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._empty = EmptyState(
            "No biography on file",
            _DEFAULT_EMPTY_DETAIL,
            action_text="Fetch bio",
            on_action=self._on_fetch_clicked,
        )

        self._text = QLabel("")
        self._text.setObjectName("BioText")
        self._text.setWordWrap(True)
        self._text.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidget(self._text)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)  # 0
        self._stack.addWidget(self._scroll)  # 1
        layout.addWidget(self._stack)

    def set_bio(self, profile: Optional[str]) -> None:
        """Show `profile` text without touching which artist a "Fetch bio"
        click would target - used internally after a successful fetch.
        Callers with an artist to show should use set_artist() instead,
        which also points the fetch button at the right artist."""
        text = (profile or "").strip()
        self._text.setText(text)
        if text:
            self._scroll.verticalScrollBar().setValue(0)
            self._stack.setCurrentWidget(self._scroll)
        else:
            if self._empty.detail_label is not None:
                self._empty.detail_label.setText(_DEFAULT_EMPTY_DETAIL)
            self._stack.setCurrentWidget(self._empty)

    def set_artist(self, artist_id: Optional[int], name: str, profile: Optional[str]) -> None:
        """The artist page's normal entry point - `artist_id`/`name` feed
        the "Fetch bio" button when nothing's saved locally yet, the same
        way LyricsPanel.load_for_path's title/artist feed its own download
        button."""
        # a fetch still in flight for whichever artist was showing before
        # is left to finish quietly in the background - _on_fetch_finished
        # discards its result once artist_id no longer matches - but this
        # page's own button must not keep reading "Fetching…" for a
        # different artist in the meantime.
        self._download_thread = None
        self._artist_id = artist_id
        self._artist_name = name
        self.set_bio(profile)
        self._refresh_fetch_button()

    def _refresh_fetch_button(self) -> None:
        btn = self._empty.action_button
        if btn is None:
            return
        downloading = self._download_thread is not None
        btn.setVisible(bool(self._artist_id))
        btn.setEnabled(bool(self._artist_id) and not downloading)
        btn.setText("Fetching…" if downloading else "Fetch bio")

    def _on_fetch_clicked(self) -> None:
        if not self._artist_id or self._download_thread is not None:
            return
        self._download_thread = BioDownloadThread([self._artist_id], parent=self)
        self._download_thread.finished_with.connect(self._on_fetch_finished)
        self._refresh_fetch_button()
        self._download_thread.start()

    def _on_fetch_finished(self, result: bio_dl.BioDownloadResult) -> None:
        self._download_thread = None
        outcome = result.outcomes[0] if result.outcomes else None
        # the user may have navigated to a different artist while this was
        # in flight - if so, that artist's own set_artist() call has
        # already set the right state, so there's nothing to reconcile
        if outcome is None or outcome.artist_id != self._artist_id:
            return
        if outcome.status == "downloaded":
            with self.ctx.session() as session:
                artist = session.get(Artist, self._artist_id)
                text = artist.profile if artist else ""
            self.set_bio(text)
            self._refresh_fetch_button()
            return
        self._refresh_fetch_button()
        message = _STATUS_MESSAGES.get(outcome.status)
        if message and self._empty.detail_label is not None:
            self._empty.detail_label.setText(message)
