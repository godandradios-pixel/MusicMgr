"""Tap-to-sync lyric editor (2026-09-28) - opened from Now Playing's Lyrics
pane for the track that's playing.

How it works: the lyric lines sit in a list with the next line to time
selected. Play the song and tap the big **Stamp** button (or press Space)
the moment each line starts - the current playback position is written
onto that line and the selection moves down one. Fix a line afterwards by
selecting it and nudging it ±0.1s / ±0.5s, or "Play from line" to hear it
again. **Save** writes the sidecar `.lrc` (see `services/lyrics_editor.py`),
backing up whatever was there before.

The starting text is the track's existing .lrc (timestamps kept, so this
doubles as a re-timing tool), or LRCLIB's lyrics via "Load from LRCLIB"
(fetched on a thread - see `LyricsTextThread`). "Edit text…" fixes words
without losing the sync (`lyrics_editor.retext`).

This dialog is modal, which hides the persistent PlayerBar's controls
behind it - so, like the full-screen video window (see video_panel.py), it
carries its own small transport row. It drives the one shared
`PlayerController`; it never starts a second player. If the song ends and
the queue moves on while editing, `_on_track_changed` steps back to the
track being edited and pauses, so a stamp can never land on the wrong
song's clock."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...config import TOUCH
from ...services import lyrics_downloader as lyrics_dl
from ...services import lyrics_editor as le
from ...services.player import PlayerController
from ..theme import COLORS
from .common import PAUSE_GLYPH, TouchButton

#: default "tap delay compensation" - people tap a beat after they hear a
#: line start, and a lyric shown a hair early reads better than late anyway
DEFAULT_LEAD_MS = 150
#: "Play from line" starts this far before the line, to hear the lead-in
PREROLL_MS = 2000

COL_TIME, COL_TEXT = 0, 1


class LyricsTextThread(QThread):
    """Runs `lyrics_downloader.fetch_lyrics_text` off the UI thread."""

    finished_with = Signal(object)  # LyricsTextFetch

    def __init__(self, *, title: str, artist: str, album: str, duration_ms: int, parent=None) -> None:
        super().__init__(parent)
        self._args = dict(title=title, artist=artist, album=album, duration_ms=duration_ms)

    def run(self) -> None:  # pragma: no cover - exercised interactively
        self.finished_with.emit(lyrics_dl.fetch_lyrics_text(**self._args))


class LyricsTextDialog(QDialog):
    """"Edit text…" - the lyric lines as one plain-text block."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit lyric text")
        self.setMinimumSize(620, 560)
        layout = QVBoxLayout(self)
        hint = QLabel("One lyric line per row. Timing is kept for lines you don't change.")
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.editor = QPlainTextEdit(text)
        font = self.editor.font()
        font.setPointSize(max(font.pointSize(), 13))
        self.editor.setFont(font)
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self.editor.toPlainText()


