"""Old-time radio library (2026-10-01): services/otr.py - air dates and
titles from James's real file names, scanning, resume, and On-Air blocks.
The file names below are copied from his D:\\Radio folder."""

from __future__ import annotations

import random
import wave
from pathlib import Path

import pytest
from sqlalchemy import select

from musicmgr.db.models import RadioEpisode, RadioShow
from musicmgr.services import otr


@pytest.mark.parametrize("stem, tag_year, expected", [
    ("1937-10-17 - Murder By The Dead", 1937, "1937-10-17"),
    ("Whistler 51-03-11 (458) High Death", 1951, "1951-03-11"),
    ("LoneRanger_380722_mortgage_on_wheat", 1938, "1938-07-22"),
    ("430613  608 The White Ticket", 1943, "1943-06-13"),
    ("Abbott_costello490428266TonysHomePermanent", 1949, "1949-04-28"),
    ("Wild_Bill_Hickok_531216_192_Nobeards_Treasure", 1953, "1953-12-16"),
    ("MercuryTheater38-07-11Dracula", 1938, "1938-07-11"),
    ("FDR Fireside Chat-1 - On the Banking Crisis (1933-03-12)", 1933, "1933-03-12"),
    ("1945-10-xx-530-V-Disc-A-The-Page-Cavanaugh-Trio", 1945, "1945-10"),
    ("Stories Of Sherlock Holmes - SA 82-05-12 (x) Field Bazaar, The", None, "1982-05-12"),
    ("Dodge (1936)", None, "1936"),
    ("1974 Gerald Ford", 1974, "1974"),
    ("Anacin", None, None),
])
def test_air_dates(stem, tag_year, expected):
    assert otr.parse_air_date(stem, tag_year).iso == expected


def test_episode_numbers():
    assert otr.parse_episode_no("Whistler 51-03-11 (458) High Death", None) == 458
    assert otr.parse_episode_no("Dodge (1936)", None) is None
    assert otr.parse_episode_no("x", "62") == 62


@pytest.mark.parametrize("tag, stem, show, prefix, expected", [
    ("430905  620 Axford Rises to Shine", "430905  620 Axford Rises to Shine",
     "The Green Hornet", [], "Axford Rises to Shine"),
    ("in Monte Cassino", "1944-05-17-BBC-Godfrey-Talbot-in-Monte-Cassino",
     "WWII News and Sounds", [], "BBC Godfrey Talbot in Monte Cassino"),
    ("Sheriffs Son", "LoneRanger_430419_sheriffs_son", "Lone Ranger", ["loneranger"], "Sheriffs Son"),
    (None, "Whistler 49-06-05 (366) Letter to Melanie", "The Whistler", ["whistler"], "Letter to Melanie"),
    ("Stories Of Sherlock Holmes - SA xx-xx-xx (x) His Last Bow",
     "Stories Of Sherlock Holmes - SA xx-xx-xx (x) His Last Bow", "Sherlock Holmes",
     ["stories", "of", "sherlock", "holmes", "sa"], "His Last Bow"),
    ("On National Security", "FDR Fireside Chat-17 - On National Security (1940-12-29)",
     "FDR Fireside Chats", ["fdr", "fireside", "chat"], "On National Security"),
    (None, "MercuryTheater38-10-16Seventeen", "Orson Welles", ["mercurytheater"], "Seventeen"),
])
def test_titles(tag, stem, show, prefix, expected):
    assert otr.episode_title(tag, stem, show, prefix) == expected


def test_common_prefix_and_dial_labels():
    stems = ["Whistler 49-06-05 (366) Letter", "Whistler 50-01-08 (397) Return", "Whistler 44-11-20 (130) Death"]
    assert otr.common_prefix(stems) == ["whistler"]
    assert otr.dial_label("The Shadow") == "Shadow"
    assert otr.dial_label("Vincent Price - The Vintage Radio Shows") == "Vincent Price"
    assert otr.dial_label("Abbott And Costello") == "Abbott & Costello"


def test_format_air_date():
    assert otr.format_air_date("1951-03-11") == "Sunday, March 11, 1951"
    assert otr.format_air_date("1945-10") == "October 1945"
    assert otr.format_air_date("1938") == "1938"
    assert otr.format_air_date(None) == ""


