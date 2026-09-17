"""Tests for services/playlists.py: manual playlist CRUD and item ordering,
playlist-folder tree safety (reparenting on delete, no moving a folder into
its own subtree), the smart-playlist rule engine, and the playback
("Most Played") playlists derived from play history.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import func, select

from musicmgr.db.models import PlayEvent, Playlist, PlaylistFolder, PlaylistItem, Track
from musicmgr.services import library as lib
from musicmgr.services import playlists as lib_pl
from musicmgr.services.matching import normalize


def make_track(session, title, *, artist="Artist", album="Album", **fields) -> Track:
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist)
    release.album_artist_id = artist_obj.id
    track = Track(
        release_id=release.id,
        title=title,
        title_key=normalize(title),
        artist_display=artist,
        **fields,
    )
    session.add(track)
    session.flush()
    return track


class TestPlaylistCrud:
    def test_create_and_rename(self, session):
        playlist = lib_pl.create_playlist(session, "My Mix")

        assert playlist.kind == Playlist.KIND_MANUAL
        lib_pl.rename_playlist(session, playlist.id, "  New Name  ")
        assert playlist.name == "New Name"

    def test_rename_ignores_a_blank_name(self, session):
        playlist = lib_pl.create_playlist(session, "Keep Me")

        lib_pl.rename_playlist(session, playlist.id, "   ")

        assert playlist.name == "Keep Me"

    def test_list_playlists_orders_pinned_first_then_alphabetically(self, session):
        lib_pl.create_playlist(session, "Zeta")
        lib_pl.create_playlist(session, "Alpha")
        pinned = lib_pl.create_playlist(session, "Beta")
        pinned.is_pinned = True
        session.flush()

        names = [p.name for p in lib_pl.list_playlists(session)]

        assert names == ["Beta", "Alpha", "Zeta"]


class TestPlaylistFolders:
    def test_create_rename_and_list(self, session):
        folder = lib_pl.create_folder(session, "  Road Trip  ")

        assert folder.name == "Road Trip"
        lib_pl.rename_folder(session, folder.id, "Renamed")
        assert [f.name for f in lib_pl.list_folders(session)] == ["Renamed"]

    def test_a_blank_folder_name_falls_back_to_a_default(self, session):
        folder = lib_pl.create_folder(session, "   ")

        assert folder.name == "New folder"

    def test_folder_and_descendant_ids_includes_nested_children_only(self, session):
        top = lib_pl.create_folder(session, "Top")
        mid = lib_pl.create_folder(session, "Mid", parent_id=top.id)
        leaf = lib_pl.create_folder(session, "Leaf", parent_id=mid.id)
        unrelated = lib_pl.create_folder(session, "Unrelated")

        ids = lib_pl.folder_and_descendant_ids(session, top.id)

        assert ids == {top.id, mid.id, leaf.id}
        assert unrelated.id not in ids

    def test_moving_a_folder_into_its_own_descendant_is_rejected(self, session):
        top = lib_pl.create_folder(session, "Top")
        child = lib_pl.create_folder(session, "Child", parent_id=top.id)

        with pytest.raises(ValueError):
            lib_pl.move_folder(session, top.id, child.id)

    def test_moving_a_folder_to_an_unrelated_parent_works(self, session):
        a = lib_pl.create_folder(session, "A")
        b = lib_pl.create_folder(session, "B")

        lib_pl.move_folder(session, b.id, a.id)

        assert b.parent_id == a.id

    def test_deleting_a_folder_reparents_its_children_and_playlists_instead_of_deleting_them(
        self, session
    ):
        top = lib_pl.create_folder(session, "Top")
        child_folder = lib_pl.create_folder(session, "Child", parent_id=top.id)
        playlist = lib_pl.create_playlist(session, "Inside", folder_id=top.id)

        lib_pl.delete_folder(session, top.id)

        assert session.get(PlaylistFolder, top.id) is None
        assert child_folder.parent_id is None
        assert playlist.folder_id is None

    def test_folder_counts_are_direct_children_only(self, session):
        top = lib_pl.create_folder(session, "Top")
        lib_pl.create_folder(session, "Child", parent_id=top.id)
        lib_pl.create_playlist(session, "P1", folder_id=top.id)
        lib_pl.create_playlist(session, "P2", folder_id=top.id)

        playlists, subfolders = lib_pl.folder_counts(session, top.id)

        assert playlists == 2
        assert subfolders == 1


class TestManualPlaylistItems:
    def test_add_tracks_assigns_sequential_positions(self, session):
        playlist = lib_pl.create_playlist(session, "Mix")
        t1, t2 = make_track(session, "One"), make_track(session, "Two")

        added = lib_pl.add_tracks(session, playlist.id, [t1.id, t2.id])

        assert added == 2
        items = session.scalars(
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist.id)
            .order_by(PlaylistItem.position)
        ).all()
        assert [i.position for i in items] == [0, 1]

    def test_add_tracks_appends_after_whatever_is_already_there(self, session):
        playlist = lib_pl.create_playlist(session, "Mix")
        t1, t2, t3 = (make_track(session, n) for n in ("One", "Two", "Three"))
        lib_pl.add_tracks(session, playlist.id, [t1.id])

        lib_pl.add_tracks(session, playlist.id, [t2.id, t3.id])

        ordered = lib_pl.playlist_tracks(session, playlist.id)
        assert [t.id for t in ordered] == [t1.id, t2.id, t3.id]

    def test_remove_item_deletes_only_that_item(self, session):
        playlist = lib_pl.create_playlist(session, "Mix")
        t1, t2 = make_track(session, "One"), make_track(session, "Two")
        lib_pl.add_tracks(session, playlist.id, [t1.id, t2.id])
        item = session.scalar(select(PlaylistItem).where(PlaylistItem.track_id == t1.id))

        lib_pl.remove_item(session, item.id)

        remaining = lib_pl.playlist_tracks(session, playlist.id)
        assert [t.id for t in remaining] == [t2.id]

    def test_reorder_applies_the_given_order(self, session):
        playlist = lib_pl.create_playlist(session, "Mix")
        t1, t2, t3 = (make_track(session, n) for n in ("One", "Two", "Three"))
        lib_pl.add_tracks(session, playlist.id, [t1.id, t2.id, t3.id])
        items = {
            i.track_id: i
            for i in session.scalars(
                select(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id)
            )
        }

        lib_pl.reorder(session, playlist.id, [items[t3.id].id, items[t1.id].id, items[t2.id].id])

        ordered = lib_pl.playlist_tracks(session, playlist.id)
        assert [t.id for t in ordered] == [t3.id, t1.id, t2.id]

    def test_reorder_ignores_an_item_belonging_to_a_different_playlist(self, session):
        p1 = lib_pl.create_playlist(session, "One")
        p2 = lib_pl.create_playlist(session, "Two")
        track = make_track(session, "Song")
        lib_pl.add_tracks(session, p1.id, [track.id])
        item = session.scalar(select(PlaylistItem).where(PlaylistItem.playlist_id == p1.id))

        lib_pl.reorder(session, p2.id, [item.id])  # item actually belongs to p1

        assert item.playlist_id == p1.id
        assert item.position == 0  # untouched

    def test_playlist_duration_sums_its_tracks(self, session):
        playlist = lib_pl.create_playlist(session, "Mix")
        t1 = make_track(session, "One", duration_ms=100_000)
        t2 = make_track(session, "Two", duration_ms=250_000)
        lib_pl.add_tracks(session, playlist.id, [t1.id, t2.id])

        assert lib_pl.playlist_duration_ms(session, playlist.id) == 350_000

    def test_playlist_tracks_for_an_unknown_id_is_empty(self, session):
        assert lib_pl.playlist_tracks(session, 999_999) == []


class TestSmartPlaylistRules:
    def test_numeric_gte(self, session):
        make_track(session, "Old", album="Old Album")
        old_release = lib.get_or_create_release(session, "Old Album", "Artist")
        old_release.year = 1970
        make_track(session, "New", album="New Album")
        new_release = lib.get_or_create_release(session, "New Album", "Artist")
        new_release.year = 2020
        session.flush()

        spec = {"rules": [{"field": "year", "op": "gte", "value": 2000}]}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["New"]

    def test_numeric_between(self, session):
        for title, year in (("A", 1960), ("B", 1970), ("C", 1980)):
            make_track(session, title, album=f"Album {title}")
            lib.get_or_create_release(session, f"Album {title}", "Artist").year = year
        session.flush()

        spec = {"rules": [{"field": "year", "op": "between", "value": [1965, 1975]}]}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["B"]

    def test_text_contains_matches_the_title(self, session):
        make_track(session, "Midnight Sun")
        make_track(session, "Daylight")

        spec = {"rules": [{"field": "title", "op": "contains", "value": "night"}]}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["Midnight Sun"]

    def test_genre_filter(self, session):
        soul_track = make_track(session, "Soul Song", album="Soul Album")
        soul_release = lib.get_or_create_release(session, "Soul Album", "Artist")
        soul_release.genres.append(lib.get_or_create_genre(session, "Soul"))
        make_track(session, "Rock Song", album="Rock Album")
        rock_release = lib.get_or_create_release(session, "Rock Album", "Artist")
        rock_release.genres.append(lib.get_or_create_genre(session, "Rock"))
        session.flush()

        spec = {"rules": [{"field": "genre", "op": "is", "value": "Soul"}]}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["Soul Song"]

    def test_match_all_requires_every_rule_to_hold(self, session):
        make_track(session, "Both", play_count=5, album="Both Album")
        lib.get_or_create_release(session, "Both Album", "Artist").year = 2000
        make_track(session, "OnlyYear", play_count=0, album="OnlyYear Album")
        lib.get_or_create_release(session, "OnlyYear Album", "Artist").year = 2000
        session.flush()

        spec = {
            "match": "all",
            "rules": [
                {"field": "year", "op": "gte", "value": 1999},
                {"field": "play_count", "op": "gte", "value": 3},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["Both"]

    def test_match_any_requires_only_one_rule_to_hold(self, session):
        make_track(session, "ByYear", play_count=0, album="ByYear Album")
        lib.get_or_create_release(session, "ByYear Album", "Artist").year = 2000
        make_track(session, "ByPlays", play_count=10, album="ByPlays Album")
        lib.get_or_create_release(session, "ByPlays Album", "Artist").year = 1900
        make_track(session, "Neither", play_count=0, album="Neither Album")
        lib.get_or_create_release(session, "Neither Album", "Artist").year = 1900
        session.flush()

        spec = {
            "match": "any",
            "rules": [
                {"field": "year", "op": "gte", "value": 1999},
                {"field": "play_count", "op": "gte", "value": 5},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert {t.title for t in results} == {"ByYear", "ByPlays"}

    def test_not_played_within_days_includes_tracks_never_played(self, session):
        make_track(
            session, "Recent",
            last_played_at=dt.datetime.now(dt.timezone.utc),
        )
        make_track(session, "Never")

        spec = {"rules": [{"field": "not_played_within_days", "op": "is", "value": 30}]}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert {t.title for t in results} == {"Never"}

    def test_played_within_days_excludes_tracks_never_played(self, session):
        make_track(
            session, "Recent",
            last_played_at=dt.datetime.now(dt.timezone.utc),
        )
        make_track(session, "Never")

        spec = {"rules": [{"field": "played_within_days", "op": "is", "value": 30}]}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert {t.title for t in results} == {"Recent"}

    def test_an_unknown_field_raises_rather_than_silently_matching_nothing(self, session):
        spec = {"rules": [{"field": "nonsense", "op": "is", "value": 1}]}

        with pytest.raises(ValueError):
            lib_pl.resolve_smart(session, json.dumps(spec))

    def test_limit_is_respected(self, session):
        for i in range(5):
            make_track(session, f"Track {i}", album=f"Album {i}")

        spec = {"rules": [], "limit": 2}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert len(results) == 2

    def test_omitting_limit_means_unlimited_not_a_hidden_default(self, session):
        """2026-09-17 - SmartPlaylistDialog's "limit to" checkbox is
        unchecked by default (mirroring MusicBee's own auto-playlist
        editor), which omits the "limit" key entirely - that has to mean
        "every match", not silently fall back to some old built-in cap."""
        for i in range(5):
            make_track(session, f"Track {i}", album=f"Album {i}")

        spec = {"rules": []}
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert len(results) == 5


class TestChartRankOrdering:
    """2026-09-17 - "chart_rank" order-by, sorting a smart playlist by the
    rank number tagged directly into Track.comment rather than by joining
    through ChartEntry - see resolve_smart's own docstring for the two
    real-data problems (same-named charts, mismatched track_id) that made
    the ChartEntry approach unworkable."""

    def test_sorts_by_the_rank_embedded_in_the_hint_rank_year_format(self, session):
        # "[Billboard] #NNN# [YEAR]" - the format James's Billboard-tagged
        # library actually uses.
        make_track(session, "Third", comment="[Billboard] #003# [2020]")
        make_track(session, "First", comment="[Billboard] #001# [2020]")
        make_track(session, "Second", comment="[Billboard] #002# [2020]")

        spec = {
            "order_by": "chart_rank",
            "rules": [
                {"field": "comment", "op": "contains", "value": "[Billboard]"},
                {"field": "comment", "op": "contains", "value": "[2020]"},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["First", "Second", "Third"]

    def test_sorts_by_the_rank_embedded_in_the_hint_year_rank_format(self, session):
        # "[Genre][YEAR] #NNN#" - the other tagging convention seen in
        # James's library (non-Billboard genre charts).
        make_track(session, "Third", comment="[Hot Country][2023] #096#")
        make_track(session, "First", comment="[Hot Country][2023] #039#")
        make_track(session, "Second", comment="[Hot Country][2023] #050#")

        spec = {
            "order_by": "chart_rank",
            "rules": [
                {"field": "comment", "op": "contains", "value": "[Hot Country]"},
                {"field": "comment", "op": "contains", "value": "[2023]"},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["First", "Second", "Third"]

    def test_a_weekly_peak_never_outranks_the_tagged_year_end_position(self, session):
        # Regression pin for the actual bug James hit: a much better
        # (lower) number elsewhere in the comment, or on a completely
        # unrelated chart tag, must never win over the one tagged rank
        # that actually matches this playlist's own hint/year.
        make_track(
            session, "RealFirst",
            comment="[Billboard] #001# [2020]\r\n[Pop][2020] #050#",
        )
        make_track(session, "RealSecond", comment="[Billboard] #002# [2020]")

        spec = {
            "order_by": "chart_rank",
            "rules": [
                {"field": "comment", "op": "contains", "value": "[Billboard]"},
                {"field": "comment", "op": "contains", "value": "[2020]"},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["RealFirst", "RealSecond"]

    def test_a_track_with_no_matching_tag_sorts_after_every_ranked_one(self, session):
        make_track(session, "Ranked", comment="[Billboard] #001# [2020]")
        # Both of these satisfy the *rules* below (their comment really
        # does contain the literal substrings "[Billboard]" and "[2020]"),
        # but neither is shaped like either rank pattern
        # (_comment_rank_patterns) - a garbled/incomplete tag, and both
        # tags present but nowhere near each other, respectively.
        make_track(session, "Malformed", comment="[Billboard][2020] no rank here")
        make_track(session, "SeparateTags", comment="[2020] some other text [Billboard]")

        spec = {
            "order_by": "chart_rank",
            "rules": [
                {"field": "comment", "op": "contains", "value": "[Billboard]"},
                {"field": "comment", "op": "contains", "value": "[2020]"},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        # Both fall into the untagged (unranked) bucket, alphabetical
        # between themselves, after the one real ranked track.
        assert [t.title for t in results] == ["Ranked", "Malformed", "SeparateTags"]

    def test_no_comment_rules_at_all_falls_back_to_alphabetical_rather_than_erroring(
        self, session
    ):
        make_track(session, "Bravo", album="Bravo Album")
        lib.get_or_create_release(session, "Bravo Album", "Artist").year = 2020
        make_track(session, "Alpha", album="Alpha Album")
        lib.get_or_create_release(session, "Alpha Album", "Artist").year = 2020
        session.flush()

        spec = {
            "order_by": "chart_rank",
            "rules": [{"field": "year", "op": "is", "value": 2020}],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["Alpha", "Bravo"]

    def test_limit_still_applies_after_the_chart_rank_sort(self, session):
        make_track(session, "Third", comment="[Billboard] #003# [2020]")
        make_track(session, "First", comment="[Billboard] #001# [2020]")
        make_track(session, "Second", comment="[Billboard] #002# [2020]")

        spec = {
            "order_by": "chart_rank",
            "limit": 2,
            "rules": [
                {"field": "comment", "op": "contains", "value": "[Billboard]"},
                {"field": "comment", "op": "contains", "value": "[2020]"},
            ],
        }
        results = lib_pl.resolve_smart(session, json.dumps(spec))

        assert [t.title for t in results] == ["First", "Second"]


class TestCreateSmartPlaylist:
    def test_creates_and_stores_the_json_rules(self, session):
        spec = {
            "match": "all",
            "rules": [{"field": "year", "op": "gte", "value": 2000}],
            "order_by": "title",
            "limit": 50,
        }

        playlist = lib_pl.create_smart_playlist(session, "Recent", spec)

        assert playlist.kind == Playlist.KIND_SMART
        assert json.loads(playlist.rules) == spec

    def test_an_invalid_spec_is_rejected_before_anything_is_saved(self, session):
        spec = {"rules": [{"field": "not_a_real_field", "op": "is", "value": 1}]}

        with pytest.raises(ValueError):
            lib_pl.create_smart_playlist(session, "Bad", spec)

        assert session.scalar(select(Playlist).where(Playlist.name == "Bad")) is None


class TestDefaultSmartPlaylists:
    def test_creates_all_three_built_ins(self, session):
        lib_pl.ensure_default_playlists(session)

        names = {p.name for p in lib_pl.list_playlists(session)}
        assert {"Recently Added", "Heavy Rotation", "Forgotten Favorites"} <= names

    def test_running_it_twice_does_not_duplicate_them(self, session):
        lib_pl.ensure_default_playlists(session)
        lib_pl.ensure_default_playlists(session)

        count = session.scalar(
            select(func.count(Playlist.id)).where(Playlist.kind == Playlist.KIND_SMART)
        )
        assert count == 3

    def test_renames_an_existing_playlist_with_the_old_british_spelling(self, session):
        """"Forgotten Favourites" -> "Forgotten Favorites" (2026-09-17).
        Someone who already has the old-spelled playlist - with whatever
        track history/position it's built up - keeps that same row, just
        renamed, rather than getting a second, freshly-created "Forgotten
        Favorites" alongside an orphaned old one."""
        old = lib_pl.create_smart_playlist(
            session, "Forgotten Favourites", {"match": "all", "rules": []}
        )

        lib_pl.ensure_default_playlists(session)

        names = [p.name for p in lib_pl.list_playlists(session)]
        assert names.count("Forgotten Favorites") == 1
        assert "Forgotten Favourites" not in names
        assert session.get(Playlist, old.id).name == "Forgotten Favorites"


