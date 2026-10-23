"""services/subtitles.py and the video player's caption bar (2026-10-23).

The sample .srt text below is copied from the styles found in D:\\Videos:
YouTube auto-generated lowercase lines with "[Music]", "♪ ... ♪" lyrics
over two lines with "(funky rock music)" cues, and cues sharing their start
time that belong on screen together (Amy Grant - Baby, Baby).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musicmgr.services import subtitles
from musicmgr.services.subtitles import Captions, find_subtitles, load_captions, parse_srt

AUTO = """1
00:00:01,360 --> 00:00:12,230
how much

2
00:00:12,240 --> 00:00:18,470
[Music]

3
00:00:18,480 --> 00:00:21,840
this is where it ends
"""

LYRICS = """

1
00:00:00,747 --> 00:00:01,580
(people cheering)

6
00:00:20,798 --> 00:00:24,862
♪ It's been too long
since I felt this way ♪
"""

STACKED = """6
00:00:31,781 --> 00:00:35,535
♪ MY TENDER LOVE

7
00:00:31,781 --> 00:00:35,535
WILL FLOW FROM ♪

8
00:00:35,618 --> 00:00:39,497
♪ THE BLUEST SKY TO THE DEEPEST
OCEAN ♪
"""


class TestParse:
    def test_auto_generated(self):
        cues = parse_srt(AUTO)
        assert [(c.start_ms, c.end_ms, c.text) for c in cues] == [
            (1360, 12230, "how much"),
            (12240, 18470, "[Music]"),
            (18480, 21840, "this is where it ends"),
        ]

    def test_leading_blank_lines_and_two_line_cues(self):
        cues = parse_srt(LYRICS)
        assert cues[1].text == "♪ It's been too long\nsince I felt this way ♪"

    def test_crlf_bom_and_tags(self):
        text = "\ufeff1\r\n00:00:01,000 --> 00:00:02,500\r\n<i>Hello</i> {\\an8}<font color=\"#fff\">there</font>\r\n"
        (cue,) = parse_srt(text)
        assert (cue.start_ms, cue.end_ms, cue.text) == (1000, 2500, "Hello there")

    def test_dot_milliseconds_and_short_fractions(self):
        (cue,) = parse_srt("1\n00:01:02.5 --> 00:01:03.25\nx\n")
        assert (cue.start_ms, cue.end_ms) == (62_500, 63_250)

    def test_broken_blocks_are_skipped(self):
        cues = parse_srt("1\nnot a time\nhello\n\n2\n00:00:01,000 --> 00:00:02,000\nok\n\n"
                         "3\n00:00:05,000 --> 00:00:04,000\nbackwards\n\n4\n00:00:06,000 --> 00:00:07,000\n")
        assert [c.text for c in cues] == ["ok"]


class TestTextAt:
    def test_between_cues_is_blank(self):
        caps = Captions(parse_srt(AUTO))
        assert caps.text_at(0) == ""
        assert caps.text_at(1360) == "how much"
        assert caps.text_at(12_235) == ""
        assert caps.text_at(20_000) == "this is where it ends"
        assert caps.text_at(99_000) == ""

    def test_cues_sharing_a_time_are_stacked(self):
        caps = Captions(parse_srt(STACKED))
        assert caps.text_at(33_000) == "♪ MY TENDER LOVE\nWILL FLOW FROM ♪"

    def test_a_long_cue_is_still_found_after_short_ones_start(self):
        caps = Captions(parse_srt(
            "1\n00:00:00,000 --> 00:01:00,000\nlong\n\n2\n00:00:10,000 --> 00:00:11,000\nshort\n"))
        assert caps.text_at(30_000) == "long"
        assert caps.text_at(10_500) == "long\nshort"

    def test_a_duplicated_cue_shows_once(self):
        caps = Captions(parse_srt(
            "1\n00:00:01,000 --> 00:00:03,000\nla\n\n2\n00:00:01,000 --> 00:00:03,000\nla\n"))
        assert caps.text_at(2000) == "la"


class TestFind:
    def test_same_name(self, tmp_path):
        video = tmp_path / "Bailey Zimmerman - Where It Ends.mp4"
        video.write_bytes(b"")
        srt = tmp_path / "Bailey Zimmerman - Where It Ends.srt"
        srt.write_text(AUTO)
        assert find_subtitles(video) == srt

    def test_upper_case_extension(self, tmp_path):
        (tmp_path / "Song.SRT").write_text(AUTO)
        assert find_subtitles(tmp_path / "Song.mp4").name == "Song.SRT"

    def test_language_tagged_prefers_english(self, tmp_path):
        (tmp_path / "Song.de.srt").write_text(AUTO)
        (tmp_path / "Song.en.srt").write_text(AUTO)
        assert find_subtitles(tmp_path / "Song.mp4").name == "Song.en.srt"

    def test_a_dotted_title_matches_its_own_file_only(self, tmp_path):
        # "Miley Cyrus - Party In The U.S.A..srt" is in D:\Videos
        (tmp_path / "Miley Cyrus - Party In The U.S.A..srt").write_text(AUTO)
        (tmp_path / "Other.srt").write_text(AUTO)
        found = find_subtitles(tmp_path / "Miley Cyrus - Party In The U.S.A..mp4")
        assert found.name == "Miley Cyrus - Party In The U.S.A..srt"

    def test_none(self, tmp_path):
        (tmp_path / "Other.srt").write_text(AUTO)
        assert find_subtitles(tmp_path / "Song.mp4") is None
        assert load_captions(tmp_path / "Song.mp4") is None

    def test_windows_1252_file(self, tmp_path):
        (tmp_path / "Song.srt").write_bytes(
            "1\n00:00:01,000 --> 00:00:02,000\ncaf\xe9\n".encode("cp1252"))
        caps = load_captions(tmp_path / "Song.mp4")
        assert caps.text_at(1500) == "café"

    def test_an_srt_with_no_cues_means_no_captions(self, tmp_path):
        (tmp_path / "Song.srt").write_text("nothing here")
        assert load_captions(tmp_path / "Song.mp4") is None


class TestCaptionBar:
    """VideoPlayerPanel - the controller's real playback is replaced, as in
    test_videos_view.py; only what the panel does with captions is checked."""

    @pytest.fixture
    def panel(self, ctx, monkeypatch):
        from musicmgr.ui.widgets.video_panel import VideoPlayerPanel

        panel = VideoPlayerPanel(ctx)
        monkeypatch.setattr(panel.controller, "play_path", lambda *a: None)
        monkeypatch.setattr(panel.controller, "duration", lambda: 30_000)
        monkeypatch.setattr(panel.controller, "position", lambda: 0)
        return panel

    def _video(self, tmp_path, with_srt=True):
        video = tmp_path / "Song.mp4"
        video.write_bytes(b"")
        if with_srt:
            (tmp_path / "Song.srt").write_text(AUTO)
        return str(video)

    def test_a_video_with_subtitles_shows_the_bar_and_cc(self, panel, tmp_path):
        panel.play(1, self._video(tmp_path), "Song", "Artist")

        assert panel.cc_btn.isHidden() is False
        assert panel.caption.isHidden() is False
        panel.controller.positionChanged.emit(19_000)
        assert panel.caption.text() == "this is where it ends"
        panel.controller.positionChanged.emit(12_235)
        assert panel.caption.text() == ""

    def test_a_video_without_subtitles_has_neither(self, panel, tmp_path):
        panel.play(1, self._video(tmp_path, with_srt=False), "Song", "Artist")

        assert panel.cc_btn.isHidden() is True
        assert panel.caption.isHidden() is True

    def test_cc_off_hides_the_bar_and_is_remembered(self, ctx, panel, tmp_path):
        from musicmgr.services import updater
        from musicmgr.ui.widgets.video_panel import CAPTIONS_PREF, VideoPlayerPanel

        panel.play(1, self._video(tmp_path), "Song", "Artist")
        panel.cc_btn.setChecked(False)

        assert panel.caption.isHidden() is True
        with ctx.session() as db:
            assert updater.get_pref(db, CAPTIONS_PREF) == "0"
        assert VideoPlayerPanel(ctx).cc_btn.isChecked() is False

    def test_cc_back_on_shows_the_current_line_at_once(self, panel, tmp_path, monkeypatch):
        panel.play(1, self._video(tmp_path), "Song", "Artist")
        panel.cc_btn.setChecked(False)
        monkeypatch.setattr(panel.controller, "position", lambda: 19_000)

        panel.cc_btn.setChecked(True)

        assert panel.caption.isHidden() is False
        assert panel.caption.text() == "this is where it ends"

    def test_the_next_video_without_subtitles_clears_the_bar(self, panel, tmp_path):
        panel.play(1, self._video(tmp_path), "Song", "Artist")
        panel.controller.positionChanged.emit(19_000)
        other = tmp_path / "Other.mp4"
        other.write_bytes(b"")

        panel.play(2, str(other), "Other", "Artist")

        assert panel.caption.isHidden() is True
        assert panel.caption.text() == ""
