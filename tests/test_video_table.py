"""Tests for ui/widgets/video_table.py: the manual multi-key sort (native
QTreeWidget column-click sorting can't do numeric Year/Time or "sort within
group, keep groups alphabetical", so this table owns it directly - see the
module docstring), the Group by None/Artist toggle, and `focus_artist`'s
scroll-and-select jump used by a collapsed search-result tile.

The table ships its own test-inspection helpers (`leaf_titles`,
`group_headers`) - built for exactly this, so most assertions here read the
rendered order back through those rather than reaching into `self.tree`
directly. A few tests still touch `.tree`/internal constants where no
public helper covers the behavior (current-item selection, telling a leaf
row apart from a group header).
"""

from __future__ import annotations

import pytest

from musicmgr.ui.widgets import video_table as vt
from musicmgr.ui.widgets.video_table import VideoRow, VideoTable


@pytest.fixture
def table(qapp):
    return VideoTable()


def row(key, title, artist="Artist", year=None, duration_ms=None, sort_key=None) -> VideoRow:
    return VideoRow(
        key=key,
        title=title,
        artist=artist,
        year=year,
        duration_ms=duration_ms,
        sort_key=sort_key if sort_key is not None else title.lower(),
    )


class TestSetRowsAndCount:
    def test_count_reflects_the_rows_given(self, table):
        table.set_rows([row(1, "One"), row(2, "Two"), row(3, "Three")])

        assert table.count() == 3

    def test_default_group_is_none(self, table):
        assert table.group_by == vt.GROUP_NONE

    def test_no_grouping_means_no_group_headers(self, table):
        table.set_rows([row(1, "One"), row(2, "Two")])

        assert table.group_headers() == []
        assert table.leaf_titles() == ["One", "Two"]


class TestDefaultSort:
    def test_rows_start_sorted_by_title_ascending(self, table):
        table.set_rows([row(1, "Zebra"), row(2, "Apple"), row(3, "Mango")])

        assert table.leaf_titles() == ["Apple", "Mango", "Zebra"]

    def test_title_sort_uses_sort_key_not_the_raw_title(self, table):
        # sort_key is the normalized key the rest of the app sorts titles by
        # (e.g. articles/punctuation stripped) - it can legitimately differ
        # from the displayed title's own alphabetical order.
        table.set_rows([
            row(1, "The Zoo", sort_key="zoo"),
            row(2, "Apple", sort_key="apple"),
        ])

        assert table.leaf_titles() == ["Apple", "The Zoo"]


class TestHeaderClickSorting:
    def test_clicking_a_column_sorts_by_it_ascending(self, table):
        table.set_rows([row(1, "B", year=2010), row(2, "A", year=2000)])

        table._on_header_clicked(vt.COL_YEAR)

        assert table.leaf_titles() == ["A", "B"]

    def test_clicking_the_same_column_again_reverses_the_order(self, table):
        table.set_rows([row(1, "B", year=2010), row(2, "A", year=2000)])
        table._on_header_clicked(vt.COL_YEAR)

        table._on_header_clicked(vt.COL_YEAR)

        assert table.leaf_titles() == ["B", "A"]

    def test_clicking_a_different_column_resets_to_ascending(self, table):
        table.set_rows([row(1, "B", year=2010), row(2, "A", year=2000)])
        table._on_header_clicked(vt.COL_YEAR)
        table._on_header_clicked(vt.COL_YEAR)  # now descending by year

        table._on_header_clicked(0)  # switch to Title - must not stay descending

        assert table.leaf_titles() == ["A", "B"]  # ascending by title

    def test_sorting_by_artist_is_case_insensitive(self, table):
        table.set_rows([row(1, "One", artist="zebra"), row(2, "Two", artist="Apple")])

        table._on_header_clicked(1)  # Artist column

        assert table.leaf_titles() == ["Two", "One"]

    def test_sorting_by_year_treats_a_missing_year_as_the_lowest(self, table):
        table.set_rows([row(1, "Has Year", year=2000), row(2, "No Year", year=None)])

        table._on_header_clicked(vt.COL_YEAR)

        assert table.leaf_titles() == ["No Year", "Has Year"]

    def test_sorting_by_duration_treats_a_missing_duration_as_the_lowest(self, table):
        table.set_rows([
            row(1, "Has Duration", duration_ms=90_000),
            row(2, "No Duration", duration_ms=None),
        ])

        table._on_header_clicked(vt.COL_DURATION)

        assert table.leaf_titles() == ["No Duration", "Has Duration"]

    def test_ties_on_the_active_column_break_by_title_sort_key(self, table):
        table.set_rows([
            row(1, "Zebra", year=2000),
            row(2, "Apple", year=2000),
        ])

        table._on_header_clicked(vt.COL_YEAR)  # both rows tie on year

        assert table.leaf_titles() == ["Apple", "Zebra"]


