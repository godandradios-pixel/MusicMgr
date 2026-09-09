"""'Album and Tracks': the library as a scrolling document.

Artist heading, then each of their releases as a block - cover art on the left,
title / year / genre / running time, and the track list laid out in two columns
with disc breaks where a release spans more than one disc.

Everything is drawn by a delegate rather than built from widgets. 300-odd
albums and 5,000 track labels as real QWidgets would cost hundreds of megabytes
and take seconds to lay out; as painted items only the visible rows cost
anything. The same geometry function backs both painting and hit-testing, so a
tap always lands on the row it looks like it landed on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional, Sequence

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScroller,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ...config import TOUCH
from ...services.library import format_duration
from ..theme import COLORS
from .common import cover_pixmap

ROLE_BLOCK = Qt.UserRole + 3

KIND_ARTIST = "artist"
KIND_ALBUM = "album"
#: an artist's matching videos, woven into the document as a search result
#: (2026-09-06) - see Block.videos and _paint_video_block. Never present in
#: the default, unfiltered document, same rule the two grids and Title
#: Details apply to their own video items.
KIND_VIDEO = "video"

# geometry
PAD_X = 24
PAD_Y = 20
ART = 190
GAP = 28
TITLE_H = 34
META_H = 26
DISC_H = 32
TRACK_H = 36
COL_GAP = 36
NUM_W = 54
DUR_W = 64
ARTIST_H = 64


@dataclass
class TrackEntry:
    track_id: int
    disc_no: int
    track_no: Optional[int]
    title: str
    duration_ms: int
    playable: bool


@dataclass
class VideoEntry:
    video_id: int
    title: str
    duration_ms: int


@dataclass
class Block:
    kind: str
    #: artist name for a heading; release title for an album; unused ("Videos")
    #: for a KIND_VIDEO block, which carries its content in `videos` instead
    title: str = ""
    release_id: Optional[int] = None
    year: Optional[int] = None
    genre: str = ""
    cover_path: Optional[str] = None
    duration_ms: int = 0
    tracks: list[TrackEntry] = field(default_factory=list)
    videos: list[VideoEntry] = field(default_factory=list)
    sort_letter: str = "#"

    @property
    def discs(self) -> list[tuple[int, list[TrackEntry]]]:
        groups: dict[int, list[TrackEntry]] = {}
        for track in self.tracks:
            groups.setdefault(track.disc_no, []).append(track)
        return sorted(groups.items())

    @property
    def multi_disc(self) -> bool:
        return len({t.disc_no for t in self.tracks}) > 1


def block_height(block: Block) -> int:
    if block.kind == KIND_ARTIST:
        return ARTIST_H
    if block.kind == KIND_VIDEO:
        rows = math.ceil(len(block.videos) / 2)
        return PAD_Y * 2 + TITLE_H + rows * TRACK_H
    content = TITLE_H + META_H + 8
    for _disc, tracks in block.discs:
        if block.multi_disc:
            content += DISC_H
        content += math.ceil(len(tracks) / 2) * TRACK_H
    return PAD_Y * 2 + max(ART, content)


def track_geometry(rect: QRect, block: Block) -> list[tuple[QRect, TrackEntry]]:
    """Where every track row sits inside `rect`. Used to paint and to hit-test."""
    if block.kind != KIND_ALBUM:
        return []
    text_x = rect.left() + PAD_X + ART + GAP
    avail = rect.right() - PAD_X - text_x
    col_w = max(160, (avail - COL_GAP) // 2)
    y = rect.top() + PAD_Y + TITLE_H + META_H + 8

    out: list[tuple[QRect, TrackEntry]] = []
    for _disc, tracks in block.discs:
        if block.multi_disc:
            y += DISC_H
        rows = math.ceil(len(tracks) / 2)
        for index, track in enumerate(tracks):
            column, row = divmod(index, rows)
            x = text_x + column * (col_w + COL_GAP)
            out.append((QRect(x, y + row * TRACK_H, col_w, TRACK_H), track))
        y += rows * TRACK_H
    return out


def video_geometry(rect: QRect, block: Block) -> list[tuple[QRect, VideoEntry]]:
    """Where every video row sits inside `rect` - the KIND_VIDEO counterpart
    to `track_geometry`. No cover art column here (a video block has no
    artwork of its own), so rows start right after the left padding instead
    of after ART + GAP."""
    if block.kind != KIND_VIDEO:
        return []
    text_x = rect.left() + PAD_X
    avail = rect.right() - PAD_X - text_x
    col_w = max(160, (avail - COL_GAP) // 2)
    y = rect.top() + PAD_Y + TITLE_H
    rows = math.ceil(len(block.videos) / 2)

    out: list[tuple[QRect, VideoEntry]] = []
    for index, video in enumerate(block.videos):
        column, row = divmod(index, rows)
        x = text_x + column * (col_w + COL_GAP)
        out.append((QRect(x, y + row * TRACK_H, col_w, TRACK_H), video))
    return out


def _without_videos(blocks: Sequence[Block]) -> list[Block]:
    """Default (unfiltered) view: video blocks only ever appear as search
    matches (see `AlbumTracksPanel.set_filter_text`'s docstring) - drop them,
    and drop an artist heading too if videos were the only thing under it, so
    a video-only artist doesn't leave a heading with nothing beneath it."""
    result: list[Block] = []
    pending_artist: Optional[Block] = None
    for block in blocks:
        if block.kind == KIND_ARTIST:
            pending_artist = block
            continue
        if block.kind == KIND_VIDEO:
            continue
        if pending_artist is not None:
            result.append(pending_artist)
            pending_artist = None
        result.append(block)
    return result


class AlbumTracksDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.width_hint = 1200
        self.hover_track: Optional[int] = None
        #: kept separate from hover_track - a video id and a track id are
        #: different tables' primary keys and can coincidentally collide
        self.hover_video: Optional[int] = None

    def sizeHint(self, option, index) -> QSize:
        block: Optional[Block] = index.data(ROLE_BLOCK)
        if block is None:
            return QSize(self.width_hint, TRACK_H)
        return QSize(self.width_hint, block_height(block))

    # -- painting ----------------------------------------------------------

    def paint(self, painter: QPainter, option, index) -> None:
        block: Optional[Block] = index.data(ROLE_BLOCK)
        if block is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if block.kind == KIND_ARTIST:
            self._paint_artist(painter, option.rect, block)
        elif block.kind == KIND_VIDEO:
            self._paint_video_block(painter, option.rect, block)
        else:
            self._paint_album(painter, option.rect, block)
        painter.restore()

    def _paint_artist(self, painter: QPainter, rect: QRect, block: Block) -> None:
        font = painter.font()
        font.setPixelSize(TOUCH["font_title"])
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(
            rect.adjusted(PAD_X, 0, -PAD_X, -8),
            Qt.AlignLeft | Qt.AlignBottom,
            block.title,
        )
        painter.setPen(QColor(COLORS["border"]))
        painter.drawLine(
            rect.left() + PAD_X, rect.bottom() - 2, rect.right() - PAD_X, rect.bottom() - 2
        )

    def _paint_album(self, painter: QPainter, rect: QRect, block: Block) -> None:
        art_x = rect.left() + PAD_X
        art_y = rect.top() + PAD_Y
        pix = cover_pixmap(block.cover_path, ART, block.title)
        clip = QPainterPath()
        clip.addRoundedRect(art_x, art_y, ART, ART, 6, 6)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(art_x, art_y, ART, ART, pix)
        painter.restore()

        text_x = art_x + ART + GAP
        right = rect.right() - PAD_X
        y = rect.top() + PAD_Y

        font = painter.font()
        font.setPixelSize(TOUCH["font_title"])
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        metrics = QFontMetrics(font)
        painter.drawText(
            text_x, y, right - text_x - 90, TITLE_H,
            Qt.AlignLeft | Qt.AlignVCenter,
            metrics.elidedText(block.title, Qt.ElideRight, right - text_x - 90),
        )

        font.setPixelSize(TOUCH["font_base"])
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text_dim"]))
        meta = " • ".join(
            x for x in (str(block.year) if block.year else "", block.genre) if x
        )
        painter.drawText(
            text_x, y + TITLE_H, right - text_x - 90, META_H,
            Qt.AlignLeft | Qt.AlignVCenter, meta,
        )
        if block.duration_ms:
            minutes = round(block.duration_ms / 60000)
            painter.drawText(
                right - 130, y + TITLE_H, 130, META_H,
                Qt.AlignRight | Qt.AlignVCenter, f"{minutes} mins",
            )

        # disc headings sit above their group; find their y from the geometry
        geometry = track_geometry(rect, block)
        if block.multi_disc:
            painter.setPen(QColor(COLORS["text_dim"]))
            disc_font = QFont(font)
            disc_font.setItalic(True)
            painter.setFont(disc_font)
            seen: set[int] = set()
            for row_rect, track in geometry:
                if track.disc_no in seen:
                    continue
                seen.add(track.disc_no)
                painter.drawText(
                    text_x, row_rect.top() - DISC_H, 200, DISC_H,
                    Qt.AlignLeft | Qt.AlignVCenter, f"Disc {track.disc_no}",
                )
            painter.setFont(font)

        for row_rect, track in geometry:
            hovered = self.hover_track == track.track_id
            if hovered:
                painter.setBrush(QColor(COLORS["surface_alt"]))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(row_rect.adjusted(-8, 2, 8, -2), 6, 6)

            colour = COLORS["text"] if track.playable else COLORS["text_dim"]
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(
                row_rect.left(), row_rect.top(), NUM_W - 14, row_rect.height(),
                Qt.AlignRight | Qt.AlignVCenter,
                str(track.track_no) if track.track_no else "",
            )

            title_x = row_rect.left() + NUM_W
            title_w = row_rect.width() - NUM_W - DUR_W
            painter.setPen(QColor(colour))
            metrics = QFontMetrics(painter.font())
            painter.drawText(
                title_x, row_rect.top(), title_w, row_rect.height(),
                Qt.AlignLeft | Qt.AlignVCenter,
                metrics.elidedText(track.title, Qt.ElideRight, title_w),
            )
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(
                row_rect.right() - DUR_W, row_rect.top(), DUR_W, row_rect.height(),
                Qt.AlignRight | Qt.AlignVCenter,
                format_duration(track.duration_ms),
            )

    def _paint_video_block(self, painter: QPainter, rect: QRect, block: Block) -> None:
        """An artist's matching videos - a search result, never part of the
        default document (see KIND_VIDEO). No cover art, no year/genre meta
        line, no disc grouping - just a "▶ Videos" heading over a plain
        title/duration list, the simplest shape that still fits this
        document's existing two-column row layout."""
        text_x = rect.left() + PAD_X
        right = rect.right() - PAD_X
        y = rect.top() + PAD_Y

        font = painter.font()
        font.setPixelSize(TOUCH["font_title"])
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(
            text_x, y, right - text_x, TITLE_H, Qt.AlignLeft | Qt.AlignVCenter, "▶ Videos",
        )

        font.setPixelSize(TOUCH["font_base"])
        font.setBold(False)
        painter.setFont(font)
        for row_rect, video in video_geometry(rect, block):
            hovered = self.hover_video == video.video_id
            if hovered:
                painter.setBrush(QColor(COLORS["surface_alt"]))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(row_rect.adjusted(-8, 2, 8, -2), 6, 6)

            title_w = row_rect.width() - DUR_W
            painter.setPen(QColor(COLORS["text"]))
            metrics = QFontMetrics(painter.font())
            painter.drawText(
                row_rect.left(), row_rect.top(), title_w, row_rect.height(),
                Qt.AlignLeft | Qt.AlignVCenter,
                metrics.elidedText(video.title, Qt.ElideRight, title_w),
            )
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(
                row_rect.right() - DUR_W, row_rect.top(), DUR_W, row_rect.height(),
                Qt.AlignRight | Qt.AlignVCenter,
                format_duration(video.duration_ms),
            )


class AlbumTracksPanel(QWidget):
    """The scrolling album/track document.

    Used to draw its own A-Z letter bar across the top; that's now the one
    shared bar under the Library search box instead (see `library.py`'s
    docstring and `cover_grid.py:JumpBar`) - LibraryView calls `jump_letters()`
    and `scroll_to_letter()` here the same way it does for the two grids,
    so all four presentations answer to the same two questions rather than
    each carrying its own letter-bar widget.

    `set_filter_text()` live-narrows the document the same way `CoverGrid.
    set_filter_text()` narrows a grid - see that method's docstring for the
    matching rules. `_raw_blocks` holds everything `load()` built; `_blocks`
    (inherited from before filtering existed - used by hit-testing, scrolling
    and the count line) is always whatever's currently on screen, i.e. the
    filtered subset when a filter is active.

    `load()` also builds a KIND_VIDEO block per artist for their matching
    videos (2026-09-06) - see that method and `_without_videos()`. They're
    stripped back out for the default, unfiltered document the same way the
    other three Library presentations hide their own video items until a
    search actually matches one.
    """

    trackChosen = Signal(int, int)   # release_id, track_id
    albumChosen = Signal(int)        # release_id
    videoChosen = Signal(int)        # video_id
    #: fires whenever the document is (re)built - LibraryView listens so it
    #: can refresh the shared A-Z bar's available letters
    updated = Signal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._blocks: list[Block] = []
        self._raw_blocks: list[Block] = []
        self._filter_text: str = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(PAD_X, 0, PAD_X, 0)
        self.heading = QLabel("Album and Tracks")
        self.heading.setObjectName("Crumb")
        head.addWidget(self.heading)
        head.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Dim")
        head.addWidget(self.count_label)
        root.addLayout(head)

        self.list = QListWidget()
        self.list.setObjectName("Document")
        self.list.setUniformItemSizes(False)
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.delegate = AlbumTracksDelegate(self)
        self.list.setItemDelegate(self.delegate)
        QScroller.grabGesture(self.list.viewport(), QScroller.LeftMouseButtonGesture)
        self.list.viewport().installEventFilter(self)
        root.addWidget(self.list, 1)

    # -- data ---------------------------------------------------------------

    def set_blocks(self, blocks: Sequence[Block]) -> None:
        self._blocks = list(blocks)
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        for block in self._blocks:
            item = QListWidgetItem()
            item.setData(ROLE_BLOCK, block)
            item.setSizeHint(QSize(self.delegate.width_hint, block_height(block)))
            self.list.addItem(item)
        self.list.setUpdatesEnabled(True)

        albums = sum(1 for b in self._blocks if b.kind == KIND_ALBUM)
        tracks = sum(len(b.tracks) for b in self._blocks)
        videos_count = sum(len(b.videos) for b in self._blocks if b.kind == KIND_VIDEO)
        text = f"{albums:,} albums · {tracks:,} tracks"
        if videos_count:
            text += f" · {videos_count:,} videos"
        self.count_label.setText(text)
        self.updated.emit()

    def jump_letters(self) -> set[str]:
        """Letters worth showing on the shared A-Z bar right now. Unlike the
        two grids, this is never None - artist headings are always in
        alphabetical order here (`load()`'s `order` argument only changes how
        a single artist's own releases are stacked), so jumping always makes
        sense."""
        return {b.sort_letter for b in self._blocks if b.kind == KIND_ARTIST}

    def set_filter_text(self, text: str) -> None:
        """Live-narrow the document to whatever matches an album title or a
        track title (James, 2026-09-06: "when on tracks, it can search by
        album or track") - an artist heading itself is no longer a search
        target; typing an artist's name here now finds nothing unless it
        also happens to be an album or track title. A case-insensitive
        substring rule, same as everywhere else in Library.

        An album match keeps that whole block, tracks and all - typing an
        album name is asking for everything on it, not a hunt through its
        tracks one at a time. A match on neither keeps the album only for
        the tracks that themselves match, so a track search still shows
        which album it lives on without flooding the result with every
        other track on it. Emptying the box restores the full document
        exactly as `load()` built it.
        """
        text = text.strip().lower()
        if text == self._filter_text:
            return
        self._filter_text = text
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self._filter_text
        if not needle:
            self.set_blocks(_without_videos(self._raw_blocks))
            return

        filtered: list[Block] = []
        pending_artist: Optional[Block] = None
        for block in self._raw_blocks:
            if block.kind == KIND_ARTIST:
                # the artist heading itself is no longer a search target
                # (James, 2026-09-06) - it only makes it into `filtered`
                # below once one of its albums/tracks/videos actually
                # matches, same as before, it just can no longer be pulled
                # in by a match on its own name.
                pending_artist = block
                continue

            if block.kind == KIND_VIDEO:
                matched_videos = [
                    v for v in block.videos if needle in v.title.lower()
                ]
                if not matched_videos:
                    continue

                if pending_artist is not None:
                    filtered.append(pending_artist)
                    pending_artist = None

                if len(matched_videos) == len(block.videos):
                    filtered.append(block)
                else:
                    filtered.append(replace(block, videos=matched_videos))
                continue

            if needle in block.title.lower():
                matched_tracks = block.tracks
            else:
                matched_tracks = [
                    t for t in block.tracks if needle in t.title.lower()
                ]
                if not matched_tracks:
                    continue

            if pending_artist is not None:
                filtered.append(pending_artist)
                pending_artist = None

            if len(matched_tracks) == len(block.tracks):
                filtered.append(block)
            else:
                # a partial (track-only) match should show that track's own
                # running time, not the whole album's - otherwise "12 mins"
                # would sit over a single visible track
                filtered.append(replace(
                    block,
                    tracks=matched_tracks,
                    duration_ms=sum(t.duration_ms or 0 for t in matched_tracks),
                ))

        self.set_blocks(filtered)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_items()

    def _resize_items(self) -> None:
        width = max(600, self.list.viewport().width())
        if width == self.delegate.width_hint:
            return
        self.delegate.width_hint = width
        for row in range(self.list.count()):
            item = self.list.item(row)
            block = item.data(ROLE_BLOCK)
            item.setSizeHint(QSize(width, block_height(block)))

    def scroll_to_letter(self, letter: str) -> None:
        for row, block in enumerate(self._blocks):
            if block.kind == KIND_ARTIST and block.sort_letter == letter:
                self.list.scrollToItem(
                    self.list.item(row), QAbstractItemView.PositionAtTop
                )
                return

    # -- interaction ---------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        if obj is self.list.viewport():
            if event.type() == event.Type.MouseButtonRelease:
                self._handle_click(event.position().toPoint())
            elif event.type() == event.Type.MouseMove:
                self._handle_hover(event.position().toPoint())
            elif event.type() == event.Type.Leave:
                changed = False
                if self.delegate.hover_track is not None:
                    self.delegate.hover_track = None
                    changed = True
                if self.delegate.hover_video is not None:
                    self.delegate.hover_video = None
                    changed = True
                if changed:
                    self.list.viewport().update()
        return super().eventFilter(obj, event)

    def _hit(
        self, point
    ) -> tuple[Optional[Block], Optional[TrackEntry], Optional[VideoEntry]]:
        index = self.list.indexAt(point)
        if not index.isValid():
            return None, None, None
        block = index.data(ROLE_BLOCK)
        if block is None:
            return None, None, None
        if block.kind == KIND_ALBUM:
            rect = self.list.visualRect(index)
            for row_rect, track in track_geometry(rect, block):
                if row_rect.adjusted(-8, 0, 8, 0).contains(point):
                    return block, track, None
            return block, None, None
        if block.kind == KIND_VIDEO:
            rect = self.list.visualRect(index)
            for row_rect, video in video_geometry(rect, block):
                if row_rect.adjusted(-8, 0, 8, 0).contains(point):
                    return block, None, video
            return block, None, None
        return block, None, None

    def _handle_hover(self, point) -> None:
        _block, track, video = self._hit(point)
        track_id = track.track_id if track else None
        video_id = video.video_id if video else None
        changed = False
        if track_id != self.delegate.hover_track:
            self.delegate.hover_track = track_id
            changed = True
        if video_id != self.delegate.hover_video:
            self.delegate.hover_video = video_id
            changed = True
        if changed:
            self.list.viewport().update()

    def _handle_click(self, point) -> None:
        block, track, video = self._hit(point)
        if block is None:
            return
        if block.kind == KIND_ALBUM:
            if track is not None:
                self.trackChosen.emit(block.release_id, track.track_id)
            else:
                # the artwork and the header act as "play this album"
                self.albumChosen.emit(block.release_id)
        elif block.kind == KIND_VIDEO:
            if video is not None:
                self.videoChosen.emit(video.video_id)
            # tapping the "Videos" heading itself does nothing - no
            # "play all these videos" action exists

    # -- loading -------------------------------------------------------------

    def load(self, order: str = "year") -> None:
        """Build the document: artists A-Z, each with their releases.

        `order` sets how a single artist's releases are stacked - "year" for
        chronological, "title" for alphabetical.
        """
        from sqlalchemy import func, select

        from ...db.models import Artist, Genre, MediaFile, Release, Track, Video, release_genres
        from ...services.matching import normalize

        def artist_key(name: str) -> str:
            return normalize(name) or name.lower()

        blocks: list[Block] = []
        with self.ctx.session() as session:
            playable = (
                select(
                    MediaFile.track_id.label("track_id"),
                    func.min(MediaFile.id).label("file_id"),
                )
                .where(MediaFile.is_missing.is_(False))
                .group_by(MediaFile.track_id)
                .subquery()
            )
            track_rows = session.execute(
                select(
                    Track.release_id,
                    Track.id,
                    Track.disc_no,
                    Track.track_no,
                    Track.title,
                    Track.duration_ms,
                    playable.c.file_id,
                )
                .outerjoin(playable, playable.c.track_id == Track.id)
                .order_by(Track.release_id, Track.disc_no, Track.track_no, Track.id)
            )
            by_release: dict[int, list[TrackEntry]] = {}
            for release_id, tid, disc, no, title, ms, file_id in track_rows:
                by_release.setdefault(release_id, []).append(
                    TrackEntry(
                        track_id=tid,
                        disc_no=disc or 1,
                        track_no=no,
                        title=title,
                        duration_ms=ms or 0,
                        playable=file_id is not None,
                    )
                )

            genre_rows = session.execute(
                select(release_genres.c.release_id, Genre.name)
                .join(Genre, Genre.id == release_genres.c.genre_id)
            )
            genres: dict[int, str] = {}
            for release_id, name in genre_rows:
                genres.setdefault(release_id, name)

            releases = list(session.scalars(select(Release)))

            video_rows = session.execute(
                select(Video.id, Video.title, Video.duration_ms, Artist.name, Video.artist_display)
                .outerjoin(Artist, Artist.id == Video.artist_id)
            )
            videos_by_key: dict[str, list[VideoEntry]] = {}
            video_display_name_by_key: dict[str, str] = {}
            for video_id, title, duration_ms, artist_name, artist_display in video_rows:
                name = artist_name or artist_display or "Unknown Artist"
                key = artist_key(name)
                videos_by_key.setdefault(key, []).append(
                    VideoEntry(video_id=video_id, title=title, duration_ms=duration_ms or 0)
                )
                video_display_name_by_key.setdefault(key, name)

        grouped_by_key: dict[str, list] = {}
        display_name_by_key: dict[str, str] = {}
        for release in releases:
            artist = release.artist_display or "Unknown Artist"
            key = artist_key(artist)
            grouped_by_key.setdefault(key, []).append(release)
            display_name_by_key.setdefault(key, artist)

        for key in sorted(set(grouped_by_key) | set(videos_by_key)):
            artist = display_name_by_key.get(key) or video_display_name_by_key.get(key, "Unknown Artist")
            first = key[:1].upper()
            letter = first if "A" <= first <= "Z" else "#"
            blocks.append(Block(kind=KIND_ARTIST, title=artist, sort_letter=letter))

            def release_key(r):
                if order == "title":
                    return (r.title_key or r.title.lower(), r.year or 0)
                return (-(r.year or 0), r.title_key or r.title.lower())

            for release in sorted(grouped_by_key.get(key, []), key=release_key):
                tracks = by_release.get(release.id, [])
                blocks.append(
                    Block(
                        kind=KIND_ALBUM,
                        title=release.title,
                        release_id=release.id,
                        year=release.year,
                        genre=genres.get(release.id, ""),
                        cover_path=release.cover_path,
                        duration_ms=sum(t.duration_ms or 0 for t in tracks),
                        tracks=tracks,
                        sort_letter=letter,
                    )
                )

            artist_videos = videos_by_key.get(key)
            if artist_videos:
                blocks.append(
                    Block(
                        kind=KIND_VIDEO,
                        title="Videos",
                        sort_letter=letter,
                        videos=sorted(artist_videos, key=lambda v: v.title.lower()),
                    )
                )
        self._raw_blocks = blocks
        self._apply_filter()
        self._resize_items()
