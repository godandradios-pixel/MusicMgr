"""Tests for services/voice_ask.py - spoken requests from the phone remote
(2026-10-15): the library look-ups, the offline phrasings, Claude's tool-use
loop (with a fake transport - no network), and playing / "Add to queue
instead" through the web remote's RemoteApi and HTTP server."""

from __future__ import annotations

import datetime as dt
import json
import urllib.request

import pytest

from musicmgr.db.models import (
    ArtistTopTrack,
    Chart,
    ChartEntry,
    ChartIssue,
    Genre,
    MediaFile,
    Playlist,
    PlaylistItem,
    Track,
)
from musicmgr.services import library as lib
from musicmgr.services import voice_ask, web_remote
from musicmgr.services.matching import normalize

from tests.test_web_remote import _free_port, _request, make_wav


def add_track(session, tmp_path, title, artist, album, year, n, *, genre=None, compilation=False):
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist, year=year)
    release.album_artist_id = artist_obj.id
    release.is_compilation = compilation
    if genre and not release.genres:
        g = session.query(Genre).filter_by(name=genre).first() or Genre(name=genre)
        release.genres.append(g)
    t = Track(release_id=release.id, title=title, title_key=normalize(title),
              artist_display=artist, track_no=n, duration_ms=1000)
    session.add(t)
    session.flush()
    path = make_wav(tmp_path / f"{album}-{n}-{title}.wav".replace("/", "_"))
    session.add(MediaFile(track_id=t.id, path=path))
    session.flush()
    return t


@pytest.fixture
def lib_data(session, tmp_path):
    T = {}
    for i, title in enumerate(["Burning Love", "Suspicious Minds", "Hound Dog"]):
        T[title] = add_track(session, tmp_path, title, "Elvis Presley", "Elvis Hits", 1972, i + 1,
                             genre="Rock")
    for i, title in enumerate(["Women", "Rocket", "Animal"]):
        T[title] = add_track(session, tmp_path, title, "Def Leppard", "Hysteria", 1987, i + 1,
                             genre="Rock")
    T["Coal Miner"] = add_track(session, tmp_path, "Coal Miner's Daughter", "Loretta Lynn",
                                "Coal Miner's Daughter", 1970, 1, genre="Country")
    T["Crazy"] = add_track(session, tmp_path, "Crazy", "Patsy Cline", "Showcase", 1961, 1,
                           genre="Country")
    # the same Burning Love again on a hits compilation - the album copy wins
    T["Burning Love (comp)"] = add_track(session, tmp_path, "Burning Love", "Elvis Presley",
                                         "Billboard 1972", 1972, 1, compilation=True)
    elvis = lib.get_or_create_artist(session, "Elvis Presley")
    elvis.profile = "American singer from Tupelo, Mississippi, the King of Rock and Roll."
    for rank, title in enumerate(["Suspicious Minds", "Can't Help Falling in Love", "Hound Dog"], 1):
        session.add(ArtistTopTrack(artist_id=elvis.id, rank=rank, title=title, playcount=100 - rank))

    chart = Chart(name="Billboard Hot 100 Year End", slug="hot100-ye", size=100)
    session.add(chart)
    session.flush()
    issue = ChartIssue(chart_id=chart.id, chart_date=dt.date(1972, 12, 31))
    session.add(issue)
    session.flush()
    for rank, (title, artist, tid) in enumerate([
        ("Burning Love", "Elvis Presley", T["Burning Love"].id),
        ("American Pie", "Don McLean", None),
        ("Suspicious Minds", "Elvis Presley", T["Suspicious Minds"].id),
    ], 1):
        session.add(ChartEntry(issue_id=issue.id, rank=rank, title=title, artist_name=artist,
                               title_key=normalize(title), artist_key=normalize(artist), track_id=tid))
    pl = Playlist(name="Road Trip")
    session.add(pl)
    session.flush()
    for pos, key in enumerate(["Rocket", "Crazy"]):
        session.add(PlaylistItem(playlist_id=pl.id, track_id=T[key].id, position=pos))
    session.commit()
    T["chart_id"] = chart.id
    return T