class TestGroupByArtist:
    def test_switching_to_artist_grouping_creates_one_header_per_artist(self, table):
        table.set_rows([
            row(1, "Song A", artist="Artist One"),
            row(2, "Song B", artist="Artist Two"),
        ])

        table._set_group(vt.GROUP_ARTIST)

        assert len(table.group_headers()) == 2

    def test_group_headers_are_sorted_case_insensitively(self, table):
        table.set_rows([
            row(1, "Song A", artist="zebra artist"),
            row(2, "Song B", artist="Apple Artist"),
        ])

        table._set_group(vt.GROUP_ARTIST)

        headers = table.group_headers()
        assert headers[0].startswith("Apple Artist")
        assert headers[1].startswith("zebra artist")

    def test_group_header_reports_the_video_count_pluralized(self, table):
        table.set_rows([
            row(1, "Song A", artist="Solo Artist"),
            row(2, "Song B", artist="Duo Artist"),
            row(3, "Song C", artist="Duo Artist"),
        ])

        table._set_group(vt.GROUP_ARTIST)

        headers = {h.split("  ·  ")[0]: h for h in table.group_headers()}
        assert headers["Solo Artist"].endswith("1 video")
        assert headers["Duo Artist"].endswith("2 videos")

    def test_a_video_with_no_artist_is_grouped_under_unknown_artist(self, table):
        table.set_rows([row(1, "Orphan Song", artist="")])

        table._set_group(vt.GROUP_ARTIST)

        assert table.group_headers()[0].startswith("Unknown artist")

    def test_rows_within_each_group_are_still_sorted_by_the_active_column(self, table):
        table.set_rows([
            row(1, "Zebra Song", artist="Shared Artist"),
            row(2, "Apple Song", artist="Shared Artist"),
        ])

        table._set_group(vt.GROUP_ARTIST)

        assert table.leaf_titles() == ["Apple Song", "Zebra Song"]

    def test_switching_back_to_no_grouping_flattens_the_list_again(self, table):
        table.set_rows([row(1, "Song A", artist="Artist One"), row(2, "Song B", artist="Artist Two")])
        table._set_group(vt.GROUP_ARTIST)

        table._set_group(vt.GROUP_NONE)

        assert table.group_headers() == []
        assert table.leaf_titles() == ["Song A", "Song B"]

    def test_setting_the_same_group_again_is_a_no_op(self, table):
        table.set_rows([row(1, "Song A", artist="Artist One")])
        table._set_group(vt.GROUP_ARTIST)
        headers_before = table.group_headers()

        table._set_group(vt.GROUP_ARTIST)

        assert table.group_headers() == headers_before

    def test_clicking_the_artist_chip_switches_the_grouping(self, table):
        artist_chip = next(
            b for b in table._group_buttons.buttons() if b.property("group") == vt.GROUP_ARTIST
        )
        table.set_rows([row(1, "Song A", artist="Artist One")])

        artist_chip.click()

        assert table.group_by == vt.GROUP_ARTIST
        assert len(table.group_headers()) == 1

    def test_clicking_the_none_chip_switches_back(self, table):
        table.set_rows([row(1, "Song A", artist="Artist One")])
        table._set_group(vt.GROUP_ARTIST)
        none_chip = next(
            b for b in table._group_buttons.buttons() if b.property("group") == vt.GROUP_NONE
        )

        none_chip.click()

        assert table.group_by == vt.GROUP_NONE


class TestFocusArtist:
    def test_switches_to_artist_grouping_if_not_already_there(self, table):
        table.set_rows([row(1, "Song A", artist="Target Artist")])
        assert table.group_by == vt.GROUP_NONE

        table.focus_artist("Target Artist")

        assert table.group_by == vt.GROUP_ARTIST

    def test_selects_the_matching_artists_header(self, table):
        table.set_rows([
            row(1, "Song A", artist="Target Artist"),
            row(2, "Song B", artist="Other Artist"),
        ])

        table.focus_artist("Target Artist")

        current = table.tree.currentItem()
        assert current is not None
        assert current.data(0, vt.ROLE_GROUP_ARTIST) == "Target Artist"

    def test_an_unknown_artist_name_is_a_no_op_after_switching_group(self, table):
        table.set_rows([row(1, "Song A", artist="Some Artist")])

        table.focus_artist("Nonexistent Artist")

        # still switches to Artist grouping (its one visible side effect)
        # but nothing gets selected since no header matches
        assert table.group_by == vt.GROUP_ARTIST

    def test_calling_it_again_while_already_grouped_by_artist_just_reselects(self, table):
        table.set_rows([
            row(1, "Song A", artist="First Artist"),
            row(2, "Song B", artist="Second Artist"),
        ])
        table.focus_artist("First Artist")

        table.focus_artist("Second Artist")

        current = table.tree.currentItem()
        assert current.data(0, vt.ROLE_GROUP_ARTIST) == "Second Artist"


class TestRowActivation:
    def test_clicking_a_leaf_row_emits_row_activated_with_its_key(self, table):
        table.set_rows([row(42, "Song A")])
        item = table.tree.topLevelItem(0)

        activated = []
        table.rowActivated.connect(activated.append)
        table._on_item_clicked(item, 0)

        assert activated == [42]

    def test_clicking_a_group_header_does_not_emit_row_activated(self, table):
        table.set_rows([row(1, "Song A", artist="Grouped Artist")])
        table._set_group(vt.GROUP_ARTIST)
        header = table.tree.topLevelItem(0)
        assert header.data(0, vt.ROLE_KEY) is None  # sanity: this is the header, not the leaf

        activated = []
        table.rowActivated.connect(activated.append)
        table._on_item_clicked(header, 0)

        assert activated == []
