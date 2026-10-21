"""services/lyrics_downloader.py - LRCLIB lookup/matching/save logic.

No Qt involved (this module is pure `requests` + stdlib), so these don't
need the `qapp`/`db` fixtures from conftest.py - just a fake `requests`
session (`_FakeSession` below) standing in for LRCLIB itself, same shape
as the original lrc_downloader.py script's own test approach.

2026-09-20 additions: the `save_plain=False` synced-only path (James:
"is there anyway we can force LRCLIB to get only Synced lyrics") - a
plain-only LRCLIB match must come back as its own `plain_only` status,
not get silently saved and not collapse into `not_found` either, and
`AlbumLyricsResult.summary()` must surface that count.

2026-10-21 additions: YouTube Music as the fallback source. The autouse
`_no_ytmusic` fixture below makes YouTube Music "have nothing" for every
test, so no test here ever reaches the network; the YouTube Music tests
override it with `_ytmusic_returns`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicmgr.services import lyrics_downloader as ld
from musicmgr.services import ytmusic_lyrics as ytl


@pytest.fixture(autouse=True)
def _no_ytmusic(monkeypatch):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return ld.LyricsTextFetch("not_found")

    monkeypatch.setattr(ytl, "fetch_lyrics_text", fake)
    return calls


def _ytmusic_returns(monkeypatch, fetch):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return fetch

    monkeypatch.setattr(ytl, "fetch_lyrics_text", fake)
    return calls


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        pass


class _FakeSession:
    """Stands in for `requests.Session` - `get_response` is called once per
    `.get()`, in order, with the request `params` handed back for callers
    that want to assert on the exact lookup that was made."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self._responses.pop(0)


def _track_file(tmp_path: Path, name: str = "track") -> Path:
    p = tmp_path / f"{name}.mp3"
    p.write_bytes(b"not really audio")
    return p


# -- download_lyrics_for_track: matching / status shape ----------------------


def test_synced_lyrics_downloaded_regardless_of_save_plain(tmp_path):
    for save_plain in (True, False):
        audio = _track_file(tmp_path, f"synced_{save_plain}")
        body = {"syncedLyrics": "[00:01.00]la la", "plainLyrics": "la la", "instrumental": False}
        session = _FakeSession([_FakeResponse(200, body)])

        outcome = ld.download_lyrics_for_track(
            session, audio, title="T", artist="A", album="Al", duration_ms=180000, save_plain=save_plain
        )

        assert outcome.status == "downloaded"
        assert audio.with_suffix(".lrc").read_text(encoding="utf-8").startswith("[00:01.00]")


def test_plain_only_saved_when_save_plain_true(tmp_path):
    audio = _track_file(tmp_path)
    body = {"syncedLyrics": None, "plainLyrics": "just words, no timing", "instrumental": False}
    session = _FakeSession([_FakeResponse(200, body)])

    outcome = ld.download_lyrics_for_track(
        session, audio, title="T", artist="A", save_plain=True
    )

    assert outcome.status == "downloaded"
    assert audio.with_suffix(".lrc").read_text(encoding="utf-8").strip() == "just words, no timing"


def test_plain_only_skipped_when_save_plain_false(tmp_path):
    audio = _track_file(tmp_path)
    body = {"syncedLyrics": None, "plainLyrics": "just words, no timing", "instrumental": False}
    session = _FakeSession([_FakeResponse(200, body)])

    outcome = ld.download_lyrics_for_track(
        session, audio, title="T", artist="A", save_plain=False
    )

    assert outcome.status == "plain_only"
    assert not audio.with_suffix(".lrc").exists()


def test_instrumental_reported_before_plain_only_check(tmp_path):
    audio = _track_file(tmp_path)
    body = {"syncedLyrics": None, "plainLyrics": None, "instrumental": True}
    session = _FakeSession([_FakeResponse(200, body)])

    outcome = ld.download_lyrics_for_track(session, audio, title="T", artist="A", save_plain=False)

    assert outcome.status == "instrumental"


