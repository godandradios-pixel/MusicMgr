"""PlayerController's two-deck playback (2026-10-01): preloading, gapless
hand-off, crossfade, and per-track volume levelling. See services/player.py's
module docstring.

The timing tests really play short WAV files through QMediaPlayer. With no
sound card (CI, the offscreen test platform) Qt's FFmpeg backend still runs
the playback clock, so track ends and hand-offs happen in real time.
"""

from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

import pytest
from PySide6.QtTest import QTest

from musicmgr.services import library as lib
from musicmgr.services import playback_prefs
from musicmgr.services.player import FALLBACK_GAIN_DB, PlayerController, QueueItem


def make_wav(path: Path, seconds: float, freq: float = 440.0, rate: int = 22050) -> str:
    frames = bytearray()
    for n in range(int(rate * seconds)):
        frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * n / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return str(path)


def item(path, n, *, release_id=None, track_gain=None, album_gain=None, ms=1000):
    return QueueItem(
        track_id=n, title=f"t{n}", artist="A", album="LP", path=path, duration_ms=ms,
        release_id=release_id, rg_track_gain=track_gain, rg_album_gain=album_gain,
        hydrated=True,
    )


@pytest.fixture
def player(db, qapp, monkeypatch):
    monkeypatch.setattr(PlayerController, "auto_measure", False)
    p = PlayerController()
    # play events for these fake track ids would fail the FK - not under test
    monkeypatch.setattr(p, "_flush_play_event", lambda **kw: None)
    yield p
    p.clear_queue()


def set_prefs(db, player, **kw):
    with db.session_scope() as s:
        playback_prefs.save(s, playback_prefs.PlaybackPrefs(**kw))
    player.reload_prefs()


