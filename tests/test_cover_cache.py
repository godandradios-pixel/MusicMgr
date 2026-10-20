"""ui/widgets/common.cover_pixmap's cache (2026-10-19 - James: "when I try
to replace album art it doesn't save my selection. I hit OK and nothing").
A replaced cover is saved under the same file name, so a cache keyed only
by path kept showing the old picture until a restart."""

import os

from PySide6.QtGui import QColor, QImage

from musicmgr.ui.widgets import common


def _write(path, color, bump=0):
    img = QImage(40, 40, QImage.Format_RGB32)
    img.fill(QColor(color))
    assert img.save(str(path), "PNG")
    if bump:
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump))


def _center(pix):
    return pix.toImage().pixelColor(20, 20).name()


def test_a_replaced_cover_shows_without_a_restart(qapp, tmp_path):
    common.clear_art_cache()
    cover = tmp_path / "Artist - Album.png"
    _write(cover, "#ff0000")
    assert _center(common.cover_pixmap(str(cover), 40, crop=True)) == "#ff0000"
    assert _center(common.cover_pixmap(str(cover), 40, crop=True)) == "#ff0000"  # cached
    _write(cover, "#0000ff", bump=2_000_000_000)
    assert _center(common.cover_pixmap(str(cover), 40, crop=True)) == "#0000ff"


def test_a_cover_picked_where_there_was_none_shows(qapp, tmp_path):
    common.clear_art_cache()
    cover = tmp_path / "Artist - Missing.png"
    placeholder = common.cover_pixmap(str(cover), 40, "Missing")
    assert not cover.exists() and not placeholder.isNull()
    _write(cover, "#00ff00")
    assert _center(common.cover_pixmap(str(cover), 40, "Missing")) == "#00ff00"
