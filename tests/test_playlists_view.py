"""PlaylistsView as a drill-down tile grid (2026-09-26 - James: "I would
like playlist to have larger touch areas like Artist, Album or Jukebox").
Folders and playlists are tiles; tapping a folder drills in, the
breadcrumb walks back out, tapping a playlist opens its own page."""

from __future__ import annotations

from musicmgr.db.models import Playlist, Track
from musicmgr.services import library as lib
from musicmgr.services import playlists as pl
from musicmgr.services.matching import normalize
from musicmgr.ui.views.playlists import PlaylistsView
from musicmgr.ui.widgets.playlist_grid import KIND_FOLDER, KIND_PLAYLIST


def make_track(session, title, album="Album") -> Track:
    lib.get_or_create_artist(session, "Artist")
    release = lib.get_or_create_release(session, album, "Artist")
    track = Track(release_id=release.id, title=title, title_key=normalize(title),
                  artist_display="Artist")
    session.add(track)
    session.flush()
    return track


def tile_titles(view: PlaylistsView) -> list[str]:
    return [t.title for t in view.grid.tiles()]


def make_view(ctx) -> PlaylistsView:
    view = PlaylistsView(ctx)
    view.refresh()
    return view


class TestBrowse:
    def test_top_level_shows_folders_first_then_playlists(self, ctx, session):
        pl.create_playlist(session, "Road Trip")
        pl.create_folder(session, "Zeta Folder")
        pl.create_folder(session, "Alpha Folder")
        view = make_view(ctx)

        tiles = view.grid.tiles()
        kinds = [t.kind for t in tiles]
        assert kinds[:2] == [KIND_FOLDER, KIND_FOLDER]
        assert [t.title for t in tiles[:2]] == ["Alpha Folder", "Zeta Folder"]
        assert "Road Trip" in tile_titles(view)
        assert not view.breadcrumb.isVisible() or view.breadcrumb.path == []

    def test_tapping_a_folder_drills_in_and_the_breadcrumb_walks_back(self, ctx, session):
        outer = pl.create_folder(session, "Billboard")
        inner = pl.create_folder(session, "2020-29", parent_id=outer.id)
        pl.create_playlist(session, "2021", folder_id=inner.id)
        pl.create_playlist(session, "Top Level")
        view = make_view(ctx)

        view._on_tile_activated({"type": KIND_FOLDER, "id": outer.id})
        assert tile_titles(view) == ["2020-29"]
        view._on_tile_activated({"type": KIND_FOLDER, "id": inner.id})
        assert tile_titles(view) == ["2021"]
        assert view.breadcrumb.path == ["Playlists", "Billboard", "2020-29"]

        view._on_crumb(1)  # "Billboard"
        assert tile_titles(view) == ["2020-29"]
        view._on_crumb(0)  # "Playlists"
        assert "Top Level" in tile_titles(view)

    def test_folder_subtitle_counts_its_contents(self, ctx, session):
        folder = pl.create_folder(session, "Charts")
        pl.create_folder(session, "Sub", parent_id=folder.id)
        pl.create_playlist(session, "A", folder_id=folder.id)
        pl.create_playlist(session, "B", folder_id=folder.id)
        view = make_view(ctx)

        tile = next(t for t in view.grid.tiles() if t.title == "Charts")
        assert tile.subtitle == "1 folder · 2 playlists"

    def test_an_empty_folder_shows_the_empty_state(self, ctx, session):
        folder = pl.create_folder(session, "Nothing Here")
        view = make_view(ctx)

        view._on_tile_activated({"type": KIND_FOLDER, "id": folder.id})

        assert view.browse_stack.currentWidget() is view.grid_empty

    def test_new_playlists_land_in_the_folder_showing(self, ctx, session):
        folder = pl.create_folder(session, "Mixes")
        view = make_view(ctx)
        view._on_tile_activated({"type": KIND_FOLDER, "id": folder.id})

        assert view._current_folder_context() == folder.id