def wait_until(cond, timeout_ms=6000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        if cond():
            return True
        QTest.qWait(20)
    return cond()


class TestPreload:
    def test_next_track_waits_on_the_other_deck(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off")
        paths = [make_wav(tmp_path / f"{i}.wav", 2) for i in range(3)]
        items = [item(p, i) for i, p in enumerate(paths)]
        player.play_tracks(items)
        assert player._active.item is items[0]
        assert player._standby.item is items[1]
        assert player._standby_ready()

    def test_preload_follows_queue_changes_and_repeat(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off")
        a, b, c = (make_wav(tmp_path / f"{i}.wav", 2) for i in range(3))
        items = [item(a, 0), item(b, 1)]
        player.play_tracks(items)
        extra = item(c, 2)
        player.enqueue([extra], play_next=True)
        assert player._standby.item is extra
        player.cycle_repeat()  # all
        player.cycle_repeat()  # one -> the same track again
        assert player._standby.item is items[0]

    def test_last_track_with_repeat_off_preloads_nothing(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off")
        player.play_tracks([item(make_wav(tmp_path / "a.wav", 2), 0)])
        assert player._standby.item is None

    def test_next_uses_the_preloaded_deck(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off")
        items = [item(make_wav(tmp_path / f"{i}.wav", 2), i) for i in range(2)]
        player.play_tracks(items)
        standby = player._standby
        player.next(user_initiated=True)
        assert player._active is standby
        assert player.current is items[1]


class TestHandOff:
    def test_gapless_hand_off_plays_the_queue_through(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off", crossfade_s=0)
        items = [item(make_wav(tmp_path / f"{i}.wav", 0.8), i, ms=800) for i in range(3)]
        changes = []
        player.trackChanged.connect(lambda it: changes.append((time.monotonic(), it)))
        start = time.monotonic()
        player.play_tracks(items)
        assert wait_until(lambda: changes and changes[-1][1] is None, 8000)
        played = [it for _, it in changes if it is not None]
        assert played == items
        # three 0.8 s tracks back to back: no time lost between them
        assert changes[-1][0] - start < 3.4

    def test_crossfade_overlaps_the_two_tracks(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off", crossfade_s=1)
        items = [item(make_wav(tmp_path / f"{i}.wav", 2.0), i, ms=2000) for i in range(2)]
        player.play_tracks(items)
        first = player._active
        assert wait_until(lambda: player._fade_from is not None, 4000)
        assert player.current is items[1]
        assert player._fade_from is first and first.is_playing() and player._active.is_playing()
        assert wait_until(lambda: player._fade_from is None, 3000)
        assert not first.is_playing()
        assert player._active.fade == 1.0

    def test_seek_during_a_crossfade_drops_the_old_track(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off", crossfade_s=1)
        items = [item(make_wav(tmp_path / f"{i}.wav", 2.0), i, ms=2000) for i in range(2)]
        player.play_tracks(items)
        assert wait_until(lambda: player._fade_from is not None, 4000)
        old = player._fade_from
        player.seek(0)
        assert player._fade_from is None and not old.is_playing()


class TestCrossfadeRules:
    def setup_items(self, player, tmp_path, release_ids, ms=200_000):
        items = [
            item(make_wav(tmp_path / f"{i}.wav", 0.3), i, release_id=r, ms=ms)
            for i, r in enumerate(release_ids)
        ]
        player._queue = items
        player._order = list(range(len(items)))
        player._cursor = 0
        return items

    def test_off_means_zero(self, player, db, tmp_path):
        set_prefs(db, player, crossfade_s=0)
        self.setup_items(player, tmp_path, [1, 2])
        assert player._crossfade_ms_for_next() == 0

    def test_same_album_hands_off_gaplessly_unless_asked(self, player, db, tmp_path):
        set_prefs(db, player, crossfade_s=4)
        self.setup_items(player, tmp_path, [7, 7])
        assert player._crossfade_ms_for_next() == 0
        set_prefs(db, player, crossfade_s=4, crossfade_same_album=True)
        assert player._crossfade_ms_for_next() == 4000

    def test_different_albums_fade_and_short_tracks_cap_it(self, player, db, tmp_path):
        set_prefs(db, player, crossfade_s=6)
        self.setup_items(player, tmp_path, [1, 2])
        assert player._crossfade_ms_for_next() == 6000
        self.setup_items(player, tmp_path, [1, 2], ms=5000)
        assert player._crossfade_ms_for_next() == 2500

    def test_repeat_one_never_fades_into_itself(self, player, db, tmp_path):
        set_prefs(db, player, crossfade_s=4)
        self.setup_items(player, tmp_path, [1, 2])
        player._repeat = "one"
        assert player._crossfade_ms_for_next() == 0


class TestLevelling:
    def gain_db(self, player, cursor=0):
        return 20 * math.log10(player._gain_factor(player._item_at(cursor), cursor))

    def load(self, player, tmp_path, specs):
        items = [
            item(make_wav(tmp_path / f"{i}.wav", 0.3), i, release_id=r, track_gain=t,
                 album_gain=a)
            for i, (r, t, a) in enumerate(specs)
        ]
        player._queue = items
        player._order = list(range(len(items)))
        player._cursor = 0
        return items

    def test_off_is_unity(self, player, db, tmp_path):
        set_prefs(db, player, levelling="off")
        self.load(player, tmp_path, [(1, -8.0, -6.0)])
        assert player._gain_factor(player._item_at(0), 0) == 1.0

    def test_track_album_and_auto(self, player, db, tmp_path):
        self.load(player, tmp_path, [(1, -8.0, -6.0), (1, -4.0, -6.0), (2, -2.0, -3.0)])
        set_prefs(db, player, levelling="track")
        assert self.gain_db(player) == pytest.approx(-8.0)
        set_prefs(db, player, levelling="album")
        assert self.gain_db(player) == pytest.approx(-6.0)
        set_prefs(db, player, levelling="auto")
        # tracks 0 and 1 are one album in order -> album gain
        assert self.gain_db(player, 0) == pytest.approx(-6.0)
        # track 2's neighbours are from another release -> track gain
        assert self.gain_db(player, 2) == pytest.approx(-2.0)
        player._shuffle = True
        assert self.gain_db(player, 0) == pytest.approx(-8.0)

    def test_preamp_and_fallback(self, player, db, tmp_path):
        self.load(player, tmp_path, [(1, None, None)])
        set_prefs(db, player, levelling="track", preamp_db=3)
        assert self.gain_db(player) == pytest.approx(FALLBACK_GAIN_DB + 3)

    def test_volume_slider_is_separate_from_gain(self, player, db, tmp_path):
        set_prefs(db, player, levelling="track")
        player.play_tracks([item(make_wav(tmp_path / "a.wav", 2), 0, track_gain=-6.0)])
        player.set_volume(0.5)
        assert player.volume == 0.5
        assert player._active.audio.volume() == pytest.approx(0.5 * 10 ** (-6 / 20), abs=1e-3)
        assert player.gain_db_for_current() == pytest.approx(-6.0)

    def test_never_louder_than_full_scale(self, player, db, tmp_path):
        set_prefs(db, player, levelling="track", preamp_db=6)
        player.play_tracks([item(make_wav(tmp_path / "a.wav", 2), 0, track_gain=10.0)])
        player.set_volume(1.0)
        assert player._active.audio.volume() == pytest.approx(1.0)

    def test_measured_level_arriving_mid_track_eases_in(self, player, db, tmp_path):
        from musicmgr.db.models import MediaFile, Track
        from musicmgr.services.matching import normalize

        set_prefs(db, player, levelling="track")
        path = make_wav(tmp_path / "a.wav", 3)
        with db.session_scope() as s:
            rel = lib.get_or_create_release(s, "LP", "A")
            t = Track(title="a", title_key=normalize("a"), release_id=rel.id)
            s.add(t)
            s.flush()
            mf = MediaFile(track_id=t.id, path=path)
            s.add(mf)
            s.flush()
            mf_id = mf.id
        it = item(path, 0)
        it.media_file_id = mf_id
        player.play_tracks([it])
        assert player.gain_db_for_current() == pytest.approx(FALLBACK_GAIN_DB)
        with db.session_scope() as s:
            s.get(MediaFile, mf_id).rg_track_gain = -12.0
        player._on_gain_measured(mf_id)
        assert it.rg_track_gain == -12.0
        assert player.gain_db_for_current() == pytest.approx(-12.0)
        # eases rather than jumps
        assert player._active.gain > 10 ** (-12 / 20)
        assert wait_until(lambda: abs(player._active.gain - 10 ** (-12 / 20)) < 1e-4, 4000)

    def test_items_built_without_ids_are_looked_up_by_path(self, player, db, tmp_path):
        from musicmgr.db.models import MediaFile, Track
        from musicmgr.services.matching import normalize

        path = make_wav(tmp_path / "a.wav", 0.3)
        with db.session_scope() as s:
            rel = lib.get_or_create_release(s, "LP", "A")
            t = Track(title="a", title_key=normalize("a"), release_id=rel.id)
            s.add(t)
            s.flush()
            s.add(MediaFile(track_id=t.id, path=path, rg_track_gain=-3.0))
        bare = QueueItem(track_id=1, title="a", artist="", album="", path=path)
        player._hydrate(bare)
        assert bare.rg_track_gain == -3.0 and bare.media_file_id is not None
