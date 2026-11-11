"""Podcasts on the Radio page's PODCAST band (2026-11-10 - James: "Let's
see if a new PODCAST band can fit in the radio. With the SETTINGS option to
hide it. New episodes, stream only, with a new D:\\Podcast folder with USB
sync SETTINGS checkbox, and include 1.2x 1.5x playback control" - then
"it should have been 1.25").

services/podcasts.py, podcast_sync.py, the tuner, the player's speed, the
dial, the Radio page, Settings and the USB folder pair. No network: feeds
come from a fake HTTP session."""

from __future__ import annotations

import datetime as dt
import wave
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db import session as db_session
from musicmgr.db.models import PodcastEpisode, PodcastFeed, WatchedFolder
from musicmgr.services import library_state as ls
from musicmgr.services import podcasts, radio_folder, stations
from musicmgr.services import usb_sync as us
from musicmgr.services.player import PlayerController, QueueItem
from musicmgr.ui.widgets import radio_dial

FEED_URL = "https://feeds.example.com/history.xml"


def rss(items: list[tuple[str, str, str]], title="History Hour") -> bytes:
    """A feed with (guid, title, pubDate) items, newest given first or not."""
    body = "".join(
        f"""<item><title>{t}</title><guid>{g}</guid><pubDate>{d}</pubDate>
        <enclosure url="https://cdn.example.com/{g}.mp3" type="audio/mpeg" length="1"/>
        <itunes:duration>1:02:03</itunes:duration>
        <description>&lt;p&gt;Notes for &lt;b&gt;{t}&lt;/b&gt;&lt;/p&gt;&lt;p&gt;Second&lt;/p&gt;</description>
        </item>"""
        for g, t, d in items)
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>{title}</title><itunes:author>Radio Folks</itunes:author>
<itunes:image href="https://cdn.example.com/cover.jpg"/>
{body}
<item><title>A video only</title><guid>vid</guid>
<enclosure url="https://cdn.example.com/v.mp4" type="video/mp4"/></item>
<item><title>No enclosure</title><guid>text</guid></item>
</channel></rss>""".encode()


EPS = [("e1", "The Alamo", "Mon, 03 Nov 2026 10:00:00 GMT"),
       ("e2", "Gettysburg", "Mon, 10 Nov 2026 10:00:00 -0500"),
       ("e0", "Plymouth", "Mon, 27 Oct 2026 10:00:00 GMT")]


class Resp:
    def __init__(self, status=200, content=b"", headers=None, data=None):
        self.status_code, self.content = status, content
        self.headers = headers or {}
        self._data = data

    def json(self):
        return self._data

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHttp:
    """Answers by URL; records each request's headers."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, timeout=None, headers=None, params=None, stream=False):
        self.calls.append((url, dict(headers or {}), dict(params or {})))
        route = self.routes.get(url)
        if callable(route):
            return route(headers or {}, params or {})
        return route or Resp(404)


