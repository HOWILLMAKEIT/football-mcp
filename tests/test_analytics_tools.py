"""In-memory tests for the analytics tools (list_teams/standings/form/h2h)."""

from __future__ import annotations

import datetime as dt
import json

from mcp import Client

import football_mcp.server as server_module
from football_mcp.models import Match
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider

D = dt.date(2026, 8, 1)
ROWS = [
    Match(competition="E0", season="2026-27", date=D, home_team="Man United",
          away_team="Chelsea", home_goals=2, away_goals=1, result="H"),
    Match(competition="E0", season="2026-27", date=D + dt.timedelta(days=7),
          home_team="Arsenal", away_team="Everton", home_goals=0, away_goals=0, result="D"),
    Match(competition="E0", season="2026-27", date=D + dt.timedelta(days=14),
          home_team="Chelsea", away_team="Arsenal", home_goals=1, away_goals=3, result="A"),
]


class StubCsv:
    async def get_season(self, competition, season):
        if competition.upper() != "E0":
            raise DataSourceError(f"unsupported competition code: {competition!r}")
        return ROWS


class StubEspn:
    name = "espn"
    has_key = True

    async def get_fixtures(self, *a, **k):
        return []

    def quota_remaining(self):
        return None


async def _call(client, name, args):
    result = await client.call_tool(name, args)
    texts = [b.text for b in result.content if hasattr(b, "text")]
    assert texts
    return json.loads(texts[0])


class TestListTeams:
    async def test_lists_names_and_counts(self):
        server_module._provider = SeasonProvider(StubCsv(), StubEspn())
        try:
            async with Client(server_module.mcp) as client:
                data = await _call(client, "list_teams", {"competition": "E0", "season": "2026-27"})
        finally:
            server_module._provider = None
        assert set(data["teams"]) == {"Man United", "Chelsea", "Arsenal", "Everton"}
        assert data["counts"]["Chelsea"] == 2


class TestStandings:
    async def test_table_with_as_of(self):
        server_module._provider = SeasonProvider(StubCsv(), StubEspn())
        try:
            async with Client(server_module.mcp) as client:
                full = await _call(
                    client, "get_standings", {"competition": "E0", "season": "2026-27"}
                )
                early = await _call(
                    client,
                    "get_standings",
                    {"competition": "E0", "season": "2026-27",
                     "as_of_date": (D + dt.timedelta(days=7)).isoformat()},
                )
        finally:
            server_module._provider = None
        # full: Arsenal 4, ManU 3, Chelsea 0, Everton 1
        assert full["rows"][0]["team"] == "Arsenal"
        assert full["rows"][0]["points"] == 4
        assert len(full["rows"]) == 4
        # as_of after matchday 2: only 2 matches count
        by_team = {r["team"]: r for r in early["rows"]}
        assert by_team["Arsenal"]["played"] == 1
        assert by_team["Man United"]["points"] == 3


class TestTeamForm:
    async def test_form_loose_name(self):
        server_module._provider = SeasonProvider(StubCsv(), StubEspn())
        try:
            async with Client(server_module.mcp) as client:
                data = await _call(
                    client,
                    "get_team_form",
                    {"team": "Manchester United", "competition": "E0", "season": "2026-27"},
                )
                err = await client.call_tool(
                    "get_team_form",
                    {"team": "Nobody", "competition": "E0", "season": "2026-27"},
                )
        finally:
            server_module._provider = None
        assert data["summary"]["W"] == 1
        assert data["entries"][0]["opponent"] == "Chelsea"
        assert err.is_error


class TestHeadToHead:
    async def test_h2h(self):
        server_module._provider = SeasonProvider(StubCsv(), StubEspn())
        try:
            async with Client(server_module.mcp) as client:
                data = await _call(
                    client,
                    "get_head_to_head",
                    {"team_a": "Arsenal", "team_b": "Chelsea",
                     "competition": "E0", "season": "2026-27"},
                )
                err = await client.call_tool(
                    "get_head_to_head",
                    {"team_a": "Chelsea", "team_b": "Everton",
                     "competition": "E0", "season": "2026-27"},
                )
        finally:
            server_module._provider = None
        assert data["summary"]["wins_a"] == 1
        assert len(data["matches"]) == 1
        assert err.is_error