def test_not_found_when_lrclib_has_nothing(tmp_path):
    audio = _track_file(tmp_path)
    # exact lookup 404s, search fallback returns an empty list
    session = _FakeSession([_FakeResponse(404), _FakeResponse(200, [])])

    outcome = ld.download_lyrics_for_track(session, audio, title="T", artist="A", save_plain=False)

    assert outcome.status == "not_found"


def test_already_exists_skips_network_entirely(tmp_path):
    audio = _track_file(tmp_path)
    audio.with_suffix(".lrc").write_text("existing", encoding="utf-8")
    session = _FakeSession([])  # would raise IndexError if .get() were called

    outcome = ld.download_lyrics_for_track(session, audio, title="T", artist="A")

    assert outcome.status == "already_exists"


def test_no_artist_skips_network_entirely(tmp_path):
    audio = _track_file(tmp_path)
    session = _FakeSession([])

    outcome = ld.download_lyrics_for_track(session, audio, title="T", artist="")

    assert outcome.status == "no_artist"


# -- AlbumLyricsResult.summary() ----------------------------------------------


def test_summary_includes_plain_only_count():
    result = ld.AlbumLyricsResult(downloaded=2, plain_only=3, not_found=1)
    assert "3 synced unavailable (plain only)" in result.summary()


def test_summary_omits_plain_only_when_zero():
    result = ld.AlbumLyricsResult(downloaded=2, not_found=1)
    assert "plain only" not in result.summary()


# -- download_lyrics_for_album: aggregation -----------------------------------


def test_album_aggregates_plain_only_and_paces_like_a_real_hit(tmp_path, monkeypatch):
    """A plain_only outcome came from a real network round trip (unlike
    already_exists/no_artist, which never call out), so it must still be
    paced by rate_limit_s like any other hit."""
    t1 = _track_file(tmp_path, "one")
    t2 = _track_file(tmp_path, "two")
    tracks = [
        ld.LyricsTrackInput(audio_path=t1, title="One", artist="A"),
        ld.LyricsTrackInput(audio_path=t2, title="Two", artist="A"),
    ]

    responses = iter(
        [
            {"syncedLyrics": None, "plainLyrics": "words", "instrumental": False},  # track one: exact hit
            {"syncedLyrics": None, "plainLyrics": None, "instrumental": True},  # track two: exact hit
        ]
    )

    def fake_download(session, audio_path, *, title, artist, album="", duration_ms=0, overwrite=False, save_plain=True, **_):
        body = next(responses)
        if body["instrumental"]:
            return ld.TrackLyricsOutcome(Path(audio_path), "instrumental")
        return ld.TrackLyricsOutcome(Path(audio_path), "plain_only")

    monkeypatch.setattr(ld, "download_lyrics_for_track", fake_download)

    sleeps: list[float] = []
    monkeypatch.setattr(ld.time, "sleep", lambda s: sleeps.append(s))

    result = ld.download_lyrics_for_album(tracks, save_plain=False, rate_limit_s=0.75)

    assert result.plain_only == 1
    assert result.instrumental == 1
    # one pace-out between the two tracks (both were real network hits)
    assert sleeps == [0.75]


# -- YouTube Music fallback (2026-10-21) ----------------------------------------

_LRCLIB_MISS = [_FakeResponse(404), _FakeResponse(200, [])]  # exact 404, empty search


def test_ytmusic_synced_used_when_lrclib_has_nothing(tmp_path, monkeypatch):
    audio = _track_file(tmp_path)
    calls = _ytmusic_returns(monkeypatch, ld.LyricsTextFetch("found", plain="la", synced="[00:02.00]la"))

    outcome = ld.download_lyrics_for_track(
        _FakeSession(list(_LRCLIB_MISS)), audio, title="T", artist="A", duration_ms=181_400, save_plain=False
    )

    assert outcome.status == "downloaded"
    assert outcome.detail == "YouTube Music"
    assert audio.with_suffix(".lrc").read_text(encoding="utf-8") == "[00:02.00]la\n"
    assert calls == [{"title": "T", "artist": "A", "album": "", "duration_ms": 181_000}]