class TestPlaybackTopTracks:
    def test_a_play_shorter_than_min_ms_does_not_count(self, session):
        track = make_track(session, "Song")
        session.add(PlayEvent(track_id=track.id, ms_played=60_000))
        session.add(PlayEvent(track_id=track.id, ms_played=5_000))  # a skip
        session.flush()

        rows = lib_pl.playback_top_tracks(session, window="all", min_ms=30_000)

        assert len(rows) == 1
        _, plays, total_ms = rows[0]
        assert plays == 1
        assert total_ms == 60_000

    def test_respects_the_requested_time_window(self, session):
        track = make_track(session, "Song")
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        session.add(PlayEvent(track_id=track.id, ms_played=60_000, started_at=old))
        session.flush()

        rows_week = lib_pl.playback_top_tracks(session, window="week", min_ms=0)
        rows_all = lib_pl.playback_top_tracks(session, window="all", min_ms=0)

        assert rows_week == []
        assert len(rows_all) == 1

    def test_orders_by_play_count_descending(self, session):
        popular = make_track(session, "Popular")
        rare = make_track(session, "Rare")
        for _ in range(3):
            session.add(PlayEvent(track_id=popular.id, ms_played=60_000))
        session.add(PlayEvent(track_id=rare.id, ms_played=60_000))
        session.flush()

        rows = lib_pl.playback_top_tracks(session, window="all", min_ms=0)

        assert [t.title for t, _, _ in rows] == ["Popular", "Rare"]