class LyricsEditorDialog(QDialog):
    #: emitted with the written path after a successful Save
    saved = Signal(object)

    def __init__(
        self,
        player: PlayerController,
        audio_path: str,
        *,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration_ms: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.player = player
        self.audio_path = str(audio_path)
        self.meta = dict(title=title, artist=artist, album=album, duration_ms=duration_ms)
        self.lines: list[le.EditLine] = []
        self.saved_path: Optional[Path] = None
        self._dirty = False
        self._now_index = -1
        self._restoring = False
        self._fetch_thread: Optional[LyricsTextThread] = None
        # the queue slot holding the track being edited, so the song ending
        # (and the queue moving on) can be undone - see _on_track_changed
        self._queue_index = player.current_index

        self.setWindowTitle("Sync lyrics")
        self.setMinimumSize(900, 640)
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- header ----
        head = QLabel(f"Sync lyrics — {title or Path(audio_path).stem}")
        head.setObjectName("Title")
        root.addWidget(head)
        sub = QLabel(" · ".join(x for x in (artist, album) if x))
        sub.setObjectName("Subtitle")
        root.addWidget(sub)

        # ---- source row ----
        source = QHBoxLayout()
        self.reload_btn = TouchButton("Reload saved .lrc")
        self.reload_btn.clicked.connect(self._reload_saved)
        self.lrclib_btn = TouchButton("Load from LRCLIB")
        self.lrclib_btn.clicked.connect(self._fetch_from_lrclib)
        self.text_btn = TouchButton("Edit text…")
        self.text_btn.clicked.connect(self._edit_text)
        for b in (self.reload_btn, self.lrclib_btn, self.text_btn):
            source.addWidget(b)
        source.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        source.addWidget(self.status, 2)
        root.addLayout(source)

        # ---- the lines ----
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("LyricsEditorTable")
        self.table.setHorizontalHeaderLabels(["Time", "Lyric"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(TOUCH["row_height"] - 20)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_TIME, QHeaderView.Fixed)
        hdr.setSectionResizeMode(COL_TEXT, QHeaderView.Stretch)
        self.table.setColumnWidth(COL_TIME, 130)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        # no keyboard focus, so Space always means "stamp" (see _stamp's
        # shortcut below) instead of the table eating it
        self.table.setFocusPolicy(Qt.NoFocus)
        root.addWidget(self.table, 1)

        # ---- per-line fixes ----
        fixes = QHBoxLayout()
        for label, delta in (("−0.5s", -500), ("−0.1s", -100), ("+0.1s", 100), ("+0.5s", 500)):
            b = TouchButton(label)
            b.clicked.connect(lambda _=False, d=delta: self._nudge_selected(d))
            fixes.addWidget(b)
        self.play_line_btn = TouchButton("▶ Play from line")
        self.play_line_btn.clicked.connect(self._play_from_selected)
        fixes.addWidget(self.play_line_btn)
        clear_btn = TouchButton("Clear time")
        clear_btn.clicked.connect(self._clear_selected)
        fixes.addWidget(clear_btn)
        break_btn = TouchButton("+ ♪ break")
        break_btn.setToolTip("Insert an empty line above this one - marks an instrumental gap")
        break_btn.clicked.connect(self._insert_break)
        fixes.addWidget(break_btn)
        delete_btn = TouchButton("Delete line")
        delete_btn.clicked.connect(self._delete_selected)
        fixes.addWidget(delete_btn)
        fixes.addStretch(1)
        for label, delta in (("All −0.1s", -100), ("All +0.1s", 100)):
            b = TouchButton(label)
            b.setToolTip("Shift every timed line")
            b.clicked.connect(lambda _=False, d=delta: self._shift_all(d))
            fixes.addWidget(b)
        root.addLayout(fixes)

        # ---- transport + stamp ----
        transport = QHBoxLayout()
        restart = TouchButton("⏮")
        restart.setToolTip("Back to the start of the song")
        restart.clicked.connect(lambda: self.player.seek(0))
        back5 = TouchButton("−5s")
        back5.clicked.connect(lambda: self.player.seek_relative(-5000))
        self.play_btn = TouchButton("▶")
        self.play_btn.clicked.connect(self._toggle_play)
        fwd5 = TouchButton("+5s")
        fwd5.clicked.connect(lambda: self.player.seek_relative(5000))
        for b in (restart, back5, self.play_btn, fwd5):
            b.setMinimumWidth(72)
            transport.addWidget(b)
        self.position_label = QLabel("00:00.00")
        self.position_label.setStyleSheet("font-size: 22px; font-weight: 700;")
        self.position_label.setMinimumWidth(140)
        self.position_label.setAlignment(Qt.AlignCenter)
        transport.addWidget(self.position_label)
        lead_label = QLabel("Tap delay")
        lead_label.setObjectName("Dim")
        lead_label.setToolTip(
            "Subtracted from every stamp, to make up for tapping just after you hear the line"
        )
        transport.addWidget(lead_label)
        self.lead_spin = QSpinBox()
        self.lead_spin.setRange(0, 1000)
        self.lead_spin.setSingleStep(50)
        self.lead_spin.setSuffix(" ms")
        self.lead_spin.setValue(DEFAULT_LEAD_MS)
        self.lead_spin.setMinimumHeight(TOUCH["button_height"] - 12)
        # adjusted with its arrows only - a focused spinbox would swallow
        # the Space key that stamps
        self.lead_spin.setFocusPolicy(Qt.NoFocus)
        transport.addWidget(self.lead_spin)
        transport.addStretch(1)
        self.stamp_btn = TouchButton("STAMP  (Space)", primary=True)
        self.stamp_btn.setMinimumSize(260, TOUCH["button_height"] + 16)
        f = self.stamp_btn.font()
        f.setPointSize(max(f.pointSize(), 16))
        f.setBold(True)
        self.stamp_btn.setFont(f)
        self.stamp_btn.clicked.connect(self._stamp)
        transport.addWidget(self.stamp_btn)
        root.addLayout(transport)

        # ---- save / cancel ----
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Space anywhere in the dialog stamps. Buttons don't take focus, or
        # Space would "click" whichever one was touched last instead.
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self._stamp)

        player.positionChanged.connect(self._on_position)
        player.playbackStateChanged.connect(self._on_state)
        player.trackChanged.connect(self._on_track_changed)
        self._on_state("playing" if player.is_playing() else "paused")
        self._on_position(player.position())

        self._reload_saved(initial=True)

    # -- line list <-> table -------------------------------------------------

    def set_lines(self, lines: list[le.EditLine], *, dirty: bool = True, select: int = 0) -> None:
        self.lines = lines
        self._dirty = dirty
        self._rebuild(select=select)

    def selected_index(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def _select(self, index: int) -> None:
        if 0 <= index < self.table.rowCount():
            self.table.selectRow(index)
            self.table.scrollToItem(
                self.table.item(index, COL_TEXT), QAbstractItemView.PositionAtCenter
            )

    def _rebuild(self, select: Optional[int] = None) -> None:
        keep = self.selected_index() if select is None else select
        self.table.setRowCount(len(self.lines))
        for row, line in enumerate(self.lines):
            time_item = QTableWidgetItem("")
            time_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_TIME, time_item)
            self.table.setItem(row, COL_TEXT, QTableWidgetItem(line.text or "♪"))
        self._paint_rows()
        self._select(min(max(keep, 0), len(self.lines) - 1))
        self._refresh_status()

    def _paint_rows(self) -> None:
        bad = set(le.out_of_order(self.lines))
        now = le.active_line_index(self.lines, self.player.position())
        self._now_index = now
        for row, line in enumerate(self.lines):
            time_item = self.table.item(row, COL_TIME)
            text_item = self.table.item(row, COL_TEXT)
            if time_item is None or text_item is None:
                continue
            time_item.setText(le.format_timestamp(line.time_ms) if line.time_ms is not None else "—")
            if row in bad:
                color = COLORS["warn"]
            elif line.time_ms is None:
                color = COLORS["text_dim"]
            else:
                color = COLORS["text"]
            time_item.setForeground(QColor(color))
            font = QFont(text_item.font())
            font.setBold(row == now)
            text_item.setFont(font)
            text_item.setForeground(
                QColor(COLORS["jukebox_key_hi"] if row == now else COLORS["text"])
            )

    def _refresh_status(self, message: str = "") -> None:
        if message:
            self.status.setText(message)
            return
        total = len(self.lines)
        if not total:
            self.status.setText("No lyric lines yet - load them from LRCLIB or type them in with Edit text…")
            return
        timed = total - le.unstamped_count(self.lines)
        parts = [f"{timed} of {total} lines timed"]
        bad = le.out_of_order(self.lines)
        if bad:
            parts.append(f"{len(bad)} out of order (orange)")
        self.status.setText(" · ".join(parts))

    def _changed(self, select: Optional[int] = None) -> None:
        self._dirty = True
        if select is not None:
            self._select(select)
        self._paint_rows()
        self._refresh_status()

    # -- sources -------------------------------------------------------------

    def _reload_saved(self, initial: bool = False) -> None:
        existing = le.load_lines_for_editing(self.audio_path)
        self.reload_btn.setEnabled(existing is not None)
        if existing is None:
            if initial:
                self.set_lines([], dirty=False)
            return
        if not initial and self._dirty and not self._confirm(
            "Reload saved lyrics?", "Throw away your changes and reload the saved .lrc?"
        ):
            return
        first_unstamped = next((i for i, l in enumerate(existing) if l.time_ms is None), 0)
        self.set_lines(existing, dirty=False, select=first_unstamped)

    def _fetch_from_lrclib(self) -> None:
        if self._fetch_thread is not None:
            return
        self.lrclib_btn.setEnabled(False)
        self.lrclib_btn.setText("Loading…")
        self._fetch_thread = LyricsTextThread(**self.meta, parent=self)
        self._fetch_thread.finished_with.connect(self._on_fetched)
        self._fetch_thread.start()

    def _on_fetched(self, fetch: "lyrics_dl.LyricsTextFetch") -> None:
        self._fetch_thread = None
        self.lrclib_btn.setEnabled(True)
        self.lrclib_btn.setText("Load from LRCLIB")
        messages = {
            "no_artist": "This track has no artist tag, so LRCLIB can't be searched.",
            "not_found": "No lyrics found on LRCLIB for this track.",
            "instrumental": "LRCLIB lists this track as instrumental.",
            "error": "Couldn't reach LRCLIB just now - try again in a bit.",
        }
        if fetch.status != "found":
            self._refresh_status(messages.get(fetch.status, "Nothing loaded."))
            return
        choice = self._choose_lrclib_text(fetch)
        if choice is None:
            return
        if self.lines and not self._confirm(
            "Replace lyrics?", "Replace the lines in the editor with LRCLIB's?"
        ):
            return
        if choice == "synced":
            lines = le.lines_from_lyrics(le.parse_lrc(fetch.synced).lines)
            self.set_lines(lines, select=0)
            self._refresh_status(f"Loaded LRCLIB's synced lyrics ({len(lines)} lines) - fix any timing, then Save.")
        else:
            lines = le.lines_from_plain_text(fetch.plain or fetch.synced)
            self.set_lines(lines, select=0)
            self._refresh_status(f"Loaded {len(lines)} lines from LRCLIB - play the song and tap Stamp as each line starts.")

    def _choose_lrclib_text(self, fetch: "lyrics_dl.LyricsTextFetch") -> Optional[str]:
        """"plain" (tap-sync from scratch), "synced" (start from LRCLIB's
        own timing), or None (cancel). Only asks when both exist."""
        if not fetch.synced:
            return "plain"
        box = QMessageBox(self)
        box.setWindowTitle("LRCLIB has synced lyrics")
        box.setText(
            "LRCLIB already has timing for this track. Start from its timing and fix it, "
            "or load just the words and tap-sync them yourself?"
        )
        synced_btn = box.addButton("Use its timing", QMessageBox.AcceptRole)
        plain_btn = box.addButton("Words only", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is synced_btn:
            return "synced"
        if box.clickedButton() is plain_btn:
            return "plain"
        return None

    def _edit_text(self) -> None:
        dialog = LyricsTextDialog("\n".join(line.text for line in self.lines), self)
        if dialog.exec() != QDialog.Accepted:
            return
        new_lines = le.retext(self.lines, dialog.text())
        self.set_lines(new_lines, select=max(self.selected_index(), 0))

    # -- stamping and fixing -------------------------------------------------

    def _stamp(self) -> None:
        if not self.lines or not self._editing_track_is_loaded():
            return
        index = self.selected_index()
        if index < 0:
            index = 0
        nxt = le.stamp(self.lines, index, self.player.position(), self.lead_spin.value())
        self._changed(select=nxt)

    def _nudge_selected(self, delta_ms: int) -> None:
        index = self.selected_index()
        le.nudge(self.lines, index, delta_ms, self.meta["duration_ms"])
        self._changed()

    def _shift_all(self, delta_ms: int) -> None:
        le.shift_all(self.lines, delta_ms, self.meta["duration_ms"])
        self._changed()

    def _clear_selected(self) -> None:
        index = self.selected_index()
        if 0 <= index < len(self.lines):
            self.lines[index].time_ms = None
            self._changed()

    def _insert_break(self) -> None:
        # goes *above* the selected line (James, 2026-09-28): the selected
        # line is the next one to stamp, so the break lands just before it
        # and is selected itself - stamp it when the music break starts,
        # then carry on with the line that follows
        index = self.selected_index()
        at = index if index >= 0 else len(self.lines)
        self.lines.insert(at, le.EditLine(text=""))
        self._dirty = True
        self._rebuild(select=at)

    def _delete_selected(self) -> None:
        index = self.selected_index()
        if 0 <= index < len(self.lines):
            del self.lines[index]
            self._dirty = True
            self._rebuild(select=min(index, len(self.lines) - 1))

    def _play_from_selected(self) -> None:
        index = self.selected_index()
        if not (0 <= index < len(self.lines)) or not self._editing_track_is_loaded():
            return
        start = self.lines[index].time_ms
        if start is None:
            # unstamped: start from the nearest timed line above it
            start = next(
                (l.time_ms for l in reversed(self.lines[:index]) if l.time_ms is not None), 0
            )
        self.player.seek(max(0, start - PREROLL_MS))
        if not self.player.is_playing():
            self.player.play()

    # -- player --------------------------------------------------------------

    def _editing_track_is_loaded(self) -> bool:
        current = self.player.current
        return current is not None and str(Path(current.path)) == str(Path(self.audio_path))

    def _toggle_play(self) -> None:
        if not self._editing_track_is_loaded():
            self._return_to_edited_track()
        self.player.toggle()

    def _on_position(self, ms: int) -> None:
        self.position_label.setText(le.format_timestamp(ms))
        if not self.lines:
            return
        now = le.active_line_index(self.lines, ms)
        if now != self._now_index:
            self._paint_rows()

    def _on_state(self, state: str) -> None:
        self.play_btn.setText(PAUSE_GLYPH if state == "playing" else "▶")

    def _on_track_changed(self, item) -> None:
        if self._restoring or self._editing_track_is_loaded():
            return
        # the song ended (or was skipped) and the queue moved on - step back
        # to the track being edited, paused, so stamps stay on its clock
        self._return_to_edited_track()
        self.player.pause()
        self._refresh_status("Song ended - tap ▶ to hear it again.")

    def _return_to_edited_track(self) -> None:
        queue = self.player.queue
        idx = self._queue_index
        if not (0 <= idx < len(queue)) or str(Path(queue[idx].path)) != str(Path(self.audio_path)):
            return
        self._restoring = True
        try:
            self.player.jump_to(idx)
        finally:
            self._restoring = False

    # -- save / close --------------------------------------------------------

    def _confirm(self, title: str, text: str) -> bool:
        return (
            QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No)
            == QMessageBox.Yes
        )

    def _save(self) -> None:
        if not self.lines:
            self._refresh_status("Nothing to save yet.")
            return
        missing = le.unstamped_count(self.lines)
        if missing and not self._confirm(
            "Some lines aren't timed",
            f"{missing} line(s) have no time yet and will be left out of the file. Save anyway?",
        ):
            return
        if le.out_of_order(self.lines) and not self._confirm(
            "Lines out of order",
            "Some lines (orange) are timed earlier than the line above them - they'll be "
            "saved in time order. Save anyway?",
        ):
            return
        try:
            path = le.save_lrc(self.audio_path, self.lines, **self.meta)
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't save lyrics", f"Couldn't write the .lrc file:\n{exc}")
            return
        self.saved_path = path
        self._dirty = False
        self.saved.emit(path)
        self.accept()

    def reject(self) -> None:
        if self._dirty and self.lines and not self._confirm(
            "Discard changes?", "Close without saving your lyric changes?"
        ):
            return
        super().reject()

    def done(self, result: int) -> None:
        for signal, slot in (
            (self.player.positionChanged, self._on_position),
            (self.player.playbackStateChanged, self._on_state),
            (self.player.trackChanged, self._on_track_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)