def make_episode(path: Path, seconds: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


@pytest.fixture
def radio_folder(tmp_path):
    root = tmp_path / "Radio"
    for stem in ("1937-10-17 - Murder By The Dead", "1937-09-26 - Death House Rescue",
                 "1937-10-24 - The Temple Bells"):
        make_episode(root / "The Shadow" / f"{stem}.wav")
    for stem in ("Whistler 51-03-11 (458) High Death", "Whistler 49-06-05 (366) Letter to Melanie"):
        make_episode(root / "The Whistler" / f"{stem}.wav")
    make_episode(root / "Commercials" / "Anacin.wav")
    make_episode(root / "Commercials" / "Buick (1940).wav")
    make_episode(root / "WWII News and Sounds" / "1944" / "1944-01-09-CBS-World-News-Today.wav")
    make_episode(root / "WWII News and Sounds" / "1944" / "1944_01_09_CBS_World_News_Today.wav")
    (root / "The Shadow" / "Season17Cover.png").write_bytes(b"png")
    (root / "_gsdata_").mkdir()
    make_episode(root / "_gsdata_" / "junk.wav")
    return root


def fake_tags(path: Path) -> dict:
    return {"length": 1800.0}


class TestScan:
    def test_shows_episodes_kinds_order_and_duplicates(self, db, radio_folder):
        result = otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        assert result.shows == 4 and result.added == 9
        with db.session_scope() as s:
            shows = {sh.name: sh for sh in otr.list_shows(s)}
            assert set(shows) == {"The Shadow", "The Whistler", "Commercials", "WWII News and Sounds"}
            assert shows["Commercials"].kind == "commercial"
            assert shows["WWII News and Sounds"].kind == "news"
            assert shows["The Shadow"].cover_path.endswith("Season17Cover.png")
            titles = [e.title for e in otr.episodes(s, shows["The Shadow"].id)]
            assert titles == ["Death House Rescue", "Murder By The Dead", "The Temple Bells"]
            assert [e.episode_no for e in otr.episodes(s, shows["The Whistler"].id)] == [366, 458]
            # the same CBS broadcast twice: one hidden
            assert len(otr.episodes(s, shows["WWII News and Sounds"].id)) == 1

    def test_rescan_is_incremental_and_marks_missing(self, db, radio_folder):
        otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        calls = []
        again = otr.scan(db.new_session, str(radio_folder),
                         read_tags=lambda p: calls.append(p) or fake_tags(p))
        assert again.unchanged == 9 and not calls
        (radio_folder / "Commercials" / "Anacin.wav").unlink()
        gone = otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        assert gone.missing == 1


class TestResume:
    def setup(self, db, radio_folder):
        otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        with db.session_scope() as s:
            show = s.scalar(select(RadioShow).where(RadioShow.name == "The Shadow"))
            return show.id, [e.id for e in otr.episodes(s, show.id)]

    def test_starts_at_first_then_resumes_mid_episode(self, db, radio_folder):
        show_id, eps = self.setup(db, radio_folder)
        with db.session_scope() as s:
            show = s.get(RadioShow, show_id)
            assert otr.current_episode(s, show).id == eps[0]
            otr.save_position(s, eps[0], 300_000, 1_800_000)
        with db.session_scope() as s:
            items = otr.queue_for_show(s, show_id)
            assert items[0].episode_id == eps[0] and items[0].start_ms == 300_000
            assert [i.episode_id for i in items] == eps
            assert items[1].start_ms == 0

    def test_finishing_moves_on_and_marks_played(self, db, radio_folder):
        show_id, eps = self.setup(db, radio_folder)
        with db.session_scope() as s:
            assert otr.save_position(s, eps[0], 1_790_000, 1_800_000) is True
        with db.session_scope() as s:
            show = s.get(RadioShow, show_id)
            assert s.get(RadioEpisode, eps[0]).played
            assert otr.current_episode(s, show).id == eps[1]
            assert otr.progress_counts(s, show_id) == (1, 3)

    def test_skipping_ahead_and_wrapping(self, db, radio_folder):
        show_id, eps = self.setup(db, radio_folder)
        with db.session_scope() as s:
            for e in eps[1:]:
                otr.mark_played(s, s.get(RadioEpisode, e))
            show = s.get(RadioShow, show_id)
            # all but the first heard: wraps back round to it
            assert otr.current_episode(s, show).id == eps[0]
            otr.mark_played(s, s.get(RadioEpisode, eps[0]), played=False)
            assert otr.current_episode(s, show).id == eps[0]

    def test_tiny_positions_are_not_worth_resuming(self, db, radio_folder):
        _, eps = self.setup(db, radio_folder)
        with db.session_scope() as s:
            otr.save_position(s, eps[0], 5_000, 1_800_000)
            assert s.get(RadioEpisode, eps[0]).position_ms == 0


class TestOnAir:
    def test_block_has_program_last_with_commercials_before(self, db, radio_folder):
        otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        with db.session_scope() as s:
            state = otr.OnAirState(year=1944, played_ids=set())
            rng = random.Random(3)
            items = otr.build_on_air_block(s, state, rng)
            kinds = []
            for item in items:
                ep = s.get(RadioEpisode, item.episode_id)
                kinds.append(s.get(RadioShow, ep.show_id).kind)
        assert kinds[-1] == "show"
        assert "commercial" in kinds[:-1]
        assert all(i.kind == "episode" and i.start_ms == 0 for i in items)

    def test_shows_alternate_and_nothing_repeats(self, db, radio_folder):
        otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        with db.session_scope() as s:
            state = otr.OnAirState(year=1944, played_ids=set())
            rng = random.Random(5)
            programs = []
            for _ in range(4):
                block = otr.build_on_air_block(s, state, rng)
                if block:
                    programs.append(block[-1])
        ids = [p.episode_id for p in programs]
        assert len(ids) == len(set(ids))
        shows = [p.artist for p in programs]
        assert all(a != b for a, b in zip(shows, shows[1:]))

    def test_evening_year_comes_from_the_collection(self, db, radio_folder):
        otr.scan(db.new_session, str(radio_folder), read_tags=fake_tags)
        with db.session_scope() as s:
            year = otr.pick_evening_year(s, random.Random(1))
        assert year in (1937, 1949, 1951)
