"""Unit tests for the derive layer: standings, form, head-to-head."""

from __future__ import annotations

import datetime as dt

from football_mcp.derive import (
    compute_head_to_head,
    compute_standings,
    compute_team_form,
)
from football_mcp.models import Match

D1, D2, D3 = (dt.date(2026, 8, 1) + dt.timedelta(days=i) for i in range(3))


def _m(date, home, away, hg, ag) -> Match:
    return Match(
        competition="E0",
        season="2026-27",
        date=date,
        home_team=home,
        away_team=away,
        home_goals=hg,
        away_goals=ag,
        result="H" if hg > ag else ("A" if ag > hg else "D"),
    )


class TestStandings:
    def test_basic_table(self):
        rows = compute_standings(
            [
                _m(D1, "Man United", "Chelsea", 2, 1),
                _m(D1, "Arsenal", "Everton", 0, 0),
                _m(D2, "Chelsea", "Arsenal", 1, 3),
                _m(D2, "Everton", "Man United", 0, 1),
            ]
        )
        # Man United 6, Arsenal 4, Everton 1 (D+L), Chelsea 0
        assert [r.team for r in rows] == ["Man United", "Arsenal", "Everton", "Chelsea"]
        top = rows[0]
        assert top.team == "Man United"
        assert (top.played, top.won, top.drawn, top.lost) == (2, 2, 0, 0)
        assert top.points == 6
        assert top.form == "WW"
        arsenal = rows[1]
        assert arsenal.form == "DW"
        assert arsenal.points == 4
        assert arsenal.goal_difference == 2  # 0-0 then 3-1

    def test_as_of_date_blocks_future(self):
        rows = compute_standings(
            [_m(D1, "A", "B", 1, 0), _m(D2, "B", "A", 5, 0), _m(D3, "A", "C", 2, 2)],
            as_of_date=D1,
        )
        # only the D1 match counts: A has 3 pts, B has 0, C absent
        by_team = {r.team: r for r in rows}
        assert by_team["A"].points == 3
        assert by_team["B"].points == 0
        assert "C" not in by_team
        assert by_team["A"].played == 1

    def test_tiebreakers(self):
        rows = compute_standings(
            [
                _m(D1, "X", "Y", 1, 0),
                _m(D2, "Z", "W", 3, 0),
            ]  # X and Z both 3 pts; Z has better GD
        )
        assert rows[0].team == "Z"

    def test_canonical_name_merge(self):
        """Same club under two spellings must not split into two rows."""
        rows = compute_standings(
            [
                _m(D1, "Man United", "Chelsea", 2, 0),
                _m(D2, "Chelsea", "Manchester United", 1, 1),
            ]
        )
        by_team = {r.team: r for r in rows}
        assert len(rows) == 2
        assert by_team["Man United"].played == 2  # merged under one key


class TestTeamForm:
    def test_form_summary_and_venue(self):
        matches = [
            _m(dt.date(2026, 8, 1), "Man United", "Chelsea", 2, 1),
            _m(dt.date(2026, 8, 9), "Liverpool", "Man United", 3, 0),
            _m(dt.date(2026, 8, 17), "Man United", "Arsenal", 1, 1),
        ]
        form = compute_team_form("Manchester United", matches, last=10)
        assert form is not None
        assert len(form.entries) == 3
        newest = form.entries[0]  # newest first
        assert newest.opponent == "Arsenal"
        assert newest.venue == "H"
        assert newest.outcome == "D"
        away = form.entries[1]
        assert away.venue == "A"
        assert away.outcome == "L"
        assert form.summary["W"] == 1
        assert form.summary["D"] == 1
        assert form.summary["L"] == 1
        assert form.summary["points"] == 4

    def test_last_n_window(self):
        matches = [
            _m(dt.date(2026, 8, 1) + dt.timedelta(days=7 * i), "A", "Man United", 0, 1)
            for i in range(5)
        ]
        form = compute_team_form("Man United", matches, last=3)
        assert form is not None
        assert len(form.entries) == 3
        assert form.summary["matches"] == 3
        assert form.summary["W"] == 3

    def test_unknown_team_returns_none(self):
        assert compute_team_form("Nobody FC", [_m(D1, "A", "B", 1, 0)]) is None


class TestHeadToHead:
    def test_h2h_summary(self):
        matches = [
            _m(D1, "Arsenal", "Chelsea", 2, 0),  # A wins
            _m(D2, "Chelsea", "Arsenal", 1, 1),  # draw
            _m(D3, "Arsenal", "Liverpool", 5, 0),  # irrelevant
            _m(dt.date(2026, 8, 4), "Chelsea", "Arsenal", 0, 1),  # A wins away
        ]
        h2h = compute_head_to_head("Arsenal", "Chelsea", matches)
        assert h2h is not None
        assert h2h.summary["wins_a"] == 2
        assert h2h.summary["wins_b"] == 0
        assert h2h.summary["draws"] == 1
        assert h2h.summary["goals_a"] == 4
        assert h2h.summary["goals_b"] == 1
        assert len(h2h.matches) == 3
        assert h2h.matches[0]["date"] == "2026-08-04"  # newest first

    def test_no_meetings_returns_none(self):
        assert compute_head_to_head("Arsenal", "Everton", [_m(D1, "A", "B", 1, 0)]) is None

    def test_penalty_shootout_counts_as_final_win(self):
        """A tie decided on penalties is a WIN for the shootout winner;
        pen_wins_* disclose it; goals stay regulation-only."""
        shootout = _m(dt.date(2026, 1, 9), "Arsenal", "Everton", 2, 2)
        shootout.shootout_winner = "Arsenal"
        h2h = compute_head_to_head(
            "Arsenal", "Everton", [shootout, _m(dt.date(2026, 2, 1), "Everton", "Arsenal", 0, 3)]
        )
        assert h2h is not None
        assert h2h.summary["wins_a"] == 2  # shootout + regulation win
        assert h2h.summary["draws"] == 0  # no phantom draw
        assert h2h.summary["pen_wins_a"] == 1
        assert h2h.summary["pen_wins_b"] == 0
        assert h2h.summary["goals_a"] == 5  # 2 + 3 regulation goals only
        assert h2h.summary["goals_b"] == 2
        # the shootout row explains itself: winner A with a level score
        assert h2h.matches[-1]["winner"] == "A"  # oldest row = the shootout tie
        assert h2h.matches[-1]["score"] == "2-2"

    def test_shootout_winner_side_b(self):
        shootout = _m(D1, "Arsenal", "Everton", 1, 1)
        shootout.shootout_winner = "Everton"
        h2h = compute_head_to_head("Arsenal", "Everton", [shootout])
        assert h2h.summary["wins_b"] == 1
        assert h2h.summary["pen_wins_b"] == 1
        assert h2h.matches[0]["winner"] == "B"