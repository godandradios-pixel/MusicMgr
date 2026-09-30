"""Tests for the tap-to-sync lyric editor (2026-09-28):
services/lyrics_editor.py (pure), lyrics_downloader.fetch_lyrics_text, and
ui/widgets/lyrics_editor.py's dialog plus its Now Playing entry points.

Headless playback never actually advances, so stamp tests patch the shared
PlayerController's `position()` to stand in for "where the song is"."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

from musicmgr.services import lyrics_downloader as lyrics_dl
from musicmgr.services import lyrics_editor as le
from musicmgr.services.lyrics import load_lyrics
from musicmgr.services.player import QueueItem
from musicmgr.ui.views.nowplaying import NowPlayingView
from musicmgr.ui.widgets.lyrics_editor import LyricsEditorDialog


# -- services/lyrics_editor.py -------------------------------------------


def test_format_timestamp_rounds_to_centiseconds():
    assert le.format_timestamp(0) == "00:00.00"
    assert le.format_timestamp(12_345) == "00:12.35"
    assert le.format_timestamp(61_004) == "01:01.00"
    assert le.format_timestamp(-50) == "00:00.00"
    assert le.format_timestamp(125 * 60_000) == "125:00.00"


def test_plain_text_drops_blank_lines_meta_and_stray_tags():
    lines = le.lines_from_plain_text("[ar:Band]\nOne\n\n[00:05.00]Two <00:06.00>too\n  \nThree\n")
    assert [l.text for l in lines] == ["One", "Two too", "Three"]
    assert all(l.time_ms is None for l in lines)


def test_plain_text_keeps_order_for_untimed_lyrics():
    lines = le.lines_from_plain_text("Verse one\n\n[Chorus]\nHook line\n")
    assert [l.text for l in lines] == ["Verse one", "[Chorus]", "Hook line"]


def test_stamp_advances_and_applies_lead():
    lines = [le.EditLine("a"), le.EditLine("b")]
    assert le.stamp(lines, 0, 10_000, lead_ms=150) == 1
    assert lines[0].time_ms == 9_850
    # last line: stays put so a second tap re-stamps it
    assert le.stamp(lines, 1, 20_000) == 1
    assert le.stamp(lines, 1, 21_000) == 1
    assert lines[1].time_ms == 21_000
    # never negative
    le.stamp(lines, 0, 50, lead_ms=150)
    assert lines[0].time_ms == 0


def test_nudge_and_shift_all_skip_unstamped_and_clamp():
    lines = [le.EditLine("a", 1_000), le.EditLine("b"), le.EditLine("c", 5_000)]
    le.nudge(lines, 0, -2_000)
    assert lines[0].time_ms == 0
    le.nudge(lines, 1, 500)
    assert lines[1].time_ms is None
    le.shift_all(lines, 400, duration_ms=5_200)
    assert [l.time_ms for l in lines] == [400, None, 5_200]


def test_out_of_order_flags_mis_taps():
    lines = [le.EditLine("a", 1_000), le.EditLine("b", 3_000), le.EditLine("c", 2_000),
             le.EditLine("d"), le.EditLine("e", 4_000)]
    assert le.out_of_order(lines) == [2]


def test_active_line_index_handles_unsorted_partial_lists():
    lines = [le.EditLine("a", 1_000), le.EditLine("b"), le.EditLine("c", 5_000)]
    assert le.active_line_index(lines, 500) == -1
    assert le.active_line_index(lines, 4_999) == 0
    assert le.active_line_index(lines, 5_000) == 2


def test_to_lrc_writes_header_and_sorted_stamped_lines_only():
    lines = [le.EditLine("second", 20_000), le.EditLine("first", 10_500),
             le.EditLine("never timed")]
    text = le.to_lrc(lines, title="Song", artist="Band", album="LP", duration_ms=185_400)
    assert text.splitlines() == [
        "[ti:Song]", "[ar:Band]", "[al:LP]", "[length:03:05]", "[by:MusicMgr]", "",
        "[00:10.50]first", "[00:20.00]second",
    ]
    assert le.unstamped_count(lines) == 1


def test_save_round_trips_through_the_lyrics_pane_reader_without_backup(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"")
    old = tmp_path / "song.lrc"
    old.write_text("[00:01.00]old\n", encoding="utf-8")

    path = le.save_lrc(audio, [le.EditLine("hello", 1_230), le.EditLine("", 4_000)], title="T")

    assert path == old
    assert not (tmp_path / "song.lrc.bak").exists()
    assert list(tmp_path.glob("*.bak")) == []
    loaded = load_lyrics(audio)
    assert loaded.synced
    assert [(l.time_ms, l.text) for l in loaded.lines] == [(1_230, "hello"), (4_000, "")]
    # and the editor reads it back with timing intact
    assert [(l.text, l.time_ms) for l in le.load_lines_for_editing(audio)] == [
        ("hello", 1_230), ("", 4_000)
    ]


def test_load_for_editing_none_without_sidecar(tmp_path):
    assert le.load_lines_for_editing(tmp_path / "nothing.mp3") is None


def test_retext_keeps_timing_for_unchanged_and_fixed_lines():
    lines = [le.EditLine("Hello wrold", 1_000), le.EditLine("Second", 2_000),
             le.EditLine("Third", 3_000)]
    new = le.retext(lines, "Hello world\nSecond\nBrand new\nThird\n\n")
    assert [(l.text, l.time_ms) for l in new] == [
        ("Hello world", 1_000), ("Second", 2_000), ("Brand new", None), ("Third", 3_000)
    ]


# -- lyrics_downloader.fetch_lyrics_text ---------------------------------


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, get_payload=None, search_payload=None):
        self.get_payload = get_payload
        self.search_payload = search_payload or []

    def get(self, url, params=None, timeout=None):
        if url == lyrics_dl.LRCLIB_GET:
            return _FakeResponse(200 if self.get_payload else 404, self.get_payload)
        return _FakeResponse(200, self.search_payload)


def test_fetch_lyrics_text_returns_both_texts_without_writing(tmp_path):
    session = _FakeSession({"plainLyrics": "A\nB", "syncedLyrics": "[00:01.00]A"})
    fetch = lyrics_dl.fetch_lyrics_text(title="T", artist="X", session=session)
    assert (fetch.status, fetch.plain, fetch.synced) == ("found", "A\nB", "[00:01.00]A")
    assert list(tmp_path.iterdir()) == []


def test_fetch_lyrics_text_statuses():
    assert lyrics_dl.fetch_lyrics_text(title="T", artist="").status == "no_artist"
    assert lyrics_dl.fetch_lyrics_text(title="T", artist="X", session=_FakeSession()).status == "not_found"
    inst = _FakeSession({"instrumental": True})
    assert lyrics_dl.fetch_lyrics_text(title="T", artist="X", session=inst).status == "instrumental"


# -- the dialog ----------------------------------------------------------


@pytest.fixture
def playing(ctx, tmp_path):
    """Two queued tracks (real empty files), the first one playing."""
    items = []
    for name in ("one", "two"):
        f = tmp_path / f"{name}.mp3"
        f.write_bytes(b"")
        items.append(QueueItem(track_id=0, title=name.title(), artist="Band", album="LP",
                               path=str(f), duration_ms=200_000))
    ctx.player.play_tracks(items)
    assert ctx.player.current.path == items[0].path
    return items


def _dialog(ctx, item, qapp):
    return LyricsEditorDialog(ctx.player, item.path, title=item.title, artist=item.artist,
                              album=item.album, duration_ms=item.duration_ms)


def test_dialog_starts_empty_without_sidecar(ctx, playing, qapp):
    d = _dialog(ctx, playing[0], qapp)
    assert d.lines == []
    assert not d.reload_btn.isEnabled()
    assert "No lyric lines" in d.status.text()


def test_dialog_loads_existing_lrc_and_selects_first_untimed(ctx, playing, qapp):
    Path(playing[0].path).with_suffix(".lrc").write_text(
        "[00:01.00]a\n[00:02.00]b\n", encoding="utf-8")
    d = _dialog(ctx, playing[0], qapp)
    assert [(l.text, l.time_ms) for l in d.lines] == [("a", 1_000), ("b", 2_000)]
    assert d.table.rowCount() == 2
    assert d.table.item(0, 0).text() == "00:01.00"
    assert d.reload_btn.isEnabled()


def test_stamping_walks_down_the_lines_and_save_writes_the_file(ctx, playing, qapp, monkeypatch):
    d = _dialog(ctx, playing[0], qapp)
    d.set_lines(le.lines_from_plain_text("First\nSecond\nThird"))
    d.lead_spin.setValue(100)
    for pos in (5_000, 9_000, 14_000):
        monkeypatch.setattr(ctx.player, "position", lambda p=pos: p)
        d.stamp_btn.click()
    assert [l.time_ms for l in d.lines] == [4_900, 8_900, 13_900]
    assert d.selected_index() == 2
    assert "3 of 3 lines timed" in d.status.text()

    d._nudge_selected(-100)
    assert d.lines[2].time_ms == 13_800

    saved = []
    d.saved.connect(saved.append)
    d._save()
    assert d.result() == QDialog.Accepted
    lrc = Path(playing[0].path).with_suffix(".lrc")
    assert saved == [lrc]
    assert [(l.time_ms, l.text) for l in load_lyrics(playing[0].path).lines] == [
        (4_900, "First"), (8_900, "Second"), (13_800, "Third")]


def test_save_asks_before_dropping_untimed_lines(ctx, playing, qapp, monkeypatch):
    d = _dialog(ctx, playing[0], qapp)
    d.set_lines([le.EditLine("timed", 1_000), le.EditLine("not yet")])
    asked = []
    monkeypatch.setattr(d, "_confirm", lambda title, text: asked.append(text) or False)
    d._save()
    assert asked and "1 line(s) have no time" in asked[0]
    assert not Path(playing[0].path).with_suffix(".lrc").exists()


def test_stamp_does_nothing_when_another_track_is_loaded(ctx, playing, qapp, monkeypatch):
    d = _dialog(ctx, playing[1], qapp)  # editing "two" while "one" plays
    d.set_lines([le.EditLine("x")])
    d._stamp()
    assert d.lines[0].time_ms is None


def test_song_ending_steps_back_to_the_edited_track(ctx, playing, qapp):
    d = _dialog(ctx, playing[0], qapp)
    ctx.player.next(user_initiated=True)  # the queue moves on
    assert ctx.player.current.path == playing[0].path
    assert "Song ended" in d.status.text()
    d.done(QDialog.Rejected)


def test_lrclib_plain_load_and_synced_choice(ctx, playing, qapp, monkeypatch):
    d = _dialog(ctx, playing[0], qapp)
    fetch = lyrics_dl.LyricsTextFetch("found", plain="A\n\nB", synced="[00:03.00]A\n[00:07.00]B")
    monkeypatch.setattr(d, "_choose_lrclib_text", lambda f: "plain")
    d._on_fetched(fetch)
    assert [(l.text, l.time_ms) for l in d.lines] == [("A", None), ("B", None)]

    monkeypatch.setattr(d, "_choose_lrclib_text", lambda f: "synced")
    monkeypatch.setattr(d, "_confirm", lambda *a: True)
    d._on_fetched(fetch)
    assert [(l.text, l.time_ms) for l in d.lines] == [("A", 3_000), ("B", 7_000)]

    d._on_fetched(lyrics_dl.LyricsTextFetch("not_found"))
    assert "No lyrics found" in d.status.text()


def test_line_tools_insert_delete_clear_shift(ctx, playing, qapp):
    d = _dialog(ctx, playing[0], qapp)
    d.set_lines([le.EditLine("a", 1_000), le.EditLine("b", 2_000)])
    d._select(1)
    d._insert_break()
    # the break goes above the selected line and becomes the selection
    assert [l.text for l in d.lines] == ["a", "", "b"]
    assert d.selected_index() == 1
    assert d.table.item(1, 1).text() == "♪"
    d._delete_selected()
    assert [l.text for l in d.lines] == ["a", "b"]
    # selecting the first line puts an intro break at the very top
    d._select(0)
    d._insert_break()
    assert [l.text for l in d.lines] == ["", "a", "b"]
    assert d.selected_index() == 0
    d._delete_selected()
    assert [l.text for l in d.lines] == ["a", "b"]
    d._select(1)
    d._clear_selected()
    assert d.lines[1].time_ms is None
    d._shift_all(500)
    assert d.lines[0].time_ms == 1_500


# -- Now Playing entry points --------------------------------------------


def test_lyrics_pane_edit_buttons_follow_the_loaded_track(ctx, playing, qapp):
    view = NowPlayingView(ctx)
    view.refresh()
    panel = view.lyrics_panel
    assert panel.audio_path == playing[0].path
    assert not panel._sync_myself_btn.isHidden()
    panel.load_for_path(None)
    assert panel._sync_myself_btn.isHidden()


def test_edit_request_opens_editor_and_reloads_pane_after_save(ctx, playing, qapp, monkeypatch):
    view = NowPlayingView(ctx)
    view.refresh()
    assert view.lyrics_panel._stack.currentIndex() == 0  # no lyrics yet

    def fake_exec(dialog):
        dialog.set_lines([le.EditLine("Sung line", 2_000)])
        dialog._save()
        return QDialog.Accepted

    monkeypatch.setattr(LyricsEditorDialog, "exec", fake_exec)
    view.lyrics_panel.editRequested.emit()
    assert view.lyrics_panel._stack.currentIndex() == 1
    assert view.lyrics_panel._list.item(0).text() == "Sung line"
    assert not view.lyrics_panel._edit_btn.isHidden()


def test_external_edit_button_hides_when_there_are_no_lyrics(ctx, playing, qapp):
    view = NowPlayingView(ctx)
    view.refresh()
    panel = view.lyrics_panel
    assert panel._stack.currentIndex() == 0  # empty state
    assert panel.edit_button.isHidden()
    assert not panel._sync_myself_btn.isHidden()
