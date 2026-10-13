"""Tests for skins and finishes (2026-10-13 - James: "I would like the
ability for the users to change 'skins'. Right now it's the brown
pallete" ... "apply at restart. and the jukebox, radio and turntable can
also get their own finish choice"): ui/skins.py, the theme picking the
choice up when it's imported, and Settings › Appearance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from musicmgr.ui import skins

ROOT = Path(__file__).resolve().parents[1]


class TestTables:
    @pytest.mark.parametrize("kind", list(skins.TABLES))
    def test_every_entry_has_every_key_of_the_default(self, kind):
        table = skins.TABLES[kind]
        keys = set(table[skins.DEFAULTS[kind]])
        for name, spec in table.items():
            assert set(spec) == keys, f"{kind}/{name}"

    def test_the_default_skin_is_todays_brown(self):
        from musicmgr.ui.theme import COLORS
        assert COLORS["jukebox_key"] == "#7a4a1f" == COLORS["accent"]
        assert COLORS["bg"] == "#101216"
        assert COLORS["jukebox_paper"] == "#f4e6bd"

    def test_a_skin_sets_the_accent_under_both_names(self):
        colors = skins.colors_for({"skin": "midnight", "jukebox": "white",
                                   "radio": "black", "turntable": "white"})
        assert colors["accent"] == colors["jukebox_key"] == skins.SKINS["midnight"]["accent"]
        assert colors["accent_hi"] == colors["jukebox_key_hi"]
        assert colors["jukebox_paper"] == skins.JUKEBOX_FINISHES["white"]["jukebox_paper"]
        assert "label" not in colors


class TestSavedChoice:
    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MUSICMGR_DEFAULT_LOOK", raising=False)
        path = tmp_path / "appearance.json"
        skins.save_choice({"skin": "forest", "jukebox": "aged", "radio": "oak",
                           "turntable": "black"}, path)
        assert skins.load_choice(path) == {"skin": "forest", "jukebox": "aged",
                                           "radio": "oak", "turntable": "black"}

    def test_unknown_or_damaged_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MUSICMGR_DEFAULT_LOOK", raising=False)
        path = tmp_path / "appearance.json"
        path.write_text(json.dumps({"skin": "neon", "radio": "black"}))
        assert skins.load_choice(path) == dict(skins.DEFAULTS, radio="black")
        path.write_text("{not json")
        assert skins.load_choice(path) == skins.DEFAULTS
        assert skins.load_choice(tmp_path / "missing.json") == skins.DEFAULTS

    def test_the_test_suite_switch(self, tmp_path):
        path = tmp_path / "appearance.json"
        path.write_text(json.dumps({"skin": "cream"}))
        assert os.environ.get("MUSICMGR_DEFAULT_LOOK")
        assert skins.load_choice(path) == skins.DEFAULTS


def test_a_new_start_picks_up_the_saved_skin(tmp_path):
    """The whole point of 'apply at restart': a fresh process with a saved
    choice builds its theme, radio and turntable from it."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "appearance.json").write_text(json.dumps(
        {"skin": "jukebox_red", "jukebox": "gold", "radio": "black", "turntable": "white"}))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MUSICMGR_HOME=str(data))
    env.pop("MUSICMGR_DEFAULT_LOOK", None)
    code = (
        "from musicmgr import config; print(config.DATA_DIR);"
        "from musicmgr.ui.theme import COLORS, stylesheet;"
        "from musicmgr.ui.widgets import radio_dial, turntable;"
        "print(COLORS['accent'], COLORS['jukebox_chrome_light']);"
        "print(radio_dial.FINISH['label'], turntable.FINISH['label']);"
        "print(COLORS['accent'] in stylesheet())"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    if Path(lines[0]) != data:
        pytest.skip("this setup doesn't take MUSICMGR_HOME")
    assert lines[1:] == ["#b0302a #f3e3b0", "Black bakelite White", "True"]


class TestSettingsAppearance:
    @pytest.fixture
    def view(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setattr(skins, "appearance_file", lambda: tmp_path / "appearance.json")
        from musicmgr.ui.views.settings import SettingsView
        return SettingsView(ctx)

    def test_choices_save_and_ask_for_a_restart(self, view, tmp_path):
        assert view.look_combos["skin"].currentData() == "walnut"
        assert view.look_restart_btn.isHidden()
        box = view.look_combos["skin"]
        box.setCurrentIndex(box.findData("midnight"))
        saved = json.loads((tmp_path / "appearance.json").read_text())
        assert saved["skin"] == "midnight" and saved["radio"] == "walnut"
        assert not view.look_restart_btn.isHidden()
        assert "Restart" in view.look_status.text()
        assert "restart to apply" in view._look_summary()
        box.setCurrentIndex(box.findData("walnut"))
        assert view.look_restart_btn.isHidden()

    def test_restart_from_source_just_says_so(self, view, ctx, monkeypatch):
        notes = []
        ctx.notified.connect(notes.append)
        monkeypatch.setattr(view, "_current_exe", lambda: None)
        view._restart_for_look()
        assert "start it again" in notes[0]

    def test_restart_leaves_out_the_update_note(self, view, monkeypatch, tmp_path):
        from musicmgr.services import updater
        calls = []
        monkeypatch.setattr(view, "_current_exe", lambda: tmp_path / "MusicMgr.exe")
        monkeypatch.setattr(updater, "relaunch", lambda exe, argv, after_update=True: calls.append(after_update))
        monkeypatch.setattr(view.window(), "close", lambda: True)
        view._restart_for_look()
        assert calls == [False]
        assert "--after-update" not in updater.relaunch_args(Path("x"), 1, [], after_update=False)
