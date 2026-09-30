"""Jukebox "Play all" / "Shuffle" for the selected genre's cards
(2026-09-30 - James: "When on the Jukebox page for a specific Genre, I
would like to be able to 'Play All' 'Shuffled' for the songs on the jukebox
cards")."""

from __future__ import annotations

from musicmgr.services import jukebox as jb
from musicmgr.services import library as lib
from musicmgr.ui.views.jukebox import JukeboxView
from tests.test_jukebox import make_track


def _board(session):
    for i, genre in enumerate(["Rock", "Pop", "Rock"]):
        artist = lib.get_or_create_artist(session, f"Artist {i}")
        a = make_track(session, f"{genre} A{i}", artist=f"Artist {i}")
        b = make_track(session, f"{genre} B{i}", artist=f"Artist {i}")
        jb.place_track(session, artist.id, a.id, genre=genre)
        jb.place_track(session, artist.id, b.id, genre=genre)
    session.flush()


def test_genre_tracks_card_order_a_then_b(session):
    _board(session)
    assert [t.title for t in jb.genre_tracks(session, "Rock")] == [
        "Rock A0", "Rock B0", "Rock A2", "Rock B2"]
    assert len(jb.genre_tracks(session, None)) == 6
    assert jb.genre_tracks(session, "Metal") == []


class TestChipMenu:
    def _view(self, ctx, session, monkeypatch):
        _board(session)
        session.commit()
        played, shuffles, notes = [], [], []
        monkeypatch.setattr(ctx, "play_tracks",
                            lambda tracks, start=0, source="", navigate=True: played.append(
                                ([t.title for t in tracks], source, navigate)) or len(tracks))
        monkeypatch.setattr(ctx.player, "set_shuffle", lambda on: shuffles.append(on))
        monkeypatch.setattr(ctx, "notify", lambda text: notes.append(text))
        monkeypatch.setattr(jb, "get_jukebox_genres", lambda s: ["Rock", "Pop", "Metal"])
        view = JukeboxView(ctx)
        view._build_genre_chips()
        return view, played, shuffles, notes

    def _menu_action(self, view, genre, text):
        chip = [c for c in view._genre_chips.buttons() if c.property("genre") == genre][0]
        return {a.text(): a for a in chip._build_context_menu().actions()}[text]

    def test_play_all_from_another_genres_chip(self, ctx, session, monkeypatch):
        view, played, shuffles, notes = self._view(ctx, session, monkeypatch)
        view._select_genre("Pop")
        self._menu_action(view, "Rock", "▶ Play all").trigger()
        assert played == [(["Rock A0", "Rock B0", "Rock A2", "Rock B2"], "jukebox", False)]
        assert shuffles == [False] and notes == ["Playing 4 Rock songs"]
        assert view._genre == "Rock"                    # board switched to follow

    def test_shuffle(self, ctx, session, monkeypatch):
        view, played, shuffles, notes = self._view(ctx, session, monkeypatch)
        self._menu_action(view, "Rock", "⇄ Shuffle").trigger()
        assert shuffles == [True] and len(played[0][0]) == 4
        assert notes == ["Shuffling 4 Rock songs"]

    def test_empty_genre(self, ctx, session, monkeypatch):
        view, played, shuffles, notes = self._view(ctx, session, monkeypatch)
        self._menu_action(view, "Metal", "▶ Play all").trigger()
        assert played == [] and notes == ["No songs on the Metal cards yet"]


def test_slot_position(session):
    _board(session)
    rock_b2 = [t for t in jb.genre_tracks(session, "Rock") if t.title == "Rock B2"][0]
    assert jb.slot_position(session, "Rock", rock_b2.id) == 1
    assert jb.slot_position(session, "Pop", rock_b2.id) is None


def test_page_flips_to_the_playing_card(ctx, session):
    from musicmgr.services.player import QueueItem

    for i in range(30):
        artist = lib.get_or_create_artist(session, f"Band {i}")
        t = make_track(session, f"Song {i}", artist=f"Band {i}")
        jb.place_track(session, artist.id, t.id, genre="Rock")
    session.commit()
    view = JukeboxView(ctx)
    view._genre = "Rock"
    view._current_cols, view._current_rows = 3, 4          # 12 cards a page
    view._rows_that_fit = lambda: 4
    view._cols_that_fit = lambda: 3
    last = jb.genre_tracks(session, "Rock")[-1]              # card 30 -> page 3
    view._on_track_changed(QueueItem(track_id=last.id, title="", artist="", album="", path=""))
    assert view._page == 2
    view._on_track_changed(QueueItem(track_id=-1, title="", artist="", album="", path=""))
    assert view._page == 2                                   # not on the board: stay
