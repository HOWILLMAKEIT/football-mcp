"""Elicitation behaviour of the scoped head-to-head tool.

Three paths, all through the in-memory v2 Client:
1. Model passes scope explicitly -> resolver answers directly, no question.
2. Model omits scope -> the user is asked; the answer picks the scope.
3. User declines -> safe fallback to "league" with an honest scope_note.
"""

from __future__ import annotations

import datetime as dt
import json

from mcp import Client
from mcp.types import ElicitResult

import football_mcp.server as server_module
from football_mcp.models import Match
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider

D = dt.date(2026, 8, 1)
LEAGUE_ROWS = [
    Match(competition="SP1", season="2025-26", date=D,
          home_team="Barcelona", away_team="Real Madrid",
          home_goals=2, away_goals=1, result="H"),
]


class StubCsv:
    def __init__(self) -> None:
        self._by_season = {"2025-26": LEAGUE_ROWS}

    async def get_season(self, competition, season):
        if competition.upper() != "SP1":
            raise DataSourceError(f"unsupported competition code: {competition!r}")
        if season not in self._by_season:
            raise DataSourceError("not published")
        return self._by_season[season]


def _cup_event(day: str, home: str, away: str, hs: str, as_: str) -> dict:
    return {
        "id": day + home,
        "date": f"{day}T20:00Z",
        "name": f"{home} at {away}",
        "status": {"type": {"state": "post", "completed": True}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"displayName": home}, "score": hs},
            {"homeAway": "away", "team": {"displayName": away}, "score": as_},
        ]}],
    }


class StubEspn:
    """ESPN-shaped stub: only get_cup_season is real; leagues unused."""

    name = "espn"
    has_key = True

    async def get_fixtures(self, *a, **k):
        return []

    def quota_remaining(self):
        return None

    async def get_cup_season(self, competition, season):
        if season != "2025-26":
            raise DataSourceError("cup season unavailable")
        if competition == "CDR":
            return [
                Match(competition="CDR", season=season, date=dt.date(2026, 1, 12),
                      home_team="Real Madrid", away_team="Barcelona",
                      home_goals=2, away_goals=4, result="A"),
            ]
        if competition == "UCL":
            return [
                Match(competition="UCL", season=season, date=dt.date(2026, 4, 28),
                      home_team="Barcelona", away_team="Real Madrid",
                      home_goals=3, away_goals=0, result="H"),
            ]
        return []  # UEL and everything else: valid empty season


def _asked_scopes() -> list:
    return []


async def _run_h2h(client_args: dict, args: dict):
    """Call get_head_to_head with a given elicitation callback config."""
    server_module._provider = SeasonProvider(StubCsv(), StubEspn())
    try:
        async with Client(server_module.mcp, **client_args) as client:
            result = await client.call_tool("get_head_to_head", args)
            texts = [b.text for b in result.content if hasattr(b, "text")]
            data = None
            if texts and not result.is_error:
                try:
                    data = json.loads(texts[0])
                except json.JSONDecodeError:
                    data = None
            return result, data
    finally:
        server_module._provider = None


class TestScopeElicitation:
    async def test_explicit_scope_never_asks(self):
        # No elicitation callback registered: a question would error out.
        result, data = await _run_h2h(
            {"raise_exceptions": False},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26", "scope": "league"},
        )
        assert not result.is_error
        assert data["scope"] == "league"
        assert data["summary"]["matches"] == 1
        assert "scope_note" not in data

    async def test_question_asked_and_answered(self):
        asked: list[str] = []

        async def answer(ctx, params):
            asked.append(params.message)
            return ElicitResult(action="accept", content={"scope": "domestic_cups"})

        result, data = await _run_h2h(
            {"elicitation_callback": answer},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26"},
        )
        assert not result.is_error
        assert asked, "resolver should have asked the user"
        assert "Barcelona" in asked[0]
        assert data["scope"] == "domestic_cups"
        # domestic cup meeting served from the ESPN stub (Real Madrid 2-4 Barca)
        assert data["summary"]["wins_a"] == 1
        assert data["by_scope"]["domestic_cups"]["matches"] == 1

    async def test_decline_falls_back_to_league(self):
        async def decline(ctx, params):
            return ElicitResult(action="decline")

        result, data = await _run_h2h(
            {"elicitation_callback": decline},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26"},
        )
        assert not result.is_error
        assert data["scope"] == "league"
        assert "scope_note" in data and "defaulted" in data["scope_note"]

    async def test_scope_all_merges_league_and_cups(self):
        result, data = await _run_h2h(
            {"raise_exceptions": False},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26", "scope": "all"},
        )
        assert not result.is_error
        assert data["summary"]["matches"] == 3  # league 1 + CDR 1 + UCL 1
        assert data["by_scope"]["league"]["matches"] == 1
        assert data["by_scope"]["domestic_cups"]["matches"] == 1
        assert data["by_scope"]["europe"]["matches"] == 1
        # per-season entries carry competition labels
        comps = {entry["competition"] for entry in data["per_season"]}
        assert comps == {"SP1", "CDR", "UCL"}

    async def test_concrete_cup_scope_selects_only_that_cup(self):
        result, data = await _run_h2h(
            {"raise_exceptions": False},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26", "scope": "UCL"},
        )
        assert not result.is_error
        assert data["scope"] == "UCL"
        assert data["summary"]["matches"] == 1  # only the UCL meeting
        assert data["summary"]["wins_a"] == 1   # Barcelona 3-0
        assert "CDR" not in data["by_competition"]
        assert "SP1" not in data["by_competition"]

    async def test_by_competition_distinguishes_ucl_and_uel(self):
        result, data = await _run_h2h(
            {"raise_exceptions": False},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26", "scope": "europe"},
        )
        assert not result.is_error
        # europe bucket rolls up, by_competition keeps them apart
        assert data["by_scope"]["europe"]["matches"] == 1
        assert data["by_competition"]["UCL"]["matches"] == 1
        assert data["by_competition"]["UCL"]["name"] == "UEFA Champions League"
        assert "UEL" not in data["by_competition"]  # no meetings -> no entry

    async def test_invalid_scope_is_a_tool_error(self):
        result, data = await _run_h2h(
            {"raise_exceptions": False},
            {"team_a": "Barcelona", "team_b": "Real Madrid",
             "competition": "SP1", "season": "2025-26", "scope": "banana"},
        )
        assert result.is_error
        texts = [b.text for b in result.content if hasattr(b, "text")]
        assert "invalid scope" in texts[0]
