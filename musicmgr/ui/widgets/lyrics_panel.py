"""Now Playing's lyrics pane - loads the current track's sibling .lrc file
(services/lyrics.py) and, if it's timestamped, highlights and auto-scrolls
to the current line as playback position updates.

Also owns the per-track "Download lyrics" button in the empty state: unlike
`JukeboxToggle`/`StarRating` (widget just reports the gesture, the view does
the DB work), this widget runs the whole download itself end-to-end. That's
a deliberate departure from that convention, not an oversight - downloading
only ever writes a local .lrc sidecar file (no DB write, and no other view
has any reason to care that it happened), so there's nothing for a view to
do with the outcome that this panel can't already do by just reloading
itself. See services/lyrics_downloader.py for why this network call exists
at all despite the rest of the app's local-files-only stance."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...services import lyrics_downloader as lyrics_dl
from ...services.lyrics import LyricsResult, current_line_index, load_lyrics
from ..theme import COLORS
from .common import EmptyState, LyricsDownloadThread

#: restored after a failed/negative download attempt, since that leaves a
#: status message (_STATUS_MESSAGES below) sitting in the same label
_DEFAULT_EMPTY_DETAIL = "Drop a matching .lrc file next to this track's audio file to see lyrics here."

_STATUS_MESSAGES = {
    "no_artist": "This track has no artist tag on file, so lyrics can't be looked up.",
    "not_found": "No lyrics found on LRCLIB for this track.",
    "instrumental": "LRCLIB lists this as an instrumental - no lyrics to show.",
    "plain_only": 'LRCLIB only has plain (unsynced) lyrics for this track - skipped since "Synced lyrics only" is checked.',
    "error": "Couldn't reach LRCLIB just now - try again in a bit.",
}


class LyricsPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 2026-09-20 - James: "is there anyway we can force LRCLIB to get
        # only Synced lyrics" - a checkbox rather than a Settings-page
        # global, so it's a per-download choice like everything else here;
        # defaults on, matching what he actually asked for. Read at click
        # time in _on_download_clicked and threaded straight through as
        # LyricsDownloadThread's save_plain (inverted - checked means
        # save_plain=False). See release_panel.py's matching checkbox for
        # the per-album button's copy of this.
        self._synced_only_checkbox = QCheckBox("Synced lyrics only")
        self._synced_only_checkbox.setChecked(True)

        self._empty = EmptyState(
            "No lyrics found",
            _DEFAULT_EMPTY_DETAIL,
            action_text="Download lyrics",
            on_action=self._on_download_clicked,
            extra_widget=self._synced_only_checkbox,
        )

        self._list = QListWidget()
        self._list.setObjectName("LyricsList")
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setWordWrap(True)
        # Qt.AlignCenter on each item centers it within the list's *viewport*,
        # which is narrower than the widget's own visible box by however much
        # the vertical scrollbar takes up on the right - so on any lyric long
        # enough to need scrolling, every centered line reads as shifted
        # toward the scrollbar instead of centered in the box you actually
        # see. Mirroring that width as a left margin whenever the scrollbar
        # is actually showing keeps the viewport - and therefore the
        # centering - symmetric in the box; on a short, unscrolled lyric
        # there's no scrollbar and no margin, so nothing shifts unnecessarily
        self._list.verticalScrollBar().rangeChanged.connect(self._sync_center_margin)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)  # 0
        self._stack.addWidget(self._list)  # 1
        layout.addWidget(self._stack)

        self._result: Optional[LyricsResult] = None
        self._current_index = -1
        self._audio_path: Optional[str] = None
        self._track_meta: dict = {}
        self._download_thread: Optional[LyricsDownloadThread] = None
        #: last position reported via update_position() for whichever track
        #: is currently loaded - kept so a lyrics download that finishes
        #: mid-playback can resync to where the song actually is (see
        #: `_on_download_finished`) instead of always flashing line 0 until
        #: the next position tick from PlayerController corrects it.
        self._last_position_ms = 0

    def _sync_center_margin(self, _minimum: int, maximum: int) -> None:
        needs_scrollbar = maximum > 0
        self._list.setViewportMargins(TOUCH["scrollbar"] if needs_scrollbar else 0, 0, 0, 0)

    def load_for_path(
        self,
        audio_path: Union[str, None],
        *,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration_ms: int = 0,
        resume_position_ms: Optional[int] = None,
    ) -> None:
        """`title`/`artist`/`album`/`duration_ms` feed the download button
        when nothing's found locally - optional and keyword-only so every
        existing single-argument call (nothing playing, or a caller that
        doesn't have this metadata handy) still works unchanged.

        `resume_position_ms` is also optional and keyword-only, and is for
        exactly one caller: `_on_download_finished` reloading the *same*
        track it just wrote a fresh .lrc for, mid-playback. Every other
        caller (a real track change, in `nowplaying.py`/`release_panel.py`)
        leaves it unset, since a track that's actually just started should
        highlight from the top, not from wherever the *previous* track
        happened to be sitting."""
        self._audio_path = audio_path
        self._track_meta = dict(title=title, artist=artist, album=album, duration_ms=duration_ms)
        self._result = load_lyrics(audio_path) if audio_path else None
        self._current_index = -1
        if resume_position_ms is None:
            self._last_position_ms = 0
        self._list.clear()
        if self._result is None or not self._result.lines:
            if self._empty.detail_label is not None:
                self._empty.detail_label.setText(_DEFAULT_EMPTY_DETAIL)
            self._refresh_download_button()
            self._stack.setCurrentWidget(self._empty)
            return
        for line in self._result.lines:
            item = QListWidgetItem(line.text or "♪")
            item.setTextAlignment(Qt.AlignCenter)
            self._list.addItem(item)
        self._stack.setCurrentWidget(self._list)
        if self._result.synced and resume_position_ms is not None:
            self._apply_highlight(current_line_index(self._result.lines, resume_position_ms))
        else:
            self._apply_highlight(0 if self._result.synced else -1)

    def update_position(self, position_ms: int) -> None:
        self._last_position_ms = position_ms
        if self._result is None or not self._result.synced or not self._result.lines:
            return
        idx = current_line_index(self._result.lines, position_ms)
        if idx != self._current_index:
            self._apply_highlight(idx)

    def _apply_highlight(self, idx: int) -> None:
        self._current_index = idx
        past = QColor(COLORS["text_dim"])
        upcoming = QColor(COLORS["text"])
        # 2026-09-13 follow-up (see ui/theme.py's #Primary comment for this
        # whole cleanup) - current lyrics line, walnut brown now instead
        # of red-orange.
        current = QColor(COLORS["jukebox_key_hi"])
        for i in range(self._list.count()):
            item = self._list.item(i)
            font = item.font()
            if i == idx:
                font.setBold(True)
                item.setFont(font)
                item.setForeground(current)
            else:
                font.setBold(False)
                item.setFont(font)
                item.setForeground(past if i < idx else upcoming)
        if 0 <= idx < self._list.count():
            self._list.scrollToItem(
                self._list.item(idx), QListWidget.ScrollHint.PositionAtCenter
            )

    # -- lyrics download ---------------------------------------------------

    def _refresh_download_button(self) -> None:
        btn = self._empty.action_button
        if btn is None:
            return
        downloading = self._download_thread is not None
        btn.setVisible(bool(self._audio_path))
        btn.setEnabled(bool(self._audio_path) and not downloading)
        btn.setText("Downloading…" if downloading else "Download lyrics")
        self._synced_only_checkbox.setVisible(bool(self._audio_path))
        self._synced_only_checkbox.setEnabled(not downloading)

    def _on_download_clicked(self) -> None:
        if not self._audio_path or self._download_thread is not None:
            return
        track = lyrics_dl.LyricsTrackInput(
            audio_path=Path(self._audio_path),
            title=self._track_meta.get("title") or "",
            artist=self._track_meta.get("artist") or "",
            album=self._track_meta.get("album") or "",
            duration_ms=self._track_meta.get("duration_ms") or 0,
        )
        self._download_thread = LyricsDownloadThread(
            [track], save_plain=not self._synced_only_checkbox.isChecked(), parent=self
        )
        self._download_thread.finished_with.connect(self._on_download_finished)
        self._refresh_download_button()
        self._download_thread.start()

    def _on_download_finished(self, result) -> None:
        self._download_thread = None
        outcome = result.outcomes[0] if result.outcomes else None
        # the user may have moved to a different track while this was
        # in flight - if so, that track's own load_for_path call has
        # already set the right state, so there's nothing to reconcile
        if outcome is None or not self._audio_path or str(outcome.audio_path) != str(
            Path(self._audio_path)
        ):
            return
        if outcome.status == "downloaded":
            # mid-playback download: resync the highlight to wherever the
            # song actually is right now, instead of flashing line 0 until
            # the next positionChanged tick from PlayerController corrects
            # it (see _last_position_ms's docstring in __init__).
            self.load_for_path(
                self._audio_path, resume_position_ms=self._last_position_ms, **self._track_meta
            )
            return
        self._refresh_download_button()
        message = _STATUS_MESSAGES.get(outcome.status)
        if message and self._empty.detail_label is not None:
            self._empty.detail_label.setText(message)
