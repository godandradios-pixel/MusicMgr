"""Artist page: header with counts and play actions, their releases, a
biography, and a short "top tracks" taste of their most-played songs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
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
from .bio_panel import BioPanel
from .common import (
    Breadcrumb,
    CoverArt,
    EmptyState,
    PopularityDownloadThread,
    TouchButton,
    TouchList,
    dim_label,
)
from .cover_grid import GridTile
from .gatefold_coverflow import GatefoldCoverflow

#: how many of an artist's most-played tracks the Top Tracks list shows -
#: a taste, not a full sortable browse (Title Details already does that,
#: with its own Artist/Album group-by - see track_details_table.py)
TOP_TRACKS_COUNT = 5

#: 2026-09-16 same-day follow-up #6 (James, looking at the list on an
#: ultrawide window: "too much blank space tween the song title on the
#: left and the number of plays to the far right") - RowDelegate always
#: right-anchors its "trail" text (see common.py's _paint_payload) to
#: whatever width the row itself is, and with the Profile toggle gone
#: (follow-up #5) this list's container now stretches to the page's full
#: width like everything else here - on a wide window that left a huge gap
#: between a short title and the playcount. Capping the whole section's
#: width keeps the row's own text close to its trail regardless of how
#: wide the window is, the same fix in spirit as TwoColumnRowDelegate's
#: (release page's own two-column tracklist) reason for existing, just a
#: plain width cap rather than a second column - this is a top-5 taste
#: list, not a full tracklist with enough rows to fill two columns.
TOP_TRACKS_MAX_WIDTH = 780


def _normalize_title(text: str) -> str:
    """Same qualifier-stripping shape as lastfm_popularity._normalize
    (duplicated rather than imported - see that module's own note on
    lyrics_downloader._normalize/artist_bio_downloader._clean_extract for
    why every one of these keeps its own copy) - deliberately identical so
    "the same song" always groups the same way here as it did when
    Track.lastfm_popularity got stamped onto every pressing that matched
    one Last.fm title (see services/lastfm_popularity.py). Used by
    _refresh_top_tracks below to collapse every pressing of one song (the
    studio cut, a live recording, a remaster, an anniversary reissue) into
    a single Top Tracks row - 2026-09-16 follow-up #3, James: "the same
    song shows up as the most popular with a different release", after
    Rush's "Tom Sawyer" filled 4 of the 5 Top Tracks slots (studio,
    40th-anniversary remaster, a live recording, and a compilation cut all
    separately "matched" the same Last.fm entry and all carried the same
    score)."""
    text = (text or "").lower()
    text = re.sub(
        r"\([^)]*(remaster|remastered|edition|version|live|mono|stereo|deluxe|anniversary)[^)]*\)",
        "",
        text,
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@dataclass
class _TopTrackRow:
    """One row of the Top Tracks list - 2026-09-16 follow-up #4 (James: "I
    don't want the list to be constrained by only copies I own, I want the
    top songs to be their top songs whether they are in the library or
    not"). A plain `Track` couldn't represent this list on its own anymore
    once it could include a song James doesn't have a file for at all."""

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
    """"177M plays" - the same shorthand YouTube Music's own "Top songs"
    uses (the screenshot James pointed to when asking for this), rather
    than a bare "177000000"."""
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
    """One artist: their discography as a folding cover row, plus
    play-everything actions.

    Used to also list the artist's music videos underneath the release row
    (Video.artist_id - see services/video_scanner.py); removed 2026-09-15
    (James: "Remove the video list...introduce a Video sidebar menu
    option") in favor of a real "Videos" section of its own (see
    NAV_ITEMS in ui/app.py) rather than duplicating a per-artist listing
    here.

    2026-09-16 follow-up - James, looking at a mocked-up "artist profile"
    page (photo + a short writeup + a top-tracks list): "Add a new menu
    option called artist profile. Display the artist image, collect a
    brief writeup of the artist and include a list of their top tracks in
    order of popularity." Asked where this should live, James chose here
    (this existing page already has the artist's photo and name) over a
    new sidebar section, so this class grew two more sections below the
    release row rather than a new one being built: the biography
    (`BioPanel`, see its own docstring for where the writeup comes from)
    and a short Top Tracks list, below.

    Same-day follow-up - James: "Don't automatically show the profile in
    the scroll window. Force a click on a [Profile] button similar to the
    Play Everything, Shuffle, add to Queue." The first cut always showed
    both new sections, which just made an already-tall page taller by
    default; `self.profile_section` (biography + top tracks together)
    started hidden and only opened once a "Profile" button next to Play
    everything/Shuffle/Add to queue was tapped. Undone by same-day
    follow-up #5 below - both the button and the hidden-by-default section
    are gone now.

    Same-day follow-up #2 - James, asked where "Top Tracks" popularity came
    from: "I was thinking more like youtube playlist count or some
    authoritative source" → first picked Spotify's own artist top-tracks
    endpoint, then switched to Last.fm's equivalent that same day once
    Spotify's Developer Mode turned out to need a Premium subscription and
    (worse) had dropped that endpoint entirely for a personal app's tier -
    see services/lastfm_popularity.py's module docstring for the full
    story. `_refresh_top_tracks` now ranks by `Track.lastfm_popularity`
    (see services/lastfm_popularity.py) when at least one of this
    artist's tracks has it set, falling back to the original play_count-
    based ranking otherwise - so a library with no Last.fm API key
    configured (or one Last.fm simply doesn't recognize) behaves exactly
    as before. A "Fetch popularity" button next to the Top Tracks label
    runs the same lookup as Settings' bulk "Update track popularity from
    Last.fm…", just for this one artist - mirrors BioPanel's own per-
    artist "Fetch bio" button sitting alongside Settings' bulk profile
    download.

    Same-day follow-up #3 - James, on Rush's Top Tracks: "the same song
    shows up as the most popular with a different release. Can't we do
    something like Youtube music has" (a screenshot of YouTube Music's
    "Top songs" showed one row per song, not one per pressing). Every
    pressing of "Tom Sawyer" James owns (the studio cut, a 40th-anniversary
    remaster, a live recording, a compilation cut) matched the same
    Last.fm entry and so carried the same lastfm_popularity, and the old
    ranking treated all four as separate contenders - four of the five Top
    Tracks slots went to one song. `_refresh_top_tracks` now groups every
    pressing of the same song together first (see _normalize_title above)
    and ranks one representative per song, so the list reads as five
    different songs, the way YouTube Music's does - not five rows that
    might repeat.

    Same-day follow-up #4 - James: "I don't want the list to be
    constrained by only copies I own, I want the top songs to be their top
    songs whether they are in the library or not." Until now, Last.fm's
    top tracks only ever got used to *rank* songs James already owned -
    the artist page could never show a song missing from his library at
    all. `services.lastfm_popularity.update_popularity_for_artist` now
    stores the whole fetched chart as `ArtistTopTrack` rows (see that
    model's docstring), independent of ownership; `_refresh_top_tracks`
    shows that chart directly once it exists, matching each entry against
    the library at *display* time (`_owned_by_title`) rather than at fetch
    time, so a song bought later is recognized the next time this page
    opens, no new fetch needed. A row for a song James doesn't own reads
    "Not in your library" and can't be tapped to play (see
    _play_top_track) - everything else about it (rank, title, Last.fm's
    own playcount) still shows.

    Same-day follow-up #5 - James: "I really don't like that Profile
    button and hide profile. If you remove that profile button can you
    display their bio to the right of what's there on the left (picture,
    name, Play Everything, Shuffle, etc." Removes the "Profile"/"Hide
    profile" toggle button and the `self.profile_section` it showed/hid
    entirely - the biography (`self.bio`) no longer has any open/closed
    state of its own. It's now laid out directly inside the header card,
    to the right of the portrait/name/stats/actions column, taking
    whatever width that column doesn't use - always visible, the same as
    the release row below it. Top Tracks, which used to share
    `profile_section` with the biography, keeps its own always-visible
    section below the release row (it never had a hide/show state to give
    up - only the biography did, via the button this follow-up removes).
    Superseded by follow-up #7 below - the biography didn't stay in the
    header card for long.

    Same-day follow-up #6 - James, looking at the Top Tracks list on an
    ultrawide window: "too much blank space tween the song title on the
    left and the number of plays to the far right." With the Profile
    toggle gone (follow-up #5), Top Tracks' own section stretched to the
    page's full width like everything else here, and RowDelegate always
    right-anchors its "trail" text (the playcount) to wherever that row's
    own right edge lands - so a short title on a wide window left a huge
    gap before the playcount. `TOP_TRACKS_MAX_WIDTH` (above) caps that
    section's width so the row's own text stays close to its trail
    regardless of window width - the same fix in spirit as
    TwoColumnRowDelegate's reason for existing (release_panel.py's own
    two-column tracklist), just a plain width cap rather than a second
    column, since this is a top-5 taste list, not a full tracklist with
    enough rows to fill two columns of its own.

    Same-day follow-up #7 - James, still not happy with follow-up #5's
    header-card biography: "create a two column bottom with artist profile
    in first column and then the top songs in a 2nd column to the right of
    the profile." This is also what fixed a real layout bug follow-up #5
    had introduced: crammed into the header card's QHBoxLayout alongside
    the portrait/name/stats/actions, the biography's height was capped to
    whatever that row's *sizeHint* negotiated (driven by the short
    name/stats/actions column, since BioPanel's own internal QScrollArea
    always reports a small sizeHint regardless of how much text is inside
    it) - so a real multi-line biography simply overflowed past the
    header card's bottom edge and drew on top of the release row
    underneath instead of pushing it down. `profile_row` below - a plain
    QHBoxLayout of its own, given a full row below the releases the same
    way Top Tracks already had one - gives the biography column room to be
    exactly as tall as `self.bio.setFixedHeight()` says (matching Top
    Tracks' own fixed height, so the two columns read as a matched pair)
    without fighting the header card for space. The header card itself is
    back to hugging its own content width (portrait + name/stats/actions
    only, `Qt.AlignLeft`) rather than stretching to fill the page - exactly
    what it was before follow-up #5, since the biography that used to
    justify stretching it now lives in its own row instead.
    """

    releaseActivated = Signal(int)

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._artist_id: Optional[int] = None
        self._artist_name: str = ""
        self._tracks: list = []
        #: this artist's Last.fm chart (see ArtistTopTrack's docstring),
        #: rank-ordered, exactly as last fetched - empty until a fetch has
        #: ever run for this artist, in which case _refresh_top_tracks
        #: falls back to ranking locally-owned tracks by play_count alone.
        self._lastfm_top_tracks: list[ArtistTopTrack] = []
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
        #: same one-thread-attribute-per-page-of-work shape as BioPanel's
        #: own _fetch_thread, reset on every set_artist (see there)
        self._popularity_thread: Optional[PopularityDownloadThread] = None

        # 2026-09-16 follow-up - adding the biography and top-tracks
        # sections below the release row made this page taller than it
        # used to be, and (unlike the release row's own horizontal
        # scroll/drag) there was no vertical scrolling anywhere on this
        # page before - it was built assuming everything always fit a
        # fixed touch-panel screen. Wrapping the whole thing in a
        # QScrollArea now means a long biography or a small screen just
        # scrolls instead of clipping silently off the bottom; the release
        # row's own left/right drag-to-browse (GatefoldCoverflow) is a
        # separate, horizontal gesture on its own viewport and doesn't
        # fight with this outer vertical one.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # replaces a lone "‹ Artists" pill - see release_panel.py's
        # Breadcrumb for why (LibraryView wires the one click target this
        # page needs: back to the Artists grid).
        self.breadcrumb = Breadcrumb()
        # the release row's own "N releases" counter shares this row
        # rather than getting one of its own below the header card
        # (2026-09-15, James: "move the 41 releases up a row") - built by
        # GatefoldCoverflow but laid out here, since *where* it sits is
        # this page's call, not that widget's. The pager arrows that used
        # to sit next to it here moved down onto the release row itself,
        # large, flanking the artwork - see GatefoldCoverflow's own
        # docstring (2026-09-16, James: the arrows up here were "too
        # small").
        crumb_row = QHBoxLayout()
        crumb_row.setSpacing(8)
        crumb_row.addWidget(self.breadcrumb)
        crumb_row.addStretch(1)
        layout.addLayout(crumb_row)

        header = QFrame()
        header.setObjectName("Card")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(18)

        self.portrait = CoverArt(120, circular=True)
        hl.addWidget(self.portrait, 0, Qt.AlignVCenter)

        meta = QVBoxLayout()
        meta.setSpacing(4)

        self.name = QLabel("")
        self.name.setStyleSheet("font-size: 26px; font-weight: 700;")
        self.name.setWordWrap(True)
        # a wrapped QLabel inside this many nested layouts (VBox in HBox in a
        # Card frame) can occasionally negotiate a wildly inflated height on
        # first layout, reserving far more vertical space than the actual
        # text needs and leaving a tall blank gap above it - two lines of a
        # 26px name is already a very long artist name, so cap it there as a
        # hard ceiling regardless of what the layout negotiation computes
        self.name.setMaximumHeight(72)
        meta.addWidget(self.name)

        self.stats = dim_label("")
        meta.addWidget(self.stats)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        play = TouchButton("Play everything", primary=True)
        play.clicked.connect(lambda: self.play(shuffle=False))
        shuffle = TouchButton("Shuffle")
        shuffle.clicked.connect(lambda: self.play(shuffle=True))
        queue = TouchButton("Add to queue")
        queue.clicked.connect(lambda: self.ctx.enqueue_tracks(self._tracks))
        for b in (play, shuffle, queue):
            actions.addWidget(b)
        actions.addStretch(1)
        meta.addLayout(actions)
        # meta (and therefore the card) hugs its own content width rather
        # than stretching to the full page width - see the class docstring's
        # same-day follow-up #7 for why the biography that briefly lived
        # here (follow-up #5) moved out into its own row below instead.
        hl.addLayout(meta, 0)
        layout.addWidget(header, 0, Qt.AlignLeft)

        # a scroll/drag-driven "gatefold" row rather than a page-filling
        # grid - an artist can have anywhere from one release to several
        # hundred, and this is one section on a page. See
        # gatefold_coverflow.py for the fold itself; sorted by title, same
        # as the top-level Library grids.
        self.releases = GatefoldCoverflow(noun="release", panel_size=190)
        self.releases.tileActivated.connect(self.releaseActivated.emit)
        layout.addWidget(self.releases, 0)
        crumb_row.addWidget(self.releases.count_label)

        # -- profile: biography + top tracks, side by side - 2026-09-16
        # same-day follow-up #7 (James, on follow-up #5's header-card
        # biography: "still not happy with the layout...create a two column
        # bottom with artist profile in first column and then the top songs
        # in a 2nd column to the right"). Follow-up #5 had squeezed the
        # biography into the header card next to the portrait/name/buttons;
        # that card's height was only ever as tall as the name/stats/action
        # buttons needed, so a real biography's several lines of text simply
        # overflowed past the card's bottom edge and drew on top of the
        # release row underneath rather than pushing it down (QHBoxLayout
        # sizes a row to its tallest *sizeHint*, and BioPanel's internal
        # QScrollArea reports a small one regardless of the text inside it,
        # so the header never actually grew to fit it). Giving the
        # biography its own full row below the releases - the same row
        # Top Tracks already had to itself - sidesteps that entirely: two
        # independent columns, side by side, each free to be as tall as it
        # needs without fighting the other for the header's height.
        profile_row = QHBoxLayout()
        profile_row.setSpacing(24)

        bio_column = QVBoxLayout()
        bio_column.setSpacing(10)
        bio_label = QLabel("Biography")
        bio_label.setObjectName("Crumb")
        bio_column.addWidget(bio_label)
        self.bio = BioPanel(ctx)
        # matches top_tracks_stack's own fixed height below (TOUCH
        # ["row_height"] * TOP_TRACKS_COUNT + 16) so the two columns read as
        # a matched pair rather than one dwarfing the other - BioPanel's own
        # internal QScrollArea (already there for the empty state) handles
        # a biography longer than this box within it, same as it always has.
        self.bio.setFixedHeight(TOUCH["row_height"] * TOP_TRACKS_COUNT + 16)
        bio_column.addWidget(self.bio, 0)
        # stretch factor so the biography column soaks up whatever width
        # the (capped-width) Top Tracks column on the right doesn't use -
        # unlike that column, prose reads fine at any width, so there's no
        # reason to cap this one too.
        profile_row.addLayout(bio_column, 1)

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
        self.fetch_popularity_btn = TouchButton("Fetch popularity")
        self.fetch_popularity_btn.clicked.connect(self._on_fetch_popularity_clicked)
        top_row.addWidget(self.fetch_popularity_btn)
        top_tracks_layout.addLayout(top_row)

        self.top_tracks_empty = EmptyState(
            "Nothing played yet",
            "Play some of this artist's tracks and their favorites will show up here.",
        )
        # the standard (not the shorter tree_row_height) row height -
        # RowDelegate's primary/secondary two-line layout is tuned for it,
        # and the whole page scrolls now (see above) so there's no need to
        # shrink rows just to save vertical space.
        self.top_tracks = TouchList()
        self.top_tracks.itemActivatedPayload.connect(self._play_top_track)

        self.top_tracks_stack = QStackedWidget()
        self.top_tracks_stack.addWidget(self.top_tracks_empty)  # 0
        self.top_tracks_stack.addWidget(self.top_tracks)  # 1
        self.top_tracks_stack.setFixedHeight(TOUCH["row_height"] * TOP_TRACKS_COUNT + 16)
        top_tracks_layout.addWidget(self.top_tracks_stack, 0)

        # a stretch factor here too (not just a bare addWidget) - passing
        # none would leave it sized to its own small sizeHint instead of
        # filling out to TOP_TRACKS_MAX_WIDTH's cap; the cap itself is what
        # keeps this stretch factor from growing the column past a sensible
        # reading width the way the biography column to its left is allowed
        # to (see profile_row.addLayout(bio_column, 1) above).
        profile_row.addWidget(top_tracks_section, 1)

        layout.addLayout(profile_row)

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

    def set_artist(self, artist_id: Optional[int]) -> None:
        self._artist_id = artist_id
        self._artist_name = ""
        self._tracks = []
        self._lastfm_top_tracks = []
        self._top_tracks = []
        self._playable_top_tracks = []
        # a "Fetch popularity" started for the *previous* artist page has
        # nothing useful to land on this one - the thread itself (if still
        # running) is left alone rather than killed - only the button's
        # re-entrancy guard is reset, matching BioPanel.set_artist.
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
            self.breadcrumb.set_path(["Artists", artist.name])
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
            # them - see class docstring's follow-up #4 and
            # ArtistTopTrack's own docstring.
            self._lastfm_top_tracks = list(
                session.scalars(
                    select(ArtistTopTrack)
                    .where(ArtistTopTrack.artist_id == artist_id)
                    # 2026-09-16 same-day follow-up #5 (James, looking at a
                    # real chart: "why does it seem like after you fetch
                    # popularity, the songs are in order of most popular. For
                    # example, Winner Takes All has 14M plays and the song
                    # above it Mamma Mia has 10M") - Last.fm's own returned
                    # order (ArtistTopTrack.rank, see that model's docstring)
                    # isn't always strictly descending by the playcount
                    # number it also returns for each track - whatever
                    # internal weighting produces Last.fm's chart position
                    # can rank two close tracks in an order that doesn't
                    # match their displayed playcounts. Sorting by playcount
                    # itself here instead means the list James sees is
                    # always in the order of the numbers actually printed on
                    # it. `func.coalesce(..., -1)` pushes a track with no
                    # playcount at all (Last.fm returned nothing usable -
                    # see _fetch_top_tracks) to the bottom rather than the
                    # top, and `.rank` is still the tiebreaker for equal/
                    # missing playcounts, so the order stays fully
                    # deterministic rather than however sqlite happens to
                    # return ties.
                    .order_by(func.coalesce(ArtistTopTrack.playcount, -1).desc(), ArtistTopTrack.rank)
                )
            )
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
        self._refresh_top_tracks()
        # same reasoning as ReleaseDetailPanel.set_release - lets the
        # bottom PlayerBar's play button start this artist's tracks even
        # before "Play everything"/a release/a track's been tapped.
        self.ctx.set_viewing(self._tracks, source=f"artist:{artist_id}")

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
        # only worth re-pulling this artist's chart (and therefore
        # re-ranking Top Tracks) if this fetch's result still applies to
        # whatever artist is currently on screen - a slow fetch finishing
        # after James has already moved on to a different artist page
        # shouldn't repaint it out from under him.
        if self._artist_id is not None:
            with self.ctx.session() as session:
                self._tracks = list(lib.list_tracks_for_album_artist(session, self._artist_id))
                self._lastfm_top_tracks = list(
                    session.scalars(
                        select(ArtistTopTrack)
                        .where(ArtistTopTrack.artist_id == self._artist_id)
                        # sorted by playcount, not fetch-order rank - see
                        # the other ArtistTopTrack query above (set_artist)
                        # for why
                        .order_by(func.coalesce(ArtistTopTrack.playcount, -1).desc(), ArtistTopTrack.rank)
                    )
                )
            self._refresh_top_tracks()

    # -- top tracks -------------------------------------------------------

    def _owned_by_title(self) -> dict[str, Track]:
        """Groups self._tracks by `_normalize_title` and keeps only the
        single best pressing per group (highest play_count, then rating,
        then earliest release year so a studio original wins a tie over a
        live/anniversary reissue, then title, purely for a stable pick) -
        2026-09-16 follow-up #3, see _normalize_title/class docstring.
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
        """Two different ways to build the list, depending on whether a
        Last.fm fetch has ever run for this artist (self._lastfm_top_tracks).

        2026-09-16 follow-up #4 (see class docstring) - when it has, the
        list shows that whole chart: every entry appears whether James owns
        it or not, matched against the library by (normalized) title right
        now (_owned_by_title) rather than whatever ownership state existed
        when the fetch ran - so a song bought later is picked up the next
        time this page opens, no new fetch needed. An unowned entry's row
        reads "Not in your library" and can't be tapped to play (see
        _play_top_track). `chart` below (self._lastfm_top_tracks) already
        comes back from set_artist/_on_popularity_fetch_finished sorted by
        playcount descending, not Last.fm's own fetch-order rank - see the
        2026-09-16 same-day follow-up #5 comment on that query for why.

        Falls back to the pre-follow-up-#4 approach - locally-owned tracks
        only, ranked by Track.play_count (denormalized, kept in sync by
        PlayHistory writes) - only when no Last.fm chart has ever been
        fetched for this artist, so a library with no Last.fm API key
        configured (or one Last.fm doesn't recognize) behaves exactly as
        it did before Last.fm was introduced at all. Ties fall back to
        rating, then title, purely for a stable order.

        Either way, every pressing of the same owned song is collapsed
        into one representative first (follow-up #3) so a song with
        several pressings can't fill multiple slots.
        """
        owned_by_title = self._owned_by_title()

        if self._lastfm_top_tracks:
            self.top_label.setText("Popular on Last.fm")
            chart = self._lastfm_top_tracks[:TOP_TRACKS_COUNT]

            self._top_tracks = []
            self._playable_top_tracks = []
            rows = []
            for i, entry in enumerate(chart):
                owned_track = owned_by_title.get(_normalize_title(entry.title))
                if owned_track is not None:
                    track_index = len(self._playable_top_tracks)
                    self._playable_top_tracks.append(owned_track)
                    secondary = _release_caption(owned_track)
                else:
                    track_index = None
                    secondary = "Not in your library"
                self._top_tracks.append(
                    _TopTrackRow(title=entry.title, track=owned_track, playcount=entry.playcount)
                )
                rows.append({
                    "lead": str(i + 1),
                    "primary": entry.title,
                    "secondary": secondary,
                    "trail": _format_playcount(entry.playcount),
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

    def _play_top_track(self, payload: dict) -> None:
        idx = payload.get("track_index")
        if idx is None:
            # a real row with nothing to play - James doesn't own this
            # song yet (see class docstring's follow-up #4) - rather than
            # silently doing nothing on a tap that looked like it should
            # do something.
            if self._top_tracks:
                self.ctx.notify("You don't own this track yet")
            return
        if not self._playable_top_tracks:
            return
        # plays through the owned Top Tracks only, starting at the tapped
        # row's position among them - not self._tracks (the full
        # discography) and not self._top_tracks (which may include unowned
        # rows with nothing to queue) - "start here and keep going through
        # this artist's other favorites you own" reads better than jumping
        # into wherever that track happens to fall in the full disc/track
        # running order.
        self.ctx.play_tracks(
            self._playable_top_tracks, start=idx, source=f"artist:{self._artist_id}:top"
        )


def _release_caption(track: Track) -> str:
    release = track.release
    if release is None:
        return ""
    return f"{release.title} ({release.year})" if release.year else release.title
