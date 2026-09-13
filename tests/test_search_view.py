"""Tests for ui/views/search.py: the single search box across artists,
releases and tracks - the query-length gate, the three result lists'
row-building (including the "dim it if the file's missing" color and the
artist's live track count), and the three tap-to-play actions.

The 220ms debounce timer itself is not exercised here - `_schedule` just
starts it, and spinning a real Qt event loop for it would make these tests
slow and timing-flaky for no real coverage gain. What actually matters -
the query evaluation in `_run_search` - is called directly instead, the
same way `refresh()` itself already calls it directly once there's
existing text rather than going through the timer.
"""

from __future__ import annotations

import pytest

from musicmgr.db.models import MediaFile, Track
from musicmgr.services import library as lib
from musicmgr.services.matching import normalize
from musicmgr.ui.theme import COLORS
from musicmgr.ui.views.search import SearchView


@pytest.fixture
def view(ctx):
    return SearchView(ctx)


def make_track(session, title, *, artist="Artist", album="Album", year=None, **fields) -> Track:
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist, year=year)
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
    lib.add_credit(session, artist_obj, track=track)
    return track


def add_file(session, track, *, path=None, is_missing=False) -> MediaFile:
    mf = MediaFile(
        track_id=track.id,
        path=path or f"/music/{track.title}.mp3",
        is_missing=is_missing,
    )
    session.add(mf)
    session.flush()
    return mf


