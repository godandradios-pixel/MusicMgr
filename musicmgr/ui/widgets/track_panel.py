"""Flat list of every track in the library.

Default order is artist → album → track number, so an artist's records read in
sleeve order rather than alphabetically by song title.

Grouping uses the *album* artist while each row displays the *track* artist.
Otherwise a single "Artist feat. Guest" credit would sort away from the rest of
its album and split the record in half.

Rows are built from one flat query into plain dataclasses rather than ORM
objects: a 5,000-track library would otherwise mean 5,000 live instances plus
their relationships held open behind the view.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import func, select

from ...db.models import MediaFile, Release, Track
from ...services.library import format_duration
from ...services.player import QueueItem
from ..theme import COLORS
from .common import ChipButton, TouchButton, TouchList, dim_label
from .cover_grid import JumpBar


@dataclass
class TrackRow:
    track_id: int
    title: str
    artist: str
    album: str
    position: str
    duration_ms: int
    path: Optional[str]
    cover_path: Optional[str]
    #: album artist, lower-cased - what the artist grouping and jump bar use
    artist_key: str
    album_key: str
    title_key: str
    #: sleeve ordering, used as the tiebreak inside an album
    disc_no: int = 1
    track_no: int = 9999

    @property
    def playable(self) -> bool:
        return bool(self.path)

    def to_queue_item(self) -> QueueItem:
        return QueueItem(
            track_id=self.track_id,
            title=self.title,
            artist=self.artist,
            album=self.album,
            path=self.path or "",
            duration_ms=self.duration_ms or 0,
            cover_path=self.cover_path,
            position=self.position or None,
        )


SORTS = (
    ("Artist", "artist", "artist_key"),
    ("Album", "album", "album_key"),
    ("Title", "title", "title_key"),
    ("Recently added", "added", None),
)


class TrackListPanel(QWidget):
    """Sort chips + play actions + the full track list + an A-Z jump bar."""

    trackChosen = Signal(int)

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._rows: list[TrackRow] = []
        self._ordered: list[TrackRow] = []
        self._sort = SORTS[0]

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        sort_label = QLabel("Sort")
        sort_label.setObjectName("Dim")
        bar.addWidget(sort_label)
        self._sort_group = QButtonGroup(self)
        self._sort_group.setExclusive(True)
        for idx, (label, _key, _alpha) in enumerate(SORTS):
            chip = ChipButton(label)
            chip.setProperty("sort_index", idx)
            self._sort_group.addButton(chip)
            bar.addWidget(chip)
            if idx == 0:
                chip.setChecked(True)
        self._sort_group.buttonClicked.connect(self._on_sort_changed)

        bar.addSpacing(16)
        play = TouchButton("Play all", primary=True)
        play.clicked.connect(lambda: self.play_from(0))
        shuffle = TouchButton("Shuffle all")
        shuffle.clicked.connect(self.shuffle_all)
        bar.addWidget(play)
        bar.addWidget(shuffle)
        bar.addStretch(1)
        self.count_label = dim_label("")
        bar.addWidget(self.count_label)
        root.addLayout(bar)

        body = QHBoxLayout()
        body.setSpacing(6)
        self.list = TouchList()
        self.list.itemActivatedPayload.connect(self._on_row_tapped)
        body.addWidget(self.list, 1)

        self.jump = JumpBar()
        self.jump.letterPicked.connect(self.scroll_to_letter)
        body.addWidget(self.jump)
        root.addLayout(body, 1)

    # -- data -----------------------------------------------------------------

    def load(self) -> None:
        """One query for the whole library, ordered artist → album → track."""
        rows: list[TrackRow] = []
        with self.ctx.session() as session:
            # the file a track would actually play: lowest-id present file
            playable = (
                select(
                    MediaFile.track_id.label("track_id"),
                    func.min(MediaFile.id).label("file_id"),
                )
                .where(MediaFile.is_missing.is_(False))
                .group_by(MediaFile.track_id)
                .subquery()
            )
            stmt = (
                select(
                    Track.id,
                    Track.title,
                    Track.title_key,
                    Track.artist_display,
                    Track.position,
                    Track.disc_no,
                    Track.track_no,
                    Track.duration_ms,
                    Release.title,
                    Release.title_key,
                    Release.artist_display,
                    Release.cover_path,
                    MediaFile.path,
                )
                .join(Release, Release.id == Track.release_id)
                .outerjoin(playable, playable.c.track_id == Track.id)
                .outerjoin(MediaFile, MediaFile.id == playable.c.file_id)
                .order_by(Track.added_at.desc(), Track.id.desc())
            )
            for r in session.execute(stmt):
                (
                    track_id, title, title_key, track_artist, position, disc_no,
                    track_no, duration_ms, album, album_key, album_artist,
                    cover_path, path,
                ) = r
                artist = track_artist or album_artist or ""
                grouping = (album_artist or track_artist or "").lower()
                rows.append(
                    TrackRow(
                        track_id=track_id,
                        title=title,
                        artist=artist,
                        album=album or "",
                        position=position or (str(track_no) if track_no else ""),
                        duration_ms=duration_ms or 0,
                        path=path,
                        cover_path=cover_path,
                        artist_key=grouping,
                        album_key=(album_key or (album or "").lower()),
                        title_key=title_key or title.lower(),
                        disc_no=disc_no or 1,
                        track_no=track_no if track_no is not None else 9999,
                    )
                )
        self._rows = rows
        self._rebuild()

    def _on_sort_changed(self, button) -> None:
        self._sort = SORTS[button.property("sort_index")]
        self._rebuild()

    def _sorted(self) -> list[TrackRow]:
        key = self._sort[1]
        if key == "artist":
            return sorted(
                self._rows,
                key=lambda r: (r.artist_key, r.album_key, r.disc_no, r.track_no),
            )
        if key == "album":
            return sorted(
                self._rows,
                key=lambda r: (r.album_key, r.disc_no, r.track_no),
            )
        if key == "title":
            return sorted(self._rows, key=lambda r: (r.title_key, r.artist_key))
        return list(self._rows)  # the query already returns newest first

    def _rebuild(self) -> None:
        self._ordered = self._sorted()
        payloads = []
        for row in self._ordered:
            secondary = " · ".join(x for x in (row.artist, row.album) if x)
            payloads.append({
                "lead": row.position,
                "primary": row.title,
                "secondary": secondary,
                "trail": format_duration(row.duration_ms),
                "color": COLORS["text"] if row.playable else COLORS["text_dim"],
                "key": row.track_id,
            })
        self.list.set_rows(payloads)

        total_ms = sum(r.duration_ms or 0 for r in self._ordered)
        missing = sum(1 for r in self._ordered if not r.playable)
        summary = f"{len(self._ordered):,} tracks · {format_duration(total_ms)}"
        if missing:
            summary += f" · {missing} unplayable"
        self.count_label.setText(summary)

        alpha_field = self._sort[2]
        self.jump.setVisible(alpha_field is not None)
        if alpha_field is not None:
            self.jump.set_available({self._letter_of(r) for r in self._ordered})

    def _letter_of(self, row: TrackRow) -> str:
        field = self._sort[2] or "title_key"
        source = str(getattr(row, field, "") or "").strip()
        first = source[:1].upper()
        return first if "A" <= first <= "Z" else "#"

    def scroll_to_letter(self, letter: str) -> None:
        for index, row in enumerate(self._ordered):
            if self._letter_of(row) == letter:
                item = self.list.item(index)
                if item is not None:
                    self.list.scrollToItem(item, QAbstractItemView.PositionAtTop)
                    self.list.setCurrentItem(item)
                return

    # -- playback --------------------------------------------------------------

    def _on_row_tapped(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        for index, row in enumerate(self._ordered):
            if row.track_id == payload.get("key"):
                self.play_from(index)
                return

    def play_from(self, index: int) -> None:
        items = [r.to_queue_item() for r in self._ordered if r.playable]
        if not items:
            self.ctx.notify("No playable files in this list")
            return
        # the tapped row may sit behind unplayable ones, so map by track id
        target = self._ordered[index].track_id if 0 <= index < len(self._ordered) else None
        start = next((i for i, it in enumerate(items) if it.track_id == target), 0)
        self.ctx.player.set_shuffle(False)
        self.ctx.player.play_tracks(items, start=start, source="library:tracks")

    def shuffle_all(self) -> None:
        items = [r.to_queue_item() for r in self._ordered if r.playable]
        if not items:
            self.ctx.notify("No playable files in this list")
            return
        self.ctx.player.set_shuffle(True)
        self.ctx.player.play_tracks(items, start=0, source="library:tracks")
