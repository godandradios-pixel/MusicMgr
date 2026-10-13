"""The tag editor dialogs (2026-10-06).

James: "Tag editor with MusicBrainz lookup. Fix tags in the files
themselves, plus a 'look up this album' button. Better tags would also
improve chart matching."

`TagEditorDialog` - "Edit tags…" on an album page, or on songs picked in
Title Details (one or many; right-click or the button above the table).
The top half holds the fields an album shares - album, album artist,
year, genre, label and numbers, compilation - and a change there goes to
every song. A field the songs disagree on shows "Different on each
song" and is left alone unless something is typed into it. The table
below holds each song's own disc, number, title, artist and comment.
Anything changed is tinted, and only changed fields are written.

"Look up on MusicBrainz…" (`MusicBrainzDialog`) searches for the album,
lists the matching releases (pressings are told apart by year, country,
format, label and track count) and, once one is picked, fills the boxes
in from it - songs are paired with the release's tracks by title, then
number and length. Nothing is written until Save, so a lookup can be
looked over, corrected by hand, or cancelled. The genre is only taken
from MusicBrainz when asked ("Also set the genre"): MusicBrainz's
community genres are often not the ones the jukebox and charts are
organised by. "Use its cover art" puts the release's front cover from
the Cover Art Archive on the album.

Save writes the files on a worker thread (services/tag_editor.py) and the
library follows: ratings, plays, playlists, the jukebox card and chart
matches stay with each song, even when a fixed album title files it
under a different album.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...services import musicbrainz as mbz
from ...services import tag_editor as te
from ...services import tag_writer as tw
from ..theme import COLORS
from .common import TouchButton, TouchList, dim_label

#: the album-wide fields at the top of the editor, in order
ALBUM_FIELDS = (
    ("album", "Album"),
    ("album_artist", "Album artist"),
    ("date", "Year / date"),
    ("genre", "Genre"),
    ("label", "Label"),
    ("catalog_number", "Catalog #"),
    ("barcode", "Barcode"),
    ("country", "Country"),
)

#: the per-song columns of the table
COL_DISC, COL_TRACK, COL_TITLE, COL_ARTIST, COL_COMMENT, COL_FILE = range(6)
ROW_FIELDS = {COL_DISC: "disc", COL_TRACK: "track", COL_TITLE: "title",
              COL_ARTIST: "artist", COL_COMMENT: "comment"}
HEADERS = ["Disc", "#", "Title", "Artist", "Comment", "File"]

MIXED = "Different on each song"

CHANGED_BG = QColor(165, 107, 52, 70)  # jukebox_key_hi, faint

#: an always-visible box for the checkboxes (the platform's own indicator
#: barely shows on the dark theme - the same fix as Title Details' "Videos
#: only")
CHECKBOX_QSS = f"""
QCheckBox {{ color: {COLORS['text']}; spacing: 8px; }}
QCheckBox::indicator {{ width: 20px; height: 20px; border: 2px solid {COLORS['text_dim']};
    border-radius: 4px; background: {COLORS['surface_alt']}; }}
QCheckBox::indicator:hover {{ border-color: {COLORS['jukebox_key_hi']}; }}
QCheckBox::indicator:checked {{ background: {COLORS['jukebox_key_hi']};
    border-color: {COLORS['jukebox_key_hi']}; }}
QCheckBox::indicator:indeterminate {{ background: {COLORS['surface_hi']};
    border-color: {COLORS['jukebox_key_hi']}; }}
