"""Radio - "radio from this song" (2026-10-01, services/radio.py).

A small hand-built library: a seed artist, artists Last.fm calls similar,
an artist that shares chart weeks with the seed, and unrelated artists from
another era. The picks are random draws, so the tests use a seeded RNG and
check tendencies over many picks rather than exact lists.
"""

from __future__ import annotations

import datetime as dt
import random
import wave
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db.models import Artist, ArtistSimilar, ChartEntry, ChartIssue, MediaFile, Track
from musicmgr.services import charts as chart_svc
from musicmgr.services import lastfm_popularity as lfm
from musicmgr.services import library as lib
from musicmgr.services import radio
from musicmgr.services.matching import normalize


def make_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)


def add_track(session, tmp_path, artist_name, title, year=1960, album=None, rating=None,
              plays=0, skips=0, last_played=None):
    artist = lib.get_or_create_artist(session, artist_name)
    release = lib.get_or_create_release(session, album or f"{artist_name} Hits", artist_name,
                                        year=year)
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display=artist_name, rating=rating, play_count=plays,
                  skip_count=skips, last_played_at=last_played, duration_ms=100)
    session.add(track)
    session.flush()
    lib.add_credit(session, artist, track=track)
    path = tmp_path / artist_name / f"{album or 'hits'}-{title}.wav"
    make_wav(path)
    session.add(MediaFile(track_id=track.id, path=str(path), duration_ms=100))
    session.flush()
    return track


@pytest.fixture
def library(db, tmp_path):
    """seed: Elvis (1956-62). similar on Last.fm: Buddy Holly, Roy Orbison.
    chart neighbour: Connie Francis. unrelated, 1990s: Grunge Band, Boy Band."""
    with db.session_scope() as s:
        ids = {}
        for i in range(6):
            ids[f"elvis{i}"] = add_track(s, tmp_path, "Elvis Presley", f"Elvis Song {i}",
                                         year=1956 + i).id
        for name in ("Buddy Holly", "Roy Orbison", "Connie Francis"):
            for i in range(5):
                add_track(s, tmp_path, name, f"{name} Song {i}", year=1958 + i)
        for name in ("Grunge Band", "Boy Band"):
            for i in range(5):
                add_track(s, tmp_path, name, f"{name} Song {i}", year=1994 + i)
        elvis = s.scalar(select(Artist).where(Artist.name == "Elvis Presley"))
        for name, match in (("Buddy Holly", 1.0), ("Roy Orbison", 0.8), ("Not In Library", 0.9)):
            s.add(ArtistSimilar(artist_id=elvis.id, name=name, name_key=normalize(name),
                                match=match))
        elvis.similar_fetched_at = dt.datetime.now(dt.timezone.utc)
        ids["elvis_artist"] = elvis.id
    return ids


def artist_of(session, track_id):
    return session.get(Track, track_id).artist_display


class TestNeighbours:
    def test_similar_artists_resolve_to_library_artists_by_name(self, db, library):
        with db.session_scope() as s:
            sims = radio.similar_library_artists(s, library["elvis_artist"])
            names = {s.get(Artist, a).name: m for a, m in sims.items()}
        assert names == {"Buddy Holly": 1.0, "Roy Orbison": 0.8}

    def test_chart_neighbours_need_shared_weeks(self, db, library):
        with db.session_scope() as s:
            chart = chart_svc.get_or_create_chart(s, "Hot 100")
            tracks = {t.artist_display: t for t in s.scalars(select(Track))}
            for week in range(5):
                issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(1960, 1, 1 + week))
                s.add(issue)
                s.flush()
                charted = ["Elvis Presley", "Connie Francis"] + (["Boy Band"] if week == 0 else [])
                for rank, name in enumerate(charted, 1):
                    t = tracks[name]
                    s.add(ChartEntry(issue_id=issue.id, rank=rank, title=t.title,
                                     artist_name=name, title_key=t.title_key,
                                     artist_key=normalize(name), track_id=t.id))
            s.flush()
            near = {s.get(Artist, a).name: v
                    for a, v in radio.chart_neighbours(s, library["elvis_artist"]).items()}
        # Boy Band shares one week only - below CHART_MIN_SHARED
        assert near == {"Connie Francis": 1.0}

    def test_main_artist_prefers_the_name_in_the_track_text(self, db, library):
        with db.session_scope() as s:
            t = s.get(Track, library["elvis0"])
            lib.add_credit(s, lib.get_or_create_artist(s, "AAA Wrong Credit"), track=t)
            s.flush()
            assert radio.main_artist_id(s, t.id) == library["elvis_artist"]


