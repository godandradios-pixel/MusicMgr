"""Tests for the tag editor's screens (2026-10-06): TagEditorDialog,
MusicBrainzDialog, the album page's "Edit tags…" button, Title Details'
multi-select "Edit tags…", and the player queue following an edit.

Everything saves on the test's own thread (`use_thread = False`) - the
in-memory test database lives on one connection per thread (see
conftest.py)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from sqlalchemy import select

from musicmgr.db import session as db_session
from musicmgr.db.models import MediaFile, Release, Track
from musicmgr.services import musicbrainz as mb
from musicmgr.services import scanner
from musicmgr.services import tag_writer as tw
from musicmgr.services.player import QueueItem
from musicmgr.ui.widgets import tag_editor as ted

import test_musicbrainz as tm

FIXTURE = Path(__file__).parent / "fixtures" / "audio" / "silence.mp3"


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    from musicmgr import config
    monkeypatch.setattr(ted.TagEditorDialog, "use_thread", False)
    monkeypatch.setattr(ted.TagEditorDialog, "_warn", lambda self, text: None)
    monkeypatch.setattr(ted.MusicBrainzDialog, "auto_search", False)
    monkeypatch.setattr(mb, "MIN_INTERVAL_S", 0)
    art = tmp_path / "artwork"
    art.mkdir()
    monkeypatch.setattr(config, "ART_DIR", art)


def make_album(root: Path, titles, album="Back in Blak", artist="ACDC", genre="Rock",
               comments=None) -> list[Path]:
    paths = []
    for i, title in enumerate(titles, start=1):
        p = root / artist / album / f"{i:02d} {title}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE, p)
        tw.write_values(p, tw.TagValues(title=title, artist=artist, album=album,
                                        album_artist=artist, track=str(i), date="1980",
                                        genre=genre,
                                        comment=(comments or {}).get(i, "")))
        paths.append(p)
    with db_session.session_scope() as s:
        scanner.scan_folder(s, root)
    return paths


def ids_for(paths) -> list[int]:
    with db_session.session_scope() as s:
        return [s.scalar(select(MediaFile.track_id).where(MediaFile.path == str(p))) for p in paths]


@pytest.fixture
def album(db, tmp_path):
    return make_album(tmp_path / "music", tm.TITLES[:3], comments={2: "[Rock][1980] #081#"})


class TestEditorDialog:
    def test_loads_the_songs_with_nothing_to_save(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        assert d.table.rowCount() == 3
        assert d.table.item(1, ted.COL_COMMENT).text() == "[Rock][1980] #081#"
        assert d.album_edits["album"].text() == "Back in Blak"
        assert d.edits() == {}
        assert not d.save_btn.isEnabled()

    def test_an_album_field_goes_to_every_song(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        d.album_edits["album"].setText("Back in Black")
        edits = d.edits()
        assert len(edits) == 3
        assert all(e.changed_fields() == {"album": "Back in Black"} for e in edits.values())
        assert d.save_btn.text() == "Save 3 songs"

    def test_a_cell_changes_one_song(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        d.table.item(0, ted.COL_TITLE).setText("Hell's Bells")
        edits = d.edits()
        assert list(edits) == [ids_for(album)[0]]
        assert edits[ids_for(album)[0]].changed_fields() == {"title": "Hell's Bells"}

    def test_a_field_the_songs_disagree_on_is_left_alone(self, ctx, db, tmp_path):
        a = make_album(tmp_path / "a", ["One"], album="A", genre="Rock")
        b = make_album(tmp_path / "b", ["Two"], album="B", genre="Pop")
        d = ted.TagEditorDialog(ctx, ids_for(a + b))
        assert d.album_edits["genre"].text() == ""
        assert d.album_edits["genre"].placeholderText() == ted.MIXED
        assert d.edits() == {}
        d.album_edits["genre"].setText("Oldies")
        assert {e.genre for e in d.edits().values()} == {"Oldies"}

    def test_clearing_a_shared_field_removes_it(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        d.album_edits["genre"].setText("")
        assert all(e.changed_fields() == {"genre": ""} for e in d.edits().values())

    def test_compilation_checkbox(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        d.compilation_check.setChecked(True)
        assert all(e.compilation == "1" for e in d.edits().values())

    def test_save_writes_the_files_and_tells_the_app(self, ctx, album):
        ids = ids_for(album)
        d = ted.TagEditorDialog(ctx, ids)
        d.album_edits["album"].setText("Back in Black")
        d.album_edits["album_artist"].setText("AC/DC")
        saved, changed, notes = [], [], []
        d.saved.connect(saved.append)
        ctx.libraryChanged.connect(lambda: changed.append(True))
        ctx.notified.connect(notes.append)
        d.save()
        assert saved and saved[0].written == 3 and saved[0].moved == 0
        assert changed == [True]
        assert notes and "3 files updated" in notes[0]
        assert tw.read_values(album[0]).album == "Back in Black"
        with db_session.session_scope() as s:
            rels = list(s.scalars(select(Release)))
            assert [(r.title, r.artist_display) for r in rels] == [("Back in Black", "AC/DC")]

    def test_a_missing_file_shows_but_cannot_be_edited(self, ctx, album):
        ids = ids_for(album)
        album[2].unlink()
        d = ted.TagEditorDialog(ctx, ids)
        assert "isn't there" in d.table.item(2, ted.COL_FILE).text()
        assert not (d.table.item(2, ted.COL_TITLE).flags() & Qt.ItemIsEditable)
        d.album_edits["album"].setText("Back in Black")
        assert len(d.edits()) == 2


class TestMusicBrainzInEditor:
    def release(self):
        return mb.get_release("rel-1980", http=tm.FakeHttp([("/release/", tm.FakeResponse(200, tm.RELEASE))]))

    def test_lookup_fills_the_boxes_without_saving(self, ctx, db, tmp_path):
        titles = ["Hell's Bells", "Shoot To Thrill", "Back In Black"]
        paths = make_album(tmp_path / "m", titles)
        d = ted.TagEditorDialog(ctx, ids_for(paths))
        matched = d.apply_release(self.release())
        assert matched == 3
        assert d.album_edits["album"].text() == "Back in Black"
        assert d.album_edits["album_artist"].text() == "AC/DC"
        assert d.album_edits["genre"].text() == "Rock"  # genre left alone by default
        assert [d.table.item(i, ted.COL_TITLE).text() for i in range(3)] == [
            "Hells Bells", "Shoot to Thrill", "Back in Black"]
        assert d.table.item(2, ted.COL_TRACK).text() == "6"
        e = d.edits()[ids_for(paths)[0]]
        assert e.mb_album_id == "rel-1980" and e.mb_recording_id == "r1"
        assert e.track_total == "10"
        assert "3 of 3 songs matched" in d.status.text()
        assert tw.read_values(paths[0]).album == "Back in Blak"  # nothing written yet

    def test_genre_when_asked(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        d.apply_release(self.release(), include_genre=True)
        assert d.album_edits["genre"].text() == "Hard Rock, Rock"

    def test_look_up_button_uses_the_picker(self, ctx, album, monkeypatch):
        rel = self.release()

        class Picker:
            include_genre = False
            cover = b"\xff\xd8cover"
            def __init__(self, artist, album_title, count, has_cover, parent):
                Picker.args = (artist, album_title, count, has_cover)
                self.release = rel
            def exec(self):
                return ted.QDialog.Accepted

        d = ted.TagEditorDialog(ctx, ids_for(album), has_cover=False)
        monkeypatch.setattr(d, "lookup_dialog_class", Picker)
        d.look_up()
        assert Picker.args == ("ACDC", "Back in Blak", 3, False)
        assert d.album_edits["album"].text() == "Back in Black"
        assert d._cover == b"\xff\xd8cover"
        d.save()
        with db_session.session_scope() as s:
            rel_row = s.scalar(select(Release))
            assert rel_row.cover_path and Path(rel_row.cover_path).read_bytes() == b"\xff\xd8cover"

    def test_no_match_fills_nothing(self, ctx, db, tmp_path):
        paths = make_album(tmp_path / "x", ["Totally Different Song"])
        d = ted.TagEditorDialog(ctx, ids_for(paths))
        assert d.apply_release(self.release()) == 0
        assert d.edits() == {}
        assert "nothing was filled in" in d.status.text()


    def test_a_song_that_matches_nothing_is_left_alone(self, ctx, db, tmp_path):
        good = make_album(tmp_path / "a", ["Hells Bells"])
        other = make_album(tmp_path / "b", ["Something Else Entirely"], album="One",
                           artist="Other")
        d = ted.TagEditorDialog(ctx, ids_for(good + other))
        assert d.apply_release(self.release()) == 1
        edits = d.edits()
        assert ids_for(other)[0] not in edits
        e = edits[ids_for(good)[0]]
        assert e.album == "Back in Black" and e.album_artist == "AC/DC" and e.date == "1980-07-25"
        assert d.album_edits["album"].text() == ""  # the boxes stay as the files have them
        assert "1 left exactly as they were" in d.status.text()

    def test_a_second_lookup_starts_over(self, ctx, album):
        d = ted.TagEditorDialog(ctx, ids_for(album))
        d.apply_release(self.release())
        other = mb.MBRelease(id="other", title="Something Else", artist="Nobody",
                             tracks=[mb.MBTrack(1, 1, "1", "Unrelated", "Nobody")],
                             disc_sizes={1: 1})
        assert d.apply_release(other) == 0
        assert d.edits() == {}
        assert d.album_edits["album"].text() == "Back in Blak"

class TestPickerDialog:
    def test_results_and_choose(self, qapp):
        dialog = ted.MusicBrainzDialog("AC/DC", "Back in Black", 10, has_cover=False)
        assert dialog.cover_check.isChecked()
        assert not dialog.genre_check.isChecked()
        results = [mb.ReleaseSummary(**mb._summary_from(r)[0]) for r in tm.SEARCH["releases"]]
        dialog.show_results(results)
        assert dialog.list.count() == 4
        assert dialog.use_btn.isEnabled()
        assert dialog.list.payload_at(1)["trail"] == "✓ same track count"
        rel = TestMusicBrainzInEditor().release()
        dialog.choose(rel, b"img")
        assert dialog.release is rel and dialog.cover == b"img"

    def test_error_text(self, qapp):
        dialog = ted.MusicBrainzDialog("", "", None)
        dialog.show_results([], "Couldn't reach MusicBrainz - check the internet connection and try again.")
        assert "internet" in dialog.status.text()
        assert not dialog.use_btn.isEnabled()


class TestAlbumPage:
    def test_edit_tags_follows_the_album_to_its_new_id(self, ctx, album, monkeypatch):
        from musicmgr.ui.widgets.release_panel import ReleaseDetailPanel
        with db_session.session_scope() as s:
            rid = s.scalar(select(Release.id))
        panel = ReleaseDetailPanel(ctx)
        panel.set_release(rid)
        assert panel.edit_tags_btn.isEnabled()

        class Editor(ted.TagEditorDialog):
            def exec(self):
                self.album_edits["album"].setText("Back in Black")
                self.save()
                return 1
        monkeypatch.setattr(ReleaseDetailPanel, "tag_editor_class", property(lambda self: Editor))
        panel.edit_tags()
        assert panel.release_id == rid  # the album itself was renamed
        assert panel.rel_title.text() == "Back in Black"
        assert len(panel.tracks) == 3


class TestTitleDetails:
    def test_multi_select_asks_to_edit_those_songs(self, qapp):
        from musicmgr.ui.widgets.track_details_table import TrackDetailRow, TrackDetailsTable
        table = TrackDetailsTable()
        rows = [TrackDetailRow(track_id=i, genre="Rock", album_artist="A", album="B",
                               track_no=i, title=f"Song {i}", duration_ms=1000, year=1980,
                               rating=None, sort_key=f"song {i}") for i in (1, 2, 3)]
        table.set_rows(rows)
        assert not table.edit_tags_btn.isEnabled()
        sel = table.view.selectionModel()
        for r in (0, 2):
            sel.select(table.model.index(r, 0),
                       QItemSelectionModel.Select | QItemSelectionModel.Rows)
        assert table.selected_track_ids() == [1, 3]
        assert table.edit_tags_btn.text() == "Edit tags (2)…"
        asked = []
        table.editTagsRequested.connect(asked.append)
        table.edit_tags_btn.click()
        assert asked == [[1, 3]]

    def test_library_view_opens_the_editor(self, ctx, monkeypatch):
        from musicmgr.ui.views.library import LibraryView
        opened = []

        class Editor:
            def __init__(self, ctx_, ids, parent=None):
                opened.append(ids)
            def exec(self):
                return 0
        view = LibraryView(ctx)
        monkeypatch.setattr(view, "tag_editor_class", Editor)
        view.edit_tags([5, 6])
        assert opened == [[5, 6]]


class TestTooMany:
    def test_a_huge_selection_is_turned_down(self, ctx, monkeypatch):
        from musicmgr.ui.views.library import LibraryView
        notes, opened = [], []
        ctx.notified.connect(notes.append)
        view = LibraryView(ctx)
        monkeypatch.setattr(view, "tag_editor_class", lambda *a, **k: opened.append(a))
        view.edit_tags(list(range(ted.MAX_SONGS + 1)))
        assert not opened and "up to 500" in notes[0]


class TestPlayerFollowsEdits:
    def test_queued_song_moves_to_its_new_track(self, ctx, album):
        old_id = ids_for(album)[0]
        # the queued item points at no real file, so the player's loudness
        # measuring can't hold the song open (Windows won't replace an
        # open file - that case is test_a_briefly_locked_file_is_waited_for)
        ctx.player.set_queue([QueueItem(track_id=old_id, title="Hells Bells", artist="ACDC",
                                        album="Back in Blak",
                                        path=str(album[0].with_name("not-opened.mp3")))])
        d = ted.TagEditorDialog(ctx, [old_id])
        d.table.item(0, ted.COL_TITLE).setText("Hells Bells Revisited")
        d.save()
        item = ctx.player.queue[0]
        assert item.track_id != old_id
        assert item.track_id == ids_for(album)[0]
        assert item.title == "Hells Bells Revisited"