def inline(session):
    """run_main for tests: everything already on the main thread."""
    return lambda fn: fn(session)


def titles(session, ids):
    return [session.get(Track, i).title for i in ids]


class TestOffline:
    @pytest.fixture
    def ask(self, session, lib_data):
        tools = voice_ask.LibraryTools()
        return lambda text: voice_ask.offline_ask(session, tools, text)

    def test_artist_mix(self, ask, session):
        r = ask("Play some Elvis")
        assert r.shuffle and r.engine == "offline"
        assert sorted(titles(session, r.track_ids)) == ["Burning Love", "Hound Dog", "Suspicious Minds"]

    def test_duplicate_copy_is_dropped_for_the_album_one(self, ask, session, lib_data):
        r = ask("play elvis presley")
        assert lib_data["Burning Love"].id in r.track_ids
        assert lib_data["Burning Love (comp)"].id not in r.track_ids

    def test_greatest_hits_uses_top_songs(self, ask, session):
        r = ask("play the greatest hits of elvis")
        # Last.fm order, owned songs only
        assert titles(session, r.track_ids) == ["Suspicious Minds", "Hound Dog"]

    def test_chart_top_n_of_year(self, ask, session):
        r = ask("play the top 20 of 1972")
        assert titles(session, r.track_ids) == ["Burning Love", "Suspicious Minds"]
        assert r.title == "Top 20 of 1972" and not r.shuffle

    def test_number_ones(self, ask, session):
        r = ask("play number ones from 1972")
        assert titles(session, r.track_ids) == ["Burning Love"]

    def test_no_chart_for_that_year(self, ask):
        r = ask("play the top 10 of 1950")
        assert not r.track_ids and "1950" in r.message

    def test_playlist(self, ask, session):
        r = ask("play my road trip playlist")
        assert titles(session, r.track_ids) == ["Rocket", "Crazy"]

    def test_album(self, ask, session):
        r = ask("play the album hysteria")
        assert titles(session, r.track_ids) == ["Women", "Rocket", "Animal"]

    def test_song_by_artist(self, ask, session):
        r = ask("play Rocket by Def Leppard")
        assert titles(session, r.track_ids) == ["Rocket"]

    def test_genre(self, ask, session):
        r = ask("play some country")
        assert sorted(titles(session, r.track_ids)) == ["Coal Miner's Daughter", "Crazy"]

    def test_nothing(self, ask):
        r = ask("play zzzz qqqq")
        assert not r.track_ids and "couldn't find" in r.message


class TestTools:
    def test_chart_songs_counts_gaps(self, session, lib_data):
        out = voice_ask.LibraryTools().chart_songs(session, lib_data["chart_id"], year=1972)
        assert [s["title"] for s in out["songs"]] == ["Burning Love", "Suspicious Minds"]
        assert out["chart_songs_in_range"] == 3 and out["owned_in_range"] == 2

    def test_artist_profile_and_find(self, session, lib_data):
        tools = voice_ask.LibraryTools()
        prof = tools.artist_profile(session, "elvis")
        assert prof["name"] == "Elvis Presley" and "Tupelo" in prof["profile"]
        found = tools.find_artists(session, query="Tupelo")["artists"]
        assert [a["name"] for a in found] == ["Elvis Presley"]
        assert [a["name"] for a in tools.find_artists(session, genre="Country")["artists"]] \
            in (["Loretta Lynn", "Patsy Cline"], ["Patsy Cline", "Loretta Lynn"])

    def test_overview_lists_charts(self, session, lib_data):
        ov = voice_ask.LibraryTools().overview(session)
        assert ov["songs"] == 9 and "Country" in ov["genres"]
        assert ov["charts"][0].startswith("Billboard Hot 100 Year End (Year End, 1972-1972")


