"""Deprecated shim.

The grid now serves artists as well as albums, so it lives in `cover_grid`.
This module stays so older imports keep working.
"""

from __future__ import annotations

from .cover_grid import (  # noqa: F401
    ALBUM_SORTS,
    ARTIST_SORTS,
    ROLE_TILE,
    SHAPE_CIRCLE,
    SHAPE_SQUARE,
    CoverDelegate,
    CoverGrid,
    GridTile,
    JumpBar,
    SortOption,
)

#: previous names
ROLE_ALBUM = ROLE_TILE
AlbumGrid = CoverGrid
AlbumTile = GridTile
AlbumDelegate = CoverDelegate
