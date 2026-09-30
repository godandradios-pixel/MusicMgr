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


class TestButtons:
    def _view(self, ctx, session, monkeypatch):
        _board(session)
        session.commit()
        played, shuffles, notes = [], [], []
        monkeypatch.setattr(ctx, "play_tracks",
                            lambda tracks, start=0, source="": played.append(
                                ([t.title for t in tracks], source)) or len(tracks))
        monkeypatch.setattr(ctx.player, "set_shuffle", lambda on: shuffles.append(on))
        monkeypatch.setattr(ctx, "notify", lambda text: notes.append(text))
        view = JukeboxView(ctx)
        view._genre = "Rock"
        return view, played, shuffles, notes

    def test_play_all(self, ctx, session, monkeypatch):
        view, played, shuffles, notes = self._view(ctx, session, monkeypatch)
        view.play_all_btn.click()
        assert played == [(["Rock A0", "Rock B0", "Rock A2", "Rock B2"], "jukebox")]
        assert shuffles == [False] and notes == ["Playing 4 Rock songs"]

    def test_shuffle(self, ctx, session, monkeypatch):
        view, played, shuffles, notes = self._view(ctx, session, monkeypatch)
        view.shuffle_all_btn.click()
        assert shuffles == [True] and len(played[0][0]) == 4
        assert notes == ["Shuffling 4 Rock songs"]

    def test_empty_genre(self, ctx, session, monkeypatch):
        view, played, shuffles, notes = self._view(ctx, session, monkeypatch)
        view._genre = "Metal"
        view.play_all_btn.click()
        assert played == [] and notes == ["No songs on the Metal cards yet"]

