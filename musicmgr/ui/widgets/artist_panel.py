"""Artist page: header with counts and play actions, their releases, a
biography, and a short "top tracks" taste of their most-played songs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import func, select

from ...config import TOUCH
from ...db.models import Artist, ArtistTopTrack, Release, Track
from ...services import library as lib
from ...services.library import format_duration
from ...services import lastfm_popularity as popularity_dl
from ...services import top_songs
from .bio_panel import BioPanel
from .common import (
    Breadcrumb,
    CoverArt,
    EmptyState,
    NeighborArrows,
    PopularityDownloadThread,
    TouchButton,
    TouchList,
    dim_label,
)
from .cover_grid import GridTile
from .gatefold_coverflow import GatefoldCoverflow
from ..theme import make_compact

#: how many of an artist's most-played tracks the Top Tracks list shows -
#: a taste, not a full sortable browse (Title Details already does that,
#: with its own Artist/Album group-by - see track_details_table.py).
TOP_TRACKS_COUNT = 8

#: RowDelegate right-anchors its "trail" text (see common.py's
#: _paint_payload) to the row's full width, so on a wide window a short
#: title sits far from its playcount. Capping the section's width keeps
#: them close - a plain cap rather than TwoColumnRowDelegate's second
#: column, since this is a short taste list.
TOP_TRACKS_MAX_WIDTH = 780


def _normalize_title(text: str) -> str:
    """Same qualifier-stripping shape as lastfm_popularity._normalize, so
    "the same song" groups the same way here as when Track.lastfm_popularity
    was stamped onto every matching pressing. Used by _refresh_top_tracks to
    collapse every pressing of one song (studio cut, live recording,
    remaster, reissue) into a single Top Tracks row. Delegates to
    services/top_songs.song_key, which also folds " - Remastered 2011" /
    "[Live at ...]" / "(Single Version)" style versions together."""
    return top_songs.song_key(text)


@dataclass
class _TopTrackRow:
    """One row of the Top Tracks list. The list shows the artist's top
    songs whether or not they're in the library, so a row may have no
    `Track` at all."""

    title: str
    #: the owned pressing chosen to represent this song (see
    #: ArtistDetailPanel._owned_by_title), or None if James doesn't own
    #: it - a None here is what makes a row show "Not in your library"
    #: and not respond to a tap (see _play_top_track).
    track: Optional[Track]
    #: Last.fm's playcount for this entry, when the list came from a
    #: fetched Last.fm chart - None in the local play_count fallback,
    #: where a playcount was never fetched to begin with.
    playcount: Optional[int] = None


def _format_playcount(n: Optional[int]) -> str:
    """"177M plays" - the same shorthand YouTube Music's "Top songs" uses,
    rather than a bare "177000000"."""
    if not n:
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B plays"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M plays"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K plays"
    return f"{n} plays"


class ArtistDetailPanel(QWidget):
    """One artist: header card (portrait, breadcrumb, name, stats, play
    actions), biography, Top Tracks, and their discography as a folding
    cover row. Music videos live in their own "Videos" section (NAV_ITEMS in
    ui/app.py), not here.

    Layout: a two-column top section - header card over the biography on
    the left, Top Tracks on the right - then the release coverflow, full
    width. The whole page sits in a vertical QScrollArea. See the comments
    in __init__ for the details.

    Top Tracks (`_refresh_top_tracks`) shows, in priority order: the songs
    James picked (services/top_songs.py, "Choose…"); this artist's stored
    Last.fm chart (`ArtistTopTrack`, fetched by "Fetch popularity" - the
    same lookup as Settings' bulk Last.fm update, for one artist); or, with
    no chart, owned tracks ranked by play_count, so a library with no
    Last.fm key still works. Chart entries are matched against the library
    at display time (`_owned_by_title`), so a song bought later is picked up
    with no new fetch; an unowned entry reads "Not in your library" and
    can't be played. Every pressing/version of one song collapses into a
    single row (see _normalize_title), so one song can't fill several slots.
    """

    releaseActivated = Signal(int)
    #: the ‹ / › arrows beside the breadcrumb - LibraryView decides which
    #: artist is "previous"/"next" (see _artist_neighbors) and hands the
    #: answer back through set_neighbors()
    previousArtistRequested = Signal()
    nextArtistRequested = Signal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._artist_id: Optional[int] = None
        self._artist_name: str = ""
        self._tracks: list = []
        self._release_ids: list[int] = []
        #: this artist's Last.fm chart (see ArtistTopTrack's docstring),
        #: sorted by playcount - empty until a fetch has run for this artist,
        #: in which case _refresh_top_tracks ranks owned tracks by play_count.
        self._lastfm_top_tracks: list[ArtistTopTrack] = []
        #: the songs James chose for this artist (services/top_songs.py) -
        #: shown instead of Last.fm's chart when there are any
        self._picks: list[str] = []
        #: the TOP_TRACKS_COUNT rows the Top Tracks list is currently
        #: showing (some may have no owned pressing at all - see
        #: _TopTrackRow) - `self.top_tracks`'s displayed order.
        self._top_tracks: list[_TopTrackRow] = []
        #: just the *ownable* tracks among self._top_tracks, in the same
        #: order - what _play_top_track's `start` index into
        #: self.ctx.play_tracks actually refers to, since a row with no
        #: owned pressing has nothing to queue.
        self._playable_top_tracks: list = []
        #: the running "Fetch popularity" thread for *this* artist, if any -
        #: same shape as BioPanel's _fetch_thread, reset on every set_artist
        self._popularity_thread: Optional[PopularityDownloadThread] = None

        # the whole page scrolls vertically so a long biography or a small
        # screen doesn't clip; the coverflow's horizontal drag-to-browse is a
        # separate gesture on its own viewport and doesn't conflict.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # -- top section: header card + biography (left), Top Tracks
        # (right), so the page's main content is above the fold:
        #   * the breadcrumb lives inside the header card, above the name
        #     (the card's stats line already gives the release count);
        #   * Top Tracks sits in the right half, beside Play/Shuffle;
        #   * the biography fills the left column under the header card,
        #     stretching to the Top Tracks column's height. It's a plain VBox
        #     child, not inside the header card's row - there, BioPanel's
        #     small sizeHint capped its height and long text overflowed onto
        #     the release row.
        # The release coverflow follows underneath, full width.
        top_section = QHBoxLayout()
        top_section.setSpacing(24)

        left_column = QVBoxLayout()
        left_column.setSpacing(10)

        header = QFrame()
        header.setObjectName("Card")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(18)

        self.portrait = CoverArt(120, circular=True)
        hl.addWidget(self.portrait, 0, Qt.AlignVCenter)

        meta = QVBoxLayout()
        meta.setSpacing(4)

        # see release_panel.py's Breadcrumb (LibraryView wires the click
        # target: back to the Artists grid, or wherever _open_artist's
        # `root` says).
        self.breadcrumb = Breadcrumb()
        # the ‹ › arrows (common.NeighborArrows) sit right after the trail;
        # the trailing stretch keeps them hugging it.
        crumb_row = QHBoxLayout()
        crumb_row.setSpacing(0)
        crumb_row.addWidget(self.breadcrumb, 0)
        self.neighbor_arrows = NeighborArrows("artist")
        self.neighbor_arrows.previousRequested.connect(self.previousArtistRequested.emit)
        self.neighbor_arrows.nextRequested.connect(self.nextArtistRequested.emit)
        self.prev_artist_btn = self.neighbor_arrows.prev_btn
        self.next_artist_btn = self.neighbor_arrows.next_btn
        crumb_row.addWidget(self.neighbor_arrows, 0)
        crumb_row.addStretch(1)
        meta.addLayout(crumb_row)

        self.name = QLabel("")
        self.name.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.name.setWordWrap(True)
        # a wrapped QLabel this deep in nested layouts can negotiate an
        # inflated height on first layout, leaving a tall blank gap - two
        # lines of a 26px name is already very long, so cap it there
        self.name.setMaximumHeight(72)
        meta.addWidget(self.name)

        self.stats = dim_label("")
        meta.addWidget(self.stats)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        # compact (40px) buttons - see theme.make_compact
        play = make_compact(TouchButton("Play everything", primary=True))
        play.clicked.connect(lambda: self.play(shuffle=False))
        shuffle = make_compact(TouchButton("Shuffle"))
        shuffle.clicked.connect(lambda: self.play(shuffle=True))
        queue = make_compact(TouchButton("Add to queue"))
        queue.clicked.connect(lambda: self.ctx.enqueue_tracks(self._tracks))
        # artist radio - services/radio.py
        radio = make_compact(TouchButton("Radio"))
        radio.setToolTip("An endless mix of this artist and similar ones")
        radio.clicked.connect(self.start_radio)
        self.radio_button = radio
        for b in (play, shuffle, queue, radio):
            actions.addWidget(b)
        actions.addStretch(1)
        meta.addLayout(actions)
        hl.addLayout(meta, 0)
        # the card hugs its own content width - the biography below is
        # what fills out the rest of this column.
        left_column.addWidget(header, 0, Qt.AlignLeft)

        # "Edit bio" shares the heading's line rather than taking a row
        # inside BioPanel - same heading + compact button shape as the Top
        # Tracks column's "Fetch popularity".
        self.bio = BioPanel(ctx, inline_edit=False)
        bio_row = QHBoxLayout()
        bio_row.setSpacing(8)
        bio_label = QLabel("Biography")
        bio_label.setObjectName("Crumb")
        bio_row.addWidget(bio_label)
        bio_row.addStretch(1)
        bio_row.addWidget(self.bio.edit_button)
        left_column.addLayout(bio_row)
        # stretches to the height the Top Tracks column sets, less the
        # header card; BioPanel's own QScrollArea handles longer text, and
        # the minimum keeps a few lines readable on a short window.
        self.bio.setMinimumHeight(TOUCH["row_height"] * 2)
        left_column.addWidget(self.bio, 1)

        top_section.addLayout(left_column, 1)

        top_tracks_section = QWidget()
        # see TOP_TRACKS_MAX_WIDTH above - keeps the row's title/trail text
        # from spreading out across the whole page width on a wide window.
        top_tracks_section.setMaximumWidth(TOP_TRACKS_MAX_WIDTH)
        top_tracks_layout = QVBoxLayout(top_tracks_section)
        top_tracks_layout.setContentsMargins(0, 0, 0, 0)
        top_tracks_layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.top_label = QLabel("Top Tracks")
        self.top_label.setObjectName("Crumb")
        top_row.addWidget(self.top_label)
        top_row.addStretch(1)
        # always visible, unlike BioPanel's "Fetch bio" (which only shows up
        # in its empty state) - Top Tracks can already have play_count-based
        # content to show, so there's no empty state to hang this button on.
        self.fetch_popularity_btn = make_compact(TouchButton("Fetch popularity"))
        self.fetch_popularity_btn.clicked.connect(self._on_fetch_popularity_clicked)
        # see TopSongsDialog below
        self.choose_top_btn = make_compact(TouchButton("Choose…"))
        self.choose_top_btn.setToolTip("Pick the songs shown here yourself")
        self.choose_top_btn.clicked.connect(self.choose_top_songs)
        top_row.addWidget(self.choose_top_btn)
        top_row.addWidget(self.fetch_popularity_btn)
        top_tracks_layout.addLayout(top_row)

        self.top_tracks_empty = EmptyState(
            "Nothing played yet",
            "Play some of this artist's tracks and their favorites will show up here.",
        )
        # the standard (not the shorter tree_row_height) row height -
        # RowDelegate's primary/secondary two-line layout is tuned for it.
        self.top_tracks = TouchList()
        self.top_tracks.itemActivatedPayload.connect(self._play_top_track)

        self.top_tracks_stack = QStackedWidget()
        self.top_tracks_stack.addWidget(self.top_tracks_empty)  # 0
        self.top_tracks_stack.addWidget(self.top_tracks)  # 1
        self.top_tracks_stack.setFixedHeight(TOUCH["row_height"] * TOP_TRACKS_COUNT + 16)
        top_tracks_layout.addWidget(self.top_tracks_stack, 0)
        top_tracks_layout.addStretch(1)

        # stretch 1 like the left column, capped by TOP_TRACKS_MAX_WIDTH -
        # on a normal window the two split the page roughly in half; on an
        # ultrawide one the left column (card + prose) takes the extra.
        top_section.addWidget(top_tracks_section, 1)

        layout.addLayout(top_section)

        # a scroll/drag-driven "gatefold" row rather than a page-filling
        # grid - an artist can have anywhere from one release to several
        # hundred, and this is one section on a page. See
        # gatefold_coverflow.py for the fold itself (the open release's
        # title/year is painted over its cover); sorted by title, same as
        # the top-level Library grids. The coverflow's own "N releases"
        # counter isn't placed - the header card's stats line says it.
        self.releases = GatefoldCoverflow(noun="release", panel_size=142)
        self.releases.tileActivated.connect(self.releaseActivated.emit)
        layout.addWidget(self.releases, 0)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    # -- api ------------------------------------------------------------------

    @property
    def artist_id(self) -> Optional[int]:
        return self._artist_id

    @property
    def tracks(self) -> list:
        return self._tracks

    def set_neighbors(self, previous_name: Optional[str], next_name: Optional[str]) -> None:
        """Enable/label the ‹ › arrows beside the breadcrumb - None means
        there's no artist in that direction (first/last in the list), which
        greys that arrow out."""
        self.neighbor_arrows.set_neighbors(previous_name, next_name)

    @property
    def release_ids(self) -> list[int]:
        """This artist's releases in the coverflow's own order (by
        sort_key, same as GatefoldCoverflow.set_tiles) - what the album
        page's ‹ › arrows step through when it was opened from here."""
        return list(self._release_ids)

    def set_artist(self, artist_id: Optional[int], root_label: str = "Artists") -> None:
        """`root_label` names the breadcrumb's leading crumb - "Artists"
        (the grid this page normally hangs off of) unless the caller opened
        this page from somewhere else entirely, e.g. LibraryView's
        `_open_artist_from_missing_metadata` passing "Missing metadata" so
        the crumb (and its click target, wired by the caller - see
        LibraryView._on_artist_breadcrumb) leads back there instead."""
        self._artist_id = artist_id
        self._artist_name = ""
        self._tracks = []
        self._release_ids = []
        self._lastfm_top_tracks = []
        self._picks = []
        self._top_tracks = []
        self._playable_top_tracks = []
        # a "Fetch popularity" started for the previous artist has nothing
        # to land on this one - the thread is left running, only the
        # button's re-entrancy guard is reset, matching BioPanel.set_artist.
        self._popularity_thread = None
        self.fetch_popularity_btn.setEnabled(True)
        self.fetch_popularity_btn.setText("Fetch popularity")
        if artist_id is None:
            self.name.setText("")
            self.stats.setText("")
            self.releases.set_tiles([])
            self.breadcrumb.set_path([])
            self.bio.set_artist(None, "", None)
            self.top_tracks.clear()
            self.top_tracks_stack.setCurrentWidget(self.top_tracks_empty)
            return

        tiles: list[GridTile] = []
        with self.ctx.session() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                return
            self.name.setText(artist.name)
            self._artist_name = artist.name
            self.breadcrumb.set_path([root_label, artist.name])
            # captured while `artist` is still attached to this session -
            # BioPanel itself opens its own session later, for a fetch
            self.bio.set_artist(artist_id, artist.name, artist.profile)

            # every release this artist is the *album* artist for - a guest
            # verse or a production credit elsewhere does not belong on their
            # page, only in search (see lib.list_artists)
            stmt = (
                select(Release, func.count(func.distinct(Track.id)))
                .outerjoin(Track, Track.release_id == Release.id)
                .where(
                    Release.album_artist_id == artist_id,
                    Release.is_compilation.is_(False),
                )
                .group_by(Release.id)
                .order_by(func.coalesce(Release.year, 9999), Release.title)
            )
            total_ms = 0
            for release, track_count in session.execute(stmt):
                tiles.append(
                    GridTile(
                        key=release.id,
                        title=release.title,
                        subtitle=release.artist_display or "",
                        year=release.year,
                        cover_path=release.cover_path,
                        count=track_count or 0,
                        sort_key=release.title_key or release.title.lower(),
                    )
                )

            self._tracks = list(lib.list_tracks_for_album_artist(session, artist_id))
            # Last.fm's chart for this artist, if a fetch has ever run for
            # them - see ArtistTopTrack's docstring.
            self._lastfm_top_tracks = list(
                session.scalars(
                    select(ArtistTopTrack)
                    .where(ArtistTopTrack.artist_id == artist_id)
                    # sorted by playcount rather than Last.fm's chart
                    # position (ArtistTopTrack.rank), which isn't always
                    # descending by the playcount it reports - so the list
                    # reads in the order of the numbers printed on it.
                    # coalesce(..., -1) puts a missing playcount last, and
                    # rank breaks ties deterministically.
                    .order_by(func.coalesce(ArtistTopTrack.playcount, -1).desc(), ArtistTopTrack.rank)
                )
            )
            self._picks = top_songs.get_picks(session, artist_id)
            total_ms = sum(t.duration_ms or 0 for t in self._tracks)
            self.portrait.set_source(
                artist.image_path or (tiles[0].cover_path if tiles else None),
                artist.name,
            )

        releases = len(tiles)
        tracks = len(self._tracks)
        self.stats.setText(
            f"{releases} release{'s' if releases != 1 else ''} · "
            f"{tracks} track{'s' if tracks != 1 else ''} · {format_duration(total_ms)}"
        )
        self.releases.set_tiles(tiles)
        self._release_ids = [t.key for t in sorted(tiles, key=lambda t: t.sort_key)]
        self._refresh_top_tracks()
        # same reasoning as ReleaseDetailPanel.set_release - lets the
        # bottom PlayerBar's play button start this artist's tracks even
        # before "Play everything"/a release/a track's been tapped.
        self.ctx.set_viewing(self._tracks, source=f"artist:{artist_id}")

    def start_radio(self) -> None:
        if self._artist_id is not None and self.ctx.radio.start_from_artist(self._artist_id):
            self.ctx.navigateRequested.emit("nowplaying")

    def play(self, shuffle: bool = False) -> None:
        if not self._tracks:
            self.ctx.notify("Nothing playable for that artist")
            return
        self.ctx.player.set_shuffle(shuffle)
        self.ctx.play_tracks(self._tracks, start=0, source=f"artist:{self._artist_id}")

    # -- popularity fetch (Last.fm) -----------------------------------------

    def _on_fetch_popularity_clicked(self) -> None:
        """Same lookup as Settings' bulk "Update track popularity from
        Last.fm…" (services.lastfm_popularity.update_popularity_for_artists),
        just handed a batch of one - this artist - and reported back here
        instead of a page-wide progress bar. Mirrors BioPanel._on_fetch_clicked
        (PopularityDownloadThread alongside that class's BioDownloadThread,
        both in ui/widgets/common.py)."""
        if self._artist_id is None:
            return
        if self._popularity_thread is not None and self._popularity_thread.isRunning():
            return
        if not popularity_dl.has_api_key():
            self.ctx.notify(
                "Add a Last.fm API key in Settings first (\"Last.fm API key…\")"
            )
            return
        self.fetch_popularity_btn.setEnabled(False)
        self.fetch_popularity_btn.setText("Fetching…")
        self._popularity_thread = PopularityDownloadThread([self._artist_id], parent=self)
        self._popularity_thread.finished_with.connect(self._on_popularity_fetch_finished)
        self._popularity_thread.start()

    def _on_popularity_fetch_finished(self, result: popularity_dl.PopularityResult) -> None:
        self.fetch_popularity_btn.setEnabled(True)
        self.fetch_popularity_btn.setText("Fetch popularity")
        self.ctx.notify(result.summary())
        # only re-rank if this artist is still on screen - a slow fetch
        # finishing after James moved on shouldn't repaint the page.
        if self._artist_id is not None:
            with self.ctx.session() as session:
                self._tracks = list(lib.list_tracks_for_album_artist(session, self._artist_id))
                self._lastfm_top_tracks = list(
                    session.scalars(
                        select(ArtistTopTrack)
                        .where(ArtistTopTrack.artist_id == self._artist_id)
                        # sorted by playcount, not rank - see set_artist
                        .order_by(func.coalesce(ArtistTopTrack.playcount, -1).desc(), ArtistTopTrack.rank)
                    )
                )
            self._refresh_top_tracks()

    # -- top tracks -------------------------------------------------------

    def _owned_by_title(self) -> dict[str, Track]:
        """Groups self._tracks by `_normalize_title` and keeps only the
        single best pressing per group (highest play_count, then rating,
        then earliest release year so a studio original wins a tie over a
        live/anniversary reissue, then title, purely for a stable pick).
        Used both to match a Last.fm chart entry against ownership and,
        with no Last.fm chart yet, to rank owned songs by play_count."""
        groups: dict[str, list[Track]] = {}
        for t in self._tracks:
            groups.setdefault(_normalize_title(t.title), []).append(t)

        representative_by_title: dict[str, Track] = {}
        for key, pressings in groups.items():
            pressings.sort(
                key=lambda t: (
                    -(t.play_count or 0),
                    -(t.rating or 0),
                    t.release.year if t.release and t.release.year else 9999,
                    (t.title or "").lower(),
                )
            )
            representative_by_title[key] = pressings[0]
        return representative_by_title

    def _refresh_top_tracks(self) -> None:
        """Build the list from James's picks, else the Last.fm chart, else
        owned tracks by play_count.

        With picks or a chart, every entry appears whether James owns it or
        not, matched against the library by normalized title right now
        (_owned_by_title); an unowned row reads "Not in your library" and
        can't be tapped to play (see _play_top_track). The chart arrives
        sorted by playcount (see the query in set_artist).

        With neither, owned tracks are ranked by Track.play_count
        (denormalized, kept in sync by PlayHistory writes), ties by rating
        then title for a stable order.

        Either way, every pressing of the same song is collapsed into one
        representative first so a song can't fill multiple slots.
        """
        owned_by_title = self._owned_by_title()
        # one row per song, every version's plays together (see
        # services/top_songs.py)
        chart = top_songs.distinct_chart(self._lastfm_top_tracks)

        if self._picks or chart:
            plays_by_key = {song.key: song.playcount for song in chart}
            if self._picks:
                self.top_label.setText("Top Songs · your picks")
                entries = [(title, plays_by_key.get(top_songs.song_key(title)))
                           for title in self._picks[:TOP_TRACKS_COUNT]]
            else:
                self.top_label.setText("Popular on Last.fm")
                entries = [(song.title, song.playcount) for song in chart[:TOP_TRACKS_COUNT]]

            self._top_tracks = []
            self._playable_top_tracks = []
            rows = []
            for i, (title, playcount) in enumerate(entries):
                owned_track = owned_by_title.get(_normalize_title(title))
                if owned_track is not None:
                    track_index = len(self._playable_top_tracks)
                    self._playable_top_tracks.append(owned_track)
                    secondary = _release_caption(owned_track)
                else:
                    track_index = None
                    secondary = "Not in your library"
                self._top_tracks.append(
                    _TopTrackRow(title=title, track=owned_track, playcount=playcount)
                )
                rows.append({
                    "lead": str(i + 1),
                    "primary": title,
                    "secondary": secondary,
                    "trail": _format_playcount(playcount),
                    "track_index": track_index,
                })

            self.top_tracks.set_rows(rows)
            self.top_tracks_stack.setCurrentWidget(self.top_tracks)
            return

        self.top_label.setText("Top Tracks")
        ranked = sorted(
            owned_by_title.values(),
            key=lambda t: (-(t.play_count or 0), -(t.rating or 0), (t.title or "").lower()),
        )
        top = ranked[:TOP_TRACKS_COUNT]
        has_plays = any((t.play_count or 0) > 0 for t in top)

        self._top_tracks = [_TopTrackRow(title=t.title, track=t) for t in top]
        self._playable_top_tracks = list(top)

        if not top or not has_plays:
            self._top_tracks = []
            self._playable_top_tracks = []
            self.top_tracks.clear()
            self.top_tracks_stack.setCurrentWidget(self.top_tracks_empty)
            return

        self.top_tracks.set_rows([
            {
                "lead": str(i + 1),
                "primary": track.title,
                "secondary": _release_caption(track),
                "trail": format_duration(track.duration_ms or 0),
                "track_index": i,
            }
            for i, track in enumerate(top)
        ])
        self.top_tracks_stack.setCurrentWidget(self.top_tracks)

    # -- choosing the top songs ---------------------------------------------

    #: tests swap the dialog out
    top_songs_dialog_class = None

    def choose_top_songs(self) -> None:
        """Let James pick the top songs himself - from Last.fm's chart
        (one entry per song) and the songs he owns, or typed in."""
        if self._artist_id is None:
            return
        chart = top_songs.distinct_chart(self._lastfm_top_tracks)
        current = [row.title for row in self._top_tracks]
        owned = sorted({t.title for t in self._owned_by_title().values() if t.title},
                       key=str.lower)
        cls = self.top_songs_dialog_class or TopSongsDialog
        dialog = cls(self._artist_name, current, chart, owned, has_picks=bool(self._picks),
                     parent=self)
        if dialog.exec() != QDialog.Accepted:
            if hasattr(dialog, "deleteLater"):
                dialog.deleteLater()
            return
        titles = dialog.titles()
        if hasattr(dialog, "deleteLater"):
            dialog.deleteLater()
        with self.ctx.session() as session:
            self._picks = top_songs.set_picks(session, self._artist_id, titles)
        self._refresh_top_tracks()

    def _play_top_track(self, payload: dict) -> None:
        idx = payload.get("track_index")
        if idx is None:
            # James doesn't own this song yet - say so rather than
            # silently ignoring a tap that looks like it should do something
            if self._top_tracks:
                self.ctx.notify("You don't own this track yet")
            return
        if not self._playable_top_tracks:
            return
        # plays through the owned Top Tracks only, starting at the tapped
        # row's position among them - "start here and keep going through
        # this artist's favorites you own", not the full discography order.
        self.ctx.play_tracks(
            self._playable_top_tracks, start=idx, source=f"artist:{self._artist_id}:top"
        )


def _release_caption(track: Track) -> str:
    release = track.release
    if release is None:
        return ""
    return f"{release.title} ({release.year})" if release.year else release.title


class TopSongsDialog(QDialog):
    """Choose an artist's top songs (TOP_TRACKS_COUNT places). Each place
    is a box to pick from - Last.fm's songs first (one entry per song, with
    plays), then the songs in the library - or to type into. "Use Last.fm's
    list" drops the picks and goes back to the chart. After Ok, `titles()`
    holds the picks in order (an empty list for "use Last.fm's")."""

    def __init__(self, artist_name: str, current: list[str], chart, owned: list[str],
                 has_picks: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Top songs — {artist_name}")
        self.setMinimumWidth(560)
        self._use_lastfm = False
        layout = QVBoxLayout(self)
        head = QLabel(f"Top songs for {artist_name}")
        head.setObjectName("Title")
        layout.addWidget(head)
        note = dim_label("Pick a song for each place from Last.fm's list and your library, "
                         "or type one in. Versions of the same song (live, remastered) count "
                         "as one song. Leave a place empty to show fewer.")
        note.setWordWrap(True)
        layout.addWidget(note)

        options: list[str] = []
        seen: set[str] = set()
        labels: dict[str, str] = {}
        for song in chart:
            if song.key not in seen:
                seen.add(song.key)
                options.append(song.title)
                plays = _format_playcount(song.playcount)
                labels[song.title] = f"{song.title}  ·  {plays}" if plays else song.title
        for title in owned:
            key = top_songs.song_key(title)
            if key and key not in seen:
                seen.add(key)
                options.append(title)
                labels[title] = f"{title}  ·  in your library"

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.boxes: list[QComboBox] = []
        for i in range(TOP_TRACKS_COUNT):
            box = QComboBox()
            box.setEditable(True)
            box.setInsertPolicy(QComboBox.NoInsert)
            box.addItem("", "")
            for title in options:
                box.addItem(labels.get(title, title), title)
            text = current[i] if i < len(current) else ""
            idx = box.findData(text)
            if idx >= 0:
                box.setCurrentIndex(idx)
            box.setEditText(text)
            box.activated.connect(lambda idx, b=box: b.setEditText(b.itemData(idx) or ""))
            # the app's combo boxes draw no arrow, so a button opens the list
            pick = make_compact(TouchButton("Pick ▾"))
            pick.clicked.connect(box.showPopup)
            grid.addWidget(QLabel(str(i + 1)), i, 0)
            grid.addWidget(box, i, 1)
            grid.addWidget(pick, i, 2)
            self.boxes.append(box)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        buttons = QHBoxLayout()
        self.lastfm_btn = TouchButton("Use Last.fm's list")
        self.lastfm_btn.setEnabled(has_picks)
        self.lastfm_btn.clicked.connect(self._use_lastfm_list)
        cancel = TouchButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = TouchButton("Save", primary=True)
        save.setObjectName("PrimaryWarm")
        save.clicked.connect(self.accept)
        buttons.addWidget(self.lastfm_btn)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _use_lastfm_list(self) -> None:
        self._use_lastfm = True
        self.accept()

    def titles(self) -> list[str]:
        if self._use_lastfm:
            return []
        out = []
        for box in self.boxes:
            text = box.currentText().strip()
            data = box.itemData(box.currentIndex()) if box.currentIndex() >= 0 else None
            # a picked entry's label carries its plays - keep the title only
            if data and box.itemText(box.currentIndex()) == text:
                text = data
            out.append(text)
        return [t for t in out if t]

