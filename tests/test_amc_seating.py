"""Replay tests over a captured AMC GraphQL seating response.

The fixture is AMC Lincoln Square's IMAX auditorium: 16x28 grid, 448 cells,
297 real seats, 13 free. It exercises every quirk the source has to handle -
padding cells, cross-aisles, love seats, wheelchair and companion positions,
and seat names running right to left.
"""

from __future__ import annotations

import json

import pytest

from screenwatch.seating.groups import find_groups
from screenwatch.seating.model import SeatDataUnavailable, SeatKind, SeatStatus
from screenwatch.seating.render import to_svg, to_unicode_grid
from screenwatch.seating.sources.amc import AmcSeatSource


@pytest.fixture(scope="module")
def payload():
    from conftest import FIXTURES

    return json.loads((FIXTURES / "amc" / "seating-144316355.json").read_text())


@pytest.fixture(scope="module")
def auditorium(payload):
    return AmcSeatSource.parse(
        payload, showtime_id="144316355", venue_id="amc-lincoln-square-13"
    )


class TestParsing:
    def test_drops_padding_and_keeps_real_seats(self, payload, auditorium):
        cells = payload["data"]["viewer"]["showtime"]["seatingLayout"]["seats"]
        assert len(cells) == 448
        assert len(auditorium.seats) == 297

    def test_availability_matches_the_source(self, auditorium):
        assert auditorium.available == 13
        assert auditorium.capacity == 297

    def test_geometry_is_trusted(self, auditorium):
        assert auditorium.geometry_confidence == 1.0
        assert auditorium.screen_id == "1"
        assert auditorium.venue_id == "amc-lincoln-square-13"

    def test_seat_kinds_are_typed(self, auditorium):
        kinds = {s.kind for s in auditorium.seats}
        assert SeatKind.LOVESEAT in kinds
        assert SeatKind.WHEELCHAIR in kinds
        assert SeatKind.COMPANION in kinds

    def test_available_flag_is_the_authority_not_seat_status(self, auditorium):
        """`seatStatus` splits real availability across 'Available' and
        'Unblocked' and is blank for padding, so `available` decides."""
        assert sum(1 for s in auditorium.seats if s.status is SeatStatus.AVAILABLE) == 13

    def test_row_labels_come_from_seat_names(self, auditorium):
        labels = [r[0].row_label for r in auditorium.rows()]
        assert labels[0] == "A"
        assert "J" in labels and "N" in labels

    def test_row_one_is_the_front(self, auditorium):
        front = auditorium.rows()[0]
        assert front[0].row_label == "A"
        assert all(s.y == 0.0 for s in front)


class TestCrossAisles:
    def test_missing_row_numbers_survive_as_depth_gaps(self, auditorium):
        """Lincoln Square's IMAX has no rows 5, 11 or 12 - those are walkways.
        Interpolating depth over ordinal position would erase them and put
        row 6 closer to the screen than it physically is."""
        assert auditorium.row_count == 13          # 16 numbered, 3 are walkways

        depths = sorted({s.y for s in auditorium.seats})
        gaps = [round(b - a, 4) for a, b in zip(depths, depths[1:])]
        assert max(gaps) > min(gaps), "expected uneven spacing from cross-aisles"

    def test_column_gaps_from_padding_become_aisles(self, auditorium):
        assert any(s.aisle_adjacent for s in auditorium.seats)

    def test_seat_names_run_right_to_left(self, auditorium):
        """'A15' sits at column 7 and 'A1' at column 22. Geometry must use
        the column; the name is only a label."""
        row_a = auditorium.rows()[0]
        assert row_a[0].col_index < row_a[-1].col_index
        assert int(row_a[0].col_label) > int(row_a[-1].col_label)


class TestFailureModes:
    def test_graphql_errors_raise_seat_data_unavailable(self):
        with pytest.raises(SeatDataUnavailable, match="GraphQL error"):
            AmcSeatSource.parse(
                {"errors": [{"message": "bad id"}]}, showtime_id="1"
            )

    def test_general_admission_is_permanent_not_transient(self):
        """No seat map will ever exist for this screening, so it must not
        read as a failed fetch that is worth retrying."""
        with pytest.raises(SeatDataUnavailable, match="general admission"):
            AmcSeatSource.parse(
                {"data": {"viewer": {"showtime": {"isReservedSeating": False}}}},
                showtime_id="1",
            )

    def test_missing_showtime_raises(self):
        with pytest.raises(SeatDataUnavailable, match="no showtime"):
            AmcSeatSource.parse({"data": {"viewer": {"showtime": None}}}, showtime_id="1")

    def test_sold_out_layout_absent_raises(self):
        """Verified live: AMC returns no layout for a sold-out showing, which
        is why return-watching has to key off the status field instead."""
        with pytest.raises(SeatDataUnavailable, match="no seating layout"):
            AmcSeatSource.parse(
                {"data": {"viewer": {"showtime": {"isReservedSeating": True,
                                                  "seatingLayout": None}}}},
                showtime_id="1",
            )

    def test_all_padding_raises_rather_than_returning_an_empty_room(self):
        with pytest.raises(SeatDataUnavailable, match="only padding"):
            AmcSeatSource.parse(
                {"data": {"viewer": {"showtime": {
                    "isReservedSeating": True, "auditorium": 1,
                    "seatingLayout": {"seats": [
                        {"type": "NotASeat", "row": 1, "column": 1,
                         "available": False, "name": ""}]}}}}},
                showtime_id="1",
            )


class TestDownstreamUsesIt:
    def test_group_finding_works_on_the_real_room(self, auditorium):
        groups = find_groups(auditorium, 2)
        assert groups and groups[0].size == 2

    def test_a_near_sellout_cannot_seat_a_large_party_together(self, auditorium):
        """13 free seats scattered across a 297-seat house. Four together is
        not available, and the system should say so rather than invent it."""
        [best] = find_groups(auditorium, 6)[:1]
        assert not best.complete or not best.cohesion.is_together

    def test_renders_a_real_imax_house_legibly(self, auditorium):
        grid = to_unicode_grid(auditorium)
        body = [l for l in grid.splitlines() if l and l[0].isalpha()]
        assert body and all(len(l) <= 48 for l in body)
        assert "SCREEN" in grid and "13/297 free" in grid

    def test_svg_renders_the_real_room(self, auditorium):
        svg = to_svg(auditorium)
        assert svg.startswith("<svg") and svg.count("<rect") >= 297
