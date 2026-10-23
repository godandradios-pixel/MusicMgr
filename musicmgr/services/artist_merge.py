"""Merge spelling variants of one artist ("Tim McGraw" / "Tim Mc Graw",
"MercyMe" / "Mercy Me", "B.J. Thomas" / "Bj Thomas" ... - two artists in
the library, each with part of the songs).

`merge_artists` moves everything that points at the variant - song and
album credits, albums, videos, jukebox slots, aliases, a picture or bio the
kept artist lacks - onto the kept artist, deletes the variant, and records
the variant's spelling as an alias. `library.get_or_create_artist` checks
those aliases, so a file still tagged "Bj Thomas" files under B.J. Thomas on
its next scan instead of bringing the duplicate back.

ARTIST_MERGES is a reviewed list (keeping M / MO and M.I.A. / Mia apart and
merging The Kingsmen with King's Men, B.o.B with Bob and The Blue Notes
with Bluenotes). `apply_artist_merges` runs it once per data folder at
startup (ui/app.py), so every PC whose library came from the same files
gets the same clean-up.
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..db.models import (
    Artist,
    ArtistAlias,
    ArtistMembership,
    ArtistSimilar,
    ArtistTopPick,
    ArtistTopTrack,
    Credit,
    JukeboxSlot,
    Release,
    Video,
)
from .matching import normalize, sort_name

#: (kept name, spellings merged into it)
ARTIST_MERGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('Tim McGraw', ('Tim Mc Graw',)),
    # (ARTIST_MERGE_V2_PREF): videos named "for KING + COUNTRY -"
    ('for KING & COUNTRY', ('for KING + COUNTRY', 'for KING and COUNTRY')),
    ('MercyMe', ('Mercy Me',)),
    ('Coldplay', ('Cold Play',)),
    ('NEEDTOBREATHE', ('Need To Breathe',)),
    ('KC', ('K.C.',)),
    ('Blink 182', ('Blink182',)),
    ('The Kingsmen', ('King’s Men', 'Kings Men',)),
    ('LL Cool J', ('L L Cool J',)),
    ('B.B. King', ('Bb King',)),
    ('The Blue Notes', ('Bluenotes',)),
    ('B.J. Thomas', ('Bj Thomas',)),
    ("The B-52's", ('The B‐52s',)),
    ("The Go-Go's", ("The GoGo's",)),
    ('DJ Laz', ('D.J. Laz',)),
    ('Run-DMC', ('Run‐D.M.C.',)),
    ('BulletBoys', ('Bullet Boys',)),
    ("Destiny's Child", ('Destinys Child',)),
    ('*NSYNC', ("'N Sync",)),
    ('OneRepublic', ('One Republic',)),
    ('C.W. McCall', ('CW McCall',)),
    ('Chris de Burgh', ('Chris DeBurgh',)),
    ('JoJo', ('Jo Jo',)),
    ('The Cufflinks', ('The Cuff Links',)),
    ('Yoko / Plastic Ono Band', ('Yoko / The Plastic Ono Band',)),
    ('Da Brat', ('DaBrat',)),
    ('Dababy', ('Da Baby',)),
    ('Julius LaRosa', ('Julius La Rosa',)),
    ('E-40', ('E40',)),
    ('Ray J', ('RayJ',)),
    ('Smash Mouth', ('Smashmouth',)),
    ("Jamie O'Neal", ('Jamie Oneal',)),
    ('K-Ci', ('KCi',)),
    ('B.o.B', ('Bob',)),
    ('O-Town', ('OTown',)),
    ('Staind', ("Stain'd",)),
    ('Theory Of A Deadman', ('Theory Of A Dead Man',)),
    ('Bing Crosby / The Andrews Sisters', ('Bing Crosby-Andrews Sisters',)),
    ('D.J. Lace', ('DJ Lace',)),
    ('The Funny Boners', ('The Funnyboners',)),
    ('The De Castro Sisters', ('DeCastro Sisters',)),
    ('PM Dawn', ('P.M. Dawn',)),
    ('The D.E.Y.', ('The Dey',)),
    ('American Hi Fi', ('American HiFi',)),
    ('B.T. Express', ('Bt Express',)),
    ('Bass Kraft', ('Basskraft',)),
    ('Clarence “Frog Man” Henry', ('Clarence "Frogman" Henry',)),
    ('D.H.T.', ('DHT',)),
    ('Eagle-Eye Cherry', ('EagleEye Cherry',)),
    ('Fat Man Scoop', ('Fatman Scoop',)),
    ('G-Unit', ('GUnit',)),
    ('Gabe Real', ('GabeReal',)),
    ('The Red Jumpsuit Apparatus', ('Red Jump Suit Apparatus',)),
    ('The Blue Jeans', ('The Bluejeans',)),
    ('2 BMF', ('2BMF',)),
    ('B. W. Stevenson', ('Bw Stevenson',)),
    ('C.J.', ('CJ',)),
    ('D.N.A.', ('DNA',)),
    ('D.R.S', ('DRS',)),
    ('Junior M.A.F.I.A.', ('Junior MAFIA',)),
    ('Le Toya', ('Letoya',)),
    ('N.E.R.D', ('Nerd',)),
    ('Peg La Centra', ('Peg LaCentra',)),
    ('Sean Paul of the YoungBloodZ', ('Sean Paul of Youngbloodz',)),
    ('Soul Decision', ('SoulDecision',)),
    ('T.A.T.U.', ('Tatu',)),
    ('The Kinleys', ("The Kinley's",)),
    ('The Wiseguys', ('Wiseguys, The',)),
    ('Thousand Foot Krutch', ('Thousandfootkrutch',)),
    ('Von Ray', ('VonRay',)),
    ('John Cafferty & the Beaver Brown Band', ('John Cafferty Beaver Brown Band',)),
    ('Roberta Flack & Donny Hathaway', ('Roberta Flack Donny Hathaway',)),
)


def _key(name: str) -> str:
    from .scanner import artist_key

    return artist_key(name)


def add_alias(session: Session, artist: Artist, name: str) -> None:
    key = _key(name)
    if not key or key == artist.name_key:
        return
    exists = session.scalar(select(ArtistAlias.id).where(
        ArtistAlias.artist_id == artist.id, ArtistAlias.alias_name_key == key))
    if exists is None:
        session.add(ArtistAlias(artist_id=artist.id, alias_name=name, alias_name_key=key))


def merge_artists(session: Session, loser: Artist, winner: Artist) -> None:
    """Fold `loser` into `winner` and delete it."""
    if loser.id == winner.id:
        return
    lid, wid = loser.id, winner.id
    session.flush()
    session.execute(update(Credit).where(Credit.artist_id == lid).values(artist_id=wid))
    # one credit per song/album and role is enough
    seen: set[tuple] = set()
    for cid, track_id, release_id, role in session.execute(
        select(Credit.id, Credit.track_id, Credit.release_id, Credit.role)
        .where(Credit.artist_id == wid).order_by(Credit.id)
    ):
        key = (track_id, release_id, role)
        if key in seen:
            session.execute(delete(Credit).where(Credit.id == cid))
        seen.add(key)
    session.execute(update(Release).where(Release.album_artist_id == lid).values(album_artist_id=wid))
    session.execute(update(Video).where(Video.artist_id == lid).values(artist_id=wid))
    session.execute(update(JukeboxSlot).where(JukeboxSlot.artist_id == lid).values(artist_id=wid))
    session.execute(update(ArtistAlias).where(ArtistAlias.alias_artist_id == lid).values(alias_artist_id=wid))
    for alias in session.scalars(select(ArtistAlias).where(ArtistAlias.artist_id == lid)).all():
        add_alias(session, winner, alias.alias_name)
    session.execute(delete(ArtistAlias).where(ArtistAlias.artist_id == lid))
    session.execute(update(ArtistMembership).where(ArtistMembership.group_id == lid).values(group_id=wid))
    session.execute(update(ArtistMembership).where(ArtistMembership.member_id == lid).values(member_id=wid))
    if not session.scalar(select(ArtistTopTrack.id).where(ArtistTopTrack.artist_id == wid).limit(1)):
        session.execute(update(ArtistTopTrack).where(ArtistTopTrack.artist_id == lid).values(artist_id=wid))
    if not session.scalar(select(ArtistTopPick.id).where(ArtistTopPick.artist_id == wid).limit(1)):
        session.execute(update(ArtistTopPick).where(ArtistTopPick.artist_id == lid).values(artist_id=wid))
    for table in (ArtistTopTrack, ArtistTopPick, ArtistSimilar):
        session.execute(delete(table).where(table.artist_id == lid))
    for field in ("image_path", "profile", "real_name", "urls", "spotify_id"):
        if not getattr(winner, field, None) and getattr(loser, field, None):
            setattr(winner, field, getattr(loser, field))
    name = loser.name
    session.execute(delete(Artist).where(Artist.id == lid))
    session.expire_all()
    winner = session.get(Artist, wid)
    add_alias(session, winner, name)
    session.flush()


def _find(session: Session, name: str) -> Optional[Artist]:
    key = _key(name)
    artist = session.scalar(select(Artist).where(Artist.name_key == key).limit(1))
    if artist is None:
        artist = session.scalar(select(Artist).where(Artist.name == name).limit(1))
    return artist


def _weight(session: Session, artist: Artist) -> int:
    songs = session.scalar(select(func.count(Credit.id)).where(Credit.artist_id == artist.id)) or 0
    albums = session.scalar(select(func.count(Release.id)).where(Release.album_artist_id == artist.id)) or 0
    return songs + 5 * albums


def apply_artist_merges(session: Session,
                        merges: Iterable[tuple[str, Iterable[str]]] = ARTIST_MERGES) -> int:
    """Merge each group onto its kept name; returns how many artists were
    merged away. Groups with nothing to merge just get their aliases."""
    merged = 0
    for keep, variants in merges:
        names = [keep, *variants]
        found: list[Artist] = []
        for name in names:
            artist = _find(session, name)
            if artist is not None and artist not in found:
                found.append(artist)
        if not found:
            continue
        winner = next((a for a in found if a.name == keep), None) \
            or max(found, key=lambda a: _weight(session, a))
        for loser in [a for a in found if a.id != winner.id]:
            merge_artists(session, loser, winner)
            merged += 1
        winner = session.get(Artist, winner.id)
        if winner.name != keep and not session.scalar(
            select(Artist.id).where(Artist.name_key == _key(keep), Artist.id != winner.id)
        ):
            add_alias(session, winner, winner.name)
            winner.name, winner.name_key, winner.name_sort = keep, _key(keep), sort_name(keep)
        for name in variants:
            add_alias(session, winner, name)
        session.flush()
    return merged