def test_ytmusic_synced_beats_lrclib_plain(tmp_path, monkeypatch):
    audio = _track_file(tmp_path)
    _ytmusic_returns(monkeypatch, ld.LyricsTextFetch("found", plain="la", synced="[00:02.00]la"))
    body = {"syncedLyrics": None, "plainLyrics": "lrclib words", "instrumental": False}

    outcome = ld.download_lyrics_for_track(
        _FakeSession([_FakeResponse(200, body)]), audio, title="T", artist="A", save_plain=True
    )

    assert outcome.detail == "YouTube Music"
    assert audio.with_suffix(".lrc").read_text(encoding="utf-8").startswith("[00:02.00]")


def test_lrclib_plain_beats_ytmusic_plain(tmp_path, monkeypatch):
    audio = _track_file(tmp_path)
    _ytmusic_returns(monkeypatch, ld.LyricsTextFetch("found", plain="youtube words"))
    body = {"syncedLyrics": None, "plainLyrics": "lrclib words", "instrumental": False}

    outcome = ld.download_lyrics_for_track(
        _FakeSession([_FakeResponse(200, body)]), audio, title="T", artist="A", save_plain=True
    )

    assert outcome.status == "downloaded"
    assert audio.with_suffix(".lrc").read_text(encoding="utf-8").strip() == "lrclib words"


def test_ytmusic_plain_saved_only_when_plain_allowed(tmp_path, monkeypatch):
    _ytmusic_returns(monkeypatch, ld.LyricsTextFetch("found", plain="youtube words"))

    audio = _track_file(tmp_path, "plain_ok")
    outcome = ld.download_lyrics_for_track(
        _FakeSession(list(_LRCLIB_MISS)), audio, title="T", artist="A", save_plain=True
    )
    assert outcome.status == "downloaded" and outcome.detail == "YouTube Music"

    audio = _track_file(tmp_path, "synced_only")
    outcome = ld.download_lyrics_for_track(
        _FakeSession(list(_LRCLIB_MISS)), audio, title="T", artist="A", save_plain=False
    )
    assert outcome.status == "plain_only"
    assert not audio.with_suffix(".lrc").exists()


def test_lrclib_synced_never_asks_ytmusic(tmp_path, _no_ytmusic):
    audio = _track_file(tmp_path)
    body = {"syncedLyrics": "[00:01.00]x", "plainLyrics": "x", "instrumental": False}
    ld.download_lyrics_for_track(_FakeSession([_FakeResponse(200, body)]), audio, title="T", artist="A")
    assert _no_ytmusic == []


def test_instrumental_never_asks_ytmusic(tmp_path, _no_ytmusic):
    audio = _track_file(tmp_path)
    body = {"syncedLyrics": None, "plainLyrics": None, "instrumental": True}
    outcome = ld.download_lyrics_for_track(_FakeSession([_FakeResponse(200, body)]), audio, title="T", artist="A")
    assert outcome.status == "instrumental"
    assert _no_ytmusic == []


def test_use_ytmusic_false_skips_it(tmp_path, monkeypatch):
    calls = _ytmusic_returns(monkeypatch, ld.LyricsTextFetch("found", synced="[00:01.00]x"))
    audio = _track_file(tmp_path)
    outcome = ld.download_lyrics_for_track(
        _FakeSession(list(_LRCLIB_MISS)), audio, title="T", artist="A", use_ytmusic=False
    )
    assert outcome.status == "not_found"
    assert calls == []


def test_summary_counts_ytmusic_downloads():
    result = ld.AlbumLyricsResult(downloaded=5, from_ytmusic=2)
    assert "5 downloaded (2 from YouTube Music)" in result.summary()


