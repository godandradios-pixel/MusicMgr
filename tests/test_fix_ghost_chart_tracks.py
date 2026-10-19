"""tools/fix_ghost_chart_tracks.py (2026-10-19): which titles lost a number."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import fix_ghost_chart_tracks as fx  # noqa: E402


def test_number_cut_off_the_title():
    assert fx.number_cut("6 Inch", "Inch")
    assert fx.number_cut("777-9311", "9311")
    assert fx.number_cut("5-10-15 Hours", "10-15 Hours")
    assert fx.number_cut("99 Luftballons", "Luftballons")


def test_not_a_cut_number():
    assert not fx.number_cut("Inch", "Inch")
    assert not fx.number_cut("Big Inch", "Inch")
    assert not fx.number_cut("6Inch", "Inch")
    assert not fx.number_cut(None, "Inch")
    assert not fx.number_cut("6 Inch", "")


def test_only_files_named_after_a_number_are_read():
    assert fx._may_be_cut(r"D:\Music\Beyonce\Lemonade\6 Inch.mp3", "Inch")
    assert fx._may_be_cut(r"D:\Music\The Time\777-9311.mp3", "9311")
    assert not fx._may_be_cut(r"D:\Music\Rush\01 Tom Sawyer.mp3", "Tom Sawyer")
    assert not fx._may_be_cut(r"D:\Music\Rush\1-01 Tom Sawyer.mp3", "Tom Sawyer")
    assert not fx._may_be_cut(r"D:\Music\Nena\Luftballons.mp3", "Luftballons")
