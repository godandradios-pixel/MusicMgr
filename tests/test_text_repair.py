"""Tests for services/text_repair.py: chart text whose accented letters
were mangled on the way in - "Expos�" (letter lost) and "ExposÃ©"
(UTF-8 read as Windows-1252) - repaired on import and for rows already in
the library (Library health's "Chart songs with garbled letters")."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from musicmgr.db.models import ChartEntry
from musicmgr.services import charts
from musicmgr.services import library_health as lh
from musicmgr.services.text_repair import (
    ChartTextRepairer, decode_csv_bytes, fix_mojibake, is_damaged, repair_chart_entries,
)
from tests.test_charts import make_owned_track

L = "�"


class TestDecode:
    def test_clean_utf8(self):
        assert decode_csv_bytes("Exposé".encode("utf-8")) == "Exposé"

    def test_bom_dropped(self):
        assert decode_csv_bytes(b"\xef\xbb\xbfrank") == "rank"

    def test_stray_cp1252_byte_does_not_spoil_utf8_accents(self):
        # one Windows-1252 é (0xE9) in an otherwise UTF-8 file
        raw = "Beyoncé,".encode("utf-8") + b"Expos\xe9"
        assert decode_csv_bytes(raw) == "Beyoncé,Exposé"


class TestMojibake:
    @pytest.mark.parametrize("bad,good", [
        ("ExposÃ©", "Exposé"),
        ("BeyoncÃ©", "Beyoncé"),
        ("Donâ€™t Stop", "Don’t Stop"),
        ("Mötley Crüe", "Mötley Crüe"),   # already fine: untouched
        ("Exposé", "Exposé"),
    ])
    def test_fix(self, bad, good):
        assert fix_mojibake(bad) == good


class TestRepairer:
    def test_lost_letter_found_from_library(self):
        r = ChartTextRepairer(["Exposé", "Point of No Return"])
        assert r.repair(f"Expos{L}") == "Exposé"

    def test_prefers_accented_spelling_but_falls_back_to_plain(self):
        assert ChartTextRepairer(["Expose"]).repair(f"Expos{L}") == "Expose"
        assert ChartTextRepairer(["Expose", "Exposé"]).repair(f"Expos{L}") == "Exposé"

    def test_case_insensitive(self):
        assert ChartTextRepairer(["Exposé"]).repair(f"EXPOS{L}") == "Exposé"

    def test_word_inside_longer_name(self):
        r = ChartTextRepairer(["Beyoncé"])
        assert r.repair(f"Destiny's Child feat. Beyonc{L}") == "Destiny's Child feat. Beyoncé"

    def test_ambiguous_left_alone(self):
        r = ChartTextRepairer(["Café", "Cafè"], known_words=())
        assert r.repair(f"Caf{L}") == f"Caf{L}"

    def test_unknown_left_alone(self):
        assert ChartTextRepairer(["Madonna"], known_words=()).repair(f"Expos{L}") == f"Expos{L}"

    # James's library, 2026-10-30: most \ufffd were Wikipedia's non-breaking
    # spaces, sometimes swallowed together with an accented letter
    @pytest.mark.parametrize("bad,good", [
        (f"Coolio{L}featuring{L}L.V.", "Coolio featuring L.V."),
        (f"Beyonc{L}featuring{L}Sean Paul", "Beyoncé featuring Sean Paul"),
        (f"Jay-Z{L}featuring{L}Beyonc{L}", "Jay-Z featuring Beyoncé"),
        (f"Method Man{L}featuring{L}Mary J. Blige", "Method Man featuring Mary J. Blige"),
        (f"Ros{L}and{L}Bruno Mars", "Rosé and Bruno Mars"),
        (f"Christina Aguilera,{L}Lil' Kim,{L}M{L}a{L}and{L}Pink",
         "Christina Aguilera, Lil' Kim, Mýa and Pink"),
        (f"Eric Ben{L}t{L}featuring{L}Tamia", "Eric Benét featuring Tamia"),
        (f"John Legend{L}featuring{L}Andr{L} 3000", "John Legend featuring André 3000"),
        (f"Major Lazer{L}featuring{L}Justin Bieber{L}and{L}M{L}",
         "Major Lazer featuring Justin Bieber and MØ"),
        (f"Diddy {L} Dirty Money", "Diddy \u2013 Dirty Money"),
        (f"Macarena{L}(Bayside Boys Mix)", "Macarena (Bayside Boys Mix)"),
    ])
    def test_wikipedia_spaces(self, bad, good):
        names = ["Method Man", "Beenie Man", "Man in the Mirror", "Jay-Z", "Jay-Z and Kanye West"]
        assert ChartTextRepairer(names).repair(bad) == good

    def test_clean_text_untouched(self):
        assert ChartTextRepairer([]).repair("Let Me Be the One") == "Let Me Be the One"


def _csv(tmp_path, body: bytes):
    p = tmp_path / "hot100.csv"
    p.write_bytes(b"date,rank,song,artist\n" + body)
    return p


def test_import_fills_lost_letter_and_matches(session, tmp_path):
    track = make_owned_track(session, "Let Me Be the One", "Exposé")
    path = _csv(tmp_path, f"1987-02-07,77,Let Me Be the One,Expos{L}\n".encode("utf-8"))
    result = charts.import_chart_csv(session, path, chart_name="Hot 100")
    entry = session.scalar(select(ChartEntry))
    assert entry.artist_name == "Exposé"
    assert entry.track_id == track.id
    assert result.matched == 1


def test_import_cp1252_byte(session, tmp_path):
    path = _csv(tmp_path, b"1987-02-07,80,Point of No Return,Expos\xe9\n")
    charts.import_chart_csv(session, path, chart_name="Hot 100")
    assert session.scalar(select(ChartEntry.artist_name)) == "Exposé"


def _bad_row(session, tmp_path, artist):
    path = _csv(tmp_path, b"1987-02-07,80,Point of No Return,X\n")
    charts.import_chart_csv(session, path, chart_name="Hot 100")
    e = session.scalar(select(ChartEntry))
    e.artist_name = artist          # as an older import left it
    session.flush()
    return e


def test_existing_rows_repaired_and_matched(session, tmp_path):
    track = make_owned_track(session, "Point of No Return", "Exposé")
    e = _bad_row(session, tmp_path, f"Expos{L}")
    e.track_id = None
    session.flush()
    assert is_damaged(e.artist_name)
    fixed, left = repair_chart_entries(session)
    assert (fixed, left) == (1, 0)
    assert e.artist_name == "Exposé" and e.artist_key == "expose"
    assert e.track_id == track.id


def test_library_health_finds_and_fixes(session, tmp_path):
    make_owned_track(session, "Point of No Return", "Exposé")
    _bad_row(session, tmp_path, "ExposÃ©")
    f = lh.check_library(session).get("garbled_chart_text")
    assert f.count == 1 and f.examples == ["Point of No Return - ExposÃ©"]
    msg = lh.apply_fix(session, f)
    assert msg.startswith("Repaired 1 chart song")
    assert lh.check_library(session).get("garbled_chart_text").ok
