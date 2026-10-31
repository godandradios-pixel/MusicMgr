"""Library browser.

Four presentations, each with its own top-level nav button in the sidebar
(see app.py:NAV_ITEMS/LIBRARY_MODE_KEYS/navigate), so the sidebar always
shows which one you're in:

  * **Albums**  - cover grid -> release detail
  * **Artists** - circular portrait grid -> artist page -> release detail
  * **Tracks**  - 'Album and Tracks': a scrolling document, artist heading
    then each release with its tracks in two columns
  * **Title Details** - a flat, sortable table of every track: Genre, Album
    Artist, Album, Track #, Title, Time, Year, and a tappable Rating column
    (see `widgets/track_details_table.py`).

`select_mode(mode)` is the seam: each of the sidebar's four Library buttons
calls it by name (via app.py's navigate()) to switch presentations, and it's
the same seam tests use rather than reaching into this view's internals.

A search box in the header live-filters whichever of these is on screen,
each by its own kind of thing: Artists by artist name, Albums by album
title, Tracks by album or track title (see
`AlbumTracksPanel.set_filter_text`), and Title Details by any column (see
`TrackDetailsTable._row_matches`). Matching videos are woven in only as
search results, never in the unfiltered browse - a tile in Albums/Artists,
a "▶ Videos" block under its artist in Tracks, a row in Title Details; see
`_video_tiles()`, `AlbumTracksPanel.load()`, and `TrackDetailRow.is_video`.
Each presentation matches a video the way it matches its own content:
Albums/Artists by the video's *artist* name only (so "rush" doesn't find
an unrelated "Rush, Rush" video - see `CoverGrid.set_filter_text`), Tracks
by its title, Title Details by either. Tapping a video switches to the
Videos tab and plays it there (`_activate_video()`). In Albums/Artists, a
search matching an artist's name collapses that artist's videos into one
"▶ N videos" tile, which opens Videos grouped by Artist and scrolled to
that artist (`_activate_video_group()`; see `CoverGrid._rebuild()`'s
"video_group" handling).

One A-Z jump bar under the search box is shared by all four
presentations. `_refresh_jump_bar()` asks the active one
(`CoverGrid.jump_letters()`, `AlbumTracksPanel.jump_letters()`, or
`TrackDetailsTable.jump_letters()`) which letters make sense, and hides
the bar when there's no alphabetical order to jump within (Albums or
Title Details sorted by Year, etc.). Tapping a letter both scrolls to it
(`_on_letter_key`, off `JumpBar.letterPicked`, which snaps to the nearest
letter with content) and types the literal letter into the search box
(`_on_letter_typed`, off `JumpBar.letterTyped`) - a touch panel has no
keyboard. Space and Backspace buttons sit next to the search box for the
same reason, and Clear is its own oversized button rather than
QLineEdit's tiny clear icon.

Every route ends at the same `ReleaseDetailPanel`, so a release looks and
behaves the same however you reached it. Rather than a single "‹ Back" pill,
that panel (and the artist page before it) carries a real breadcrumb trail
(`ReleaseDetailPanel`/`ArtistDetailPanel.breadcrumb`, a `common.py:Breadcrumb`)
- every ancestor is a tappable link, so a release opened from an artist page
shows "Artists › Dana Voss › Paper Cathedral" and either of the first two
crumbs jumps straight there, not just one step back. `_open_release()` is
where a route's ancestor trail and click targets are built; see it and
`_on_release_breadcrumb()` below.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import func, select

from ...db.models import Artist, Release, Track
from ...services import library as lib
from ...services import videos as video_svc
from ..context import AppContext
from ..widgets.artist_panel import ArtistDetailPanel
from ..widgets.common import TouchButton, dim_label
from ..widgets.cover_grid import (
    ALBUM_SORTS,
    ARTIST_SORTS,
    SHAPE_CIRCLE,
    CoverGrid,
    GridTile,
    JumpBar,
)
from ..widgets.release_panel import ReleaseDetailPanel
from ..widgets.album_tracks import AlbumTracksPanel
from ..widgets.track_details_table import TrackDetailRow, TrackDetailsTable
from .base import BaseView

BROWSE_ALBUM = "album"
BROWSE_ARTIST = "artist"
BROWSE_TRACK = "track"
BROWSE_DETAILS = "details"

GRID_MODES = (BROWSE_ALBUM, BROWSE_ARTIST)

PANE_ALBUM_GRID = 0
PANE_RELEASE_DETAIL = 1
PANE_DETAILS = 2
PANE_ARTIST_GRID = 3
PANE_ARTIST_DETAIL = 4
PANE_TRACKS = 5

# kept for callers written against the previous pane names
PANE_GRID = PANE_ALBUM_GRID
PANE_ALBUM_DETAIL = PANE_RELEASE_DETAIL


class LibraryView(BaseView):
    title_text = "Library"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._mode = BROWSE_ALBUM
        self._loaded: set[str] = set()
        #: click targets for the release-detail breadcrumb's ancestor crumbs,
        #: index-matched to whatever `_open_release()` last built (see there
        #: and `_on_release_breadcrumb()`)
        self._release_breadcrumb_actions: list[Callable[[], None]] = []
        #: same idea, for the artist-detail breadcrumb's leading crumb (see
        #: _build_artist_detail/_on_artist_breadcrumb/_open_artist) - just
        #: one action (index 0), since unlike a release's ancestor chain
        #: this page only ever has the one leading crumb before the
        #: artist's own name.
        self._artist_breadcrumb_actions: list[Callable[[], None]] = []
        #: the `root` the artist page was last opened with, so the ‹ ›
        #: arrows keep the same leading crumb while stepping
        #: from one artist to the next
        self._artist_root: Optional[tuple[str, Callable[[], None]]] = None
        #: what the album page was last opened with - its breadcrumb
        #: ancestors and the release-id list its ‹ › arrows step through
        #: (None = no list, arrows hidden) - kept so stepping reopens the
        #: next album with the same trail (see _open_release/_step_release)
        self._release_ancestors: list[tuple[str, Callable[[], None]]] = []
        self._release_siblings: Optional[Callable[[], list]] = None
        self._search_text = ""

        self.stats = dim_label("")
        self.header.insertWidget(1, self.stats)

        # just the search controls (the modes are in the sidebar). Kept on
        # self so the panes' count labels can be added to it (see below).
        header_controls = QHBoxLayout()
        self.header_controls = header_controls
        header_controls.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search this view…")
        self.search_box.setFixedWidth(640)
        self.search_box.setToolTip(
            "Searches whatever's on screen: Artists by artist name, Albums "
            "by album title, Tracks by album or track title or a featured "
            "artist, Title Details "
            "by any of its columns - also matches videos the same way "
            "(by artist in Artists/Albums, by title in Tracks) - or tap "
            "letters in the A-Z bar to spell one out"
        )
        self.search_box.textChanged.connect(self._on_search_changed)
        header_controls.addWidget(self.search_box)

        self.search_space_btn = TouchButton("Space")
        self.search_space_btn.setFixedWidth(96)
        self.search_space_btn.setToolTip("Insert a space")
        self.search_space_btn.clicked.connect(lambda: self.search_box.insert(" "))
        header_controls.addWidget(self.search_space_btn)

        self.search_backspace_btn = TouchButton("⌫")
        self.search_backspace_btn.setFixedWidth(72)
        self.search_backspace_btn.setToolTip("Backspace")
        self.search_backspace_btn.clicked.connect(self.search_box.backspace)
        header_controls.addWidget(self.search_backspace_btn)

        # its own oversized button rather than QLineEdit's built-in clear
        # icon, which is sized for a mouse pointer, not a fingertip
        self.search_clear_btn = TouchButton("✕")
        self.search_clear_btn.setFixedWidth(72)
        self.search_clear_btn.setToolTip("Clear the search box")
        self.search_clear_btn.clicked.connect(self.search_box.clear)
        header_controls.addWidget(self.search_clear_btn)

        self._search_widgets = [
            self.search_box,
            self.search_space_btn,
            self.search_backspace_btn,
            self.search_clear_btn,
        ]

        header_controls.addStretch(1)
        self.body().addLayout(header_controls)

        # one shared A-Z bar for all four presentations, directly under the
        # search box - see the module docstring and _refresh_jump_bar()
        self.jump_bar = JumpBar()
        self.jump_bar.letterPicked.connect(self._on_letter_key)
        self.jump_bar.letterTyped.connect(self._on_letter_typed)
        self.body().addWidget(self.jump_bar)

        self.panes = QStackedWidget()
        self.panes.addWidget(self._build_album_grid())     # 0
        self.panes.addWidget(self._build_release_detail())  # 1
        self.panes.addWidget(self._build_details())         # 2
        self.panes.addWidget(self._build_artist_grid())     # 3
        self.panes.addWidget(self._build_artist_detail())   # 4
        self.panes.addWidget(self._build_track_list())      # 5
        self.body().addWidget(self.panes, 1)

        # The grids' "N albums" / "N artists" count and Tracks' "N albums ·
        # N tracks" sit at the right end of the search row, and the panes'
        # own header rows
        # (and Tracks' heading) are hidden, freeing a row for content.
        for grid in (self.album_grid, self.artist_grid, self.track_panel):
            self.header_controls.addWidget(grid.count_label)
            grid.header_bar.setVisible(False)
        self.panes.currentChanged.connect(self._sync_grid_counts)
        self._sync_grid_counts()

        ctx.libraryChanged.connect(self._on_library_changed)
        ctx.openArtistRequested.connect(self._open_artist_from_elsewhere)
        ctx.openReleaseRequested.connect(self._open_release_from_elsewhere)
        ctx.openArtistFromMissingMetadataRequested.connect(
            self._open_artist_from_missing_metadata
        )
        ctx.openReleaseFromMissingMetadataRequested.connect(
            self._open_release_from_missing_metadata
        )

    def _sync_grid_counts(self, _index: int = 0) -> None:
        """Show the count for whichever grid is on screen - Albums or
        Artists or Tracks - and none on the detail pages or Title Details."""
        current = self.panes.currentIndex()
        self.album_grid.count_label.setVisible(current == PANE_ALBUM_GRID)
        self.artist_grid.count_label.setVisible(current == PANE_ARTIST_GRID)
        self.track_panel.count_label.setVisible(current == PANE_TRACKS)

    # -- construction ---------------------------------------------------------

    def _build_album_grid(self) -> QWidget:
        # no sort picker here - always by Artist (see "Sort chips removed
        # from Library grids" in architecture.md)
        # art_size=115 (smaller than CoverGrid's 190px default) fits three
        # rows instead of two on the jukebox screen (Windows 125% scaling),
        # with a little headroom; TILE_CAPTION/TILE_PAD don't shrink with it.
        self.album_grid = CoverGrid(
            sorts=ALBUM_SORTS, noun="album",
            default_sort="subtitle", show_sort_picker=False, art_size=115,
        )
        self.album_grid.tileActivated.connect(
            lambda rid: self._open_release(rid, [
                ("Albums", lambda: self.panes.setCurrentIndex(PANE_ALBUM_GRID)),
            ], siblings=self._album_grid_order)
        )
        self.album_grid.videoActivated.connect(self._activate_video)
        self.album_grid.videoGroupActivated.connect(self._activate_video_group)
        self.album_grid.updated.connect(self._refresh_jump_bar)
        return self.album_grid

    def _build_artist_grid(self) -> QWidget:
        # no sort picker here - always by Name (see "Sort chips removed
        # from Library grids" in architecture.md)
        # art_size=115 - matches the album grid, so both grids read as one
        # size. It's below the 130px of the artist page's "More from this
        # artist" strip (artist_panel.py); if portraits feel too strip-like,
        # this is the value to revisit.
        self.artist_grid = CoverGrid(
            sorts=ARTIST_SORTS, shape=SHAPE_CIRCLE, noun="artist",
            default_sort="title", show_sort_picker=False, art_size=115,
        )
        self.artist_grid.tileActivated.connect(self._open_artist)
        self.artist_grid.videoActivated.connect(self._activate_video)
        self.artist_grid.videoGroupActivated.connect(self._activate_video_group)
        self.artist_grid.updated.connect(self._refresh_jump_bar)
        return self.artist_grid

    def _build_artist_detail(self) -> QWidget:
        self.artist_detail = ArtistDetailPanel(self.ctx)
        # the page's one leading crumb is whatever _open_artist's `root`
        # last set (default: the Artists grid; see _on_artist_breadcrumb/
        # _artist_breadcrumb_actions) - the same "index-matched actions
        # list" shape _open_release uses for its longer ancestor chain.
        self.artist_detail.breadcrumb.crumbActivated.connect(self._on_artist_breadcrumb)
        self.artist_detail.releaseActivated.connect(self._open_release_from_artist)
        self.artist_detail.previousArtistRequested.connect(lambda: self._step_artist(-1))
        self.artist_detail.nextArtistRequested.connect(lambda: self._step_artist(1))
        self.artist_detail.removeFromLibraryRequested.connect(self.remove_from_library)
        return self.artist_detail

    def _artist_order(self, artist_id: int) -> list:
        """The artist ids the page's ‹ › arrows step through - the Artists
        grid's own on-screen order (so a live search narrows it too), or
        the whole grid in that same sort order when the current artist
        isn't among the filtered results (opened from Now Playing or the
        Missing metadata dashboard while a search happened to be active)."""
        if BROWSE_ARTIST not in self._loaded:
            self._load_artists()
        order = self.artist_grid.item_keys()
        if artist_id not in order:
            order = self.artist_grid.item_keys(filtered=False)
        return order

    def _artist_neighbors(self, artist_id: int) -> tuple[Optional[int], Optional[int]]:
        order = self._artist_order(artist_id)
        try:
            i = order.index(artist_id)
        except ValueError:
            return None, None
        prev_id = order[i - 1] if i > 0 else None
        next_id = order[i + 1] if i + 1 < len(order) else None
        return prev_id, next_id

    def _step_artist(self, direction: int) -> None:
        current = self.artist_detail.artist_id
        if current is None:
            return
        prev_id, next_id = self._artist_neighbors(current)
        target = prev_id if direction < 0 else next_id
        if target is not None:
            self._open_artist(target, root=self._artist_root)

    def _update_artist_arrows(self, artist_id: int) -> None:
        prev_id, next_id = self._artist_neighbors(artist_id)
        names: dict[int, str] = {}
        wanted = [i for i in (prev_id, next_id) if i is not None]
        if wanted:
            with self.ctx.session() as session:
                for row_id, name in session.execute(
                    select(Artist.id, Artist.name).where(Artist.id.in_(wanted))
                ):
                    names[row_id] = name
        self.artist_detail.set_neighbors(
            names.get(prev_id) if prev_id is not None else None,
            names.get(next_id) if next_id is not None else None,
        )

    def _on_artist_breadcrumb(self, index: int) -> None:
        if 0 <= index < len(self._artist_breadcrumb_actions):
            self._artist_breadcrumb_actions[index]()

    def _open_release_from_artist(self, release_id: int) -> None:
        artist_name = self.artist_detail.name.text() or "Artist"
        # the artist page's own release order, captured now - the arrows
        # keep stepping through *this* artist's releases even though
        # nothing reloads the artist page while you're on the album page
        release_ids = self.artist_detail.release_ids
        self._open_release(release_id, [
            ("Artists", lambda: self.panes.setCurrentIndex(PANE_ARTIST_GRID)),
            (artist_name, lambda: self.panes.setCurrentIndex(PANE_ARTIST_DETAIL)),
        ], siblings=lambda: release_ids)

    def _build_track_list(self) -> QWidget:
        self.track_panel = AlbumTracksPanel(self.ctx)
        self.track_panel.trackChosen.connect(self._play_album_from_track)
        self.track_panel.albumChosen.connect(self._play_album)
        self.track_panel.videoChosen.connect(self._activate_video)
        self.track_panel.updated.connect(self._refresh_jump_bar)
        return self.track_panel

    def _build_release_detail(self) -> QWidget:
        self.album_detail = ReleaseDetailPanel(self.ctx, art_size=190)
        self.album_detail.breadcrumb.crumbActivated.connect(self._on_release_breadcrumb)
        self.album_detail.neighbor_arrows.previousRequested.connect(lambda: self._step_release(-1))
        self.album_detail.neighbor_arrows.nextRequested.connect(lambda: self._step_release(1))
        return self.album_detail

    def _on_release_breadcrumb(self, index: int) -> None:
        if 0 <= index < len(self._release_breadcrumb_actions):
            self._release_breadcrumb_actions[index]()

    def _build_details(self) -> QWidget:
        self.details_table = TrackDetailsTable()
        self.details_table.ratingChanged.connect(self._on_rating_changed)
        self.details_table.videoActivated.connect(self._activate_video)
        self.details_table.trackActivated.connect(self._play_from_details)
        # the tag editor (ui/widgets/tag_editor.py)
        self.details_table.editTagsRequested.connect(self.edit_tags)
        self.details_table.removeFromLibraryRequested.connect(self.remove_from_library)
        self.details_table.updated.connect(self._refresh_jump_bar)
        # No Jukebox column here (room for the title instead - see
        # track_details_table.py's module docstring); tracks are added to
        # the board from Now Playing's toggle or the Jukebox page's picker.


        # no heading above the table, like every other pane - the count is
        # already in `self.stats` at the top of the page.
        return self.details_table

    # -- data loading ---------------------------------------------------------

    def refresh(self) -> None:
        with self.ctx.session() as session:
            stats = lib.library_stats(session)
        self.stats.setText(
            f"{stats['artists']:,} artists · {stats['releases']:,} releases · "
            f"{stats['tracks']:,} tracks"
        )
        if self._mode == BROWSE_ALBUM:
            if BROWSE_ALBUM not in self._loaded:
                self._load_albums()
        elif self._mode == BROWSE_ARTIST:
            if BROWSE_ARTIST not in self._loaded:
                self._load_artists()
        elif self._mode == BROWSE_TRACK:
            if BROWSE_TRACK not in self._loaded:
                self._load_tracks()
        else:
            if BROWSE_DETAILS not in self._loaded:
                self._load_details()

    def _on_library_changed(self) -> None:
        self._loaded.clear()
        self.refresh()

    def _on_mode_changed(self, mode: str) -> None:
        self._mode = mode
        if self._mode == BROWSE_ALBUM:
            self.panes.setCurrentIndex(PANE_ALBUM_GRID)
            if BROWSE_ALBUM not in self._loaded:
                self._load_albums()
            else:
                self._apply_search()
        elif self._mode == BROWSE_ARTIST:
            self.panes.setCurrentIndex(PANE_ARTIST_GRID)
            if BROWSE_ARTIST not in self._loaded:
                self._load_artists()
            else:
                self._apply_search()
        elif self._mode == BROWSE_TRACK:
            self.panes.setCurrentIndex(PANE_TRACKS)
            if BROWSE_TRACK not in self._loaded:
                self._load_tracks()
            else:
                self._apply_search()
        else:
            self.panes.setCurrentIndex(PANE_DETAILS)
            if BROWSE_DETAILS not in self._loaded:
                self._load_details()
            else:
                self._apply_search()
        self._refresh_jump_bar()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._apply_search()

    def _on_letter_key(self, letter: str) -> None:
        """Scrolls whichever presentation is active straight to this letter.
        `letter` is `JumpBar.letterPicked`'s scroll target - it snaps to the
        nearest letter that actually has something to jump to, which is right
        for scrolling (see `JumpBar._letter_at`) but wrong for typing (see
        `_on_letter_typed` below, wired to the bar's separate `letterTyped`
        signal instead) - a tap on a letter with no matches right now should
        still spell that literal letter into the search box, even though
        there's nowhere for it to scroll to.
        """
        if self._mode == BROWSE_ALBUM:
            self.album_grid.scroll_to_letter(letter)
        elif self._mode == BROWSE_ARTIST:
            self.artist_grid.scroll_to_letter(letter)
        elif self._mode == BROWSE_TRACK:
            self.track_panel.scroll_to_letter(letter)
        elif self._mode == BROWSE_DETAILS:
            self.details_table.scroll_to_letter(letter)

    def _on_letter_typed(self, letter: str) -> None:
        """The shared A-Z bar doubles as a keyboard for the search box, since
        there's no physical keyboard on a touch panel and tapping letters can
        spell out a search term the same way typing one would. This always
        gets the literal letter tapped (`JumpBar.letterTyped`), never the
        scroll-snapped one `_on_letter_key` gets - after "RU" nothing starts
        with "S", but "S" still has to land in the box for "RUSH". '#' is
        never typed, only jumped to. Uses insert(), like the Space button,
        so it lands at the cursor.
        """
        if letter != "#":
            self.search_box.insert(letter)

    def _refresh_jump_bar(self) -> None:
        """Recomputes which letters the shared A-Z bar offers, and whether
        it shows at all, for whichever presentation is currently active.
        Wired to each presentation's `updated` signal (the two grids, the
        Tracks document, and Title Details - whose own `updated` also fires
        on a header-click sort change, not just a row-set change), since any
        of those can change what's alphabetical right now - e.g. switching
        Albums to sort by Year, sorting Title Details by Rating instead of
        Title, or typing a search term that leaves nothing on screen."""
        if self._mode == BROWSE_ALBUM:
            letters = self.album_grid.jump_letters()
        elif self._mode == BROWSE_ARTIST:
            letters = self.artist_grid.jump_letters()
        elif self._mode == BROWSE_TRACK:
            letters = self.track_panel.jump_letters() or None
        else:
            letters = self.details_table.jump_letters()
        self.jump_bar.setVisible(bool(letters))
        if letters:
            self.jump_bar.set_available(letters)

    def _apply_search(self) -> None:
        needle = self._search_text
        if self._mode == BROWSE_ALBUM:
            self.album_grid.set_filter_text(needle)
        elif self._mode == BROWSE_ARTIST:
            self.artist_grid.set_filter_text(needle)
        elif self._mode == BROWSE_TRACK:
            self.track_panel.set_filter_text(needle)
        elif self._mode == BROWSE_DETAILS:
            self.details_table.set_filter_text(needle)

    def _video_tiles(self, session) -> list[GridTile]:
        """GridTiles for every video, woven into Albums/Artists search results -
        hidden by CoverGrid unless the search box matches one (see the module
        docstring). Shared by both grids since a video tile means
        the same thing in either: tap it, and Videos opens and plays it."""
        tiles: list[GridTile] = []
        for row in video_svc.list_videos_for_search(session):
            tiles.append(GridTile(
                key=row["video_id"],
                title=f"▶ {row['title']}",
                subtitle=row["artist_name"],
                year=row["year"],
                cover_path=row.get("thumbnail_path") or row["artist_image_path"],
                sort_key=row["sort_key"],
                kind="video",
                initials_source=row["artist_name"],
            ))
        return tiles

    def _load_albums(self) -> None:
        """One query for every release, newest first so 'Recently added' needs
        no extra work."""
        tiles: list[GridTile] = []
        with self.ctx.session() as session:
            stmt = (
                select(Release, func.count(Track.id))
                .outerjoin(Track, Track.release_id == Release.id)
                .group_by(Release.id)
                .order_by(Release.created_at.desc(), Release.id.desc())
            )
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
            tiles.extend(self._video_tiles(session))
        self.album_grid.set_tiles(tiles)
        self._loaded.add(BROWSE_ALBUM)
        self._apply_search()

    def _load_artists(self) -> None:
        """Album artists: everyone a release is filed under.

        Not "everyone with a credit" - a producer, a session musician, or a
        featured guest does not get a tile just for that, and a compilation's
        "Various Artists" credit isn't anyone's album either. All of that is
        still reachable through Search; it just doesn't clutter this grid.
        Counts (releases, tracks) only cover releases this artist is the album
        artist for, which is also what the artist page they open into shows -
        tile and page must always agree.

        A tile uses the artist's own portrait when one has been imported
        (Settings -> Import artist images), and otherwise borrows cover art
        from one of their releases - picked deterministically (earliest release
        that has any) so the grid does not reshuffle between visits.
        """
        tiles: list[GridTile] = []
        with self.ctx.session() as session:
            stmt = (
                select(
                    Artist,
                    func.count(func.distinct(Release.id)).label("releases"),
                    func.count(func.distinct(Track.id)).label("tracks"),
                )
                .join(Release, Release.album_artist_id == Artist.id)
                .outerjoin(Track, Track.release_id == Release.id)
                .where(Release.is_compilation.is_(False))
                .group_by(Artist.id)
                .order_by(Artist.created_at.desc(), Artist.id.desc())
            )
            rows = list(session.execute(stmt))

            cover_stmt = (
                select(Release.album_artist_id, Release.cover_path, Release.year, Release.id)
                .where(
                    Release.cover_path.is_not(None),
                    Release.album_artist_id.is_not(None),
                )
                .order_by(func.coalesce(Release.year, 9999), Release.id)
            )
            covers: dict[int, str] = {}
            for artist_id, cover_path, _year, _rid in session.execute(cover_stmt):
                covers.setdefault(artist_id, cover_path)

            for artist, release_count, track_count in rows:
                sort_source = artist.name_sort or artist.name_key or artist.name
                tiles.append(
                    GridTile(
                        key=artist.id,
                        title=artist.name,
                        # "5 releases · 96", not "... 96 tracks": the full
                        # caption doesn't fit a 115px tile, and the second
                        # number is unambiguous in context.
                        subtitle=(
                            f"{release_count} release"
                            f"{'s' if release_count != 1 else ''} · {track_count}"
                        ),
                        cover_path=artist.image_path or covers.get(artist.id),
                        count=release_count or 0,
                        extra=track_count or 0,
                        sort_key=sort_source.lower(),
                    )
                )
            tiles.extend(self._video_tiles(session))
        self.artist_grid.set_tiles(tiles)
        self._loaded.add(BROWSE_ARTIST)
        self._apply_search()

    def _load_tracks(self) -> None:
        self.track_panel.load()
        self._loaded.add(BROWSE_TRACK)
        self._apply_search()

    def _play_album_from_track(self, release_id: int, track_id: int) -> None:
        """Tapping a track plays its album from that point."""
        with self.ctx.session() as session:
            tracks = lib.list_tracks(session, release_id=release_id)
            start = next(
                (i for i, t in enumerate(tracks) if t.id == track_id), 0
            )
            self.ctx.play_tracks(tracks, start=start, source="library:tracks")

    def _play_album(self, release_id: int) -> None:
        with self.ctx.session() as session:
            tracks = lib.list_tracks(session, release_id=release_id)
            self.ctx.play_tracks(tracks, start=0, source="library:tracks")

    # -- navigation -----------------------------------------------------------

    def _open_artist(
        self, artist_id: int, root: Optional[tuple[str, Callable[[], None]]] = None
    ) -> None:
        """`root` overrides the breadcrumb's leading crumb - (label, click
        action) - for a caller that didn't reach this page through the
        Artists grid (default: back to that grid) - see
        _open_artist_from_missing_metadata below for the one other case
        today, the artist-page equivalent of _open_release's own
        `ancestors` list."""
        self._artist_root = root
        label, action = root or ("Artists", lambda: self.panes.setCurrentIndex(PANE_ARTIST_GRID))
        self._artist_breadcrumb_actions = [action]
        self.artist_detail.set_artist(artist_id, root_label=label)
        self._update_artist_arrows(artist_id)
        self.panes.setCurrentIndex(PANE_ARTIST_DETAIL)

    def _activate_video(self, video_id: int) -> None:
        """A search-matched video (in any of the four presentations - see the
        module docstring) switches to the Videos tab and plays
        it there - a video plays in its own embedded pane, never inline
        inside the Library view."""
        self.ctx.navigateRequested.emit("videos")
        self.ctx.playVideoRequested.emit(video_id)

    def _activate_video_group(self, artist: str) -> None:
        """A collapsed "N videos" tile (Albums/Artists grid) -
        the search matched this artist's name rather than any one video's
        own title, so there's no single video to hand off to like
        `_activate_video` does. Switches to the Videos tab grouped by
        Artist and scrolled to this artist's section instead of playing
        anything."""
        self.ctx.navigateRequested.emit("videos")
        self.ctx.focusVideoArtistRequested.emit(artist)

    def _open_release(
        self,
        release_id: int,
        ancestors: list[tuple[str, Callable[[], None]]],
        siblings: Optional[Callable[[], list]] = None,
    ) -> None:
        """`siblings` returns the release ids the page's ‹ › arrows step
        through - the Albums grid's order, or an artist's
        releases. None hides the arrows (Now Playing, Missing metadata:
        there's no list to be "next" in)."""
        self._release_ancestors = list(ancestors)
        self._release_siblings = siblings
        self._release_breadcrumb_actions = [action for _, action in ancestors]
        self.album_detail.set_breadcrumb_ancestors([label for label, _ in ancestors])
        self.album_detail.set_release(release_id)
        self._update_release_arrows(release_id)
        self.panes.setCurrentIndex(PANE_RELEASE_DETAIL)

    def _album_grid_order(self) -> list:
        """The Albums grid's on-screen order (live search included), or the
        whole grid if the open album isn't among the filtered results."""
        order = self.album_grid.item_keys()
        current = self.album_detail.release_id
        if current is not None and current not in order:
            order = self.album_grid.item_keys(filtered=False)
        return order

    def _release_neighbors(self, release_id: int) -> tuple[Optional[int], Optional[int]]:
        if self._release_siblings is None:
            return None, None
        order = self._release_siblings()
        try:
            i = order.index(release_id)
        except ValueError:
            return None, None
        return (
            order[i - 1] if i > 0 else None,
            order[i + 1] if i + 1 < len(order) else None,
        )

    def _step_release(self, direction: int) -> None:
        current = self.album_detail.release_id
        if current is None:
            return
        prev_id, next_id = self._release_neighbors(current)
        target = prev_id if direction < 0 else next_id
        if target is not None:
            self._open_release(target, self._release_ancestors, self._release_siblings)

    def _update_release_arrows(self, release_id: int) -> None:
        arrows = self.album_detail.neighbor_arrows
        if self._release_siblings is None:
            arrows.setVisible(False)
            return
        prev_id, next_id = self._release_neighbors(release_id)
        titles: dict[int, str] = {}
        wanted = [i for i in (prev_id, next_id) if i is not None]
        if wanted:
            with self.ctx.session() as session:
                for row_id, title in session.execute(
                    select(Release.id, Release.title).where(Release.id.in_(wanted))
                ):
                    titles[row_id] = title
        arrows.set_neighbors(
            titles.get(prev_id) if prev_id is not None else None,
            titles.get(next_id) if next_id is not None else None,
        )
        arrows.setVisible(True)

    def _open_artist_from_elsewhere(self, artist_id: int) -> None:
        """ctx.openArtistRequested - Now Playing's tappable artist name.
        Sets Artist mode first so the sidebar highlights the
        right Library button for what's actually on screen, the same
        ordering app.py's navigate() uses for its own "library" hand-off."""
        self.select_mode(BROWSE_ARTIST)
        self._open_artist(artist_id)

    def _open_release_from_elsewhere(self, release_id: int) -> None:
        """ctx.openReleaseRequested - Now Playing's tappable album art/title.
        A release opened this way isn't part of the Albums or
        Artists browsing hierarchy, so its breadcrumb gets a standalone "Now
        Playing" ancestor that jumps back to Now Playing itself, rather than
        one of those grids."""
        self._open_release(release_id, [
            ("Now Playing", lambda: self.ctx.navigateRequested.emit("nowplaying")),
        ])

    def _return_to_missing_metadata(self) -> None:
        """The "Missing metadata" breadcrumb crumb's click target, shared by
        both _open_artist_from_missing_metadata and
        _open_release_from_missing_metadata below - a way back to the
        dashboard. Settings reopens its dialog from its cached scan
        (SettingsView._last_metadata_scan/view_missing_metadata) rather
        than rerunning it, so the round trip is cheap."""
        self.ctx.navigateRequested.emit("settings")
        self.ctx.missingMetadataBackRequested.emit()

    def _open_artist_from_missing_metadata(self, artist_id: int) -> None:
        """ctx.openArtistFromMissingMetadataRequested - the "Missing
        metadata" dashboard's Artist profiles/Track popularity tabs (see
        MissingMetadataDialog). Same shape as _open_artist_from_elsewhere,
        just with the breadcrumb's leading crumb pointing back to the
        dashboard instead of the Artists grid."""
        self.select_mode(BROWSE_ARTIST)
        self._open_artist(artist_id, root=("Missing info", self._return_to_missing_metadata))

    def _open_release_from_missing_metadata(self, release_id: int) -> None:
        """ctx.openReleaseFromMissingMetadataRequested - the "Missing
        metadata" dashboard's Album artwork/Lyrics tabs. Same shape as
        _open_release_from_elsewhere's own "Now Playing" ancestor, just
        leading back to the dashboard instead."""
        self._open_release(release_id, [
            ("Missing info", self._return_to_missing_metadata),
        ])

    # -- Title Details ----------------------------------------------------------

    def _load_details(self) -> None:
        with self.ctx.session() as session:
            rows = lib.list_track_details(session)
            video_rows = video_svc.list_videos_for_search(session)
        details = [TrackDetailRow(**row) for row in rows]
        details.extend(
            TrackDetailRow(
                track_id=0,
                # a video's kind lives in media_type, not a fake genre
                genre="",
                album_artist=v["artist_name"],
                album="",
                track_no=None,
                title=f"▶ {v['title']}",
                duration_ms=v["duration_ms"],
                year=v["year"],
                rating=None,
                sort_key=v["sort_key"],
                is_video=True,
                video_id=v["video_id"],
                media_type=v["media_type"],
            )
            for v in video_rows
        )
        self.details_table.set_rows(details)
        self._loaded.add(BROWSE_DETAILS)
        self._apply_search()

    #: tests swap the dialog out
    tag_editor_class = None

    def edit_tags(self, track_ids: list) -> None:
        """Open the tag editor on songs picked in Title Details.
        Saving emits libraryChanged, which reloads the table."""
        if not track_ids:
            return
        from ..widgets.tag_editor import MAX_SONGS, TagEditorDialog

        if len(track_ids) > MAX_SONGS:
            self.ctx.notify(f"Pick up to {MAX_SONGS} songs at a time to edit their tags")
            return
        cls = self.tag_editor_class or TagEditorDialog
        dialog = cls(self.ctx, list(track_ids), parent=self)
        dialog.exec()
        if hasattr(dialog, "deleteLater"):
            dialog.deleteLater()

    def _confirm_remove(self, count: int) -> bool:  # pragma: no cover - dialog
        from PySide6.QtWidgets import QMessageBox

        what = "this song" if count == 1 else f"these {count} songs"
        return QMessageBox.question(
            self, "Remove from library",
            f"Remove {what} from the library?\n\n"
            "There's no file left. Plays, playlist places, chart matches and "
            "jukebox sides go with it (or move to the copy you have, when "
            "it's plainly the same song). An album or artist left with "
            "nothing is removed too. A backup of the library is made daily.",
        ) == QMessageBox.Yes

    #: tests swap the question out
    confirm_remove = None

    def remove_from_library(self, track_ids: list) -> None:
        """Right-click "Remove from library" on songs with no file left
        (Title Details, an artist's Top Tracks) - services/track_removal.py."""
        from ...services import track_removal

        if not track_ids:
            return
        ask = self.confirm_remove or self._confirm_remove
        if not ask(len(track_ids)):
            return
        with self.ctx.session() as session:
            result = track_removal.remove_fileless_tracks(session, track_ids)
        self.ctx.notify(result.summary())
        if not result.removed:
            return
        self.ctx.libraryChanged.emit()
        self._after_removal()

    def _after_removal(self) -> None:
        """The artist or album page on screen may be the one just emptied:
        go back to its grid, or reload it."""
        pane = self.panes.currentIndex()
        if pane == PANE_ARTIST_DETAIL and self.artist_detail.artist_id is not None:
            with self.ctx.session() as session:
                alive = session.get(Artist, self.artist_detail.artist_id) is not None
            if alive:
                self.artist_detail.set_artist(self.artist_detail.artist_id,
                                              root_label=(self._artist_root or ("Artists",))[0])
            else:
                self._loaded.discard(BROWSE_ARTIST)
                self.panes.setCurrentIndex(PANE_ARTIST_GRID)
                if self._mode == BROWSE_ARTIST:
                    self._load_artists()
        elif pane == PANE_RELEASE_DETAIL:
            release_id = self.album_detail.release_id
            with self.ctx.session() as session:
                alive = release_id is not None and session.get(Release, release_id) is not None
            if not alive:
                self.panes.setCurrentIndex(PANE_ALBUM_GRID)

    def _on_rating_changed(self, track_id: int, rating: int) -> None:
        with self.ctx.session() as session:
            lib.set_track_rating(session, track_id, rating)

    def _play_from_details(self, track_id: int) -> None:
        """A double-clicked Title Details row (`TrackDetailsTable.
        trackActivated`) queues the whole currently
        visible/sorted table, starting at that row - the same "queue
        what's on screen" rule `TrackListPanel.play_from` already uses for
        the Tracks presentation's own flat all-library list, since Title
        Details is exactly that same kind of thing. Rows without a
        playable file (missing media, or one that predates a rescan) are
        skipped rather than breaking the queue - the tapped row itself may
        be one of them if its own file went missing since the table last
        loaded, hence matching by track_id below rather than assuming the
        tapped index survives the filter.

        When the tapped row itself isn't playable, this notifies rather
        than starting some other track (e.g. the first playable row)."""
        rows = self.details_table.model.rows()
        items = [r.to_queue_item() for r in rows if r.playable]
        if not items:
            self.ctx.notify("No playable files in this list")
            return
        start = next((i for i, it in enumerate(items) if it.track_id == track_id), None)
        if start is None:
            self.ctx.notify("No available file for this track")
            return
        self.ctx.player.set_shuffle(False)
        self.ctx.player.play_tracks(items, start=start, source="library:details")

    # -- convenience for tests / external callers -----------------------------

    def select_mode(self, mode: str) -> bool:
        """Activate a browse mode by name. This is the seam the sidebar's
        Artists/Albums/Tracks/Title Details buttons call into (see
        app.py:navigate), and the same seam tests use rather than reaching
        into this view's internals."""
        if mode not in (BROWSE_ALBUM, BROWSE_ARTIST, BROWSE_TRACK, BROWSE_DETAILS):
            return False
        self._on_mode_changed(mode)
        return True

    @property
    def mode(self) -> str:
        """The active presentation - read by the sidebar so it can keep the
        Artists/Albums/Tracks/Title Details submenu in sync when Library is
        opened some other way than clicking one of those items directly."""
        return self._mode