class TestPlaybackMovers:
    def test_a_track_with_no_plays_in_the_previous_window_is_a_new_entry(self, session):
        track = make_track(session, "Brand New")
        now = dt.datetime.now(dt.timezone.utc)
        session.add(PlayEvent(track_id=track.id, started_at=now - dt.timedelta(days=1)))
        session.flush()

        rows = lib_pl.playback_movers(session, window_days=7)

        assert len(rows) == 1
        _, _rank, prev_rank, delta = rows[0]
        assert prev_rank == 0
        assert delta == 999


class TestBuiltinPlaybackPlaylists:
    def test_seeds_all_three_and_is_idempotent_by_slug(self, session):
        lib_pl.ensure_builtin_playback_playlists(session)
        lib_pl.ensure_builtin_playback_playlists(session)

        slugs = {p.slug for p in lib_pl.list_playlists(session) if p.slug}
        assert slugs == {"most-played-week", "most-played-month", "most-played-all"}

    def test_resolve_playback_uses_the_playlists_own_stored_window(self, session):
        track = make_track(session, "Hit")
        session.add(PlayEvent(track_id=track.id, ms_played=60_000))
        session.flush()
        lib_pl.ensure_builtin_playback_playlists(session)
        playlist = session.scalar(select(Playlist).where(Playlist.slug == "most-played-all"))

        rows = lib_pl.resolve_playback(session, playlist)

        assert [t.title for t, _, _ in rows] == ["Hit"]

    def test_playlist_tracks_resolves_a_playback_playlist_through_the_same_path(self, session):
        track = make_track(session, "Hit")
        session.add(PlayEvent(track_id=track.id, ms_played=60_000))
        session.flush()
        lib_pl.ensure_builtin_playback_playlists(session)
        playlist = session.scalar(select(Playlist).where(Playlist.slug == "most-played-all"))

        tracks = lib_pl.playlist_tracks(session, playlist.id)

        assert [t.title for t in tracks] == ["Hit"]
