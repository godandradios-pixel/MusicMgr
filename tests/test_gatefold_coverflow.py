"""Tests for ui/widgets/gatefold_coverflow.py's panel spacing.

2026-09-16, James looking at an artist's release row: "reduce the blank
space between the albums." The row spaces panels by a fixed linear step
(`d * self._spacing`) while each panel's own *visible* width shrinks
much faster than linearly as it folds away from focus (see `_FOLD_RATE`/
`_shape_at`) - so the gap between two folded panels grew rapidly with
distance from focus even though the gap right next to the focused panel
looked fine. `_CoverflowCanvas` now spaces panels by the sum of their
actual visible half-widths plus one fixed `_EDGE_GAP`, precomputed into
`_offset_table` at construction time - these tests pin that behavior down
directly against the private geometry helpers, the same way
test_player_bar.py reaches into `PlayerBar`'s own internals.
"""

from __future__ import annotations

import pytest

from musicmgr.ui.widgets.gatefold_coverflow import (
    _EDGE_GAP,
    _MAX_VISIBLE_DELTA,
    _CoverflowCanvas,
)


@pytest.fixture
def canvas(qapp):
    return _CoverflowCanvas(panel_size=190)


class TestOffsetTable:
    def test_focused_panel_has_zero_offset(self, canvas):
        assert canvas._offset_table[0] == 0.0

    def test_every_consecutive_pair_is_exactly_edge_gap_apart(self, canvas):
        # the far edge of panel n-1 and the near edge of panel n should
        # sit _EDGE_GAP apart everywhere in the row, not just next to focus
        for n in range(1, _MAX_VISIBLE_DELTA + 1):
            far_edge_prev = canvas._offset_table[n - 1] + canvas._half_width_at(n - 1)
            near_edge_this = canvas._offset_table[n] - canvas._half_width_at(n)
            assert near_edge_this - far_edge_prev == pytest.approx(_EDGE_GAP, abs=1e-6)

    def test_step_size_shrinks_the_further_from_focus(self, canvas):
        # this is the actual bug fix: a folded panel takes up less visible
        # width, so the step to reach it should be smaller too - the old
        # fixed-ratio spacing kept every step the same size regardless,
        # which is exactly what let the gap balloon
        steps = [
            canvas._offset_table[n] - canvas._offset_table[n - 1]
            for n in range(1, _MAX_VISIBLE_DELTA + 1)
        ]
        assert steps == sorted(steps, reverse=True)
        assert steps[0] > steps[-1]


class TestCenterOffset:
    def test_zero_distance_is_zero_offset(self, canvas):
        assert canvas._center_offset(0.0) == 0.0

    def test_matches_the_table_at_whole_numbers(self, canvas):
        for n in range(_MAX_VISIBLE_DELTA + 1):
            assert canvas._center_offset(float(n)) == pytest.approx(
                canvas._offset_table[n]
            )
            assert canvas._center_offset(float(-n)) == pytest.approx(
                -canvas._offset_table[n]
            )

    def test_interpolates_smoothly_between_whole_numbers(self, canvas):
        # a fractional distance (mid-animation) should land strictly
        # between its two neighboring whole-unit offsets, not jump
        halfway = canvas._center_offset(1.5)
        assert canvas._offset_table[1] < halfway < canvas._offset_table[2]

    def test_never_indexes_past_the_table_at_the_max_visible_delta(self, canvas):
        # panels beyond _MAX_VISIBLE_DELTA are culled by _panel_geometry
        # before this is ever called with them, but this must not raise
        # even right at (or numerically just past) the boundary
        canvas._center_offset(float(_MAX_VISIBLE_DELTA))
        canvas._center_offset(float(_MAX_VISIBLE_DELTA) + 0.4)


class TestPanelGeometryNoOverlap:
    def test_adjacent_visible_panels_never_overlap(self, canvas, qapp):
        canvas.resize(1200, 260)
        tiles = []
        from musicmgr.ui.widgets.cover_grid import GridTile

        for i in range(12):
            tiles.append(GridTile(key=i, title=f"Album {i}", sort_key=f"{i:02d}"))
        canvas.set_tiles(tiles)
        canvas._scroll_pos = 6.0  # mid-list, so panels exist on both sides

        geoms = [
            (i, canvas._panel_geometry(i)) for i in range(len(tiles))
        ]
        geoms = [(i, g) for i, g in geoms if g is not None]
        geoms.sort(key=lambda pair: pair[1][0])  # left to right by center x

        for (i1, g1), (i2, g2) in zip(geoms, geoms[1:]):
            cx1, _, scale1, cos_t1, _ = g1
            cx2, _, scale2, cos_t2, _ = g2
            right_edge = cx1 + (canvas._panel_size / 2) * scale1 * cos_t1
            left_edge = cx2 - (canvas._panel_size / 2) * scale2 * cos_t2
            assert left_edge >= right_edge - 1e-6