class FakeClaude:
    """Plays back scripted assistant turns and records what was sent."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.sent = []

    def __call__(self, api_key, payload, timeout):
        self.sent.append(json.loads(json.dumps(payload)))
        return {"content": self.turns.pop(0)}


def tool(name, **args):
    return {"type": "tool_use", "id": f"tu_{name}", "name": name, "input": args}


KEY = voice_ask.AskPrefs(api_key="sk-test", model="claude-sonnet-5-5")


class TestClaude:
    def test_looks_up_then_plays_only_ids_it_was_given(self, session, lib_data):
        fake = FakeClaude([
            [tool("chart_songs", chart_id=lib_data["chart_id"], year=1972)],
            [tool("play_songs", song_ids=[lib_data["Suspicious Minds"].id, 999999,
                                          lib_data["Burning Love"].id],
                  title="1972 hits", message="2 of the 3 - no American Pie.")],
        ])
        r = voice_ask.Asker(inline(session), transport=fake).ask("hits from 72", KEY)
        assert r.engine == "claude" and r.title == "1972 hits"
        # the made-up id 999999 is dropped, order kept
        assert titles(session, r.track_ids) == ["Suspicious Minds", "Burning Love"]
        first = fake.sent[0]
        assert first["messages"] == [{"role": "user", "content": "hits from 72"}]
        assert "Billboard Hot 100 Year End" in first["system"][0]["text"]
        assert {t["name"] for t in first["tools"]} >= {"chart_songs", "artist_profile", "play_songs"}
        result = fake.sent[1]["messages"][-1]["content"][0]
        assert result["type"] == "tool_result" and "Burning Love" in result["content"]

    def test_say_plays_nothing(self, session, lib_data):
        fake = FakeClaude([[tool("say", message="You don't have any Beatles.")]])
        r = voice_ask.Asker(inline(session), transport=fake).ask("play the beatles", KEY)
        assert r.track_ids == [] and r.message == "You don't have any Beatles."

    def test_bad_tool_arguments_are_reported_back_not_raised(self, session, lib_data):
        fake = FakeClaude([[tool("artist_profile", nonsense=1)], [tool("say", message="ok")]])
        voice_ask.Asker(inline(session), transport=fake).ask("who is elvis", KEY)
        assert "Bad arguments" in fake.sent[1]["messages"][-1]["content"][0]["content"]

    def test_api_error_falls_back_to_offline(self, session, lib_data):
        def broken(*_a):
            raise voice_ask.ClaudeError(401, "invalid x-api-key")

        r = voice_ask.Asker(inline(session), transport=broken).ask("play the album hysteria", KEY)
        assert r.engine == "offline" and titles(session, r.track_ids) == ["Women", "Rocket", "Animal"]
        assert r.message.startswith("The Anthropic API key")

    def test_no_key_means_offline(self, session, lib_data):
        def never(*_a):
            raise AssertionError("no key - Claude must not be called")

        asker = voice_ask.Asker(inline(session), transport=never, gemini_transport=never)
        r = asker.ask("play zzz qqq", voice_ask.AskPrefs(model="claude-sonnet-5-5"))
        assert not r.track_ids and "Anthropic API key" in r.message
        # Gemini is the default choice; its key is separate
        r = asker.ask("play zzz qqq", voice_ask.AskPrefs(api_key="sk-claude-only"))
        assert not r.track_ids and "Google AI Studio key" in r.message

    def test_time_budget(self, session, lib_data):
        clock = iter([0.0, 0.0, 100.0, 100.0])
        fake = FakeClaude([[tool("list_charts")], [tool("say", message="late")]])
        asker = voice_ask.Asker(inline(session), transport=fake, clock=lambda: next(clock))
        r = asker.ask("anything", KEY)  # times out, then offline
        assert r.engine == "offline" and len(fake.sent) == 1


def test_prefs_round_trip(session):
    prefs = voice_ask.load_prefs(session)
    assert prefs.model == voice_ask.DEFAULT_MODEL and prefs.provider == voice_ask.GEMINI
    voice_ask.save_prefs(session, voice_ask.AskPrefs(" sk-1 ", "claude-haiku-5-5", " AIza-2 "))
    prefs = voice_ask.load_prefs(session)
    assert (prefs.api_key, prefs.model, prefs.gemini_key) == ("sk-1", "claude-haiku-5-5", "AIza-2")
    assert prefs.provider == voice_ask.CLAUDE and prefs.active_key == "sk-1"
    prefs.model = "gemini-3.5-flash-lite"
    assert prefs.active_key == "AIza-2"


class FakeGemini:
    """Plays back scripted Gemini turns (lists of parts) and records them."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.sent = []

    def __call__(self, api_key, model, payload, timeout):
        self.sent.append((api_key, model, json.loads(json.dumps(payload))))
        return {"candidates": [{"content": {"role": "model", "parts": self.turns.pop(0)}}]}


