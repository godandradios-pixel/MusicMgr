"""2026-10-19 library audit: artist credits that were split wrongly (or not
at all), and spelling variants of one artist - see matching.split_artists,
scanner.resplit_track_credits and services/artist_merge.py."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import Artist, ArtistAlias, Credit, Release, Track
from musicmgr.services import artist_merge, scanner
from musicmgr.services import library as lib
from musicmgr.services.matching import split_artists


@pytest.mark.parametrize("credit, names", [
    # names that aren't lists stay whole
    ("Blood, Sweat & Tears", ["Blood, Sweat & Tears"]),
    ("Earth, Wind & Fire", ["Earth, Wind & Fire"]),
    ("Earth, Wind and Fire", ["Earth, Wind and Fire"]),
    ("Crosby, Stills, Nash & Young", ["Crosby, Stills, Nash & Young"]),
    ("Peter, Paul & Mary", ["Peter, Paul & Mary"]),
    ("Hank Williams, Jr.", ["Hank Williams, Jr."]),
    ("10,000 Maniacs", ["10,000 Maniacs"]),
    ("Tyler, The Creator", ["Tyler, The Creator"]),
    ("Fisher, Eddie", ["Fisher, Eddie"]),
    ("Tommy Dorsey and His Orchestra", ["Tommy Dorsey and His Orchestra"]),
    ("? and the Mysterians", ["? and the Mysterians"]),
    ("Bruce Hornsby (& The Range)", ["Bruce Hornsby (& The Range)"]),
    # 2026-10-21: the band's "&"/"+" is part of its name
    ("for KING & COUNTRY", ["for KING & COUNTRY"]),
    ("FOR KING + COUNTRY", ["FOR KING + COUNTRY"]),
    ("for KING & COUNTRY + Dolly Parton", ["for KING & COUNTRY", "Dolly Parton"]),
    ("for KING & COUNTRY ft. Lecrae & The WRLDFMS Tony Williams",
     ["for KING & COUNTRY", "Lecrae", "The WRLDFMS Tony Williams"]),
    # lists that are lists come apart
    ("Blood, Sweat & Tears ft. David Clayton-Thomas", ["Blood, Sweat & Tears", "David Clayton-Thomas"]),
    ("Shawn Mendes, Camila Cabello", ["Shawn Mendes", "Camila Cabello"]),
    ("Paul Anka, George Hamilton IV, & Johnny Nash", ["Paul Anka", "George Hamilton IV", "Johnny Nash"]),
    ("Nelly, Paul Wall, and Mystikal", ["Nelly", "Paul Wall", "Mystikal"]),
    ("Pitbull feat. Ne-Yo, Afrojack & Nayer", ["Pitbull", "Ne-Yo", "Afrojack", "Nayer"]),
    ("Marilyn McCoo & Billy Davis, Jr.", ["Marilyn McCoo", "Billy Davis, Jr."]),
    ("Nelly Furtado ft.Timbaland", ["Nelly Furtado", "Timbaland"]),
    ("ft. Enya", ["Enya"]),
    ("ft.\xa0Billy Ray Cyrus", ["Billy Ray Cyrus"]),
    ("Huntrix:\xa0Ejae,\xa0Audrey Nuna\xa0and\xa0Rei Ami", ["Huntrix", "Ejae", "Audrey Nuna", "Rei Ami"]),
    ("Xavier Cugat and His Waldorf-Astoria Orchestra;Loretta Clemens",
     ["Xavier Cugat and His Waldorf-Astoria Orchestra", "Loretta Clemens"]),
    # big-band credits: the singers in brackets
    ("Glenn Miller (Tex Beneke & chorus, vocal)", ["Glenn Miller", "Tex Beneke"]),
    ("Guy Lombardo (Carmen Lombardo, vocal)", ["Guy Lombardo", "Carmen Lombardo"]),
    ("Ozzie Nelson (Ozzie Nelson, vocal)", ["Ozzie Nelson"]),
    ("Ted Weems (voc. Elmer Feldkamp, male trio)", ["Ted Weems", "Elmer Feldkamp"]),
    ("Johnny Mercer (feat. Paul Weston", ["Johnny Mercer", "Paul Weston"]),
    ("Dawn (featuring Tony Orlando)", ["Dawn", "Tony Orlando"]),
    ("Tommy Dorsey and His Orchestra, vocal refrain by Frank Sinatra",
     ["Tommy Dorsey and His Orchestra", "Frank Sinatra"]),
    # recording notes aren't people
    ("Ben Selvin (as ‘Lloyd Keating’)", ["Ben Selvin"]),
    ("Abe Lyman [aka Barry Wood]", ["Abe Lyman"]),
    ("Russ Morgan (Decca version)", ["Russ Morgan"]),
    # ordinary credits unchanged
    ("Daft Punk feat. Pharrell Williams & Nile Rodgers", ["Daft Punk", "Pharrell Williams", "Nile Rodgers"]),
    ("AC/DC", ["AC/DC"]),
    ("", []),
])
def test_split_artists(credit, names):
    assert split_artists(credit) == names


def _track(session, title: str, display: str, credited: list[str]) -> Track:
    release = Release(title=f"{title} album", title_key=title.lower(), artist_display=display)
    session.add(release)
    session.flush()
    track = Track(release_id=release.id, title=title, title_key=title.lower(), artist_display=display)
    session.add(track)
    session.flush()
    for i, name in enumerate(credited):
        lib.add_credit(session, lib.get_or_create_artist(session, name),
                       Credit.ROLE_MAIN if i == 0 else Credit.ROLE_FEATURING, track=track, position=i)
    session.flush()
    return track


def _performers(session, track) -> list[str]:
    return [session.get(Artist, c.artist_id).name for c in session.scalars(
        select(Credit).where(Credit.track_id == track.id).order_by(Credit.position))]


class TestResplitTrackCredits:
    def test_rewrites_wrong_credits_and_removes_the_junk_artists(self, session):
        bst = _track(session, "Spinning Wheel", "Blood, Sweat & Tears", ["Blood, Sweat", "Tears"])
        miller = _track(session, "Moonlight", "Glenn Miller (Tex Beneke & chorus, vocal)",
                        ["Glenn Miller (Tex Beneke", "chorus, vocal)"])
        fine = _track(session, "One More Time", "Daft Punk", ["Daft Punk"])

        changed, removed = scanner.resplit_track_credits(session)

        assert changed == 2
        assert removed == 4
        assert _performers(session, bst) == ["Blood, Sweat & Tears"]
        assert _performers(session, miller) == ["Glenn Miller", "Tex Beneke"]
        assert _performers(session, fine) == ["Daft Punk"]
        names = set(session.scalars(select(Artist.name)))
        assert not names & {"Blood, Sweat", "Tears", "Glenn Miller (Tex Beneke", "chorus, vocal)"}
        assert scanner.resplit_track_credits(session) == (0, 0)

    def test_for_king_and_country_back_together(self, session):
        """2026-10-21: 167 songs on PC1 credited to "for KING" + "COUNTRY"."""
        song = _track(session, "God Only Knows", "for KING & COUNTRY", ["for KING", "COUNTRY"])
        duet = _track(session, "TOGETHER", "for KING & COUNTRY ft. Tori Kelly",
                      ["for KING", "COUNTRY", "Tori Kelly"])

        scanner.resplit_track_credits(session)

        assert _performers(session, song) == ["for KING & COUNTRY"]
        assert _performers(session, duet) == ["for KING & COUNTRY", "Tori Kelly"]
        assert not set(session.scalars(select(Artist.name))) & {"for KING", "COUNTRY"}

    def test_keeps_an_artist_that_is_still_an_album_artist(self, session):
        track = _track(session, "Song", "Earth, Wind & Fire", ["Earth, Wind", "Fire"])
        track.release.album_artist_id = lib.get_or_create_artist(session, "Earth, Wind").id
        session.flush()

        scanner.resplit_track_credits(session)

        assert session.scalar(select(Artist).where(Artist.name == "Earth, Wind")) is not None


class TestArtistMerge:
    def test_merge_moves_everything_and_remembers_the_spelling(self, session):
        a = _track(session, "Hooked On A Feeling", "B.J. Thomas", ["B.J. Thomas"])
        b = _track(session, "Raindrops", "Bj Thomas", ["Bj Thomas"])
        loser = session.scalar(select(Artist).where(Artist.name == "Bj Thomas"))
        b.release.album_artist_id = loser.id
        loser.profile = "Billy Joe Thomas ..."
        session.flush()

        merged = artist_merge.apply_artist_merges(session, [("B.J. Thomas", ("Bj Thomas",))])

        keep = session.scalar(select(Artist).where(Artist.name == "B.J. Thomas"))
        assert merged == 1
        assert session.scalar(select(func.count(Artist.id)).where(Artist.name == "Bj Thomas")) == 0
        assert _performers(session, a) == ["B.J. Thomas"] and _performers(session, b) == ["B.J. Thomas"]
        assert b.release.album_artist_id == keep.id
        assert keep.profile == "Billy Joe Thomas ..."
        assert lib.get_or_create_artist(session, "Bj Thomas").id == keep.id
        assert session.scalar(select(ArtistAlias).where(ArtistAlias.alias_name == "Bj Thomas")) is not None
        assert artist_merge.apply_artist_merges(session, [("B.J. Thomas", ("Bj Thomas",))]) == 0

    def test_the_kept_name_wins_even_with_fewer_songs(self, session):
        _track(session, "One", "OTown", ["OTown"])
        _track(session, "Two", "OTown", ["OTown"])
        _track(session, "Three", "O-Town", ["O-Town"])

        artist_merge.apply_artist_merges(session, [("O-Town", ("OTown",))])

        assert list(session.scalars(select(Artist.name).where(Artist.name.in_(("O-Town", "OTown"))))) == ["O-Town"]

    def test_resplit_after_a_merge_changes_nothing(self, session):
        _track(session, "Live Like You Were Dying", "Tim Mc Graw", ["Tim Mc Graw"])
        _track(session, "Humble And Kind", "Tim McGraw", ["Tim McGraw"])
        artist_merge.apply_artist_merges(session, [("Tim McGraw", ("Tim Mc Graw",))])

        assert scanner.resplit_track_credits(session) == (0, 0)

    def test_reviewed_list_keeps_look_alikes_apart(self):
        groups = {keep: set(v) for keep, v in artist_merge.ARTIST_MERGES}
        assert "MØ" not in groups.get("M", set())
        assert not any("MØ" in v or "Mia" in v for v in groups.values())
        assert "King’s Men" in groups["The Kingsmen"]
