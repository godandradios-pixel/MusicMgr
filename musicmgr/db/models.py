"""
Discogs-inspired relational model for a local music library.

Layering, from most abstract to most concrete:

    Master        a "work" / release group   ("Rumours")
      Release     a specific issue of it     ("Rumours, 1977 US LP, Warner BSK-3010")
        Track     a position on that release ("A1 Second Hand News")
          MediaFile  the actual bytes on disk (/music/.../01 Second Hand News.flac)

Artists attach to releases and tracks through Credit rows, so an artist can be
the main act on one track, a featured guest on another, and the producer on a
third without duplicating rows. `Release.album_artist_id` is separate from all
of that: it is the one artist a release is *filed under*, independent of who's
credited on any individual track.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------
# association tables
# --------------------------------------------------------------------------

release_genres = Table(
    "release_genres",
    Base.metadata,
    Column("release_id", ForeignKey("releases.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)

release_styles = Table(
    "release_styles",
    Base.metadata,
    Column("release_id", ForeignKey("releases.id", ondelete="CASCADE"), primary_key=True),
    Column("style_id", ForeignKey("styles.id", ondelete="CASCADE"), primary_key=True),
)


# --------------------------------------------------------------------------
# artists
# --------------------------------------------------------------------------


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(400), index=True)
    #: lower-cased, punctuation-stripped form used for dedup and matching
    name_key: Mapped[str] = mapped_column(String(400), index=True)
    #: "Beatles, The" - how the artist files alphabetically
    name_sort: Mapped[Optional[str]] = mapped_column(String(400))
    real_name: Mapped[Optional[str]] = mapped_column(String(400))
    profile: Mapped[Optional[str]] = mapped_column(Text)
    urls: Mapped[Optional[str]] = mapped_column(Text)  # newline separated
    image_path: Mapped[Optional[str]] = mapped_column(Text)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    aliases: Mapped[list["ArtistAlias"]] = relationship(
        back_populates="artist",
        cascade="all, delete-orphan",
        foreign_keys="ArtistAlias.artist_id",
    )
    credits: Mapped[list["Credit"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )
    #: releases this artist is the *album* artist for - see Release.album_artist_id.
    #: A guest verse or a producer credit does not appear here.
    album_releases: Mapped[list["Release"]] = relationship(
        back_populates="album_artist", foreign_keys="Release.album_artist_id"
    )
    #: music videos filed under this artist - see Video.artist_id.
    videos: Mapped[list["Video"]] = relationship(back_populates="artist")
    #: Last.fm's own ranked "top tracks" for this artist, independent of
    #: what James owns - see ArtistTopTrack's own docstring.
    lastfm_top_tracks: Mapped[list["ArtistTopTrack"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("name_key", name="uq_artist_name_key"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Artist {self.id} {self.name!r}>"


class ArtistAlias(Base):
    """'Also known as'. Points at another Artist row when the alias is itself
    a catalogued artist (Discogs models it this way)."""

    __tablename__ = "artist_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    alias_name: Mapped[str] = mapped_column(String(400))
    alias_name_key: Mapped[str] = mapped_column(String(400), index=True)
    alias_artist_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("artists.id", ondelete="SET NULL")
    )

    artist: Mapped["Artist"] = relationship(
        back_populates="aliases", foreign_keys=[artist_id]
    )

    __table_args__ = (UniqueConstraint("artist_id", "alias_name_key", name="uq_alias"),)


class ArtistMembership(Base):
    """Band <-> member edges."""

    __tablename__ = "artist_memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    member_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("group_id", "member_id", name="uq_membership"),)


class ArtistTopTrack(Base):
    """The full ranked list Last.fm returned for one `artist.getTopTracks`
    call (see services/lastfm_popularity.py) - stored independently of
    whether James actually owns any of these songs.

    2026-09-16 follow-up #4 (James: "I don't want the list to be
    constrained by only copies I own, I want the top songs to be their top
    songs whether they are in the library or not") - replaces an earlier
    same-day approach that stamped a popularity score onto individual
    owned `Track` rows (a since-removed `Track.lastfm_popularity` column;
    a database that already picked it up just keeps that column sitting
    there unused, same as every other already-shipped column this project
    has never bothered dropping - see db.session._ensure_columns' own
    docstring). That approach could only ever rank songs James already
    owned, which fell apart the moment he wanted the artist page to read
    like YouTube Music's "Top songs" - a real chart, including songs
    missing from the library.

    Whether a given row is something James owns is worked out at *display*
    time by matching `title` against his library
    (ui/widgets/artist_panel.py:_refresh_top_tracks), not stored here - so
    adding a missing top song to the library later is picked up immediately,
    without needing another fetch.

    Replaced wholesale (delete this artist's rows, then reinsert) on every
    fetch, rather than merged/diffed against whatever was there before -
    "this is what the last fetch said", the same as the one-shot,
    replace-everything shape `Artist.spotify_id`/`Track.spotify_popularity`
    used for their one day of existence.
    """

    __tablename__ = "artist_top_tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    #: 1-based position in Last.fm's own returned order
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    #: Last.fm's playcount for this track specifically - unbounded (a real
    #: playcount, not a 0-100 score), and only ever used to display/sort,
    #: never treated as anything but Last.fm's own number.
    playcount: Mapped[Optional[int]] = mapped_column(Integer)

    artist: Mapped["Artist"] = relationship(back_populates="lastfm_top_tracks")

    __table_args__ = (
        Index("ix_artist_top_tracks_artist_rank", "artist_id", "rank"),
    )


# --------------------------------------------------------------------------
# labels & companies
# --------------------------------------------------------------------------


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(400), index=True)
    name_key: Mapped[str] = mapped_column(String(400), index=True)
    profile: Mapped[Optional[str]] = mapped_column(Text)
    contact_info: Mapped[Optional[str]] = mapped_column(Text)
    parent_label_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("labels.id", ondelete="SET NULL")
    )
    image_path: Mapped[Optional[str]] = mapped_column(Text)

    sublabels: Mapped[list["Label"]] = relationship()
    releases: Mapped[list["Release"]] = relationship(back_populates="label")

    __table_args__ = (UniqueConstraint("name_key", name="uq_label_name_key"),)


class ReleaseCompany(Base):
    """Non-label company credits: 'Pressed By', 'Distributed By', 'Mastered At'."""

    __tablename__ = "release_companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"))
    label_id: Mapped[int] = mapped_column(ForeignKey("labels.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(120))
    catalog_number: Mapped[Optional[str]] = mapped_column(String(120))

    release: Mapped["Release"] = relationship(back_populates="companies")
    label: Mapped["Label"] = relationship()


# --------------------------------------------------------------------------
# genres / styles
# --------------------------------------------------------------------------


class Genre(Base):
    __tablename__ = "genres"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)


class Style(Base):
    __tablename__ = "styles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)


# --------------------------------------------------------------------------
# masters & releases
# --------------------------------------------------------------------------


class Master(Base):
    """The abstract album/single that several Releases are versions of."""

    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    title_key: Mapped[str] = mapped_column(String(500), index=True)
    artist_display: Mapped[Optional[str]] = mapped_column(String(500))
    year: Mapped[Optional[int]] = mapped_column(Integer)
    main_release_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("releases.id", ondelete="SET NULL")
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)

    releases: Mapped[list["Release"]] = relationship(
        back_populates="master", foreign_keys="Release.master_id"
    )


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(500), index=True)
    title_key: Mapped[str] = mapped_column(String(500), index=True)
    #: denormalised "Artist A & Artist B" string for fast list rendering
    artist_display: Mapped[Optional[str]] = mapped_column(String(500))
    #: the release's own artist - who a shelf browser files this under. Not the
    #: same thing as a track's Credit rows: a guest verse or a producer credit
    #: never changes who an album belongs to. NULL only on a release scanned
    #: before this column existed and not yet backed-filled.
    album_artist_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("artists.id", ondelete="SET NULL"), index=True
    )
    label_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("labels.id", ondelete="SET NULL")
    )
    catalog_number: Mapped[Optional[str]] = mapped_column(String(120))
    barcode: Mapped[Optional[str]] = mapped_column(String(120))
    country: Mapped[Optional[str]] = mapped_column(String(120))
    year: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    released: Mapped[Optional[str]] = mapped_column(String(20))  # ISO-ish, may be partial
    is_compilation: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    cover_path: Mapped[Optional[str]] = mapped_column(Text)
    #: 0-100, how much of this release was filled in from real tags
    data_quality: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    master: Mapped[Optional["Master"]] = relationship(
        back_populates="releases", foreign_keys=[master_id]
    )
    album_artist: Mapped[Optional["Artist"]] = relationship(
        back_populates="album_releases", foreign_keys=[album_artist_id]
    )
    label: Mapped[Optional["Label"]] = relationship(back_populates="releases")
    tracks: Mapped[list["Track"]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="Track.disc_no, Track.track_no",
    )
    formats: Mapped[list["ReleaseFormat"]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )
    companies: Mapped[list["ReleaseCompany"]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )
    genres: Mapped[list["Genre"]] = relationship(secondary=release_genres)
    styles: Mapped[list["Style"]] = relationship(secondary=release_styles)
    credits: Mapped[list["Credit"]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        primaryjoin="and_(Credit.release_id==Release.id, Credit.track_id==None)",
    )

    __table_args__ = (
        Index("ix_release_lookup", "title_key", "artist_display", "year"),
    )


class ReleaseFormat(Base):
    """Vinyl / CD / File, plus Discogs-style descriptors (LP, Album, Reissue)."""

    __tablename__ = "release_formats"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(60))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    descriptions: Mapped[Optional[str]] = mapped_column(String(400))  # comma separated
    text: Mapped[Optional[str]] = mapped_column(String(200))  # "180 gram, Blue"

    release: Mapped["Release"] = relationship(back_populates="formats")


# --------------------------------------------------------------------------
# tracks, credits, files
# --------------------------------------------------------------------------


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"), index=True
    )
    #: printed position on the sleeve: "A1", "2-04", "7"
    position: Mapped[Optional[str]] = mapped_column(String(20))
    disc_no: Mapped[int] = mapped_column(Integer, default=1)
    track_no: Mapped[Optional[int]] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500), index=True)
    title_key: Mapped[str] = mapped_column(String(500), index=True)
    artist_display: Mapped[Optional[str]] = mapped_column(String(500))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    isrc: Mapped[Optional[str]] = mapped_column(String(40))
    bpm: Mapped[Optional[float]] = mapped_column(Float)
    musical_key: Mapped[Optional[str]] = mapped_column(String(20))
    comment: Mapped[Optional[str]] = mapped_column(Text)
    rating: Mapped[Optional[int]] = mapped_column(Integer)  # 0-5, user set
    #
    # 2026-09-16 follow-up #2 then #4 - this used to be where a Last.fm
    # (briefly, a Spotify) popularity score got stamped per owned pressing.
    # Removed same-day, once James wanted Top Tracks to show an artist's
    # real top songs whether he owns them or not: a per-Track score can
    # only ever rank what's already in the library. See ArtistTopTrack
    # (this artist's full Last.fm chart, matched against ownership at
    # display time instead) for what replaced it. A database that already
    # picked up this column (or the `spotify_popularity` one before it)
    # just keeps it sitting there unused, same as every other
    # already-shipped column this project has never bothered dropping -
    # see db/session.py's _ensure_columns' own docstring.

    # denormalised playback counters, kept in sync by PlayHistory writes
    play_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    skip_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    release: Mapped["Release"] = relationship(back_populates="tracks")
    files: Mapped[list["MediaFile"]] = relationship(
        back_populates="track", cascade="all, delete-orphan"
    )
    credits: Mapped[list["Credit"]] = relationship(
        back_populates="track",
        cascade="all, delete-orphan",
        primaryjoin="Credit.track_id==Track.id",
    )

    __table_args__ = (
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="ck_rating"),
        Index("ix_track_match", "title_key", "artist_display"),
    )

    @property
    def primary_file(self) -> Optional["MediaFile"]:
        for f in self.files:
            if not f.is_missing:
                return f
        return self.files[0] if self.files else None


class Credit(Base):
    """An artist's involvement in a track or a whole release.

    role examples: Main, Featuring, Remix, Producer, Written-By, Vocals.
    `anv` is Discogs' "artist name variation" - how the name is printed on
    this particular release ("Bowie" for David Bowie).
    """

    __tablename__ = "credits"

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    release_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(120), default="Main", index=True)
    anv: Mapped[Optional[str]] = mapped_column(String(400))
    join_relation: Mapped[Optional[str]] = mapped_column(String(20))  # "&", "feat."
    position: Mapped[int] = mapped_column(Integer, default=0)

    artist: Mapped["Artist"] = relationship(back_populates="credits")
    release: Mapped[Optional["Release"]] = relationship(
        back_populates="credits", foreign_keys=[release_id]
    )
    track: Mapped[Optional["Track"]] = relationship(
        back_populates="credits", foreign_keys=[track_id]
    )

    ROLE_MAIN = "Main"
    ROLE_FEATURING = "Featuring"


class MediaFile(Base):
    """The physical audio file backing a Track. A track may have several
    (FLAC + MP3 transcode), and a file may go missing without losing metadata."""

    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text, unique=True, index=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    mtime: Mapped[Optional[float]] = mapped_column(Float)
    codec: Mapped[Optional[str]] = mapped_column(String(40))
    bitrate: Mapped[Optional[int]] = mapped_column(Integer)
    sample_rate: Mapped[Optional[int]] = mapped_column(Integer)
    channels: Mapped[Optional[int]] = mapped_column(Integer)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    track: Mapped["Track"] = relationship(back_populates="files")


# --------------------------------------------------------------------------
# playlists
# --------------------------------------------------------------------------


class PlaylistFolder(Base):
    """A folder in the playlist tree - organisation only, never a container
    tracks or playlists die with. `parent_id` NULL means top level.

    Deleting a folder (`services/playlists.py:delete_folder`) moves its direct
    subfolders and playlists up to its own parent rather than deleting them -
    there is no cascade delete here on purpose.
    """

    __tablename__ = "playlist_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("playlist_folders.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    parent: Mapped[Optional["PlaylistFolder"]] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["PlaylistFolder"]] = relationship(
        back_populates="parent", order_by="PlaylistFolder.name"
    )
    playlists: Mapped[list["Playlist"]] = relationship(back_populates="folder")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PlaylistFolder {self.id} {self.name!r}>"


class Playlist(Base):
    __tablename__ = "playlists"

    KIND_MANUAL = "manual"
    KIND_SMART = "smart"
    KIND_CHART = "chart"  # frozen snapshot built from a ChartIssue
    KIND_PLAYBACK = "playback"  # derived from this library's play history -
    # the built-in "Most Played" playlists (added 2026-08-30, moved here from
    # the Charts domain: a most-played list is something you queue up and
    # play, which is what a playlist is for, rather than a ranked-with-
    # editions leaderboard like an imported Billboard chart)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default=KIND_MANUAL)
    #: JSON rules for a smart playlist (see services/playlists.py); for a
    #: KIND_PLAYBACK playlist this instead holds a plain PLAYBACK_WINDOWS key
    #: ("week", "month", "all", ...) - not JSON, just the window string.
    rules: Mapped[Optional[str]] = mapped_column(Text)
    #: stable identifier for the built-in playback playlists, same purpose
    #: and shape as Chart.slug - lets `ensure_builtin_playback_playlists`
    #: find/re-seed them by identity even if James renames one, and lets the
    #: UI refuse to delete one outright (it would just reappear). NULL for
    #: every manual/smart/chart-snapshot playlist.
    slug: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True)
    cover_path: Mapped[Optional[str]] = mapped_column(Text)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    #: NULL = top level, same as a folder's own parent_id
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("playlist_folders.id", ondelete="SET NULL"), index=True
    )
    source_issue_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chart_issues.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    folder: Mapped[Optional["PlaylistFolder"]] = relationship(back_populates="playlists")
    items: Mapped[list["PlaylistItem"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )


class PlaylistItem(Base):
    __tablename__ = "playlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    playlist: Mapped["Playlist"] = relationship(back_populates="items")
    track: Mapped["Track"] = relationship()


# --------------------------------------------------------------------------
# jukebox - the physical 45-selector favorites board (2026-09-06). James:
# "I would like the user to be able to select an artist and add 2 songs per
# title strip. This becomes a favorites capability. Any record with 5 star
# rating gets added to a title strip." One JukeboxSlot is one numbered
# physical "record" behind the glass, holding up to two tracks - the A side
# and the B side - by a single artist. See services/jukebox.py for how slots
# get filled (both the automatic 5-star rule and the manual artist/song
# picker funnel through the same `place_track`, which is why every slot's
# two sides always share one `artist_id`) and ui/widgets/jukebox_strip.py for
# how a slot is drawn and played.
# --------------------------------------------------------------------------


class JukeboxSlot(Base):
    __tablename__ = "jukebox_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: the printed number on the title strip - "A7"/"B7" both refer to this
    #: same slot, just its two sides. Assigned once at creation and never
    #: reused or renumbered, even after the slot empties out and is deleted -
    #: exactly like a real jukebox operator not bothering to renumber the
    #: whole machine just because record #7 got pulled.
    slot_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    #: both sides of one physical 45 are by the same act, same as a real
    #: record - see `place_track`'s "same artist" matching.
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True
    )
    side_a_track_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL")
    )
    side_b_track_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL")
    )
    #: which of the five fixed board chips this card is filed under (2026-09-07
    #: follow-up, James: "I would like the jukebox page to have a chip of 5
    #: genres: Classic Rock, Country, Pop, Hairbands, Rock. Then have the pages
    #: of the cards where you can select the location and what genre page a
    #: track will be organized by"). Purely a board-organization tag, not
    #: derived from the track's own tagged Genre(s) - "Hairbands" doesn't
    #: correspond to real genre metadata any file actually carries. The
    #: default literal here must stay in sync with
    #: services/jukebox.py:DEFAULT_JUKEBOX_GENRE - this module can't import
    #: that constant without a circular import (services already imports from
    #: db.models).
    genre: Mapped[str] = mapped_column(String(40), default="Rock", server_default="Rock")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    artist: Mapped["Artist"] = relationship()
    side_a_track: Mapped[Optional["Track"]] = relationship(foreign_keys=[side_a_track_id])
    side_b_track: Mapped[Optional["Track"]] = relationship(foreign_keys=[side_b_track_id])


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------


class ChartFolder(Base):
    """A folder in the charts tree - same shape and purpose as
    `PlaylistFolder`: organisation only, never a container a chart dies with.
    `parent_id` NULL means top level.

    Deleting a folder (`services/charts.py:delete_folder`) moves its direct
    subfolders and charts up to its own parent rather than deleting them.
    """

    __tablename__ = "chart_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chart_folders.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    parent: Mapped[Optional["ChartFolder"]] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["ChartFolder"]] = relationship(
        back_populates="parent", order_by="ChartFolder.name"
    )
    charts: Mapped[list["Chart"]] = relationship(back_populates="folder")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChartFolder {self.id} {self.name!r}>"


class Chart(Base):
    """A named chart series, e.g. 'Billboard Hot 100' - a ranked, dated
    leaderboard imported from CSV. The built-in "Most Played" lists used to
    live here as a KIND_PLAYBACK chart (added 2026-08-30) but moved to
    Playlist.KIND_PLAYBACK the same day: they're something you queue up and
    play, not a ranked-with-editions leaderboard, so Playlists fits the
    concept better than Charts does. `kind` is kept as a column (rather than
    dropped back to a single implicit kind) in case a second external-vs-
    derived distinction shows up here again."""

    __tablename__ = "charts"

    KIND_EXTERNAL = "external"  # imported from CSV

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default=KIND_EXTERNAL)
    source: Mapped[Optional[str]] = mapped_column(String(300))
    description: Mapped[Optional[str]] = mapped_column(Text)
    #: how many positions a full issue of this chart has (100 for the Hot 100)
    size: Mapped[Optional[int]] = mapped_column(Integer)
    #: NULL = top level, same convention as a folder's own parent_id
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chart_folders.id", ondelete="SET NULL"), index=True
    )

    folder: Mapped[Optional["ChartFolder"]] = relationship(back_populates="charts")
    issues: Mapped[list["ChartIssue"]] = relationship(
        back_populates="chart",
        cascade="all, delete-orphan",
        order_by="ChartIssue.chart_date.desc()",
    )


class ChartIssue(Base):
    """One dated edition of a chart (the week of 1984-03-10)."""

    __tablename__ = "chart_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    chart_id: Mapped[int] = mapped_column(ForeignKey("charts.id", ondelete="CASCADE"))
    chart_date: Mapped[dt.date] = mapped_column(Date, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(300))
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    chart: Mapped["Chart"] = relationship(back_populates="issues")
    entries: Mapped[list["ChartEntry"]] = relationship(
        back_populates="issue",
        cascade="all, delete-orphan",
        order_by="ChartEntry.rank",
    )

    __table_args__ = (UniqueConstraint("chart_id", "chart_date", name="uq_issue"),)


class ChartEntry(Base):
    """One row of one issue. `track_id` is nullable: a chart can list songs you
    do not own, and those stay visible as gaps in the library."""

    __tablename__ = "chart_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("chart_issues.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(500))
    artist_name: Mapped[str] = mapped_column(String(500))
    title_key: Mapped[str] = mapped_column(String(500), index=True)
    artist_key: Mapped[str] = mapped_column(String(500), index=True)
    last_week: Mapped[Optional[int]] = mapped_column(Integer)
    peak_pos: Mapped[Optional[int]] = mapped_column(Integer)
    weeks_on_chart: Mapped[Optional[int]] = mapped_column(Integer)

    track_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), index=True
    )
    #: 0.0-1.0 confidence of the automatic match; 1.0 when confirmed by hand
    match_score: Mapped[Optional[float]] = mapped_column(Float)
    match_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    issue: Mapped["ChartIssue"] = relationship(back_populates="entries")
    track: Mapped[Optional["Track"]] = relationship()

    __table_args__ = (UniqueConstraint("issue_id", "rank", name="uq_entry_rank"),)


# --------------------------------------------------------------------------
# playback history & app state
# --------------------------------------------------------------------------


class PlayEvent(Base):
    """One listen. Everything in the 'playback charts' section is aggregated
    from this table."""

    __tablename__ = "play_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    ms_played: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    #: where the play was started from: "library", "playlist:12", "chart:3"
    source: Mapped[Optional[str]] = mapped_column(String(60))

    track: Mapped["Track"] = relationship()


class WatchedFolder(Base):
    """A folder MusicMgr watches for media - scanned for *both* music and
    videos whenever "Scan now" runs (2026-09-07 follow-up, James: "I really
    don't need a different block for audio and video. Just one block to add
    folders and then one option to scan all those folders").

    Replaces the earlier `LibraryFolder`/`VideoFolder` split, which kept
    audio and video watch-lists as two separate tables scanned by two
    separate buttons - James found that split itself to be the problem, not
    something worth preserving. Every `WatchedFolder` is now scanned by
    *both* `services.scanner.scan_folder` and `services.video_scanner.
    scan_video_folder`; each simply finds whatever files it understands and
    ignores the rest (an audio-only folder yields nothing to the video
    scanner, and vice versa), which costs one extra directory walk for the
    file type that isn't there but buys one unified list instead of two.

    A one-time migration (`db.session._migrate_watched_folders`) copies any
    `library_folders`/`video_folders` rows an upgraded database already has
    into this table the first time it's opened after upgrading; those two
    old tables are left in place afterward, unused, rather than dropped -
    consistent with this project's "no Alembic, additive-only" migration
    approach (see db/session.py)."""

    __tablename__ = "watched_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------
# videos
# --------------------------------------------------------------------------


class Video(Base):
    """A music video file.

    Deliberately its own flat table rather than living inside the
    Master/Release/Track hierarchy: a video is one file with a title and a
    performer, never a multi-track "release" the way an album is, so the
    extra layers would buy nothing here.

    `artist_id` is the same "whose shelf does this go on" concept as
    `Release.album_artist_id` - resolved the same way a scanned track's
    artist is (`services.library.get_or_create_artist`), so a video for an
    artist already in the music library lands on their existing artist page
    instead of creating a duplicate. It is nullable only in the sense that a
    brand-new artist name still gets a row created for it (get_or_create);
    NULL never happens in practice, but the FK stays SET NULL rather than
    CASCADE so deleting an artist by hand never silently deletes their videos.
    """

    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    title_key: Mapped[str] = mapped_column(String(500), index=True)
    artist_display: Mapped[Optional[str]] = mapped_column(String(500))
    artist_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("artists.id", ondelete="SET NULL"), index=True
    )
    year: Mapped[Optional[int]] = mapped_column(Integer)
    #: best-effort only - read via mutagen where the container supports it
    #: (mp4/m4v), left NULL for containers it doesn't (mkv/avi/webm/...)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text, unique=True, index=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    mtime: Mapped[Optional[float]] = mapped_column(Float)
    codec: Mapped[Optional[str]] = mapped_column(String(40))
    #: not populated yet - no frame-grab thumbnailer exists - see
    #: architecture.md's Known extension points. A video without one falls
    #: back to the same generated placeholder tile a cover-less album gets.
    thumbnail_path: Mapped[Optional[str]] = mapped_column(Text)
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    artist: Mapped[Optional["Artist"]] = relationship(back_populates="videos")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Video {self.id} {self.title!r}>"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