class TestBuildBatch:
    def state(self, s, library):
        return radio.RadioState(seed=radio.seed_from_track(s, library["elvis0"]))

    def test_no_repeats_and_artist_spacing(self, db, library):
        with db.session_scope() as s:
            st = self.state(s, library)
            picks = radio.build_batch(s, st, n=12, rng=random.Random(3))
            picks += radio.build_batch(s, st, n=8, rng=random.Random(4))
            assert len(picks) == len(set(picks))
            artists = [artist_of(s, t) for t in picks]
        for i in range(1, len(artists)):
            assert artists[i] != artists[i - 1]

    def test_similar_artists_and_era_win_over_unrelated(self, db, library):
        counts = Counter()
        with db.session_scope() as s:
            for seed in range(40):
                st = self.state(s, library)
                for t in radio.build_batch(s, st, n=4, rng=random.Random(seed)):
                    counts[artist_of(s, t)] += 1
        related = counts["Buddy Holly"] + counts["Roy Orbison"]
        unrelated = counts["Grunge Band"] + counts["Boy Band"]
        assert related > 5 * max(1, unrelated)

    def test_seed_artist_is_spaced_out(self, db, library):
        with db.session_scope() as s:
            st = self.state(s, library)
            picks = radio.build_batch(s, st, n=12, rng=random.Random(1))
            elvis_positions = [i for i, t in enumerate(picks)
                               if artist_of(s, t) == "Elvis Presley"]
        for a, b in zip(elvis_positions, elvis_positions[1:]):
            assert b - a >= radio.SEED_ARTIST_EVERY

    def test_low_ratings_skips_and_recent_plays_are_held_back(self, db, library, tmp_path):
        now = dt.datetime(2026, 10, 1, 12)
        c = radio.Candidate(1, 1, 1, "x", 1960, None, 0, 0, None, affinity=1.0)
        base = radio.track_weight(c, 1960, now)
        assert radio.track_weight(radio.Candidate(**{**c.__dict__, "rating": 1}), 1960, now) < base / 5
        assert radio.track_weight(radio.Candidate(**{**c.__dict__, "rating": 5}), 1960, now) > base
        assert radio.track_weight(
            radio.Candidate(**{**c.__dict__, "play_count": 1, "skip_count": 9}), 1960, now) < base / 2
        assert radio.track_weight(
            radio.Candidate(**{**c.__dict__, "last_played_at": now - dt.timedelta(hours=2)}),
            1960, now) < base / 5
        assert radio.track_weight(radio.Candidate(**{**c.__dict__, "year": 1995}), 1960, now) < base / 10
        assert radio.track_weight(radio.Candidate(**{**c.__dict__, "hit": True}), 1960, now) > base

    def test_same_song_on_two_albums_plays_once(self, db, library, tmp_path):
        with db.session_scope() as s:
            add_track(s, tmp_path, "Buddy Holly", "Buddy Holly Song 0", year=2005,
                      album="Greatest Hits")
            st = self.state(s, library)
            picks = radio.build_batch(s, st, n=40, rng=random.Random(2))
            keys = [(artist_of(s, t), s.get(Track, t).title_key) for t in picks]
        assert len(keys) == len(set(keys))

    def test_original_year_counts_not_the_compilation_year(self, db, library, tmp_path):
        with db.session_scope() as s:
            add_track(s, tmp_path, "Roy Orbison", "Roy Orbison Song 0", year=2015,
                      album="Remastered")
            pool = radio._candidates(s, {s.scalar(select(Artist.id).where(
                Artist.name == "Roy Orbison")): 1.0})
            radio._original_years(pool)
        assert {c.year for c in pool if c.title_key == normalize("Roy Orbison Song 0")} == {1958}


