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
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicmgr.services import lyrics_downloader as ld


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

    def fake_download(session, audio_path, *, title, artist, album="", duration_ms=0, overwrite=False, save_plain=True):
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
