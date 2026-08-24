"""Cup competitions: two-window season fetch, notes, and MCP tools."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager

import httpx
from mcp import Client

import football_mcp.server as server_module
from football_mcp.sources.espn import CUP_SLUGS, EspnSource, parse_scoreboard
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider


def _event(day: str, home: str, away: str, hs: str, as_: str,
           completed: bool, note: str | None = None) -> dict:
    notes = [{"headline": note}] if note else []
    return {
        "id": day + home,
        "date": f"{day}T19:00Z",
        "name": f"{home} at {away}",
        "status": {"type": {"state": "post" if completed else "pre",
                            "completed": completed}},
        "competitions": [{
            "notes": notes,
            "competitors": [
                {"homeAway": "home", "team": {"displayName": home}, "score": hs},
                {"homeAway": "away", "team": {"displayName": away}, "score": as_},
            ],
        }],
    }


class TestParseNote:
    def test_note_extracted(self):
        events = [_event("2026-01-09", "Oxford United", "Milton Keynes Dons",
                         "2", "2", True, "Oxford United advance 4-3 on penalties")]
        matches = parse_scoreboard({"events": events}, "FA", "2025-26")
        m = matches[0]
        assert m.played is True
        assert m.result == "D"  # regulation draw...
        assert m.note == "Oxford United advance 4-3 on penalties"  # ...settled on pens
        assert m.shootout_winner == "Oxford United"

    def test_leg_note_has_no_shootout_winner(self):
        events = [_event("2026-04-29", "Atlético Madrid", "Arsenal",
                         "1", "1", True, "1st Leg")]
        matches = parse_scoreboard({"events": events}, "UCL", "2025-26")
        assert matches[0].shootout_winner is None

    def test_leg_penalty_note_parses(self):
        events = [_event("2026-02-11", "A", "B", "1", "1", True,
                         "2nd Leg - Ac Milan advance 4-3 on penalties")]
        matches = parse_scoreboard({"events": events}, "UCL", "2025-26")
        assert matches[0].shootout_winner == "Ac Milan"


class TestCupSeason:
    async def test_two_windows_merged_and_deduped(self, tmp_path):
        """Aug..Jan and Feb..Jul halves are merged; boundary dupes removed."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            dates = request.url.params["dates"]
            calls.append(dates)
            from_, to_ = dates.split("-")
            window = (
                dt.datetime.strptime(from_, "%Y%m%d").date(),
                dt.datetime.strptime(to_, "%Y%m%d").date(),
            )

            def in_window(event: dict) -> bool:
                day = dt.date.fromisoformat(event["date"][:10])
                return window[0] <= day <= window[1]

            if dates.startswith("20250801"):
                events = [
                    _event("2025-08-09", "A", "B", "1", "0", True),
                    _event("2026-01-30", "C", "D", "2", "2", True, "C advance"),
                ]
            else:
                events = [
                    # same match re-served across the boundary -> deduped
                    _event("2026-01-30", "C", "D", "2", "2", True, "C advance"),
                    _event("2026-05-16", "E", "F", "0", "1", True),
                ]
            return httpx.Response(200, json={"events": [e for e in events if in_window(e)]})

        src = EspnSource(cache_dir=tmp_path,
                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        matches = await src.get_cup_season("FA", "2025-26")
        assert sorted(calls) == ["20250801-20260131", "20260201-20260731"]
        assert len(matches) == 3  # dupe removed
        assert matches[0].date == dt.date(2025, 8, 9)  # sorted ascending
        by_date = {m.date: m for m in matches}
        assert by_date[dt.date(2026, 1, 30)].note == "C advance"

    async def test_unknown_cup_code(self, tmp_path):
        src = EspnSource(cache_dir=tmp_path)
        try:
            await src.get_cup_season("XX", "2025-26")
        except DataSourceError as exc:
            assert "list_cup_competitions" in str(exc)
        else:
            raise AssertionError("expected DataSourceError")

    async def test_bad_season_label(self, tmp_path):
        src = EspnSource(cache_dir=tmp_path)
        try:
            await src.get_cup_season("FA", "banana")
        except DataSourceError:
            pass
        else:
            raise AssertionError("expected DataSourceError")


class StubCsv:
    async def get_season(self, competition, season):
        raise DataSourceError("no csv for cups")


class StubEspn:
    name = "espn"
    has_key = True

    async def get_fixtures(self, *a, **k):
        return []

    def quota_remaining(self):
        return None


@asynccontextmanager
async def cup_client(tmp_path, events_aug_jan=None, events_feb_jul=None):
    def handler(request: httpx.Request) -> httpx.Response:
        dates = request.url.params["dates"]
        if dates.startswith("20250801"):
            events = list(events_aug_jan or [])
        else:
            events = list(events_feb_jul or [])
        from_, to_ = dates.split("-")
        window = (
            dt.datetime.strptime(from_, "%Y%m%d").date(),
            dt.datetime.strptime(to_, "%Y%m%d").date(),
        )
        kept = [
            e for e in events
            if window[0] <= dt.date.fromisoformat(e["date"][:10]) <= window[1]
        ]
        return httpx.Response(200, json={"events": kept})

    espn = EspnSource(
        cache_dir=tmp_path,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    server_module._provider = SeasonProvider(StubCsv(), StubEspn())
    # cup tools read the espn source directly
    server_module._provider.enhancement = espn
    try:
        async with Client(server_module.mcp) as client:
            yield client
    finally:
        server_module._provider = None


class TestCupTools:
    async def test_list_cup_competitions(self, tmp_path):
        async with cup_client(tmp_path) as client:
            result = await client.call_tool("list_cup_competitions", {})
            data = json.loads([b.text for b in result.content if hasattr(b, "text")][0])
        assert data["FA"] == "England FA Cup"
        assert data["UCL"] == "UEFA Champions League"
        assert len(data) == len(CUP_SLUGS)

    async def test_get_cup_matches_filters_and_notes(self, tmp_path):
        aug_jan = [
            _event("2025-08-09", "Man City", "Gillingham", "3", "0", True),
            _event("2025-12-05", "Arsenal", "Everton", "1", "1", True,
                   "Arsenal advance 5-4 on penalties"),
            _event("2026-01-09", "Liverpool", "Accrington", "4", "0", True),
        ]
        feb_jul = [
            _event("2026-05-16", "Crystal Palace", "Man City", "1", "0", True),
            _event("2026-08-01", "X", "Y", "0", "0", False),  # next season, out of span
        ]
        async with cup_client(tmp_path, aug_jan, feb_jul) as client:
            result = await client.call_tool(
                "get_cup_matches",
                {"competition": "FA", "season": "2025-26", "team": "Man City"},
            )
            data = json.loads([b.text for b in result.content if hasattr(b, "text")][0])
        assert data["count"] == 2  # two City ties, newest first
        assert data["matches"][0]["date"] == "2026-05-16"
        assert data["total_matching"] == 2
        assert data["source"] == "espn"
        assert data["latest_played"] == "2026-05-16"

        async with cup_client(tmp_path, aug_jan, feb_jul) as client:
            result = await client.call_tool(
                "get_cup_matches", {"competition": "FA", "season": "2025-26"}
            )
            all_result_matches = json.loads(
                [b.text for b in result.content if hasattr(b, "text")][0]
            )
        # span filter keeps the season's ties only (out-of-span 2026-08-01 dropped)
        assert all_result_matches["count"] == 4
        notes = [m.get("note") for m in all_result_matches["matches"]]
        assert "Arsenal advance 5-4 on penalties" in notes
