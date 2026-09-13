"""Library browser.

Four presentations, switched from the sidebar rather than a pill row on the
page itself - Artists / Albums / Tracks / Title Details each get their own
top-level nav button (see app.py:NAV_ITEMS/LIBRARY_MODE_KEYS/navigate; a
single "Library" item with these four nested underneath used to be here
instead, flattened out 2026-09-06 at James's request), so the sidebar always
shows which one you're in and the page starts one tap closer to content:

  * **Albums**  - cover grid -> release detail
  * **Artists** - circular portrait grid -> artist page -> release detail
  * **Tracks**  - 'Album and Tracks': a scrolling document, artist heading
    then each release with its tracks in two columns
  * **Title Details** - a flat, sortable table of every track: Genre, Album
    Artist, Album, Track #, Title, Time, Year, and a tappable Rating column
    (see `widgets/track_details_table.py`). Replaced the old three-pane
    Genre browser (genre -> releases -> tracks), which filed tracks under a
    release-level tag nobody actually browses music by (2026-09-05; Album
    column added 2026-09-06).

`select_mode(mode)` is the seam: each of the sidebar's four Library buttons
calls it by name (via app.py's navigate()) to switch presentations, and it's
the same seam tests use rather than reaching into this view's internals.

A search box in the header live-filters whichever of these is on screen, but
each presentation only searches its own kind of thing (James, 2026-09-06):
Artists by artist name, Albums by album title, Tracks by album or track
title (an artist heading is no longer itself a search target - see
`AlbumTracksPanel.set_filter_text`), and Title Details by any of its
columns (see `TrackDetailsTable._row_matches`). A matching video is still
woven into whichever presentation is on screen the same way (2026-09-06) - a
tile in Albums/Artists, a "▶ Videos" block under its artist in the Tracks
document, a row in Title Details - but only ever as a search result, never
in the default, unfiltered browse; see `_video_tiles()`,
`AlbumTracksPanel.load()`, and `TrackDetailRow.is_video`. Each presentation
matches a video the same way it matches its own native content, not by both
title and artist everywhere: Albums/Artists match only the video's *artist*
name, same as they match a release's or artist's own name (James, 2026-09-06:
searching "rush" in Artists was surfacing an unrelated "Rush, Rush" video by
Paula Abdul purely because its *title* contained "rush" - see
`CoverGrid.set_filter_text`); Tracks matches only the video's own title, same
as it matches a track's; Title Details matches either, since it searches
every column. Tapping a video switches to the Videos tab and plays it there
(`_activate_video()`), the same hand-off the artist page's own video list
already used. In Albums/Artists specifically, a search that matches an
artist's *name* collapses every one of that artist's matching videos into a
single "▶ N videos" tile rather than a tile each - an artist with several
videos was otherwise flooding the grid for one name match (2026-09-06, same
day). Tapping that collapsed tile
switches to Videos grouped by Artist and scrolled to that artist's section
instead of playing a specific video (`_activate_video_group()`); see
`CoverGrid._rebuild()`'s "video_group" handling. A single A-Z jump bar sits
directly under the search box, shared
by all four presentations (a jump bar built into each grid and a letter bar
built into the Tracks document, before this). Title Details briefly opted
out of it (2026-09-05, closer to Charts' admin-table treatment - see that
section in architecture.md) but joined it the next day for consistency with
the rest of Library (2026-09-06); it only makes sense while the table is
sorted by one of its text columns (Genre/Album Artist/Album/Title), so the
bar hides itself the moment the table is sorted by Track #/Time/Year/Rating
instead, the same as it already hides for a grid sorted by something
non-alphabetical. `_refresh_jump_bar()` is what keeps the shared bar in
sync: it asks the active presentation (`CoverGrid.jump_letters()`,
`AlbumTracksPanel.jump_letters()`, or `TrackDetailsTable.jump_letters()`)
which letters make sense right now, hides the bar entirely when the answer
is "none" (Albums sorted by Year, Title Details sorted by Year, etc. -
there's no alphabetical order to jump within) and otherwise shows it.
Tapping a letter both scrolls the active presentation to it (`_on_letter_key`,
off `JumpBar.letterPicked` - the scroll-scrubbing signal, which snaps a tap
to the nearest letter that actually has something to jump to) and types it
into the search box (`_on_letter_typed`, off `JumpBar.letterTyped` - always
the literal letter tapped, snap or no snap, so typing the next letter of a
word that no longer matches anything still lands in the box) - the same
touch-panel-has-no-keyboard reasoning as `common.py:SearchBar`'s on-screen
keyboard, just spelled out one big letter at a time instead. Space and
Backspace buttons sit next to the search box for the same reason - the A-Z
bar alone can spell a word but can't separate two of them or fix a mis-tap -
and Clear is its own oversized button rather than QLineEdit's built-in
(tiny, mouse-sized) clear icon.

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

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
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
        self._search_text = ""

        self.stats = dim_label("")
        self.header.insertWidget(1, self.stats)

        # Artists/Albums/Tracks/Genre used to be a pill row right here; they
        # now live in the sidebar (app.py:_build_nav), nested under Library,
        # so this row is just the search controls.
        #
        # 2026-09-13 follow-up: stored on self (not just a local variable)
        # so _build_details() below - called later in this same __init__,
        # but from a separate method scope - can still reach it to insert
        # the "Videos only" checkbox (see _build_details).
        header_controls = QHBoxLayout()
        self.header_controls = header_controls
        header_controls.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search this view…")
        self.search_box.setFixedWidth(640)
        self.search_box.setToolTip(
            "Searches whatever's on screen: Artists by artist name, Albums "
            "by album title, Tracks by album or track title, Title Details "
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

        ctx.libraryChanged.connect(self._on_library_changed)
        ctx.openArtistRequested.connect(self._open_artist_from_elsewhere)
        ctx.openReleaseRequested.connect(self._open_release_from_elsewhere)

    # -- construction ---------------------------------------------------------

    def _build_album_grid(self) -> QWidget:
        # no sort picker here - always by Artist (see "Sort chips removed
        # from Library grids" in architecture.md, 2026-09-05)
        self.album_grid = CoverGrid(
            sorts=ALBUM_SORTS, noun="album",
            default_sort="subtitle", show_sort_picker=False,
        )
        self.album_grid.tileActivated.connect(
            lambda rid: self._open_release(rid, [
                ("Albums", lambda: self.panes.setCurrentIndex(PANE_ALBUM_GRID)),
            ])
        )
        self.album_grid.videoActivated.connect(self._activate_video)
        self.album_grid.videoGroupActivated.connect(self._activate_video_group)
        self.album_grid.updated.connect(self._refresh_jump_bar)
        return self.album_grid

    def _build_artist_grid(self) -> QWidget:
        # no sort picker here - always by Name (see "Sort chips removed
        # from Library grids" in architecture.md, 2026-09-05)
        # art_size=150 (smaller than CoverGrid's own 190px default) - James:
        # "can we make the artist pictures just a bit smaller? I want to be
        # able to fit more on the page" (2026-09-07) - fits more portraits
        # per row/page without shrinking them down to the 130px size the
        # artist page's own embedded "More from this artist" grid uses
        # (artist_panel.py), which is small enough to read as a strip
        # rather than a browsable grid of its own.
        self.artist_grid = CoverGrid(
            sorts=ARTIST_SORTS, shape=SHAPE_CIRCLE, noun="artist",
            default_sort="title", show_sort_picker=False, art_size=150,
        )
        self.artist_grid.tileActivated.connect(self._open_artist)
        self.artist_grid.videoActivated.connect(self._activate_video)
        self.artist_grid.videoGroupActivated.connect(self._activate_video_group)
        self.artist_grid.updated.connect(self._refresh_jump_bar)
        return self.artist_grid

    def _build_artist_detail(self) -> QWidget:
        self.artist_detail = ArtistDetailPanel(self.ctx)
        # the page's only ancestor crumb ("Artists") always jumps to the
        # grid, so the crumb index is irrelevant here
        self.artist_detail.breadcrumb.crumbActivated.connect(
            lambda _index: self.panes.setCurrentIndex(PANE_ARTIST_GRID)
        )
        self.artist_detail.releaseActivated.connect(self._open_release_from_artist)
        self.artist_detail.videoActivated.connect(self._open_video_from_artist)
        return self.artist_detail

    def _open_release_from_artist(self, release_id: int) -> None:
        artist_name = self.artist_detail.name.text() or "Artist"
        self._open_release(release_id, [
            ("Artists", lambda: self.panes.setCurrentIndex(PANE_ARTIST_GRID)),
            (artist_name, lambda: self.panes.setCurrentIndex(PANE_ARTIST_DETAIL)),
        ])

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
        return self.album_detail

    def _on_release_breadcrumb(self, index: int) -> None:
        if 0 <= index < len(self._release_breadcrumb_actions):
            self._release_breadcrumb_actions[index]()

    def _build_details(self) -> QWidget:
        self.details_table = TrackDetailsTable()
        self.details_table.ratingChanged.connect(self._on_rating_changed)
        self.details_table.videoActivated.connect(self._activate_video)
        self.details_table.trackActivated.connect(self._play_from_details)
        self.details_table.updated.connect(self._refresh_jump_bar)
        # There used to be a `jukeboxToggleRequested` connection here too,
        # wired to `_on_jukebox_toggle_requested`/`_open_jukebox_picker_for`
        # (a Jukebox column, added 2026-09-07). Both the signal and those
        # two handlers are gone as of a 2026-09-13 follow-up (James: "remove
        # the jukebox option on the Title details table to give me more
        # space for the title of the song") - see track_details_table.py's
        # module docstring for the removal's full rationale. Adding a track
        # to the jukebox board is still possible from Now Playing's own
        # toggle or the Jukebox page's own "+ Add to jukebox" picker.

        # 2026-09-13, second follow-up - James: "move the Videos only
        # checkbox up on the Search this View..., to the right of the
        # Space and backspace buttons" and "don't take up an entire row
        # for just that Videos only." TrackDetailsTable still owns the
        # checkbox itself (construction, styling, the toggle wiring into
        # its own `_apply_filter`) - see its own docstring - this just
        # reparents that one widget into the shared search header instead
        # of the table's own layout.
        #
        # Third follow-up, same day ("move checkbox and Videos only
        # completely to the right, justified right"): plain `addWidget`
        # rather than an `insertWidget` at some index among the search
        # buttons - `header_controls` already ends with the `addStretch(1)`
        # from `__init__` by the time this method runs, so appending here
        # lands the checkbox after that stretch, flush against the header's
        # right edge, with the flexible space it just ate pushing it there
        # rather than the search box/buttons cluster on the left.
        self.header_controls.addWidget(self.details_table.videos_only_checkbox)
        # Title Details is the only presentation this checkbox means
        # anything for, so it starts hidden (the header's `_mode` is still
        # whatever it was before Title Details was first built) and
        # `_on_mode_changed` below shows/hides it alongside every other
        # Title-Details-only sync it already does for the A-Z bar.
        self.details_table.videos_only_checkbox.setVisible(self._mode == BROWSE_DETAILS)

        return self._pane("Title Details", self.details_table, name="details_title")

    def _pane(self, heading: str, widget: QWidget, name: str) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel(heading)
        label.setObjectName("Crumb")
        setattr(self, name, label)
        layout.addWidget(label)
        layout.addWidget(widget, 1)
        return box

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
        # 2026-09-13, second follow-up (see _build_details) - the "Videos
        # only" checkbox now lives in the shared search header, so it has
        # to be shown/hidden by hand on a mode switch the same way the A-Z
        # bar already is below, rather than getting that for free from
        # QStackedWidget swapping panes the way it did as part of Title
        # Details' own layout.
        self.details_table.videos_only_checkbox.setVisible(self._mode == BROWSE_DETAILS)
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
        scroll-snapped one `_on_letter_key` gets - typing "RU" already narrows
        the Artists grid to nothing starting with "S", but "S" still has to
        land in the box as the next letter of "RUSH". '#' isn't a real
        character, so it never gets typed, only used for its usual jump. Uses
        insert(), same as the Space button, so it lands at the cursor rather
        than always at the end - one less surprise if the box already has
        text and a caret.
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
        docstring, 2026-09-06). Shared by both grids since a video tile means
        the same thing in either: tap it, and Videos opens and plays it."""
        tiles: list[GridTile] = []
        for row in video_svc.list_videos_for_search(session):
            tiles.append(GridTile(
                key=row["video_id"],
                title=f"▶ {row['title']}",
                subtitle=row["artist_name"],
                year=row["year"],
                cover_path=row["artist_image_path"],
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
                        subtitle=(
                            f"{release_count} release"
                            f"{'s' if release_count != 1 else ''} · "
                            f"{track_count} track{'s' if track_count != 1 else ''}"
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

    def _open_artist(self, artist_id: int) -> None:
        self.artist_detail.set_artist(artist_id)
        self.panes.setCurrentIndex(PANE_ARTIST_DETAIL)

    def _open_video_from_artist(self, video_id: int) -> None:
        """A tap on the artist page's "Music videos" list."""
        self._activate_video(video_id)

    def _activate_video(self, video_id: int) -> None:
        """A search-matched video (in any of the four presentations - see the
        module docstring, 2026-09-06) or an artist page's own video list
        (`_open_video_from_artist`) switches to the Videos tab and plays it
        there - a video plays in its own embedded pane, never inline inside
        the Library view."""
        self.ctx.navigateRequested.emit("videos")
        self.ctx.playVideoRequested.emit(video_id)

    def _activate_video_group(self, artist: str) -> None:
        """A collapsed "N videos" tile (Albums/Artists grid, 2026-09-06) -
        the search matched this artist's name rather than any one video's
        own title, so there's no single video to hand off to like
        `_activate_video` does. Switches to the Videos tab grouped by
        Artist and scrolled to this artist's section instead of playing
        anything."""
        self.ctx.navigateRequested.emit("videos")
        self.ctx.focusVideoArtistRequested.emit(artist)

    def _open_release(
        self, release_id: int, ancestors: list[tuple[str, Callable[[], None]]]
    ) -> None:
        self._release_breadcrumb_actions = [action for _, action in ancestors]
        self.album_detail.set_breadcrumb_ancestors([label for label, _ in ancestors])
        self.album_detail.set_release(release_id)
        self.panes.setCurrentIndex(PANE_RELEASE_DETAIL)

    def _open_artist_from_elsewhere(self, artist_id: int) -> None:
        """ctx.openArtistRequested - Now Playing's tappable artist name
        (2026-09-06). Sets Artist mode first so the sidebar highlights the
        right Library button for what's actually on screen, the same
        ordering app.py's navigate() uses for its own "library" hand-off."""
        self.select_mode(BROWSE_ARTIST)
        self._open_artist(artist_id)

    def _open_release_from_elsewhere(self, release_id: int) -> None:
        """ctx.openReleaseRequested - Now Playing's tappable album art/title
        (2026-09-06). A release opened this way isn't part of the Albums or
        Artists browsing hierarchy, so its breadcrumb gets a standalone "Now
        Playing" ancestor that jumps back to Now Playing itself, rather than
        one of those grids."""
        self._open_release(release_id, [
            ("Now Playing", lambda: self.ctx.navigateRequested.emit("nowplaying")),
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
                genre="Video",
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
            )
            for v in video_rows
        )
        self.details_table.set_rows(details)
        self.details_title.setText(f"Title Details ({len(rows):,} tracks)")
        self._loaded.add(BROWSE_DETAILS)
        self._apply_search()

    def _on_rating_changed(self, track_id: int, rating: int) -> None:
        with self.ctx.session() as session:
            lib.set_track_rating(session, track_id, rating)

    def _play_from_details(self, track_id: int) -> None:
        """A double-clicked Title Details row (`TrackDetailsTable.
        trackActivated`, 2026-09-06 follow-up) queues the whole currently
        visible/sorted table, starting at that row - the same "queue
        what's on screen" rule `TrackListPanel.play_from` already uses for
        the Tracks presentation's own flat all-library list, since Title
        Details is exactly that same kind of thing. Rows without a
        playable file (missing media, or one that predates a rescan) are
        skipped rather than breaking the queue - the tapped row itself may
        be one of them if its own file went missing since the table last
        loaded, hence matching by track_id below rather than assuming the
        tapped index survives the filter.

        2026-09-07 follow-up (James: double-clicking "Midwest Midnight"
        instead started playing "0.0 Baby", the first row in the table) -
        the tapped row itself can be one of the skipped, not-playable
        ones, and `next(..., 0)` used to fall back to queue position 0
        whenever that happened, silently starting whatever the first
        *playable* row was instead - with nothing on screen to explain
        why a completely different track started. That's distinct from
        the "nothing at all is playable" case above, which already
        notifies; this notifies too, rather than guessing at something
        to play instead."""
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
        app.py:navigate) now that those aren't chips on this page anymore,
        and the same seam tests use rather than reaching into this view's
        internals."""
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