def gcall(fn, **args):
    return {"functionCall": {"name": fn, "args": args}, "thoughtSignature": "sig-" + fn}


GKEY = voice_ask.AskPrefs(gemini_key="AIza-test", model="gemini-3.8-flash")


class TestGemini:
    def test_looks_up_then_plays(self, session, lib_data):
        fake = FakeGemini([
            [gcall("artist_top_songs", name="elvis", limit=5.0)],  # numbers may come as floats
            [{"text": "Here you go"},
             gcall("play_songs", song_ids=[float(lib_data["Hound Dog"].id), 424242],
                   title="Elvis", message="Elvis's top songs")],
        ])
        asker = voice_ask.Asker(inline(session), gemini_transport=fake)
        r = asker.ask("elvis best songs", GKEY)
        assert r.engine == "gemini" and titles(session, r.track_ids) == ["Hound Dog"]
        key, model, first = fake.sent[0]
        assert (key, model) == ("AIza-test", "gemini-3.8-flash")
        assert first["contents"] == [{"role": "user", "parts": [{"text": "elvis best songs"}]}]
        names = {f["name"] for f in first["tools"][0]["functionDeclarations"]}
        assert {"chart_songs", "play_songs", "say"} <= names
        assert "Billboard Hot 100 Year End" in first["systemInstruction"]["parts"][0]["text"]
        # the model's turn goes back untouched (thought signature included),
        # then the look-up's answer as a functionResponse
        second = fake.sent[1][2]["contents"]
        assert second[1]["parts"][0]["thoughtSignature"] == "sig-artist_top_songs"
        resp = second[2]["parts"][0]["functionResponse"]
        assert resp["name"] == "artist_top_songs"
        assert [s["title"] for s in resp["response"]["result"]["songs"]] == ["Suspicious Minds", "Hound Dog"]

    def test_text_only_answer(self, session, lib_data):
        fake = FakeGemini([[{"text": "Which Elvis album?", "thought": False}]])
        r = voice_ask.Asker(inline(session), gemini_transport=fake).ask("elvis album", GKEY)
        assert r.track_ids == [] and r.message == "Which Elvis album?"

    def test_quota_falls_back_to_offline(self, session, lib_data):
        def quota(*_a):
            raise voice_ask.AIError(429, "Resource has been exhausted", voice_ask.GEMINI)

        r = voice_ask.Asker(inline(session), gemini_transport=quota).ask("play the top 20 of 1972", GKEY)
        assert r.engine == "offline" and len(r.track_ids) == 2
        assert r.message.startswith("Gemini's free limit is used up")

    def test_bad_key_message(self):
        err = voice_ask.AIError(400, "API key not valid. Please pass a valid API key.", voice_ask.GEMINI)
        assert "Google AI Studio key" in err.friendly()

    def test_blocked_prompt(self, session, lib_data):
        def blocked(*_a):
            return {"promptFeedback": {"blockReason": "SAFETY"}}

        r = voice_ask.Asker(inline(session), gemini_transport=blocked).ask("play the album hysteria", GKEY)
        assert r.engine == "offline" and titles(session, r.track_ids) == ["Women", "Rocket", "Animal"]


