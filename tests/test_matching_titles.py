"""Chart matching: the title has to match too (2026-10-25).

Title and artist were scored together (65% / 35%), and one title found
anywhere inside another - even as part of a word - counted as 0.9. So a
perfect artist could carry a title only 57% alike, and "Men" matched
"Midnight Train to Georgia (instrumental)" ("men" is in "instrumental").
James's Playback.fm one-hit-wonder playlists filled up with other songs by
the same acts: "Hurricane" (MS MR) -> "Beautiful Hurricane".
"""

import pytest

from musicmgr.services.matching import (
    artist_similarity, best_match, score_pair, similarity, title_similarity,
)


class TestAnotherSongBySameArtist:
    @pytest.mark.parametrize("chart, library", [
        ("Hurricane", "Beautiful Hurricane"),
        ("Help Me", "Please Help Me I'm Falling"),
        ("Human", "Only Human"),
        ("Too Young", "Too Young To Love"),
        ("Somebody", "Somebody to Love"),
        ("How To Hate", "How To Love"),
    ])
    def test_is_not_a_match(self, chart, library):
        assert score_pair(chart, "Same Act", library, "Same Act") == 0.0


class TestPartOfAWordIsNotContainment:
    @pytest.mark.parametrize("chart, library", [
        ("Men", "Midnight Train to Georgia (instrumental)"),
        ("Sing", "Heaven Must Be Missing An Angel"),
        ("Body", "Is Anybody Loving You These Days"),
        ("Ex", "Excerpt From Lamplight Symphony"),
    ])
    def test_titles(self, chart, library):
        assert title_similarity(chart, library) < 0.5
        assert best_match(chart, "Same Act", [(1, library, "Same Act")]) is None

    def test_a_one_letter_artist_is_not_inside_every_name(self):
        # "M" (the French singer) scored 0.9 against "Fair to Midland"
        assert artist_similarity("Fair to Midland", "M") < 0.45
        assert similarity("m", "fair to midland") < 0.5

    def test_whole_word_containment_still_counts_for_artists(self):
        assert artist_similarity("Gladys Knight", "Gladys Knight & the Pips") >= 0.9


class TestVersionsOfTheSameSongStillMatch:
    @pytest.mark.parametrize("chart, library", [
        ("Billie Jean", "Billie Jean (Single Version)"),
        ("Billie Jean", "Billie Jean Single Version"),
        ("Kiss Me", "Kiss Me (Official Paris Version)"),
        ("Crush", "CRUSH Original Version"),
        ("Billionaire", "Billionaire ft. Bruno Mars"),
        ("Back To Back", "Back to Back Freestyle"),
        ("Cold One", "Cold One (Live At Barclays Center, Brooklyn, NY / January 27, 2017)"),
        ("Gimme a Lil' Kiss, Will Ya, Huh?", "Gimme A Little Kiss (Will Ya Huh)"),
        ("(I Can't Get No) Satisfaction", "Satisfaction"),
        ("Star Wars Theme - Cantina Band", "Star Wars Theme"),
        ("\"Joy\" Pt. I", "Joy (Part 1)"),
        ("Uncle Albert/Admiral Halsey", "Uncle Albert  Admiral Halsey"),
        ("Ran$om", "Ransom"),
    ])
    def test_matches(self, chart, library):
        assert best_match(chart, "Same Act", [(1, library, "Same Act")]) is not None

    def test_either_side_of_a_double_a_side(self):
        assert title_similarity("Moody Blue/She Thinks I Still Care", "Moody Blue") == 1.0
        assert title_similarity("Tremor Christ/Spin The Black Circle", "Tremor Christ") == 1.0

    def test_a_slash_inside_a_title_is_not_two_sides(self):
        assert title_similarity("24", "24/7") < 0.8


class TestUnlinkTool:
    """tools/unlink_loose_chart_matches.py re-scores existing links."""

    def test_unlinks_only_links_that_no_longer_match(self, tmp_path):
        import sqlite3
        import importlib.util
        from pathlib import Path

        tool = Path(__file__).resolve().parent.parent / "tools" / "unlink_loose_chart_matches.py"
        spec = importlib.util.spec_from_file_location("unlink_tool", tool)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        db = tmp_path / "library.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            create table tracks (id integer primary key, title text, artist_display text);
            create table charts (id integer primary key, name text);
            create table chart_issues (id integer primary key, chart_id integer);
            create table chart_entries (id integer primary key, issue_id integer, title text,
                artist_name text, track_id integer, match_score real, match_locked boolean);
            insert into tracks values (1, 'Beautiful Hurricane', 'MS MR'),
                                      (2, 'Hurricane', 'MS MR');
            insert into charts values (1, 'One-Hit Wonders');
            insert into chart_issues values (1, 1);
            insert into chart_entries values
                (1, 1, 'Hurricane', 'MS MR', 1, 0.9, 0),   -- wrong song: unlinked
                (2, 1, 'Hurricane', 'MS MR', 2, 1.0, 0),   -- right song: kept
                (3, 1, 'Hurricane', 'MS MR', 1, 0.9, 1);   -- fixed by hand: kept
        """)
        con.commit()
        con.close()

        assert mod.main(["--db", str(db)]) == 0
        con = sqlite3.connect(str(db))
        assert con.execute("select count(*) from chart_entries where track_id is null").fetchone()[0] == 0
        con.close()

        assert mod.main(["--db", str(db), "--apply"]) == 0
        con = sqlite3.connect(str(db))
        linked = dict(con.execute("select id, track_id from chart_entries"))
        assert linked == {1: None, 2: 2, 3: 1}
        assert list((tmp_path / "backups").glob("library-before-unlink-*.db"))
