"""Playlists page "Import playlist…" (2026-10-20 - James: a KLOVE Eras song
list had to go through a chart to become a playlist, and the songs the
library didn't have dropped out without a word)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from musicmgr.db.models import Playlist, PlaylistItem, Track
from musicmgr.services import library as lib
from musicmgr.services import playlist_import as imp
from musicmgr.services import playlists as pl_svc
from musicmgr.services.matching import normalize
from musicmgr.ui.views.playlists import PlaylistsView


def make_track(session, title, artist) -> Track:
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, "Album " + title, artist)
    release.album_artist_id = artist_obj.id
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display=artist)
    session.add(track)
    session.flush()
    return track


def write_xlsx(path: Path, rows: list[list]) -> Path:
    """A minimal real .xlsx: shared strings for text, numbers as numbers."""
    strings: list[str] = []
    def cell(ref, v):
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        strings.append(str(v))
        return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'
    body = "".join(
        f'<row r="{r}">' + "".join(cell(f"{'ABC'[c]}{r}", v) for c, v in enumerate(row)) + "</row>"
        for r, row in enumerate(rows, start=1))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sst = "".join(f"<si><t>{s.replace('&', '&amp;')}</t></si>" for s in strings)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml",
                    f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                    '</Relationships>')
        zf.writestr("xl/sharedStrings.xml", f"<sst {ns}>{sst}</sst>")
        zf.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")
    return path


SONGS = [["Rank", "Artist", "Title"],
         [2, "Mandisa", "Overcomer"],
         [1, "MercyMe", "To Not Worship You"],
         [3, "Nobody Known", "Not In The Library"]]


def _tracks(session, playlist_id) -> list[str]:
    return [t.title for t in pl_svc.playlist_tracks(session, playlist_id)]


class TestReading:
    def test_xlsx_rows_in_rank_order(self, tmp_path):
        songs, errors = imp.read_song_rows(write_xlsx(tmp_path / "KLOVE_Eras.xlsx", SONGS))
        assert errors == []
        assert [(s.rank, s.artist, s.title) for s in songs] == [
            (1, "MercyMe", "To Not Worship You"), (2, "Mandisa", "Overcomer"),
            (3, "Nobody Known", "Not In The Library")]

    def test_csv_without_rank_keeps_file_order(self, tmp_path):
        path = tmp_path / "list.csv"
        path.write_text("Song,Performer\nB Song,B\nA Song,A\n", encoding="utf-8")
        songs, errors = imp.read_song_rows(path)
        assert errors == [] and [s.title for s in songs] == ["B Song", "A Song"]

    def test_missing_columns_reported(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("Name,Year\nX,1999\n", encoding="utf-8")
        songs, errors = imp.read_song_rows(path)
        assert songs == [] and "artist" in errors[0]

    def test_name_keeps_capitals(self):
        assert imp.name_from_path("C:/x/KLOVE_Eras.xlsx") == "KLOVE Eras"


class TestImport:
    def test_matches_in_order_and_lists_misses(self, session, tmp_path):
        make_track(session, "Overcomer", "Mandisa")
        make_track(session, "To Not Worship You", "MercyMe")
        folder = pl_svc.create_folder(session, "KLOVE")
        result = imp.import_playlist(session, write_xlsx(tmp_path / "KLOVE_Eras.xlsx", SONGS),
                                     folder_id=folder.id)
        playlist = session.get(Playlist, result.playlist_id)
        assert (playlist.name, playlist.folder_id, playlist.kind) == (
            "KLOVE Eras", folder.id, Playlist.KIND_MANUAL)
        assert _tracks(session, playlist.id) == ["To Not Worship You", "Overcomer"]
        assert result.unmatched == [("Nobody Known", "Not In The Library")]
        assert "2 of 3 songs added" in result.summary()

    def test_a_song_listed_twice_is_added_once(self, session, tmp_path):
        make_track(session, "Overcomer", "Mandisa")
        path = tmp_path / "dupes.csv"
        path.write_text("Artist,Title\nMandisa,Overcomer\nMandisa,Overcomer\n", encoding="utf-8")
        result = imp.import_playlist(session, path)
        assert result.added == 1 and result.repeats == [("Mandisa", "Overcomer")]

    def test_nothing_matched_makes_no_playlist(self, session, tmp_path):
        path = tmp_path / "none.csv"
        path.write_text("Artist,Title\nNobody,Nothing\n", encoding="utf-8")
        result = imp.import_playlist(session, path)
        assert result.playlist_id is None
        assert session.scalar(select(Playlist).where(Playlist.name == "none")) is None


class TestButton:
    def test_imports_into_the_folder_showing(self, ctx, session, tmp_path, monkeypatch):
        make_track(session, "Overcomer", "Mandisa")
        folder = pl_svc.create_folder(session, "KLOVE")
        session.commit()
        folder_id = folder.id
        path = write_xlsx(tmp_path / "KLOVE_Eras.xlsx", SONGS)
        view = PlaylistsView(ctx)
        view._go_to_folder(folder_id)
        asked, shown = [], []
        view._ask_import_file = lambda: str(path)
        view._ask_import_name = lambda s, where: asked.append((s, where)) or s
        view._show_import_result = shown.append
        view.import_playlist()
        assert asked == [("KLOVE Eras", "“KLOVE”")]
        assert shown[0].added == 1 and len(shown[0].unmatched) == 2
        with ctx.session() as s:
            playlist = s.scalar(select(Playlist).where(Playlist.name == "KLOVE Eras"))
            assert playlist.folder_id == folder_id
        assert view._playlist_id == playlist.id  # opened straight away

    def test_cancelling_the_name_imports_nothing(self, ctx, session, tmp_path):
        path = write_xlsx(tmp_path / "KLOVE_Eras.xlsx", SONGS)
        view = PlaylistsView(ctx)
        view._ask_import_file = lambda: str(path)
        view._ask_import_name = lambda s, where: None
        view.import_playlist()
        with ctx.session() as s:
            assert s.scalar(select(Playlist).where(Playlist.name == "KLOVE Eras")) is None