# -- ytmusic_lyrics itself (a fake YTMusic client, no network) -----------------


class _FakeYT:
    def __init__(self, results, lyrics_id="MPLYt_x", lyrics=None, raise_on_search=None):
        self.results, self.lyrics_id, self.lyrics = results, lyrics_id, lyrics
        self.raise_on_search = raise_on_search
        self.calls: list = []

    def search(self, query, filter=None, limit=None):
        self.calls.append(("search", query, filter))
        if self.raise_on_search:
            raise self.raise_on_search
        return self.results

    def get_watch_playlist(self, videoId=None, limit=None):
        self.calls.append(("watch", videoId))
        return {"lyrics": self.lyrics_id}

    def get_lyrics(self, browseId, timestamps=False):
        self.calls.append(("lyrics", browseId, timestamps))
        return self.lyrics


def _song(title, artist, seconds, vid):
    return {"title": title, "artists": [{"name": artist}], "duration_seconds": seconds, "videoId": vid}


#: the real lookup, captured at import - before the autouse fixture
#: replaces the module attribute for each test
ytl_real_fetch = ytl.fetch_lyrics_text


def test_choose_best_song_matches_title_artist_and_length():
    results = [
        _song("Hotel California (Live)", "Eagles", 432, "live"),
        _song("Hotel California", "Some Cover Band", 391, "cover"),
        _song("Hotel California (2013 Remaster)", "Eagles", 391, "studio"),
        _song("Hotel California", "Eagles", 500, "too_long"),
    ]
    best = ytl.choose_best_song(results, title="Hotel California", artist="Eagles", duration_ms=390_000)
    assert best["videoId"] == "studio"


def test_choose_best_song_none_when_nothing_matches():
    results = [_song("Other Song", "Eagles", 300, "x")]
    assert ytl.choose_best_song(results, title="Hotel California", artist="Eagles") is None


def test_timed_lyrics_become_lrc_and_plain():
    lyrics = {
        "hasTimestamps": True,
        "source": "Source: LyricFind",
        "lyrics": [
            {"text": "First line", "start_time": 9200, "end_time": 10000, "id": 1},
            {"text": "♪", "start_time": 65_430, "end_time": 70000, "id": 2},
            {"text": "Second line", "start_time": 70_005, "end_time": 72000, "id": 3},
        ],
    }
    yt = _FakeYT([_song("Song", "Artist", 200, "v1")], lyrics=lyrics)
    fetch = ytl_real_fetch(title="Song", artist="Artist", duration_ms=200_000, client=yt)
    assert fetch.status == "found"
    assert fetch.synced == "[00:09.20]First line\n[01:05.43]\n[01:10.00]Second line"
    assert fetch.plain == "First line\nSecond line"
    assert ("lyrics", "MPLYt_x", True) in yt.calls


def test_untimed_lyrics_come_back_plain():
    lyrics = {"hasTimestamps": False, "source": None, "lyrics": "Just words\nMore words\n"}
    yt = _FakeYT([_song("Song", "Artist", 200, "v1")], lyrics=lyrics)
    fetch = ytl_real_fetch(title="Song", artist="Artist", client=yt)
    assert (fetch.status, fetch.plain, fetch.synced) == ("found", "Just words\nMore words", "")


def test_ytmusic_statuses():
    assert ytl_real_fetch(title="Song", artist="", client=_FakeYT([])).status == "no_artist"
    assert ytl_real_fetch(title="Song", artist="Artist", client=_FakeYT([])).status == "not_found"
    no_lyrics = _FakeYT([_song("Song", "Artist", 200, "v1")], lyrics_id=None)
    assert ytl_real_fetch(title="Song", artist="Artist", client=no_lyrics).status == "not_found"
    broken = _FakeYT([], raise_on_search=RuntimeError("endpoint changed"))
    fetch = ytl_real_fetch(title="Song", artist="Artist", client=broken)
    assert fetch.status == "error" and "endpoint changed" in fetch.detail
