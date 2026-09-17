"""Get-or-create helpers plus the query functions the UI browses with."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    Artist,
    Chart,
    ChartEntry,
    ChartIssue,
    Credit,
    Genre,
    Label,
    Master,
    MediaFile,
    PlayEvent,
    Playlist,
    PlaylistItem,
    Release,
    ReleaseFormat,
    Style,
    Track,
    release_genres,
)
from . import jukebox as jukebox_svc
from .matching import normalize, sort_name

# --------------------------------------------------------------------------
# upserts
# --------------------------------------------------------------------------


def get_or_create_artist(session: Session, name: str, **kwargs) -> Artist:
    key = normalize(name)
    if not key:
        key = "unknown artist"
        name = name or "Unknown Artist"
    artist = session.scalar(select(Artist).where(Artist.name_key == key))
    if artist is None:
        artist = Artist(
            name=name.strip(),
            name_key=key,
            name_sort=sort_name(name.strip()),
            **kwargs,
        )
        session.add(artist)
        session.flush()
    return artist


def get_or_create_label(session: Session, name: str) -> Optional[Label]:
    if not name or not name.strip():
        return None
    key = normalize(name)
    label = session.scalar(select(Label).where(Label.name_key == key))
    if label is None:
        label = Label(name=name.strip(), name_key=key)
        session.add(label)
        session.flush()
    return label


def get_or_create_genre(session: Session, name: str) -> Optional[Genre]:
    name = (name or "").strip()
    if not name:
        return None
    g = session.scalar(select(Genre).where(func.lower(Genre.name) == name.lower()))
    if g is None:
        g = Genre(name=name)
        session.add(g)
        session.flush()
    return g


def get_or_create_style(session: Session, name: str) -> Optional[Style]:
    name = (name or "").strip()
    if not name:
        return None
    s = session.scalar(select(Style).where(func.lower(Style.name) == name.lower()))
    if s is None:
        s = Style(name=name)
        session.add(s)
        session.flush()
    return s


def get_or_create_master(
    session: Session, title: str, artist_display: str, year: Optional[int]
) -> Master:
    key = normalize(title)
    stmt = select(Master).where(
        Master.title_key == key, Master.artist_display == artist_display
    )
    master = session.scalar(stmt)
    if master is None:
        master = Master(
            title=title, title_key=key, artist_display=artist_display, year=year
        )
        session.add(master)
        session.flush()
    elif year and (master.year is None or year < master.year):
        master.year = year
    return master


def get_or_create_release(
    session: Session,
    title: str,
    artist_display: str,
    year: Optional[int] = None,
    catalog_number: Optional[str] = None,
    **kwargs,
) -> Release:
    key = normalize(title)
    stmt = select(Release).where(
        Release.title_key == key,
        Release.artist_display == artist_display,
    )
    if catalog_number:
        stmt = stmt.where(
            or_(Release.catalog_number == catalog_number, Release.catalog_number.is_(None))
        )
    release = session.scalar(stmt)
    if release is None:
        release = Release(
            title=title,
            title_key=key,
            artist_display=artist_display,
            year=year,
            catalog_number=catalog_number,
            **kwargs,
        )
        session.add(release)
        session.flush()
    return release


def add_credit(
    session: Session,
    artist: Artist,
    role: str = Credit.ROLE_MAIN,
    track: Optional[Track] = None,
    release: Optional[Release] = None,
    position: int = 0,
    anv: Optional[str] = None,
) -> Credit:
    stmt = select(Credit).where(
        Credit.artist_id == artist.id,
        Credit.role == role,
        Credit.track_id == (track.id if track else None),
        Credit.release_id == (release.id if release else None),
    )
    credit = session.scalar(stmt)
    if credit is None:
        credit = Credit(
            artist_id=artist.id,
            role=role,
            track_id=track.id if track else None,
            release_id=release.id if release else None,
            position=position,
            anv=anv,
        )
        session.add(credit)
        session.flush()
    return credit


# --------------------------------------------------------------------------
# browsing queries
# --------------------------------------------------------------------------


def list_artists(session: Session, limit: int = 5000) -> Sequence[Artist]:
    """Album artists: everyone a release is filed under.

    Deliberately narrower than "everyone with a Credit row" - a producer, a
    session musician, or a featured guest never shows up here just for that,
    only for being the artist a release itself belongs to. A compilation's
    "Various Artists" credit doesn't count either, since it isn't really
    anyone's album. All of that is still reachable through search().
    """
    stmt = (
        select(Artist)
        .join(Release, Release.album_artist_id == Artist.id)
        .where(Release.is_compilation.is_(False))
        .group_by(Artist.id)
        .order_by(func.lower(func.coalesce(Artist.name_sort, Artist.name)))
        .limit(limit)
    )
    return list(session.scalars(stmt))


def fix_artist_sort_keys(session: Session) -> int:
    """One-time repair for `name_sort` values written by an earlier, buggier
    `sort_name()` (see matching.py) - a tag with irregular spacing after
    "The"/"A"/"An" (James's real "The  Romantics", a stray double space)
    left a leading space in the stored sort key, which then sorted before
    every other artist since a space is "less than" any letter or digit.

    Recomputes every artist's `name_sort` from its current `name` and only
    writes the rows that actually change, so this is cheap and safe to run
    on every startup - same idempotent-cleanup shape as
    `backfill_album_artists` below.
    """
    updated = 0
    for artist in session.scalars(select(Artist)):
        correct = sort_name(artist.name)
        if artist.name_sort != correct:
            artist.name_sort = correct
            updated += 1
    if updated:
        session.flush()
    return updated


def backfill_album_artists(session: Session) -> int:
    """One-time fix-up for a database that predates `Release.album_artist_id`.

    Every scan has always written a release-level Main credit (role=Main,
    track_id=NULL) for the album artist, so that's the primary source; a
    release somehow missing even that falls back to matching `artist_display`
    by name. Only touches rows still NULL, so it is cheap and safe to call on
    every startup - see db/session.py's column-migration note for why this
    exists instead of a real migration.
    """
    unresolved = list(
        session.scalars(select(Release).where(Release.album_artist_id.is_(None)))
    )
    if not unresolved:
        return 0

    credit_rows = session.execute(
        select(Credit.release_id, Credit.artist_id).where(
            Credit.release_id.in_([r.id for r in unresolved]),
            Credit.track_id.is_(None),
            Credit.role == Credit.ROLE_MAIN,
        )
    ).all()
    credit_map: dict[int, int] = {release_id: artist_id for release_id, artist_id in credit_rows}
    updated = 0
    for release in unresolved:
        artist_id = credit_map.get(release.id)
        if artist_id is None and release.artist_display:
            artist_id = session.scalar(
                select(Artist.id).where(
                    Artist.name_key == normalize(release.artist_display)
                )
            )
        if artist_id is not None:
            release.album_artist_id = artist_id
            updated += 1
    session.flush()
    return updated


def merge_compilation_duplicates(session: Session) -> int:
    """One-time cleanup for a database scanned before
    `services/scanner.py:_folder_album_artist` existed: an untagged
    various-artists compilation (one album folder, a different performer
    named on every file, no album-artist tag anywhere) used to create one
    Release per performer instead of one for the whole folder - a 10-track
    box set would show up as 10 near-identical album tiles.

    Finds groups of releases whose tracks' files all live in the exact same
    folder on disk and, only when they also agree on title, merges them into
    one "Various Artists" compilation release. Sharing a folder is the
    signal, never just a title alone - two genuinely different albums that
    happen to share a name but live in different folders are left alone.
    A release whose tracks are scattered across more than one folder is
    skipped too (safer to under-merge than to guess wrong).

    Idempotent, like `backfill_album_artists`: once merged there is nothing
    left to find, so it is cheap to call on every startup.
    """
    rows = session.execute(
        select(Track.release_id, MediaFile.path).join(
            MediaFile, MediaFile.track_id == Track.id
        )
    ).all()
    folders_by_release: dict[int, set[str]] = {}
    for release_id, path in rows:
        folders_by_release.setdefault(release_id, set()).add(str(Path(path).parent))

    releases_by_folder: dict[str, list[int]] = {}
    for release_id, folders in folders_by_release.items():
        if len(folders) == 1:  # a release spanning >1 folder can't be matched safely
            releases_by_folder.setdefault(next(iter(folders)), []).append(release_id)

    merged = 0
    for release_ids in releases_by_folder.values():
        if len(release_ids) <= 1:
            continue
        releases = list(
            session.scalars(select(Release).where(Release.id.in_(release_ids)))
        )
        by_title: dict[str, list[Release]] = {}
        for r in releases:
            by_title.setdefault(r.title_key, []).append(r)
        for group in by_title.values():
            if len(group) > 1:
                merged += _merge_release_group(session, group)
    if merged:
        session.flush()
    return merged


def _merge_release_group(session: Session, group: list[Release]) -> int:
    """Fold every release in `group` into the oldest one (lowest id, i.e.
    first ever scanned), so anything already referencing that id - a
    playlist item, a chart-entry match - keeps pointing at a live row."""
    various = get_or_create_artist(session, "Various Artists")
    group = sorted(group, key=lambda r: r.id)
    survivor, duplicates = group[0], group[1:]

    survivor.artist_display = "Various Artists"
    survivor.is_compilation = True
    survivor.album_artist_id = various.id
    master = get_or_create_master(session, survivor.title, "Various Artists", survivor.year)
    survivor.master_id = master.id
    if master.main_release_id is None:
        master.main_release_id = survivor.id

    if not survivor.cover_path:
        for dup in duplicates:
            if dup.cover_path:
                survivor.cover_path = dup.cover_path
                break

    genre_ids = {g.id for g in survivor.genres}
    style_ids = {s.id for s in survivor.styles}
    for dup in duplicates:
        for g in dup.genres:
            if g.id not in genre_ids:
                survivor.genres.append(g)
                genre_ids.add(g.id)
        for s in dup.styles:
            if s.id not in style_ids:
                survivor.styles.append(s)
                style_ids.add(s.id)
        for track in list(dup.tracks):
            # `Release.tracks` is cascade="all, delete-orphan" (unlike the
            # plain playlist-folder relationships elsewhere in this file) -
            # reassigning the raw `release_id` column leaves the track sitting
            # in `dup`'s already-loaded in-memory `tracks` collection, and
            # deleting `dup` below would cascade-delete it right along with
            # dup despite the FK already pointing at `survivor` in the
            # database. Going through the relationship attribute instead
            # keeps both sides of the in-memory collection in sync, so the
            # delete-orphan cascade correctly sees nothing left to orphan.
            track.release = survivor

    # the release-level "Main" credit each duplicate used to carry named its
    # own single performer - no longer accurate once it's folded into a
    # various-artists release, so it's replaced rather than left stale
    # (track-level credits are untouched; they travel with the track)
    for credit in list(survivor.credits):
        if credit.artist_id != various.id:
            session.delete(credit)
    add_credit(session, various, Credit.ROLE_MAIN, release=survivor)

    # flush the reassignment before deleting the duplicates - otherwise
    # SQLAlchemy's own relationship-cascade bookkeeping can re-query their
    # (now stale) tracks and null the FK back out from under it (same
    # gotcha as services/playlists.py:delete_folder)
    session.flush()
    for dup in duplicates:
        session.delete(dup)
    session.flush()
    return len(duplicates)


def artist_track_count(session: Session, artist_id: int) -> int:
    return (
        session.scalar(
            select(func.count(func.distinct(Credit.track_id))).where(
                Credit.artist_id == artist_id, Credit.track_id.is_not(None)
            )
        )
        or 0
    )


def list_releases(
    session: Session, artist_id: Optional[int] = None, limit: int = 5000
) -> Sequence[Release]:
    stmt = select(Release).options(selectinload(Release.tracks))
    if artist_id is not None:
        stmt = stmt.join(Track, Track.release_id == Release.id).join(
            Credit, Credit.track_id == Track.id
        ).where(Credit.artist_id == artist_id).group_by(Release.id)
    stmt = stmt.order_by(
        func.coalesce(Release.year, 9999), func.lower(Release.title)
    ).limit(limit)
    return list(session.scalars(stmt))


def list_tracks(
    session: Session,
    release_id: Optional[int] = None,
    artist_id: Optional[int] = None,
    limit: int = 20000,
) -> Sequence[Track]:
    stmt = select(Track).options(
        selectinload(Track.files), selectinload(Track.release)
    )
    if release_id is not None:
        stmt = stmt.where(Track.release_id == release_id)
    if artist_id is not None:
        stmt = stmt.join(Credit, Credit.track_id == Track.id).where(
            Credit.artist_id == artist_id
        )
    stmt = stmt.order_by(Track.disc_no, Track.track_no, Track.title).limit(limit)
    return list(session.scalars(stmt).unique())


def list_tracks_for_album_artist(
    session: Session, artist_id: int, limit: int = 20000
) -> Sequence[Track]:
    """Every track on a release this artist is the *album* artist for.

    Unlike `list_tracks(artist_id=...)`, a guest verse on someone else's album
    does not pull that track in - this is what backs the artist page reached
    from the Artists grid, so it has to agree with what put the tile there.
    """
    stmt = (
        select(Track)
        .options(selectinload(Track.files), selectinload(Track.release))
        .join(Release, Release.id == Track.release_id)
        .where(Release.album_artist_id == artist_id, Release.is_compilation.is_(False))
        .order_by(func.coalesce(Release.year, 9999), Track.disc_no, Track.track_no, Track.title)
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def search(session: Session, query: str, limit: int = 200) -> dict:
    """Single search box across artists, releases and tracks."""
    q = normalize(query)
    if not q:
        return {"artists": [], "releases": [], "tracks": []}
    like = f"%{q}%"
    artists = list(
        session.scalars(
            select(Artist).where(Artist.name_key.like(like)).limit(limit)
        )
    )
    releases = list(
        session.scalars(
            select(Release)
            .where(Release.title_key.like(like))
            .order_by(Release.year)
            .limit(limit)
        )
    )
    tracks = list(
        session.scalars(
            select(Track)
            .options(selectinload(Track.files), selectinload(Track.release))
            .where(
                or_(
                    Track.title_key.like(like),
                    Track.artist_display.ilike(f"%{query}%"),
                )
            )
            .limit(limit)
        ).unique()
    )
    return {"artists": artists, "releases": releases, "tracks": tracks}


def library_stats(session: Session) -> dict:
    return {
        "artists": session.scalar(select(func.count(Artist.id))) or 0,
        "releases": session.scalar(select(func.count(Release.id))) or 0,
        "tracks": session.scalar(select(func.count(Track.id))) or 0,
        "files": session.scalar(select(func.count(MediaFile.id))) or 0,
        "missing": session.scalar(
            select(func.count(MediaFile.id)).where(MediaFile.is_missing.is_(True))
        )
        or 0,
        "playlists": session.scalar(select(func.count(Playlist.id))) or 0,
        "charts": session.scalar(select(func.count(Chart.id))) or 0,
        "plays": session.scalar(select(func.count(PlayEvent.id))) or 0,
        "total_ms": session.scalar(select(func.sum(Track.duration_ms))) or 0,
    }


def list_track_details(session: Session, limit: int = 200000) -> list[dict]:
    """One row per track for the Title Details table (Genre / Album Artist /
    Album / Track # / Title / Time / Year / Rating / Comment - see
    ui/widgets/track_details_table.py), replacing the old three-pane Genre
    browser (2026-09-05; Album column added 2026-09-06; playback fields
    added 2026-09-06 follow-up; Comment column added 2026-09-17, while
    chasing why a Comment-based smart playlist rule matched nothing - see
    services/scanner.py's `_first_id3_comment`). A Jukebox column also
    lived there from 2026-09-07 until it was removed again 2026-09-13 (see
    that file's module docstring) - `on_jukebox` is still computed and
    returned below regardless, since `_row_matches` there still lets a
    search for the word "jukebox" find these tracks even with no dedicated
    column to show it.

    `on_jukebox` is looked up as one batched `jukebox_svc.board_track_ids`
    query up front rather than a per-row membership check - the same "one
    query, not N" rule this function already follows for genres and playable
    files, and the same lesson this table learned once before the hard way
    on a real, tens-of-thousands-of-rows library.

    Genre is release-level, not a track's own column (see `release_genres`),
    so it's joined through the track's release; a release tagged with more
    than one genre shows them all, slash-separated, rather than picking one
    arbitrarily. Album Artist prefers the release's linked `Artist` row and
    falls back to the denormalised `artist_display` string for a release
    that predates `album_artist_id` and hasn't been backfilled. Album is
    simply the release's own title. Returns plain dicts rather than ORM
    objects since a real library is the *entire* table at once - tens of
    thousands of rows - and the UI model this feeds never needs anything but
    these fields.

    `path`/`cover_path`/`artist` (James, 2026-09-06: "double click should
    automatically start playing that track") are what let
    `TrackDetailRow.to_queue_item()` build a real `QueueItem` straight from
    the row the table already has in memory, the same reason
    `ui/widgets/track_panel.py`'s own flat all-library `TrackRow` carries
    them - no second per-track query needed just to hit play. `path` uses
    the same "lowest-id present file" rule as that widget's own `playable`
    subquery: whichever media file for the track isn't flagged missing,
    picking one deterministically if more than one qualifies. `artist` is
    the track's own artist_display, falling back to the album artist for a
    track that never got its own credit - same fallback order
    `QueueItem.from_track` already uses.
    """
    genre_rows = session.execute(
        select(release_genres.c.release_id, Genre.name)
        .join(Genre, Genre.id == release_genres.c.genre_id)
        .order_by(func.lower(Genre.name))
    )
    genres_by_release: dict[int, list[str]] = {}
    for release_id, name in genre_rows:
        genres_by_release.setdefault(release_id, []).append(name)

    on_jukebox_ids = jukebox_svc.board_track_ids(session)

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
            Track, Release.id, Release.title, Release.year,
            Release.artist_display, Artist.name, Release.cover_path,
            MediaFile.path,
        )
        .join(Release, Release.id == Track.release_id)
        .outerjoin(Artist, Artist.id == Release.album_artist_id)
        .outerjoin(playable, playable.c.track_id == Track.id)
        .outerjoin(MediaFile, MediaFile.id == playable.c.file_id)
        .limit(limit)
    )
    rows: list[dict] = []
    for (
        track, release_id, album_title, year, artist_display, album_artist_name,
        cover_path, path,
    ) in session.execute(stmt):
        rows.append({
            "track_id": track.id,
            "genre": " / ".join(genres_by_release.get(release_id, [])),
            "album_artist": album_artist_name or artist_display or "",
            "album": album_title or "",
            "track_no": track.track_no,
            "title": track.title,
            "duration_ms": track.duration_ms,
            "year": year,
            "rating": track.rating,
            "sort_key": track.title_key or track.title.lower(),
            "artist": track.artist_display or artist_display or "",
            "path": path,
            "cover_path": cover_path,
            "on_jukebox": track.id in on_jukebox_ids,
            # 2026-09-17 follow-up (James: "add the comment field to the
            # title details grid") - Track.comment is already loaded on
            # `track` itself (no extra join needed), same as `rating`
            "comment": track.comment or "",
        })
    return rows


def album_artist_id_for_track(session: Session, track: Track) -> Optional[int]:
    """The artist a track's jukebox slot should file under - the same
    `Release.album_artist_id` Title Details' own Album Artist column reads
    (see `list_track_details`). A release that predates that column and
    hasn't been backfilled, or a various-artists compilation, has no single
    artist to file a jukebox slot under, so this returns None rather than
    guessing from `artist_display`."""
    release = track.release
    return release.album_artist_id if release is not None else None


def set_track_rating(session: Session, track_id: int, rating: Optional[int]) -> bool:
    """Persists a Title Details star tap. `rating` is 0-5; both 0 and None
    mean "unrated" - the UI sends 0 for a toggle-off tap (see
    track_details_table.py's `RatingDelegate`), and it's normalised to NULL
    here so an unrated track always looks the same in the database, however
    it got that way.

    Until a 2026-09-07 follow-up, a track that newly reached 5 stars was
    automatically loaded onto the jukebox, and one that dropped back below 5
    was pulled back off. James: "I don't like using my 5 stars to get a
    track on the Jukebox cards" - rating and jukebox membership are now
    fully independent; this function only ever touches `track.rating`.
    Jukebox membership itself was handled by Title Details' own Jukebox
    column (removed 2026-09-13, see track_details_table.py's module
    docstring) and is still handled by Now Playing's toggle - see
    `ui/views/nowplaying.py:NowPlayingView._on_jukebox_toggled` - and the
    Jukebox page's own "+ Add to jukebox" picker."""
    track = session.get(Track, track_id)
    if track is None:
        return False
    track.rating = rating or None
    session.flush()
    return True


#: 2026-09-13 removal note: `toggle_jukebox_membership(session, track_id)`
#: used to live here - a plain flip that turned a track's membership on
#: (always under `jukebox_svc.DEFAULT_JUKEBOX_GENRE`, no genre choice) or
#: off, called by both Title Details' Jukebox column and Now Playing's
#: toggle. James: "I want a better UI for the Jukebox 'picker'... The
#: problem with the tracks is there is no way to select which genre the
#: track should be on" - both callers now handle "turning on" themselves,
#: opening `ui/views/jukebox.py`'s `JukeboxPickerDialog` (pre-filled and
#: pre-checked for the tapped track) so a genre can actually be chosen,
#: rather than funneling through one shared function that had no room for
#: that choice. "Turning off" and the "no resolvable album artist" check
#: are simple enough (`jukebox_svc.find_code_for_track`/`remove_track`,
#: `album_artist_id_for_track` below) that both callers now just do those
#: two steps directly rather than through a middleman function only one of
#: whose two branches they could still share.


def format_duration(ms: Optional[int]) -> str:
    if not ms:
        return "--:--"
    total = int(ms // 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