def test_settings_save_both_keys_and_model(ctx, qapp):
    from musicmgr.ui.views.settings import SettingsView

    view = SettingsView(ctx)
    assert view.ask_model_combo.currentData() == voice_ask.DEFAULT_MODEL
    assert "free Google AI Studio key" in view.ask_status.text()
    view.ask_gemini_key_edit.setText("AIza-9")
    view.ask_gemini_key_edit.editingFinished.emit()
    assert "Gemini" in view.ask_status.text()
    view.ask_model_combo.setCurrentIndex(view.ask_model_combo.findData("claude-haiku-5-5"))
    assert "Anthropic API key" in view.ask_status.text()
    with ctx.session() as db:
        prefs = voice_ask.load_prefs(db)
    assert (prefs.model, prefs.gemini_key, prefs.api_key) == ("claude-haiku-5-5", "AIza-9", "")


class TestRemote:
    def test_play_then_add_to_queue_instead(self, ctx, session, lib_data):
        api = web_remote.RemoteApi(ctx, lambda: "PC1")
        hysteria = [lib_data[t] for t in ("Women", "Rocket", "Animal")]
        ctx.play_tracks(hysteria, start=1)
        assert ctx.player.current.title == "Rocket"

        result = voice_ask.AskResult(track_ids=[lib_data["Crazy"].id, lib_data["Hound Dog"].id],
                                     title="Mix", message="Two songs", engine="claude")
        out = api.play_ask(result)
        assert out["ok"] and out["count"] == 2 and out["can_queue_instead"]
        assert [s["title"] for s in out["songs"]] == ["Crazy", "Hound Dog"]
        assert ctx.player.current.title == "Crazy"

        msg = api.command({"cmd": "ask_queue_instead"})["message"]
        assert "2 songs queued" in msg
        assert ctx.player.current.title == "Rocket"
        assert [i.title for i in ctx.player.queue] == ["Women", "Rocket", "Animal", "Crazy", "Hound Dog"]
        # only once
        assert api.command({"cmd": "ask_queue_instead"})["message"] == "Nothing to put back"

    def test_nothing_playing_before_has_no_queue_instead(self, ctx, lib_data):
        api = web_remote.RemoteApi(ctx, lambda: "PC1")
        out = api.play_ask(voice_ask.AskResult(track_ids=[lib_data["Crazy"].id]))
        assert out["ok"] and not out["can_queue_instead"]

    def test_empty_result(self, ctx):
        api = web_remote.RemoteApi(ctx, lambda: "PC1")
        out = api.play_ask(voice_ask.AskResult(message="Nothing like that"))
        assert not out["ok"] and out["message"] == "Nothing like that"

    def test_http_round_trip_offline(self, qapp, ctx, lib_data):
        server = ctx.web_remote
        ok, msg = server.start(_free_port(), "Den")
        assert ok, msg
        try:
            base = f"http://127.0.0.1:{server.port}"
            req = lambda headers: urllib.request.Request(  # noqa: E731
                base + "/api/ask", data=json.dumps({"text": "play the top 20 of 1972"}).encode(),
                method="POST", headers={"Content-Type": "application/json", **headers})
            status, _ = _request(qapp, req({}))
            assert status == 403  # the command header is required here too
            status, body = _request(qapp, req({"X-MusicMgr": "1"}))
            assert status == 200, body
            out = json.loads(body)
            assert out["engine"] == "offline" and out["count"] == 2
            assert out["state"]["item"]["title"] == "Burning Love"
            status, body = _request(qapp, base + "/")
            assert b'id="askq"' in body
        finally:
            server.stop()