class TestRunSearch:
    def test_a_query_under_two_characters_clears_all_three_lists(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song")
        view.search_bar.setText("a")

        view._run_search()

        assert view.artist_list.count() == 0
        assert view.release_list.count() == 0
        assert view.track_list.count() == 0
        assert view._tracks == []

    def test_an_empty_query_also_clears_the_lists(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song")
        view.search_bar.setText("song")
        view._run_search()
        assert view.track_list.count() == 1

        view.search_bar.setText("")
        view._run_search()

        assert view.track_list.count() == 0

    def test_matching_artists_releases_and_tracks_populate_their_lists(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Wonderwall", artist="Oasis", album="Whats The Story")

        view.search_bar.setText("oasis")
        view._run_search()

        assert view.artist_list.count() == 1
        assert view.artist_list.payload_at(0)["primary"] == "Oasis"
        assert view.release_list.count() == 0  # "oasis" doesn't match the album title
        # the track list still shows a hit here - see the dedicated test
        # below, `search()` also matches a track by its artist_display.
        assert view.track_list.count() == 1

    def test_a_track_matches_by_artist_display_as_well_as_title(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Wonderwall", artist="Oasis")

        view.search_bar.setText("oasis")
        view._run_search()

        assert view.track_list.count() == 1
        assert view.track_list.payload_at(0)["primary"] == "Wonderwall"

    def test_the_artist_secondary_label_reports_the_live_track_count(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "One", artist="The Meridian Set")
            make_track(session, "Two", artist="The Meridian Set")

        view.search_bar.setText("meridian")
        view._run_search()

        assert view.artist_list.payload_at(0)["secondary"] == "2 tracks"

    def test_the_release_secondary_label_shows_year_and_artist(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song", artist="Some Artist", album="Some Album", year=1999)

        view.search_bar.setText("some album")
        view._run_search()

        assert view.release_list.payload_at(0)["secondary"] == "1999 · Some Artist"

    def test_a_release_with_no_year_shows_an_em_dash(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song", artist="Some Artist", album="Undated Album")

        view.search_bar.setText("undated")
        view._run_search()

        assert view.release_list.payload_at(0)["secondary"] == "— · Some Artist"

    def test_a_track_with_a_playable_file_is_not_dimmed(self, ctx, view):
        with ctx.session() as session:
            track = make_track(session, "Playable Song")
            add_file(session, track, is_missing=False)

        view.search_bar.setText("playable")
        view._run_search()

        assert view.track_list.payload_at(0)["color"] == COLORS["text"]

    def test_a_track_whose_only_file_is_missing_is_dimmed(self, ctx, view):
        with ctx.session() as session:
            track = make_track(session, "Gone Song")
            add_file(session, track, is_missing=True)

        view.search_bar.setText("gone")
        view._run_search()

        assert view.track_list.payload_at(0)["color"] == COLORS["text_dim"]

    def test_a_track_with_no_file_at_all_is_dimmed(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Fileless Song")

        view.search_bar.setText("fileless")
        view._run_search()

        assert view.track_list.payload_at(0)["color"] == COLORS["text_dim"]

    def test_the_headings_report_the_result_counts(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "One", artist="Repeated Name")
            make_track(session, "Two", artist="Repeated Name")

        view.search_bar.setText("repeated")
        view._run_search()

        assert view.artist_head.text() == "Artists (1)"
        assert view.track_head.text() == "Tracks (2)"

    def test_tracks_found_are_kept_for_the_track_list_play_action(self, ctx, view):
        with ctx.session() as session:
            track = make_track(session, "Findable")

        view.search_bar.setText("findable")
        view._run_search()

        assert [t.id for t in view._tracks] == [track.id]


class TestRefresh:
    def test_with_no_text_entered_refresh_does_not_search(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song")

        view.refresh()

        assert view.track_list.count() == 0

    def test_with_existing_text_refresh_re_runs_the_search(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song")
        view.search_bar.setText("song")
        view._run_search()
        with ctx.session() as session:
            make_track(session, "Second Song")

        view.refresh()  # picks up the newly-added second match

        assert view.track_list.count() == 2


class TestSchedule:
    def test_typing_starts_the_debounce_timer_without_searching_immediately(self, ctx, view):
        with ctx.session() as session:
            make_track(session, "Song")

        view.search_bar.setText("song")  # triggers textChanged -> _schedule

        assert view._timer.isActive()
        # the timer hasn't fired yet (no event loop spin), so the lists are
        # still empty - _run_search only actually runs when it does.
        assert view.track_list.count() == 0


class TestPlayActions:
    def test_play_artist_queues_every_track_credited_to_that_artist(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            artist = lib.get_or_create_artist(session, "Queued Artist")
            make_track(session, "One", artist="Queued Artist")
            make_track(session, "Two", artist="Queued Artist")
            artist_id = artist.id

        calls = []
        monkeypatch.setattr(
            ctx, "play_tracks", lambda tracks, **kw: calls.append((list(tracks), kw))
        )

        view._play_artist({"key": artist_id})

        assert len(calls) == 1
        tracks, kwargs = calls[0]
        assert {t.title for t in tracks} == {"One", "Two"}
        assert kwargs == {"source": "search"}

    def test_play_artist_with_no_payload_is_a_no_op(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(ctx, "play_tracks", lambda *a, **kw: calls.append((a, kw)))

        view._play_artist(None)

        assert calls == []

    def test_play_release_queues_every_track_on_that_release(self, ctx, view, monkeypatch):
        with ctx.session() as session:
            track = make_track(session, "Song", album="Target Album")
            release_id = track.release_id

        calls = []
        monkeypatch.setattr(
            ctx, "play_tracks", lambda tracks, **kw: calls.append((list(tracks), kw))
        )

        view._play_release({"key": release_id})

        assert len(calls) == 1
        tracks, kwargs = calls[0]
        assert [t.title for t in tracks] == ["Song"]
        assert kwargs == {"source": "search"}

    def test_play_release_with_no_payload_is_a_no_op(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(ctx, "play_tracks", lambda *a, **kw: calls.append((a, kw)))

        view._play_release(None)

        assert calls == []

    def test_play_track_starts_the_whole_result_set_at_the_tapped_tracks_index(
        self, ctx, view, monkeypatch
    ):
        with ctx.session() as session:
            make_track(session, "Alpha", artist="Shared Search Artist")
            make_track(session, "Beta", artist="Shared Search Artist")
            make_track(session, "Gamma", artist="Shared Search Artist")
        view.search_bar.setText("shared search artist")
        view._run_search()
        tapped = view._tracks[1]  # "Beta"

        calls = []
        monkeypatch.setattr(
            ctx,
            "play_tracks",
            lambda tracks, **kw: calls.append((list(tracks), kw)),
        )

        view._play_track({"key": tapped.id})

        assert len(calls) == 1
        tracks, kwargs = calls[0]
        assert tracks == view._tracks  # the whole result set, not just the one tapped
        assert kwargs == {"start": 1, "source": "search"}

    def test_play_track_with_no_payload_is_a_no_op(self, ctx, view, monkeypatch):
        calls = []
        monkeypatch.setattr(ctx, "play_tracks", lambda *a, **kw: calls.append((a, kw)))

        view._play_track(None)

        assert calls == []

    def test_play_track_for_an_id_no_longer_in_the_result_set_is_a_no_op(
        self, ctx, view, monkeypatch
    ):
        with ctx.session() as session:
            make_track(session, "Findable")
        view.search_bar.setText("findable")
        view._run_search()

        calls = []
        monkeypatch.setattr(ctx, "play_tracks", lambda *a, **kw: calls.append((a, kw)))

        view._play_track({"key": 999_999})

        assert calls == []