class TestTileArtwork:
    def test_playlist_tile_gets_its_album_covers(self, ctx, session):
        playlist = pl.create_playlist(session, "Mix")
        ids = []
        for i in range(5):
            t = make_track(session, f"Song {i}", album=f"Album {i}")
            t.release.cover_path = f"/covers/{i}.jpg"
            ids.append(t.id)
        pl.add_tracks(session, playlist.id, ids)
        view = make_view(ctx)

        tile = next(t for t in view.grid.tiles() if t.title == "Mix")
        assert tile.cover_paths == [f"/covers/{i}.jpg" for i in range(4)]
        assert tile.subtitle == "5 songs"

    def test_folder_tile_borrows_covers_from_playlists_inside(self, ctx, session):
        folder = pl.create_folder(session, "Charts")
        inner = pl.create_folder(session, "Deeper", parent_id=folder.id)
        playlist = pl.create_playlist(session, "Deep Mix", folder_id=inner.id)
        t = make_track(session, "Song", album="Deep Album")
        t.release.cover_path = "/covers/deep.jpg"
        pl.add_tracks(session, playlist.id, [t.id])
        view = make_view(ctx)

        tile = next(t for t in view.grid.tiles() if t.title == "Charts")
        assert tile.cover_paths == ["/covers/deep.jpg"]

    def test_chosen_image_is_carried_on_the_tile(self, ctx, session):
        playlist = pl.create_playlist(session, "Mix")
        playlist.cover_path = "/art/chosen.png"
        session.flush()
        view = make_view(ctx)

        tile = next(t for t in view.grid.tiles() if t.title == "Mix")
        assert tile.custom_path == "/art/chosen.png"

    def test_smart_and_playback_tiles_carry_their_kind_for_the_badge(self, ctx, session):
        view = make_view(ctx)

        kinds = {t.playlist_kind for t in view.grid.tiles() if t.kind == KIND_PLAYLIST}
        assert Playlist.KIND_PLAYBACK in kinds  # the built-in Most Played lists


class TestPlaylistPage:
    def test_tapping_a_playlist_opens_its_page(self, ctx, session):
        playlist = pl.create_playlist(session, "Road Trip")
        t = make_track(session, "Song")
        pl.add_tracks(session, playlist.id, [t.id])
        view = make_view(ctx)

        view._on_tile_activated({"type": KIND_PLAYLIST, "id": playlist.id})

        assert view.stack.currentIndex() == view.PAGE_PLAYLIST
        assert view.detail_title.text() == "Road Trip"
        assert view.track_list.count() == 1
        assert view.breadcrumb.path == ["Playlists", "Road Trip"]
        assert not view.remove_btn.isHidden()  # manual playlist
        assert view.edit_btn.isHidden()

        view._on_crumb(0)
        assert view.stack.currentIndex() == view.PAGE_BROWSE

    def test_playlist_page_inside_a_folder_breadcrumbs_through_it(self, ctx, session):
        folder = pl.create_folder(session, "Mixes")
        playlist = pl.create_playlist(session, "Road Trip", folder_id=folder.id)
        view = make_view(ctx)

        view._on_tile_activated({"type": KIND_FOLDER, "id": folder.id})
        view._on_tile_activated({"type": KIND_PLAYLIST, "id": playlist.id})

        assert view.breadcrumb.path == ["Playlists", "Mixes", "Road Trip"]
        view._on_crumb(1)
        assert tile_titles(view) == ["Road Trip"]

    def test_deleted_playlist_returns_to_the_grid_on_refresh(self, ctx, session):
        playlist = pl.create_playlist(session, "Gone Soon")
        view = make_view(ctx)
        view._on_tile_activated({"type": KIND_PLAYLIST, "id": playlist.id})

        session.delete(playlist)
        session.flush()
        view.refresh()

        assert view.stack.currentIndex() == view.PAGE_BROWSE


class TestTileMenu:
    def _texts(self, menu) -> list[str]:
        return [a.text() for a in menu.actions() if a.text()]

    def test_folder_menu(self, ctx, session):
        folder = pl.create_folder(session, "Charts")
        view = make_view(ctx)

        texts = self._texts(view._build_node_menu({"type": KIND_FOLDER, "id": folder.id}))

        assert texts == ["Open", "Rename…", "Move to folder…", "Choose image…",
                         "Sort playlists by album && track", "Delete folder…"]

    def test_playlist_menu_offers_play_and_image_reset_once_an_image_is_set(self, ctx, session):
        playlist = pl.create_playlist(session, "Mix")
        playlist.cover_path = "/art/chosen.png"
        session.flush()
        view = make_view(ctx)

        texts = self._texts(view._build_node_menu({"type": KIND_PLAYLIST, "id": playlist.id}))

        assert texts[:2] == ["Play", "Shuffle"]
        assert "Use automatic image" in texts
        assert "Delete playlist…" in texts

    def test_built_in_playback_playlists_have_no_delete(self, ctx, session):
        view = make_view(ctx)
        playback = next(t for t in view.grid.tiles() if t.playlist_kind == Playlist.KIND_PLAYBACK)

        texts = self._texts(view._build_node_menu({"type": KIND_PLAYLIST, "id": playback.id}))

        assert not any(t.startswith("Delete") for t in texts)

    def test_use_automatic_image_clears_the_chosen_one(self, ctx, session):
        playlist = pl.create_playlist(session, "Mix")
        playlist.cover_path = "/art/chosen.png"
        session.flush()
        view = make_view(ctx)

        menu = view._build_node_menu({"type": KIND_PLAYLIST, "id": playlist.id})
        next(a for a in menu.actions() if a.text() == "Use automatic image").trigger()

        session.expire_all()
        assert session.get(Playlist, playlist.id).cover_path is None
        tile = next(t for t in view.grid.tiles() if t.title == "Mix")
        assert tile.custom_path is None