class TestFetchSimilar:
    class FakeHttp:
        def __init__(self, payload=None, exc=None):
            self.payload, self.exc, self.params = payload, exc, None

        def get(self, url, params=None, timeout=None):
            if self.exc:
                raise self.exc
            self.params = params
            payload = self.payload

            class R:
                def json(self_inner):
                    return payload
            return R()

    def test_stores_list_and_timestamp(self, db, library):
        http = self.FakeHttp({"similarartists": {"artist": [
            {"name": "Boy Band", "match": "0.55"}, {"name": "Someone Else", "match": "1"}]}})
        status = radio.fetch_similar(library["elvis_artist"], http=http, api_key="k")
        assert status == "updated"
        assert http.params["method"] == "artist.getsimilar"
        with db.session_scope() as s:
            sims = {s.get(Artist, a).name: m
                    for a, m in radio.similar_library_artists(s, library["elvis_artist"]).items()}
            assert sims == {"Boy Band": 0.55}
            assert not radio.needs_similar_fetch(s, library["elvis_artist"])

    def test_network_error_stores_nothing(self, db, library):
        status = radio.fetch_similar(library["elvis_artist"],
                                     http=self.FakeHttp(exc=OSError("offline")), api_key="k")
        assert status == "error"
        with db.session_scope() as s:
            assert len(radio.similar_library_artists(s, library["elvis_artist"])) == 2

    def test_auth_error_and_no_key(self, db, library):
        assert radio.fetch_similar(library["elvis_artist"],
                                   http=self.FakeHttp({"error": 10}), api_key="k") == "auth_error"
        assert radio.fetch_similar(library["elvis_artist"]) == "not_configured"

    def test_stale_lists_are_refetched(self, db, library):
        with db.session_scope() as s:
            a = s.get(Artist, library["elvis_artist"])
            a.similar_fetched_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
            s.flush()
            assert radio.needs_similar_fetch(s, a.id)


@pytest.fixture
def controller(ctx, monkeypatch):
    monkeypatch.setattr(radio.RadioController, "threaded", False)
    monkeypatch.setattr(radio.RadioController, "fetch_enabled", False)
    monkeypatch.setattr(type(ctx.player), "auto_measure", False)
    yield ctx.radio
    ctx.player.clear_queue()


class TestController:
    def test_start_from_track_plays_seed_then_a_batch(self, ctx, controller, library):
        states = []
        controller.stateChanged.connect(lambda on, label: states.append((on, label)))
        assert controller.start_from_track(library["elvis0"])
        player = ctx.player
        assert player.source == radio.RADIO_SOURCE
        assert player.queue[0].track_id == library["elvis0"]
        assert len(player.queue) == 1 + radio.BATCH_SIZE
        assert states == [(True, "“Elvis Song 0”")]

    def test_queue_is_topped_up_near_the_end(self, ctx, controller, library):
        controller.start_from_track(library["elvis0"])
        player = ctx.player
        before = len(player.queue)
        player.jump_to(before - 2)
        assert len(player.queue) > before
        ids = [i.track_id for i in player.queue]
        assert len(ids) == len(set(ids))

    def test_playing_something_else_ends_the_radio(self, ctx, controller, library):
        controller.start_from_track(library["elvis0"])
        with ctx.session() as s:
            other = list(s.scalars(select(Track).where(Track.artist_display == "Boy Band")))
            ctx.play_tracks(other, navigate=False)
        assert not controller.active

    def test_artist_radio_opens_with_the_artist(self, ctx, controller, library):
        assert controller.start_from_artist(library["elvis_artist"])
        assert ctx.player.queue[0].artist == "Elvis Presley"
        assert controller.label == "Elvis Presley"

    def test_stop_keeps_the_queue(self, ctx, controller, library):
        controller.start_from_track(library["elvis0"])
        n = len(ctx.player.queue)
        controller.stop()
        ctx.player.jump_to(n - 1)
        assert len(ctx.player.queue) == n


class TestNowPlayingButton:
    def test_start_and_stop_from_now_playing(self, ctx, controller, library):
        from musicmgr.ui.views.nowplaying import NowPlayingView

        view = NowPlayingView(ctx)
        assert not view.radio_button.isEnabled()
        with ctx.session() as s:
            ctx.play_tracks([s.get(Track, library["elvis0"])], navigate=False)
        assert view.radio_button.isEnabled() and view.radio_button.text() == "Start radio"
        view.radio_button.click()
        assert controller.active and view.radio_button.text() == "Stop radio"
        view.radio_button.click()
        assert not controller.active and view.radio_button.text() == "Start radio"


def test_lastfm_key_is_optional_for_radio(ctx, controller, library):
    with ctx.session() as s:
        assert lfm.get_api_key(s) is None
    messages = []
    ctx.notified.connect(messages.append)
    controller.start_from_track(library["elvis0"])
    assert any("Last.fm API key" in m for m in messages)