def silent_wav(path: Path, seconds: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def feed_http(items=EPS, etag='"v1"'):
    def answer(headers, _params):
        if headers.get("If-None-Match") == etag:
            return Resp(304)
        return Resp(200, rss(items), {"ETag": etag})

    return FakeHttp({FEED_URL: answer,
                     "https://cdn.example.com/cover.jpg": Resp(200, b"\xff\xd8jpeg")})


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from musicmgr import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def subscribe(items=EPS, http=None) -> int:
    return podcasts.subscribe(db_session.new_session, FEED_URL, http=http or feed_http(items))


# -- reading feeds ------------------------------------------------------------


class TestFeed:
    def test_parse(self):
        info = podcasts.parse_feed(rss(EPS))
        assert info.title == "History Hour" and info.author == "Radio Folks"
        assert info.image_url == "https://cdn.example.com/cover.jpg"
        # newest first; the video-only and enclosure-less items are skipped
        assert [e.title for e in info.episodes] == ["Gettysburg", "The Alamo", "Plymouth"]
        ep = info.episodes[0]
        assert ep.duration_ms == 3_723_000
        assert ep.published == dt.datetime(2026, 11, 10, 15, 0)       # -0500 -> UTC
        assert ep.audio_url == "https://cdn.example.com/e2.mp3"
        assert ep.description == "Notes for Gettysburg\nSecond"

    @pytest.mark.parametrize("raw, ms", [
        ("1:02:03", 3_723_000), ("62:03", 3_723_000), ("3723", 3_723_000),
        ("", None), ("abc", None), ("0", None)])
    def test_duration(self, raw, ms):
        assert podcasts.parse_duration(raw) == ms

    def test_not_a_feed(self):
        with pytest.raises(podcasts.FeedError):
            podcasts.parse_feed(b"<html><body>hello</body></html>")
        with pytest.raises(podcasts.FeedError):
            podcasts.parse_feed(b"not xml at all <")

    def test_apple_link_is_looked_up_to_its_feed(self):
        http = FakeHttp({podcasts.ITUNES_LOOKUP: Resp(200, data={"results": [
            {"collectionName": "History Hour", "feedUrl": FEED_URL}]})})
        link = "https://podcasts.apple.com/us/podcast/history-hour/id123456789"
        assert podcasts.resolve_link(link, http=http) == FEED_URL
        assert http.calls[0][2] == {"id": "123456789"}
        assert podcasts.resolve_link(FEED_URL, http=http) == FEED_URL

    def test_search(self):
        http = FakeHttp({podcasts.ITUNES_SEARCH: Resp(200, data={"results": [
            {"collectionName": "History Hour", "artistName": "Radio Folks", "feedUrl": FEED_URL,
             "primaryGenreName": "History", "trackCount": 120},
            {"collectionName": "No feed"},
        ]})})
        found = podcasts.search("history", http=http)
        assert [f.title for f in found] == ["History Hour"]
        assert found[0].detail == "Radio Folks · History · 120 episodes"
        assert http.calls[0][2]["media"] == "podcast"

    def test_links(self):
        assert podcasts.looks_like_link("https://x.com/feed.xml")
        assert not podcasts.looks_like_link("the history hour")


# -- subscriptions and checking -------------------------------------------------


class TestSubscriptions:
    def test_subscribe_reads_the_feed_and_saves_the_cover(self, db, data_dir):
        feed_id = subscribe()
        with db.session_scope() as s:
            feed = s.get(PodcastFeed, feed_id)
            assert feed.title == "History Hour" and feed.etag == '"v1"'
            assert Path(feed.image_path).read_bytes() == b"\xff\xd8jpeg"
            eps = podcasts.episodes(s, feed_id)
            assert [e.title for e in eps] == ["Gettysburg", "The Alamo", "Plymouth"]
            # what was in the feed already isn't "new"
            assert podcasts.new_count(s, feed_id) == 0

    def test_a_link_that_isnt_a_podcast_is_refused(self, db, data_dir):
        http = FakeHttp({FEED_URL: Resp(200, b"<html></html>")})
        with pytest.raises(podcasts.FeedError):
            podcasts.subscribe(db_session.new_session, FEED_URL, http=http)
        with db.session_scope() as s:
            assert podcasts.feeds(s) == []

    def test_check_finds_new_episodes_and_asks_politely(self, db, data_dir):
        feed_id = subscribe()
        http = feed_http()
        result = podcasts.check(db_session.new_session, http=http)
        assert result.checked == 1 and result.new == 0
        assert http.calls[0][1]["If-None-Match"] == '"v1"'          # answered 304
        newer = [("e3", "Yorktown", "Tue, 11 Nov 2026 09:00:00 GMT")] + EPS
        result = podcasts.check(db_session.new_session, http=feed_http(newer, etag='"v2"'))
        assert result.new == 1 and result.per_feed == {feed_id: 1}
        assert result.summary() == "1 new episode"
        with db.session_scope() as s:
            eps = podcasts.episodes(s, feed_id)
            assert eps[0].title == "Yorktown" and podcasts.is_new(eps[0])
            assert podcasts.new_count(s, feed_id) == 1

    def test_only_due_skips_recently_checked(self, db, data_dir):
        subscribe()
        result = podcasts.check(db_session.new_session, only_due=True, http=feed_http())
        assert result.checked == 0

    def test_a_feed_that_fails_says_so(self, db, data_dir):
        feed_id = subscribe()
        result = podcasts.check(db_session.new_session, http=FakeHttp({FEED_URL: Resp(500)}))
        assert result.failed == ["History Hour"]
        with db.session_scope() as s:
            assert "500" in s.get(PodcastFeed, feed_id).last_error

    def test_episodes_that_leave_the_feed_go_unless_started(self, db, data_dir):
        feed_id = subscribe()
        with db.session_scope() as s:
            alamo = s.scalar(select(PodcastEpisode).where(PodcastEpisode.guid == "e1"))
            alamo.position_ms = 60_000
        podcasts.check(db_session.new_session, http=feed_http([EPS[1]], etag='"v2"'))
        with db.session_scope() as s:
            assert [e.guid for e in podcasts.episodes(s, feed_id)] == ["e2", "e1"]

    def test_move_and_remove(self, db, data_dir):
        a = subscribe()
        with db.session_scope() as s:
            b = podcasts.add_feed(s, "https://other.example.com/rss", "Other").id
            assert [f.id for f in podcasts.feeds(s)] == [a, b]
            assert podcasts.move(s, b, -1)
            assert [f.id for f in podcasts.feeds(s)] == [b, a]
            assert not podcasts.move(s, b, -1)
            podcasts.remove_feed(s, a)
            assert [f.id for f in podcasts.feeds(s)] == [b]
            assert s.scalar(select(PodcastEpisode)) is None


# -- playing --------------------------------------------------------------------


class TestPlaying:
    def test_resume_then_newest_unheard(self, db, data_dir):
        feed_id = subscribe()
        with db.session_scope() as s:
            feed = s.get(PodcastFeed, feed_id)
            eps = podcasts.episodes(s, feed_id)
            assert podcasts.current_episode(s, feed).guid == "e2"     # newest
            podcasts.mark_played(s, eps[0])
            assert podcasts.current_episode(s, feed).guid == "e1"     # newest not heard
            eps[2].position_ms = 90_000
            assert podcasts.current_episode(s, feed).guid == "e0"     # the one in progress

    def test_queue_streams_and_resumes(self, db, data_dir):
        feed_id = subscribe()
        with db.session_scope() as s:
            eps = podcasts.episodes(s, feed_id)
            eps[0].position_ms = 125_000
            items = podcasts.queue_for_feed(s, feed_id)
            assert [i.title for i in items] == ["Gettysburg", "The Alamo", "Plymouth"]
            first = items[0]
            assert first.kind == "podcast" and first.is_remote and not first.is_stream
            assert first.path == "https://cdn.example.com/e2.mp3"
            assert first.start_ms == 125_000 and items[1].start_ms == 0
            assert first.artist == "History Hour"

    def test_heard_past_95_percent(self, db, data_dir):
        feed_id = subscribe()
        with db.session_scope() as s:
            ep = podcasts.episodes(s, feed_id)[0]
            podcasts.save_position(s, ep.id, 600_000, 3_723_000)
            assert ep.position_ms == 600_000 and not ep.played
            podcasts.save_position(s, ep.id, 5_000, 3_723_000)
            assert ep.position_ms == 0                                # too near the start
            podcasts.save_position(s, ep.id, 3_600_000, 3_723_000)
            assert ep.played and ep.position_ms == 0

    def test_tuner_plays_and_keeps_the_place(self, ctx, data_dir, monkeypatch):
        monkeypatch.setattr(PlayerController, "auto_measure", False)
        feed_id = subscribe()
        with db_session.session_scope() as s:
            ep = podcasts.episodes(s, feed_id)[0]
            # a downloaded copy plays instead of the stream
            ep.local_path = str(silent_wav(data_dir / "Podcast" / "History Hour" / "e2.wav"))
            ep_id = ep.id
        try:
            assert ctx.tuner.tune_podcast(feed_id)
            assert ctx.tuner.tuned == ("podcast", feed_id)
            item = ctx.player.current
            assert item.kind == "podcast" and item.path.endswith("e2.wav") and not item.is_remote
            assert ctx.player.source.startswith("podcast:")
            ctx.tuner._ep_progress[1] = 200_000
            ctx.tuner._ep_progress[2] = 3_723_000
            ctx.tuner.shutdown()
            with db_session.session_scope() as s:
                assert s.get(PodcastEpisode, ep_id).position_ms == 200_000
                assert s.get(PodcastFeed, feed_id).current_episode_id == ep_id
        finally:
            ctx.player.clear_queue()

    def test_nothing_to_play(self, ctx, data_dir):
        with db_session.session_scope() as s:
            feed_id = podcasts.add_feed(s, FEED_URL, "Empty").id
        said = []
        ctx.tuner.notified.connect(said.append)
        assert not ctx.tuner.tune_podcast(feed_id)
        assert said and "Check for new episodes" in said[0]


class TestSpeed:
    def test_steps(self):
        assert podcasts.SPEEDS == (1.0, 1.25, 1.5)
        assert [podcasts.next_speed(x) for x in podcasts.SPEEDS] == [1.25, 1.5, 1.0]
        assert podcasts.speed_label(1.25) == "1.25×" and podcasts.speed_label(1.0) == "1×"

    def test_saved(self, db):
        with db.session_scope() as s:
            assert podcasts.speed(s) == 1.0
            podcasts.set_speed(s, 1.5)
            assert podcasts.speed(s) == 1.5
            podcasts.set_speed(s, 3.0)                      # not a step: back to 1x
            assert podcasts.speed(s) == 1.0

    def test_only_podcasts_are_sped_up(self, qapp):
        p = PlayerController()
        changed = []
        p.podcastRateChanged.connect(changed.append)
        deck = p._decks[0]
        deck.item = QueueItem(0, "Ep", "Show", "", "https://x/e.mp3", kind="podcast")
        p.set_podcast_rate(1.5)
        assert changed == [1.5] and deck.player.playbackRate() == pytest.approx(1.5)
        deck.item = QueueItem(0, "Song", "Band", "", "/music/song.mp3")
        p._apply_rate(deck)
        assert deck.player.playbackRate() == pytest.approx(1.0)
        p.set_podcast_rate(1.5)                             # no change, no signal
        assert changed == [1.5]

    def test_player_bar_button(self, ctx):
        from musicmgr.ui.widgets.player_bar import PlayerBar

        bar = PlayerBar(ctx.player, ctx)
        item = QueueItem(0, "Ep", "Show", "", "https://x/e.mp3", kind="podcast")
        ctx.player._queue, ctx.player._order, ctx.player._cursor = [item], [0], 0
        bar.on_track_changed(item)
        assert not bar.speed_btn.isHidden() and bar.speed_btn.text() == "1×"
        bar._cycle_speed()
        assert ctx.player.podcast_rate == 1.25 and bar.speed_btn.text() == "1.25×"
        with db_session.session_scope() as s:
            assert podcasts.speed(s) == 1.25
        song = QueueItem(0, "Song", "Band", "", "/music/song.mp3")
        ctx.player._queue = [song]
        bar.on_track_changed(song)
        assert bar.speed_btn.isHidden()
        ctx.player._queue, ctx.player._order, ctx.player._cursor = [], [], -1


# -- downloads ------------------------------------------------------------------


class TestDownloads:
    def test_root_sits_beside_the_radio_folder(self, db, data_dir):
        from musicmgr.services import otr

        with db.session_scope() as s:
            assert podcasts.get_root(s) == str(data_dir / "home" / "Podcast")
            otr.set_root(s, str(data_dir / "D" / "Radio"))
            assert podcasts.get_root(s) == str(data_dir / "D" / "Podcast")
            podcasts.set_root(s, str(data_dir / "Elsewhere"))
            assert podcasts.get_root(s) == str(data_dir / "Elsewhere")

    def test_download_and_link(self, db, data_dir):
        feed_id = subscribe()
        root = data_dir / "Podcast"
        with db.session_scope() as s:
            podcasts.set_root(s, str(root))
            feed = s.get(PodcastFeed, feed_id)
            ep = podcasts.episodes(s, feed_id)[0]
            dest = podcasts.download_path(str(root), feed, ep)
            url = ep.audio_url
        assert dest == root / "History Hour" / "2026-11-10 Gettysburg.mp3"
        http = FakeHttp({url: Resp(200, b"ID3" + b"\x00" * 5000, {"Content-Length": "5003"})})
        seen = []
        assert podcasts.download(url, dest, http=http, progress=lambda d, t: seen.append((d, t)))
        assert dest.read_bytes()[:3] == b"ID3" and seen[-1] == (5003, 5003)
        assert not dest.with_name(dest.name + ".part").exists()
        # the copy is picked up (as when it arrives on the USB drive) and played
        with db.session_scope() as s:
            assert podcasts.link_downloads(s) == 1
            ep = podcasts.episodes(s, feed_id)[0]
            assert ep.local_path == str(dest)
            assert podcasts.playable_path(ep) == str(dest)
            podcasts.delete_download(s, ep)
            assert ep.local_path is None and not dest.exists()
            assert not dest.parent.exists()                 # the emptied folder went too
            assert podcasts.playable_path(ep) == url

    def test_cancelled_download_leaves_nothing(self, tmp_path):
        dest = tmp_path / "x.mp3"
        http = FakeHttp({"u": Resp(200, b"x" * 1000)})
        assert not podcasts.download("u", dest, http=http, cancelled=lambda: True)
        assert not dest.exists() and not dest.with_name("x.mp3.part").exists()

    def test_safe_names(self):
        assert podcasts.safe_name('What? "Now": a/b\\c*') == "What Now a b c"
        assert podcasts.safe_name("...") == "Podcast"

    def test_podcast_folder_stays_out_of_the_music_scan(self, db, data_dir):
        with db.session_scope() as s:
            podcasts.set_root(s, str(data_dir / "Pods"))
            roots = radio_folder.roots(s)
        assert radio_folder.is_radio_path(str(data_dir / "Pods" / "Show" / "e.mp3"), roots)
        assert radio_folder.is_radio_path("D:\\Podcast\\Show\\e.mp3", roots)
        assert not radio_folder.is_radio_path("D:\\Music\\Podcast Songs\\a.mp3", roots)


# -- the dial, the page and Settings --------------------------------------------


class TestDial:
    def test_band_sits_above_sxm(self):
        keys = radio_dial.BAND_KEYS
        assert keys.index("pod") == keys.index("sxm") - 1
        assert radio_dial.BY_KEY["pod"].label == "PODCAST"

    def test_hidden_by_setting(self, db):
        with db.session_scope() as s:
            assert podcasts.shown(s) and podcasts.BAND not in stations.hidden_bands(s)
            podcasts.set_shown(s, False)
            assert stations.hidden_bands(s) == {podcasts.BAND}
            stations.set_sxm_shown(s, False)
            assert stations.hidden_bands(s) == {podcasts.BAND, "sxm"}

    def test_eight_bands_fit(self, qapp):
        w = radio_dial.RadioSet()
        w.resize(760, 420)                  # the set's smallest size
        w._relayout()
        strips = w._geom["strips"]
        assert len(strips) == 8
        assert all(r.height() > 20 for r in strips.values())
        keys = w._geom["keys"]
        assert all(r.width() > 40 for r in keys.values())

    def test_podcasts_on_the_band(self, db, data_dir):
        from musicmgr.ui.views.tuner import dial_entries, dial_spot

        feed_id = subscribe()
        with db.session_scope() as s:
            entries, _ = dial_entries(s)
            assert entries["pod"] == [("podcast", feed_id, "HISTORY HOUR")] or \
                [k for k, _, _ in entries["pod"]] == ["podcast"]
            band, pos, _label = dial_spot(s, "podcast", feed_id)
            assert band == "pod" and pos == 0.5


class TestRadioPage:
    def _view(self, ctx):
        from musicmgr.ui.views.tuner import TunerView

        view = TunerView(ctx)
        view.refresh()
        return view

    def test_empty_band_offers_find(self, ctx, data_dir):
        view = self._view(ctx)
        view.set_band("pod")
        assert view.radio.band == "pod"
        assert not view.btn_pod_find.isHidden()
        assert view.find_btn.text() == "Find podcasts…"
        assert "No podcasts yet" in view.status.text()

    def test_pointing_at_a_podcast(self, ctx, data_dir, monkeypatch):
        feed_id = subscribe()
        monkeypatch.setattr(type(self._view(ctx)), "check_podcasts", lambda *a, **k: None)
        view = self._view(ctx)
        view.set_band("pod")
        view.radio.set_tuned("pod", 0, animate=False)
        view._update_info()
        assert view._pointed() == ("podcast", feed_id, False)
        assert "History Hour" in view.status.text() and "Gettysburg" in view.status.text()
        for b in (view.btn_listen, view.btn_episodes, view.btn_pod_check, view.btn_remove):
            assert not b.isHidden()
        assert view.btn_band.isHidden()            # podcasts stay on their band
        view.remove_podcast(feed_id, ask=False)
        with db_session.session_scope() as s:
            assert podcasts.feeds(s) == []

    def test_setting_hides_the_band(self, ctx, data_dir):
        view = self._view(ctx)
        assert view.radio.is_shown("pod")
        with db_session.session_scope() as s:
            podcasts.set_shown(s, False)
        view.refresh()
        assert not view.radio.is_shown("pod")

    def test_appearance_checkbox(self, ctx):
        from musicmgr.ui.views.settings import SettingsView

        view = SettingsView(ctx)
        assert view.podcast_band_cb.isChecked()
        view.podcast_band_cb.setChecked(False)
        with db_session.session_scope() as s:
            assert not podcasts.shown(s)
        assert "PODCAST band off" in view._look_summary()
        assert ls.PODCASTS in view.usb_data_cbs


# -- USB sync -----------------------------------------------------------------------


@pytest.fixture
def drive(tmp_path, db, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    music = tmp_path / "PC" / "Music"
    music.mkdir(parents=True)
    root = tmp_path / "USB"
    (root / "Music").mkdir(parents=True)
    d = us.write_marker(root, "Test USB")
    with db_session.session_scope() as s:
        s.add(WatchedFolder(path=str(music)))
    return d


def pod_pairs(s, drive):
    return [p for p in us.ensure_default_pairs(s, drive) if p.kind == us.PODCAST_KIND]


class TestFolderPair:
    def test_downloads_pair_with_the_drive(self, tmp_path, drive):
        folder = tmp_path / "PC" / "Podcast"
        silent_wav(folder / "History Hour" / "e.wav")
        with db_session.session_scope() as s:
            podcasts.set_root(s, str(folder))
            (pair,) = pod_pairs(s, drive)
            assert pair.usb_rel_path == "Podcast" and pair.enabled
        assert (drive.root / "Podcast").is_dir()

    def test_nothing_downloaded_nothing_paired(self, tmp_path, drive):
        with db_session.session_scope() as s:
            podcasts.set_root(s, str(tmp_path / "PC" / "Podcast"))
            assert pod_pairs(s, drive) == []

    def test_a_new_pc_gets_the_folder_from_the_drive(self, tmp_path, drive):
        silent_wav(drive.root / "Podcast" / "History Hour" / "e.wav")
        with db_session.session_scope() as s:
            (pair,) = pod_pairs(s, drive)
            assert Path(pair.local_path) == tmp_path / "home" / "Podcast"
            assert podcasts.get_root(s) == str(tmp_path / "home" / "Podcast")

    def test_unticking_podcasts_switches_the_pair_off(self, tmp_path, drive):
        folder = tmp_path / "PC" / "Podcast"
        silent_wav(folder / "Show" / "e.wav")
        with db_session.session_scope() as s:
            podcasts.set_root(s, str(folder))
            assert pod_pairs(s, drive)[0].enabled
            ls.set_category_enabled(s, ls.PODCASTS, False)
            assert not pod_pairs(s, drive)[0].enabled
            ls.set_category_enabled(s, ls.PODCASTS, True)
            assert pod_pairs(s, drive)[0].enabled

    def test_unticked_from_the_start_never_pairs(self, tmp_path, drive):
        folder = tmp_path / "PC" / "Podcast"
        silent_wav(folder / "Show" / "e.wav")
        with db_session.session_scope() as s:
            podcasts.set_root(s, str(folder))
            ls.set_category_enabled(s, ls.PODCASTS, False)
            assert pod_pairs(s, drive) == []


class TestSubscriptionsTravel:
    def test_subscriptions_and_progress(self, tmp_path, monkeypatch, qapp):
        from tests.test_library_state import PC

        usb_root = tmp_path / "USB"
        (usb_root / "Music").mkdir(parents=True)
        drive = us.write_marker(usb_root)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        pc1 = PC("pc1", tmp_path / "pc1", drive, monkeypatch)
        pc2 = PC("pc2", tmp_path / "pc2", drive, monkeypatch)

        pc1.use()
        feed_id = subscribe()
        with db_session.session_scope() as s:
            eps = podcasts.episodes(s, feed_id)
            podcasts.mark_played(s, eps[0])
            eps[1].position_ms = 754_000
            s.get(PodcastFeed, feed_id).current_episode_id = eps[1].id
        pc1.sync()

        notes = pc2.sync()
        assert notes.podcasts_in and "podcasts updated" in notes.summary()
        pc2.use()
        with db_session.session_scope() as s:
            (feed,) = podcasts.feeds(s)
            assert feed.feed_url == FEED_URL and feed.title == "History Hour"
            assert feed.last_checked is None                     # read on the next visit
        # the Radio page reads the feed; the next sync brings the progress
        podcasts.check(db_session.new_session, http=feed_http())
        pc2.sync()
        pc2.use()
        with db_session.session_scope() as s:
            (feed,) = podcasts.feeds(s)
            eps = podcasts.episodes(s, feed.id)
            assert [e.played for e in eps] == [True, False, False]
            assert eps[1].position_ms == 754_000
            assert podcasts.current_episode(s, feed).guid == "e1"
            # PC2 unsubscribes
            podcasts.remove_feed(s, feed.id)
        pc2.sync()
        pc1.sync()
        pc1.use()
        with db_session.session_scope() as s:
            assert podcasts.feeds(s) == []

    def test_last_tuned_podcast_travels(self, db, data_dir):
        from musicmgr.services import prefs_sync

        feed_id = subscribe()
        with db.session_scope() as s:
            portable = prefs_sync._portable_last(s, f"podcast|{feed_id}")
            assert portable == f"podcast|{FEED_URL}"
            assert prefs_sync._local_last(s, portable) == f"podcast|{feed_id}"
