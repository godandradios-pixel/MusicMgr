"""Chart matching: the artist has to be the same act, and Re-match is fast
(2026-10-29 - James: "back in the 30s and 40s a song was sung by many
artists in that time period ... It must include the artist").

The old artist minimum (0.45) let two different acts through on spelling
noise and shared band words - "Riley-Farley Orchestra" matched Tommy
Dorsey's record of the same song, and across PC1's charts ~1,000 songs
were on another act's record ("Rod Wave" / "Matroda"). The minimum is now
0.8, with spelling variants of one act read as the same name.

The speed-ups (in-memory candidate lookup, memoised normalising, the
difflib upper-bound shortcut, one answer per exact song) must give exactly
the same matches as before - checked here on small cases, and on PC1's
whole library (355,557 chart rows) when they were written."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from musicmgr.db.models import Chart, ChartEntry, ChartIssue, Track
from musicmgr.services import charts as chart_svc
from musicmgr.services import library as lib
from musicmgr.services import matching
from musicmgr.services.matching import artist_similarity, normalize, score_pair


class TestSameAct:
    @pytest.mark.parametrize("a, b", [
        ("Victor Young & His Orchestra", "Victor Young"),
        ("Coleman Hawkins and His Orchestra", "Coleman Hawkins"),
        ("Bing Crosby", "Bing Crosby with John Scott Trotter Orchestra"),
        ("Little Jack Little", "Little Jack Little (Little Jack Little, vocal)"),
        ("Ella Fitzgerald & The Ink Spots", "The Ink Spots with Ella Fitzgerald"),
        ("Justin Bieber Duet With Mariah Carey", "Mariah Carey & Justin Bieber"),
        ("t.A.T.u.", "Tatu"),
        ("SOS Band", "The S.O.S. Band"),
        ("N*E*R*D", "Nerd"),
        ("Junior M.A.F.I.A.", "Junior MAFIA"),
        ("Matchbox 20", "Matchbox Twenty"),
        ("Fifth Dimension", "5th Dimension"),
        ("Four Non Blondes", "4 Non Blondes"),
        ('Missy "Misdemeanor" Elliott', "Missy Elliott"),
        ("Clarence Henry", "Clarence “Frog Man” Henry"),
        ("Dave 'baby' Cortez", "Dave Cortez"),
        ("Cash Out", "Ca$h Out"),
        ("Pink", "P!nk"),
        ("The Walker Bros.", "Walker Brothers"),
        ("1910 Fruitgum Company", "The 1910 Fruitgum Co."),
        ("Simon & Garfunkel", "Simon and Garfunkel"),
        ("Sisqó", "Sisqo"),
        ("The Band", "Band"),
        ("Arthur Smith", "Arthur (Guitar Boogie) Smith"),
        ('Nat "King" Cole', "Nat King Cole"),
        ('Evelyn "Champagne" King', "Evelyn Champagne King"),
        ("U.S. Bonds", "Gary \u201cU.S.\u201d Bonds"),
        ("soulDecision featuring Thrust", "Soul Decision"),
        ("H.E.R.", "HER"),
        ("Joji", "Joji & Diplo"),
        ("Dolly Parton, Emmylou Harris & Linda Ronstadt", "Linda, Dolly Parton and Emmylou Harris"),
        ("Jawsh 685 x Jason Derulo", "Jason Derulo & Jawsh 685"),
    ])
    def test_spellings_of_one_act_pass(self, a, b):
        assert artist_similarity(a, b) >= matching.MIN_ARTIST_SIMILARITY

    @pytest.mark.parametrize("a, b", [
        ("Riley-Farley Orchestra", "Tommy Dorsey And His Orchestra"),
        ("Victor Young & His Orchestra", "Glenn Miller and His Orchestra"),
        ("Artie Shaw and His Orchestra", "Benny Goodman and His Orchestra"),
        ("Jack Teter Trio", "Nat King Cole Trio"),
        ("Glenn Miller", "Glen Gray"),
        ("Natalie Cole", "Nat King Cole"),
        ("Billy Currington", "Billy Cunningham"),
        ("Sonny James", "Joni James"),
        ("The Forester Sisters", "Pointer Sisters"),
        ("Rod Wave", "Matroda"),
        ("Post Malone", "The Police"),
        ("DJ Khaled Featuring Quavo & Takeoff", "Beyoncé featuring André 3000"),
        ("H.E.R. featuring Bryson Tiller", "Cher"),
        ("Seal", "Sean"),
        ("Amber", "America"),
        ("Future Featuring Drake", "DJ Khaled ft. Drake"),
    ])
    def test_different_acts_fail(self, a, b):
        assert artist_similarity(a, b) < matching.MIN_ARTIST_SIMILARITY

    def test_same_song_by_another_band_is_not_a_match(self):
        assert score_pair("The Music Goes Round & Round", "Riley-Farley Orchestra",
                          "The Music Goes Round and Round",
                          "Tommy Dorsey And His Orchestra") == 0.0
        assert score_pair("The Music Goes Round & Round", "Riley-Farley Orchestra",
                          "The Music Goes 'Round And Around",
                          "Riley-Farley Orch. (Mike Riley, vocal)") > 0.72


class TestShortcutsGiveTheSameScores:
    PAIRS = [
        ("Hurricane", "MS MR", "Beautiful Hurricane", "MS MR"),
        ("Billie Jean", "Michael Jackson", "Billie Jean (Single Version)", "Michael Jackson"),
        ("Moody Blue/She Thinks I Still Care", "Elvis Presley", "Moody Blue", "Elvis Presley"),
        ("Love Somebody", "Rick Springfield", "Love Somebody", "Rick Springfield"),
        ("Let's Hear It for the Boy", "Deniece Williams", "Let's Hear It For The", "Deniece Williams"),
        ("Dreamline", "Rush", "Dreamline", "Rush with The Clockwork Angels String Ensemble"),
        ("You", "Ten Sharp", "You", "Dan + Shay"),
        ("Gee", "The Crows", "Gee Whiz", "Carla Thomas"),
        ("Tequila", "The Champs", "Tequila", "Champs"),
    ]

    @pytest.mark.parametrize("pair", PAIRS)
    def test_floor_never_changes_a_passing_score(self, pair):
        """With the floors the shortcut may skip difflib, but only when the
        answer would be thrown away anyway - so score_pair is unchanged."""
        t_a, a_a, t_b, a_b = pair
        exact_t = matching.title_similarity(t_a, t_b)
        exact_a = matching.artist_similarity(a_a, a_b)
        expected = (0.0 if exact_t < matching.MIN_TITLE_SIMILARITY
                    or exact_a < matching.MIN_ARTIST_SIMILARITY
                    else round(exact_t * 0.65 + exact_a * 0.35, 4))
        assert score_pair(*pair) == expected


def _owned(session, title, artist) -> Track:
    release = lib.get_or_create_release(session, f"{title} {artist} Album", artist)
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display=artist)
    session.add(track)
    session.flush()
    return track


def _chart(session, name, songs, year=1936):
    chart = Chart(name=name, slug=f"{name}-{year}".lower().replace(" ", "-"))
    session.add(chart)
    session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(year, 12, 31))
    session.add(issue)
    session.flush()
    for rank, (title, artist) in enumerate(songs, start=1):
        session.add(ChartEntry(issue_id=issue.id, rank=rank, title=title, artist_name=artist,
                               title_key=normalize(title), artist_key=normalize(artist),
                               peak_pos=rank))
    session.flush()
    return chart


class TestFastRematch:
    def test_in_memory_lookup_returns_the_sql_rows_in_the_sql_order(self, session):
        # same title, several artists and ids: the exact-title rows come in
        # ix_track_match's order (artist, then id), the word ones by id
        release = lib.get_or_create_release(session, "Mixed", "Various")
        for title, artist in [("Dreamline", "Rush with Strings"), ("Dreamline", "Rush"),
                              ("Dreamlines", "Someone"), ("Dreamline", "Rush"),
                              ("A Dream", "Other"), ("Day Dreamline", "X"), ("Dreamline", None)]:
            session.add(Track(release_id=release.id, title=title, title_key=normalize(title),
                              artist_display=artist))
        session.flush()
        for key in ("dreamline", "dream", "a", "a dream", "day dreamline", "nothing"):
            from_sql = chart_svc._candidate_tracks(session, key)
            session.info[chart_svc._INDEX_KEY] = chart_svc._TrackIndex(session)
            try:
                from_memory = chart_svc._candidate_tracks(session, key)
            finally:
                session.info.pop(chart_svc._INDEX_KEY)
            assert from_memory == from_sql, key

    def test_rematch_all_gives_the_same_matches_as_one_at_a_time(self, session, db):
        riley = _owned(session, "The Music Goes 'Round And Around", "Riley-Farley Orchestra")
        dorsey = _owned(session, "The Music Goes Round and Round", "Tommy Dorsey And His Orchestra")
        cheek = _owned(session, "Cheek to Cheek", "Fred Astaire")
        songs = [("The Music Goes Round & Round", "Riley-Farley Orchestra"),
                 ("The Music Goes Round & Round", "Tommy Dorsey"),
                 ("Cheek To Cheek", "Fred Astaire"),
                 ("Cheek To Cheek", "Eddy Duchin")]
        _chart(session, "Year End", songs)
        _chart(session, "One-Hit Wonders", songs[:2])
        session.commit()

        # one entry at a time, no index, no shared answers
        expected = {}
        for entry in session.scalars(select(ChartEntry)):
            chart_svc.match_entry(session, entry)
            expected[entry.id] = entry.track_id
            entry.track_id = None
        session.commit()

        chart_svc.rematch_all_charts()

        with db.session_scope() as s:
            got = {e.id: e.track_id for e in s.scalars(select(ChartEntry))}
        assert got == expected
        assert sorted(got.values(), key=lambda v: v or 0) == sorted(
            [riley.id, dorsey.id, cheek.id, None, riley.id, dorsey.id], key=lambda v: v or 0)
        assert chart_svc._INDEX_KEY not in session.info