"""


def _check(text: str) -> QCheckBox:
    box = QCheckBox(text)
    box.setStyleSheet(CHECKBOX_QSS)
    return box


def _note(text: str) -> QLabel:
    label = dim_label(text)
    label.setWordWrap(True)
    return label


# --------------------------------------------------------------------------
# worker threads
# --------------------------------------------------------------------------


class MusicBrainzSearchThread(QThread):
    finished_with = Signal(object, str)  # list[ReleaseSummary], error

    def __init__(self, artist: str, album: str, track_count: Optional[int], parent=None) -> None:
        super().__init__(parent)
        self.artist, self.album, self.track_count = artist, album, track_count

    def run(self) -> None:  # pragma: no cover - network
        try:
            self.finished_with.emit(
                mbz.search_releases(self.artist, self.album, self.track_count), "")
        except mbz.MusicBrainzError as exc:
            self.finished_with.emit([], str(exc))
        except Exception as exc:
            self.finished_with.emit([], f"Something went wrong ({exc}).")


class MusicBrainzReleaseThread(QThread):
    finished_with = Signal(object, object, str)  # MBRelease | None, cover bytes | None, error

    def __init__(self, release_id: str, want_cover: bool, parent=None) -> None:
        super().__init__(parent)
        self.release_id, self.want_cover = release_id, want_cover

    def run(self) -> None:  # pragma: no cover - network
        try:
            rel = mbz.get_release(self.release_id)
            cover = None
            if self.want_cover and rel.has_front_cover:
                cover = mbz.fetch_front_cover(self.release_id)
            self.finished_with.emit(rel, cover, "")
        except mbz.MusicBrainzError as exc:
            self.finished_with.emit(None, None, str(exc))
        except Exception as exc:
            self.finished_with.emit(None, None, f"Something went wrong ({exc}).")


class TagSaveThread(QThread):
    progress = Signal(int, int, str)
    finished_with = Signal(object, str)  # SaveResult | None, error

    def __init__(self, edits: dict, cover: Optional[bytes], parent=None) -> None:
        super().__init__(parent)
        self.edits, self.cover = edits, cover

    def run(self) -> None:  # pragma: no cover - exercised interactively
        try:
            result = te.save_edits(
                self.edits, progress=lambda d, t, n: self.progress.emit(d, t, n),
                should_stop=self.isInterruptionRequested, cover=self.cover)
            self.finished_with.emit(result, "")
        except Exception as exc:
            self.finished_with.emit(None, str(exc) or type(exc).__name__)


# --------------------------------------------------------------------------
# MusicBrainz picker
# --------------------------------------------------------------------------


class MusicBrainzDialog(QDialog):
    """Search MusicBrainz for an album and pick the release that matches.
    After Ok: `release` (an MBRelease with its tracks), `cover` (bytes or
    None) and `include_genre`."""

    #: tests switch the network off and feed results in by hand
    auto_search = True

    def __init__(self, artist: str, album: str, track_count: Optional[int] = None,
                 has_cover: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Look up on MusicBrainz")
        self.setMinimumSize(820, 600)
        self.track_count = track_count
        self.release: Optional[mbz.MBRelease] = None
        self.cover: Optional[bytes] = None
        self._results: list[mbz.ReleaseSummary] = []
        self._search_thread: Optional[QThread] = None
        self._release_thread: Optional[QThread] = None

        layout = QVBoxLayout(self)
        head = QLabel("Find this album on MusicBrainz")
        head.setObjectName("Title")
        layout.addWidget(head)
        layout.addWidget(_note(
            "MusicBrainz lists each pressing of an album separately - pick the one that "
            "matches your copy. Nothing is saved until you press Save in the editor."))

        row = QHBoxLayout()
        self.artist_edit = QLineEdit(artist)
        self.artist_edit.setPlaceholderText("Artist")
        self.album_edit = QLineEdit(album)
        self.album_edit.setPlaceholderText("Album")
        self.search_btn = TouchButton("Search")
        self.search_btn.clicked.connect(self.search)
        self.artist_edit.returnPressed.connect(self.search)
        self.album_edit.returnPressed.connect(self.search)
        row.addWidget(self.artist_edit, 2)
        row.addWidget(self.album_edit, 3)
        row.addWidget(self.search_btn)
        layout.addLayout(row)

        self.status = dim_label("")
        layout.addWidget(self.status)
        self.list = TouchList()
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.list.itemActivatedPayload.connect(lambda _p: self.use_selected())
        layout.addWidget(self.list, 1)

        opts = QHBoxLayout()
        self.genre_check = _check("Also set the genre from MusicBrainz")
        self.genre_check.setToolTip(
            "MusicBrainz's genres are voted on by its users and may not be the ones "
            "your jukebox and charts use - left off, the genre stays as it is")
        self.cover_check = _check("Use its cover art")
        self.cover_check.setToolTip("The release's front cover from the Cover Art Archive")
        self.cover_check.setChecked(not has_cover)
        opts.addWidget(self.genre_check)
        opts.addWidget(self.cover_check)
        opts.addStretch(1)
        layout.addLayout(opts)

        buttons = QHBoxLayout()
        self.use_btn = TouchButton("Use this release", primary=True)
        self.use_btn.setObjectName("PrimaryWarm")
        self.use_btn.clicked.connect(self.use_selected)
        cancel = TouchButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.use_btn)
        layout.addLayout(buttons)
        self._sync()
        if self.auto_search and (artist or album):
            self.search()

    @property
    def include_genre(self) -> bool:
        return self.genre_check.isChecked()

    def _sync(self) -> None:
        busy = self._release_thread is not None
        self.use_btn.setEnabled(bool(self.list.current_payload()) and not busy)
        self.search_btn.setEnabled(self._search_thread is None)

    def search(self) -> None:
        if self._search_thread is not None:
            return
        artist, album = self.artist_edit.text().strip(), self.album_edit.text().strip()
        if not artist and not album:
            self.status.setText("Type an album title (and artist) to search for.")
            return
        self.status.setText("Searching MusicBrainz…")
        self.list.set_rows([])
        thread = MusicBrainzSearchThread(artist, album, self.track_count, self)
        thread.finished_with.connect(self._on_search_done)
        self._search_thread = thread
        self._sync()
        if self.auto_search:
            thread.start()

    def _on_search_done(self, results, error: str) -> None:
        if self.sender() is not self._search_thread:
            return
        self._search_thread = None
        self.show_results(results, error)

    def show_results(self, results: Sequence[mbz.ReleaseSummary], error: str = "") -> None:
        self._results = list(results)
        if error:
            self.status.setText(error)
        elif not self._results:
            self.status.setText("Nothing found. Try fewer words, or check the spelling.")
        else:
            self.status.setText(f"{len(self._results)} release{'s' if len(self._results) != 1 else ''}"
                                " - best match first")
        rows = []
        for i, r in enumerate(self._results):
            title = r.title + (f" ({r.disambiguation})" if r.disambiguation else "")
            fits = self.track_count and r.track_count == self.track_count
            rows.append({
                "primary": f"{title} — {r.artist}",
                "secondary": " · ".join(x for x in (r.describe(), r.status if r.status != "Official"
                                                    else "", r.kind) if x),
                "trail": "✓ same track count" if fits else "",
                "index": i,
            })
        self.list.set_rows(rows)
        if rows:
            self.list.setCurrentRow(0)
        self._sync()

    def use_selected(self) -> None:
        payload = self.list.current_payload() or {}
        i = payload.get("index")
        if i is None or i >= len(self._results) or self._release_thread is not None:
            return
        self.status.setText("Fetching the track list…")
        thread = MusicBrainzReleaseThread(self._results[i].id, self.cover_check.isChecked(), self)
        thread.finished_with.connect(self._on_release_done)
        self._release_thread = thread
        self._sync()
        thread.start()

    def _on_release_done(self, release, cover, error: str) -> None:
        if self.sender() is not self._release_thread:
            return
        self._release_thread = None
        self._sync()
        if release is None:
            self.status.setText(error or "Couldn't fetch that release.")
            return
        self.choose(release, cover)

    def choose(self, release: mbz.MBRelease, cover: Optional[bytes] = None) -> None:
        """The release to use (tests call this directly)."""
        self.release = release
        self.cover = cover
        self.accept()

    def done(self, result: int) -> None:  # noqa: D102
        for thread in (self._search_thread, self._release_thread):
            if thread is not None and thread.isRunning():
                try:
                    thread.finished_with.disconnect()
                except (RuntimeError, TypeError):
                    pass
                thread.wait(200)
        super().done(result)


# --------------------------------------------------------------------------
# the editor
# --------------------------------------------------------------------------


#: the most songs one editor takes - each song's file is read when it
#: opens, so a whole-library selection would sit there for minutes
MAX_SONGS = 500


class TagEditorDialog(QDialog):
    """Edit the tags of one or more songs. `saved` fires with the
    SaveResult once the files are written and the library updated, before
    `ctx.libraryChanged` - so an album page can switch to the album its
    songs now belong to before the library reloads."""

    saved = Signal(object)

    #: tests set this False to save on the calling thread
    use_thread = True
    #: tests replace the lookup dialog
    lookup_dialog_class = MusicBrainzDialog

    def __init__(self, ctx, track_ids: Sequence[int], parent=None, has_cover: bool = True) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.has_cover = has_cover
        with ctx.session() as s:
            self.rows: list[te.EditRow] = te.load_rows(s, list(track_ids))
        self._extra: dict[int, tw.TagValues] = {}
        self._cover: Optional[bytes] = None
        self._release_note = ""
        self._thread: Optional[TagSaveThread] = None
        self._loading = True
        self.result_: Optional[te.SaveResult] = None

        albums = {r.values.album for r in self.rows}
        if len(self.rows) == 1:
            heading = f"Edit tags — {self.rows[0].values.title or self.rows[0].file_name}"
        elif len(albums) == 1 and next(iter(albums)):
            heading = f"Edit tags — {next(iter(albums))}"
        else:
            heading = f"Edit tags — {len(self.rows)} songs"
        self.setWindowTitle(heading)
        self.setMinimumSize(1000, 680)

        layout = QVBoxLayout(self)
        title = QLabel(heading)
        title.setObjectName("Title")
        layout.addWidget(title)
        layout.addWidget(_note(
            "Changes are written into the music files themselves, so other players and "
            "your other PCs see them too. Ratings, plays, playlists, the jukebox and chart "
            "matches stay with each song."))

        # -- album-wide fields ---------------------------------------------
        # three rows: album and album artist wide, then the rest four and
        # two across, so the song table keeps most of the height
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.album_edits: dict[str, QLineEdit] = {}
        self.album_labels: dict[str, QLabel] = {}
        self._album_orig: dict[str, Optional[str]] = {}
        editable = [r for r in self.rows if r.editable]
        places = {
            "album": (0, 0, 3), "album_artist": (0, 4, 3),
            "date": (1, 0, 1), "genre": (1, 2, 1), "label": (1, 4, 1), "country": (1, 6, 1),
            "catalog_number": (2, 0, 1), "barcode": (2, 2, 1),
        }
        for name, label in ALBUM_FIELDS:
            vals = {getattr(r.values, name) or "" for r in editable}
            common = next(iter(vals)) if len(vals) == 1 else None
            self._album_orig[name] = common
            edit = QLineEdit(common or "")
            if common is None and editable:
                edit.setPlaceholderText(MIXED)
                edit.setToolTip("The songs don't agree on this - type to set it on all of them")
            edit.textChanged.connect(self._sync)
            self.album_edits[name] = edit
            lab = QLabel(label)
            self.album_labels[name] = lab
            r, c, span = places[name]
            grid.addWidget(lab, r, c)
            grid.addWidget(edit, r, c + 1, 1, span)
        comp_vals = {r.values.compilation or "" for r in editable}
        self.compilation_check = _check("Compilation (various artists)")
        self.compilation_check.setTristate(len(comp_vals) > 1)
        if len(comp_vals) > 1:
            self.compilation_check.setCheckState(Qt.PartiallyChecked)
        else:
            self.compilation_check.setChecked(comp_vals == {"1"})
        self._comp_orig = self.compilation_check.checkState()
        self.compilation_check.stateChanged.connect(self._sync)
        grid.addWidget(self.compilation_check, 2, 4, 1, 4)
        for col in (1, 3, 5, 7):
            grid.setColumnStretch(col, 1)
        layout.addLayout(grid)

        # -- per-song table --------------------------------------------------
        self.table = QTableWidget(len(self.rows), len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed | QAbstractItemView.SelectedClicked)
        head = self.table.horizontalHeader()
        for col, mode in ((COL_DISC, QHeaderView.Fixed), (COL_TRACK, QHeaderView.Fixed),
                          (COL_TITLE, QHeaderView.Stretch), (COL_ARTIST, QHeaderView.Stretch),
                          (COL_COMMENT, QHeaderView.Stretch), (COL_FILE, QHeaderView.Interactive)):
            head.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(COL_DISC, 60)
        self.table.setColumnWidth(COL_TRACK, 60)
        self.table.setColumnWidth(COL_FILE, 220)
        dim = QBrush(QColor(COLORS["text_dim"]))
        for i, row in enumerate(self.rows):
            for col, name in ROW_FIELDS.items():
                item = QTableWidgetItem(getattr(row.values, name) or "")
                item.setData(Qt.UserRole, getattr(row.values, name) or "")
                if not row.editable:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setForeground(dim)
                    item.setToolTip(row.problem)
                self.table.setItem(i, col, item)
            f = QTableWidgetItem(row.file_name or "—")
            f.setFlags(f.flags() & ~Qt.ItemIsEditable)
            f.setForeground(dim)
            f.setToolTip(row.problem or (row.path or ""))
            if not row.editable:
                f.setText(f"⚠ {row.problem}")
            self.table.setItem(i, COL_FILE, f)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        # -- status and buttons ------------------------------------------------
        self.status = dim_label("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("ProgressWarm")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.lookup_btn = TouchButton("Look up on MusicBrainz…")
        self.lookup_btn.clicked.connect(self.look_up)
        self.lookup_btn.setEnabled(bool(editable))
        buttons.addWidget(self.lookup_btn)
        buttons.addStretch(1)
        self.cancel_btn = TouchButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = TouchButton("Save", primary=True)
        self.save_btn.setObjectName("PrimaryWarm")
        self.save_btn.clicked.connect(self.save)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.save_btn)
        layout.addLayout(buttons)

        self._loading = False
        self._sync()
        if not editable:
            self.status.setText(self.rows[0].problem if len(self.rows) == 1 and self.rows
                                else "None of these songs can be edited.")

    # -- state ------------------------------------------------------------------

    def _album_value(self, name: str) -> Optional[str]:
        """The album field's edit: None = leave alone."""
        text = self.album_edits[name].text().strip()
        orig = self._album_orig[name]
        if orig is None:  # mixed: only an entry counts
            return text or None
        return None if text == orig else text

    def _compilation_value(self) -> Optional[str]:
        state = self.compilation_check.checkState()
        if state == self._comp_orig or state == Qt.PartiallyChecked:
            return None
        return "1" if state == Qt.Checked else ""

    def edits(self) -> dict[int, tw.TagValues]:
        """track id -> just the fields that change, for every editable song."""
        out: dict[int, tw.TagValues] = {}
        album_changes = {name: self._album_value(name) for name, _ in ALBUM_FIELDS}
        comp = self._compilation_value()
        for i, row in enumerate(self.rows):
            if not row.editable:
                continue
            final = tw.merge_values(row.values, self._extra.get(row.track_id, tw.TagValues()))
            for name, val in album_changes.items():
                if val is not None:
                    setattr(final, name, val)
            if comp is not None:
                final.compilation = comp
            for col, name in ROW_FIELDS.items():
                item = self.table.item(i, col)
                setattr(final, name, item.text().strip())
            diff = tw.diff_values(row.values, final)
            if not diff.is_empty():
                out[row.track_id] = diff
        return out

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        orig = item.data(Qt.UserRole) or ""
        item.setBackground(QBrush(CHANGED_BG) if item.text().strip() != orig else QBrush())
        self._sync()

    def _sync(self) -> None:
        if self._loading:
            return
        for name, edit in self.album_edits.items():
            changed = self._album_value(name) is not None
            edit.setStyleSheet(
                "QLineEdit { background: rgba(165, 107, 52, 70); }" if changed else "")
            self.album_labels[name].setStyleSheet(
                f"color: {COLORS['jukebox_key_hi']}; font-weight: 600;" if changed else "")
        n = len(self.edits())
        busy = self._thread is not None
        self.save_btn.setEnabled(n > 0 and not busy)
        self.save_btn.setText(f"Save {n} song{'s' if n != 1 else ''}" if n else "Save")
        self.lookup_btn.setEnabled(not busy and any(r.editable for r in self.rows))
        if not busy and not self.progress.isVisible():
            parts = []
            if self._release_note:
                parts.append(self._release_note)
            if self._cover:
                parts.append("The release's cover art will be used.")
            self.status.setText(" ".join(parts))

    # -- MusicBrainz -----------------------------------------------------------

    def _lookup_terms(self) -> tuple[str, str]:
        artist = self.album_edits["album_artist"].text().strip()
        album = self.album_edits["album"].text().strip()
        editable = [r for r in self.rows if r.editable]
        if not artist and editable:
            artist = editable[0].values.album_artist or editable[0].values.artist or ""
        if not album and editable:
            album = editable[0].values.album or ""
        return artist, album

    def look_up(self) -> None:
        artist, album = self._lookup_terms()
        count = sum(1 for r in self.rows if r.editable)
        dialog = self.lookup_dialog_class(artist, album, count, self.has_cover, self)
        accepted = dialog.exec() == QDialog.Accepted and dialog.release is not None
        release, genre, cover = dialog.release, dialog.include_genre, dialog.cover
        if hasattr(dialog, "deleteLater"):
            dialog.deleteLater()
        if accepted:
            self.apply_release(release, include_genre=genre, cover=cover)

    def _reset_to_files(self) -> None:
        """Every box back to what the files say (a new lookup starts over)."""
        self._extra = {}
        self._cover = None
        for name, edit in self.album_edits.items():
            edit.setText(self._album_orig[name] or "")
        self.compilation_check.setTristate(self._comp_orig == Qt.PartiallyChecked)
        self.compilation_check.setCheckState(self._comp_orig)
        for i, _row in enumerate(self.rows):
            for col in ROW_FIELDS:
                item = self.table.item(i, col)
                item.setText(item.data(Qt.UserRole) or "")

    def apply_release(self, release: mbz.MBRelease, include_genre: bool = False,
                      cover: Optional[bytes] = None) -> int:
        """Fill the boxes in from a MusicBrainz release, starting from the
        files' own tags. Returns how many songs were matched to one of its
        tracks. Songs that match nothing are left exactly as they are - the
        album's details only go onto the songs that matched."""
        self._reset_to_files()
        editable = [(i, r) for i, r in enumerate(self.rows) if r.editable]
        local = []
        for i, r in editable:
            disc = self.table.item(i, COL_DISC).text().strip()
            num = self.table.item(i, COL_TRACK).text().strip()
            local.append(mbz.LocalTrack(
                title=self.table.item(i, COL_TITLE).text().strip(),
                disc=int(disc) if disc.isdigit() else 1,
                track=int(num) if num.isdigit() else None,
                length_ms=r.duration_ms,
            ))
        matches = mbz.match_tracks(local, release)
        matched = 0
        album_vals: Optional[tw.TagValues] = None
        carried = [name for name, _ in ALBUM_FIELDS] + [
            "compilation", "track_total", "disc_total", "isrc", *tw.MB_FIELDS]
        for (i, row), j in zip(editable, matches):
            if j is None:
                continue
            matched += 1
            keep_side = not (row.values.track or "1").isdigit()
            vals = mbz.values_for(release, release.tracks[j], keep_side=keep_side,
                                  include_genre=include_genre)
            album_vals = album_vals or vals
            for col, name in ROW_FIELDS.items():
                new = getattr(vals, name)
                if new is not None and name != "comment":
                    self.table.item(i, col).setText(new)
            self._extra[row.track_id] = tw.TagValues(**{name: getattr(vals, name)
                                                        for name in carried})
        if album_vals is not None and matched == len(editable):
            # every song matched: show the album's details in the boxes too
            for name, _ in ALBUM_FIELDS:
                new = getattr(album_vals, name)
                if new is not None:
                    self.album_edits[name].setText(new)
            self.compilation_check.setTristate(False)
            self.compilation_check.setChecked(release.is_compilation)
        self._cover = cover if matched else None
        unmatched = len(editable) - matched
        desc = release.describe()
        if matched:
            self._release_note = (
                f"Filled in from MusicBrainz: {release.title} — {release.artist}"
                f"{' (' + desc + ')' if desc else ''}. {matched} of {len(editable)} songs matched"
                + (f"; {unmatched} left exactly as they were" if unmatched else "")
                + ". Look it over, then Save.")
        else:
            self._release_note = (f"None of these songs matched a track on {release.title} - "
                                  "nothing was filled in. Try another release.")
        self._sync()
        return matched

    # -- saving ------------------------------------------------------------------

    def save(self) -> None:
        edits = self.edits()
        if not edits or self._thread is not None:
            return
        self.save_btn.setEnabled(False)
        self.lookup_btn.setEnabled(False)
        # no stopping halfway: a song is written whole, and the album with it
        self.cancel_btn.setEnabled(False)
        self.table.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Saving…")
        if not self.use_thread:
            try:
                result = te.save_edits(edits, cover=self._cover)
                self._finish(result, "")
            except Exception as exc:
                self._finish(None, str(exc))
            return
        thread = TagSaveThread(edits, self._cover, self)
        thread.progress.connect(self._on_progress)
        thread.finished_with.connect(self._finish)
        self._thread = thread
        thread.start()

    def _on_progress(self, done: int, total: int, text: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.status.setText(text)

    def _finish(self, result: Optional[te.SaveResult], error: str) -> None:
        if self._thread is not None:
            self._thread.wait()
        self._thread = None
        self.progress.setVisible(False)
        self.table.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        if result is None:
            self.status.setText(f"Saving failed: {error}")
            self._sync()
            return
        self.result_ = result
        if result.track_ids or result.moves:
            try:
                self.ctx.player.refresh_tracks(result.track_ids, result.moves)
            except Exception:  # pragma: no cover - the player is optional here
                pass
        self.saved.emit(result)
        self.ctx.libraryChanged.emit()
        self.ctx.notify(result.summary())
        if result.failed:
            listed = "\n".join(f"• {name}: {why}" for name, why in result.failed[:12])
            more = f"\n…and {len(result.failed) - 12} more" if len(result.failed) > 12 else ""
            self._warn(f"{len(result.failed)} file{'s' if len(result.failed) != 1 else ''} "
                       f"couldn't be written:\n\n{listed}{more}\n\n"
                       "A song that's playing may be locked - try again after it ends.")
        self.accept()

    def _warn(self, text: str) -> None:  # pragma: no cover - modal box
        QMessageBox.warning(self, "Some files weren't changed", text)

    def reject(self) -> None:  # noqa: D102
        if self._thread is not None:
            return  # Esc / the window's close box while saving: let it finish
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: D102
        if self._thread is not None:
            event.ignore()
            return
        super().closeEvent(event)

