"""Tests for ui/widgets/player_bar.py's video-mode handling of the pulse
visualizer.

2026-09-15, two follow-ups same day: the visualizer briefly got real
audio-reactive data for video too, then James asked for that back out
entirely ("I really don't need the pulse visualizer [for video]. I want
to look at the video") along with the strip itself hidden during video
playback, its row of height given back to the video rather than sitting
there idle - see PlayerBar.enter_video_mode/_update_height and this
module's own docstring for the full history.

SPECTRUM_ENABLED is monkeypatched off in every test here so `_start_spectrum`
takes its early-return branch instead of spawning a real, un-awaited
SpectrumThread (see that flag's own docstring for why that's worth
avoiding) - not that video mode starts one any more anyway, but a queued
track's own analysis on PlayerBar construction/on_track_changed would
still fire one without this.
"""

from __future__ import annotations

import pytest

from musicmgr.services.video_player import VideoController
from musicmgr.ui.widgets import player_bar as player_bar_module
from musicmgr.ui.widgets.player_bar import PlayerBar
from musicmgr.ui.widgets.visualizer import VISUALIZER_HEIGHT


@pytest.fixture
def bar(ctx, monkeypatch):
    monkeypatch.setattr(player_bar_module, "SPECTRUM_ENABLED", False)
    return PlayerBar(ctx.player, ctx)


@pytest.fixture
def controller(qapp):
    return VideoController()


class TestEnterVideoMode:
    def test_starts_no_analysis_of_the_videos_audio(self, bar, controller):
        bar.enter_video_mode(controller, "Anthem", "Rush")

        assert bar._spectrum_path is None

    def test_hides_the_visualizer_strip(self, bar, controller):
        bar.enter_video_mode(controller, "Anthem", "Rush")

        assert bar.visualizer.isHidden() is True

    def test_shrinks_the_bar_by_the_visualizers_height(self, bar, controller):
        # maximumHeight(), not height() - setFixedHeight() updates the
        # widget's size constraints synchronously, but this bar is never
        # actually shown/laid out in this headless test, so its real
        # on-screen height() only catches up once an event loop processes
        # the resulting resize event
        before = bar.maximumHeight()

        bar.enter_video_mode(controller, "Anthem", "Rush")

        assert bar.maximumHeight() == before - VISUALIZER_HEIGHT


class TestExitVideoMode:
    def test_shows_the_visualizer_strip_again(self, bar, controller):
        bar.enter_video_mode(controller, "Anthem", "Rush")

        bar.exit_video_mode()

        assert bar.visualizer.isHidden() is False

    def test_restores_the_bars_full_height(self, bar, controller):
        original = bar.maximumHeight()
        bar.enter_video_mode(controller, "Anthem", "Rush")

        bar.exit_video_mode()

        assert bar.maximumHeight() == original

    def test_falls_back_to_whatever_is_queued(self, bar, controller):
        bar.enter_video_mode(controller, "Anthem", "Rush")

        bar.exit_video_mode()

        # nothing queued in this test - on_track_changed(None) clears it,
        # same contract an emptied queue always had
        assert bar._spectrum_path is None


class TestTransportStaysCentred:
    """James: "can the stop and play button at the bottom be in a fixed
    centered location? It moves around depending on the length of the
    album name"."""

    def _play_centre(self, bar):
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        return bar.play_btn.mapTo(bar, bar.play_btn.rect().center()).x()

    def test_play_button_doesnt_move_with_the_text(self, bar):
        bar.resize(1600, bar.height())
        bar.show()
        bar.title.setText("Take It On the Run")
        bar.subtitle.setText("REO Speedwagon — Hi Infidelity")
        short = self._play_centre(bar)
        bar.title.setText("Rockin' Into the Night")
        bar.subtitle.setText(".38 Special — 20th Century Masters: The Millennium Collection: "
                             "The Best of .38 Special")
        long = self._play_centre(bar)
        assert short == long
        assert abs(long - bar.width() / 2) <= 2

    def test_long_text_is_cut_short_with_the_full_text_in_the_tooltip(self, bar):
        bar.resize(1600, bar.height())
        bar.show()
        text = ".38 Special — 20th Century Masters: The Millennium Collection: The Best of"
        bar.subtitle.setText(text)
        from PySide6.QtWidgets import QApplication, QLabel

        QApplication.processEvents()
        shown = QLabel.text(bar.subtitle)
        assert shown.endswith("…") and shown != text
        assert bar.subtitle.text() == text and bar.subtitle.toolTip() == text
