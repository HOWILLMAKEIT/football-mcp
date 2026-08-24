"""In-memory MCP client tests for the tool layer (SDK v2 pattern).

v2 testing story: `async with Client(mcp)` connects straight to the server
object, no transport, no subprocess. The provider is injected with stub
sources so these tests stay offline. The client context is opened inside
each test (not a fixture) so anyio cancel scopes never cross tasks.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager

from mcp import Client

import football_mcp.server as server_module
from football_mcp.models import Match, OddsTriple
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider

TODAY = dt.date.today()
YESTERDAY = TODAY - dt.timedelta(days=1)
LAST_WEEK = TODAY - dt.timedelta(days=7)

DEFAULT_ROWS = [
    Match(
        competition="E0",
        season="2026-27",
        date=LAST_WEEK,
        home_team="Man United",
        away_team="Chelsea",
        home_goals=2,
        away_goals=1,
        result="H",
        market_avg_close=OddsTriple(home=1.4, draw=5.0, away=8.0),
    ),
    Match(
        competition="E0",
        season="2026-27",
        date=YESTERDAY,
        home_team="Arsenal",
        away_team="Everton",
        home_goals=1,
        away_goals=0,
        result="H",
    ),
]


class StubCsv:
    def __init__(self, matches) -> None:
        self._matches = matches

    async def get_season(self, competition, season):
        if competition.upper() not in {"E0"}:  # mimic real source validation
            raise DataSourceError(f"unsupported competition code: {competition!r}")
        return self._matches


class StubEspn:
    name = "espn"
    has_key = True

    async def get_fixtures(self, competition, season, date_from, date_to):
        return []

    def quota_remaining(self):
        return None


@asynccontextmanager
async def client_with(rows=None):
    server_module._provider = SeasonProvider(StubCsv(rows if rows else DEFAULT_ROWS), StubEspn())
    try:
        async with Client(server_module.mcp) as client:
            yield client
    finally:
        server_module._provider = None


async def _call(client, name, args):
    result = await client.call_tool(name, args)
    texts = [b.text for b in result.content if hasattr(b, "text")]
    assert texts, "tool returned no text content"
    return json.loads(texts[0])


class TestListCompetitions:
    async def test_lists_codes_and_names(self):
        async with client_with() as client:
            data = await _call(client, "list_competitions", {})
            assert data["E0"] == "England Premier League"
            assert data["SP1"] == "Spain La Liga"
            assert len(data) == 18


class TestGetMatches:
    async def test_basic_query_desc(self):
        async with client_with() as client:
            data = await _call(
                client,
                "get_matches",
                {"competition": "E0", "season": "2026-27", "only": "played"},
            )
        assert data["count"] == 2
        assert data["total_matching"] == 2
        assert data["matches"][0]["date"] == YESTERDAY.isoformat()  # desc order
        row = data["matches"][0]
        assert row["home"] == "Arsenal"
        assert row["score"] == "1-0"
        assert row["result"] == "H"
        assert "odds" not in row  # compact default: no odds

    async def test_team_filter_canonical(self):
        async with client_with() as client:
            data = await _call(
                client,
                "get_matches",
                {"competition": "E0", "season": "2026-27", "team": "Manchester United"},
            )
            assert data["count"] == 1
            none = await _call(
                client,
                "get_matches",
                {"competition": "E0", "season": "2026-27", "team": "Real Madrid"},
            )
        assert none["count"] == 0

    async def test_date_window(self):
        async with client_with() as client:
            data = await _call(
                client,
                "get_matches",
                {
                    "competition": "E0",
                    "season": "2026-27",
                    "date_from": LAST_WEEK.isoformat(),
                    "date_to": LAST_WEEK.isoformat(),
                },
            )
        assert data["count"] == 1
        assert data["matches"][0]["date"] == LAST_WEEK.isoformat()

    async def test_odds_gates(self):
        async with client_with() as client:
            with_odds = await _call(
                client,
                "get_matches",
                {
                    "competition": "E0",
                    "season": "2026-27",
                    "include_odds": True,
                    "date_from": LAST_WEEK.isoformat(),
                    "date_to": LAST_WEEK.isoformat(),
                },
            )
            odds = with_odds["matches"][0]["odds"]
            assert odds["market_avg_close"]["home"] == 1.4
            assert "pinnacle_open" not in odds  # detail off

            bare = await _call(
                client,
                "get_matches",
                {
                    "competition": "E0",
                    "season": "2026-27",
                    "date_from": YESTERDAY.isoformat(),
                    "date_to": YESTERDAY.isoformat(),
                },
            )
        assert "odds" not in bare["matches"][0]  # row without odds stays clean

    async def test_freshness_carried(self):
        async with client_with() as client:
            data = await _call(
                client, "get_matches", {"competition": "E0", "season": "2026-27"}
            )
        assert data["freshness"]["csv_latest_played"] == YESTERDAY.isoformat()

    async def test_limit_clamped(self):
        async with client_with() as client:
            data = await _call(
                client,
                "get_matches",
                {"competition": "E0", "season": "2026-27", "limit": 1},
            )
        assert data["count"] == 1
        assert data["total_matching"] == 2

    async def test_bad_params_raise_tool_error(self):
        async with client_with() as client:
            result = await client.call_tool(
                "get_matches", {"competition": "E0", "season": "2026-27", "only": "wat"}
            )
            assert result.is_error

    async def test_unknown_competition_raises_tool_error(self):
        async with client_with() as client:
            result = await client.call_tool(
                "get_matches", {"competition": "XX", "season": "2026-27"}
            )
            texts = [b.text for b in result.content if hasattr(b, "text")]
        assert result.is_error
        assert "unsupported competition" in texts[0]

    async def test_bad_season_raises_tool_error(self):
        async with client_with() as client:
            result = await client.call_tool(
                "get_matches", {"competition": "E0", "season": "banana"}
            )
        assert result.is_error
