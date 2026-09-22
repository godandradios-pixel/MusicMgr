"""Tests for LibraryView's "Missing metadata" back-navigation (2026-09-22 -
James: "is there any way we can have a back button when you go from missing
metadata to the album and then back"). An artist/release page opened via
ctx.openArtistFromMissingMetadataRequested/openReleaseFromMissingMetadataRequested
(Settings' MissingMetadataDialog - see test_settings_view.py) gets a
"Missing metadata" leading breadcrumb crumb instead of the ordinary
Artists/Albums-grid one, and tapping it hands off back to Settings via
ctx.missingMetadataBackRequested rather than Library doing anything with
Settings directly - the same "views only ever talk through ctx" shape
Now Playing's own tappable-artist/tappable-album back-to-Now-Playing crumb
(_open_release_from_elsewhere) already uses.
"""

from __future__ import annotations

import pytest

from musicmgr.services import library as lib
from musicmgr.ui.views.library import LibraryView


@pytest.fixture
def view(ctx):
    return LibraryView(ctx)


def make_release(session, artist="Artist", album="Album"):
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist)
    release.album_artist_id = artist_obj.id
    session.flush()
    return artist_obj, release


class TestOpenArtistFromMissingMetadata:
    def test_breadcrumb_leads_back_to_settings_via_ctx(self, view, ctx, session):
        artist, _ = make_release(session, artist="Some Artist")

        view._open_artist_from_missing_metadata(artist.id)

        assert view.artist_detail._artist_id == artist.id
        assert len(view._artist_breadcrumb_actions) == 1
        navigated = []
        back_requested = []
        ctx.navigateRequested.connect(navigated.append)
        ctx.missingMetadataBackRequested.connect(lambda: back_requested.append(True))

        view._artist_breadcrumb_actions[0]()

        assert navigated == ["settings"]
        assert back_requested == [True]


class TestOpenReleaseFromMissingMetadata:
    def test_breadcrumb_leads_back_to_settings_via_ctx(self, view, ctx, session):
        _, release = make_release(session, artist="Some Artist", album="Some Album")

        view._open_release_from_missing_metadata(release.id)

        assert len(view._release_breadcrumb_actions) == 1
        navigated = []
        back_requested = []
        ctx.navigateRequested.connect(navigated.append)
        ctx.missingMetadataBackRequested.connect(lambda: back_requested.append(True))

        view._release_breadcrumb_actions[0]()

        assert navigated == ["settings"]
        assert back_requested == [True]


class TestOpenArtistFromElsewhereUnaffected:
    def test_now_playing_style_open_still_defaults_to_the_artists_grid(self, view, session):
        """The pre-existing ctx.openArtistRequested path (Now Playing's
        tappable artist name) must keep its old behavior - the new `root`
        param on _open_artist defaults to the Artists grid, not "Missing
        metadata", for every caller that doesn't pass one explicitly."""
        artist, _ = make_release(session, artist="Elsewhere Artist")

        view._open_artist_from_elsewhere(artist.id)

        assert len(view._artist_breadcrumb_actions) == 1
        # calling it must not touch ctx.missingMetadataBackRequested/navigateRequested
        # at all - it just switches panes locally, same as before this feature
        view._artist_breadcrumb_actions[0]()
        from musicmgr.ui.views.library import PANE_ARTIST_GRID
        assert view.panes.currentIndex() == PANE_ARTIST_GRID
