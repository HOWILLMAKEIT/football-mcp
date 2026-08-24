"""Offline tests: api-football source parsing, quota, cache; season provider."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from football_mcp.sources.api_football import ApiFootballSource, parse_fixtures
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider


def _payload(status: str = "FT", home: int | None = 2, away: int | None = 1) -> dict:
    # Fixture sits "last night": inside any gap window the provider computes.
    last_night = dt.date.today() - dt.timedelta(days=1)
    return {
        "results": 1,
        "errors": [],
        "response": [
            {
                "fixture": {
                    "id": 1,
                    "date": f"{last_night.isoformat()}T19:00:00+00:00",
                    "status": {"short": status},
                },
                "teams": {
                    "home": {"id": 33, "name": "Manchester United"},
                    "away": {"id": 135, "name": "Brighton and Hove Albion"},
                },
                "goals": {"home": home, "away": away},
            }
        ],
    }


class TestApiFootballParsing:
    def test_finished_fixture(self):
        matches = parse_fixtures(_payload(), "E0", "2026-27")
        assert len(matches) == 1
        m = matches[0]
        assert m.home_team == "Manchester United"
        assert m.home_goals == 2
        assert m.result == "H"
        assert m.date == dt.date.today() - dt.timedelta(days=1)
        assert m.kick_off == "19:00"
        assert m.played is True

    def test_not_started_fixture(self):
        matches = parse_fixtures(_payload(status="NS", home=None, away=None), "E0", "2026-27")
        assert matches[0].played is False
        assert matches[0].home_goals is None

    def test_api_errors_raise(self):
        with pytest.raises(DataSourceError, match="errors"):
            parse_fixtures({"errors": {"key": "invalid"}}, "E0", "2026-27")


class TestApiFootballQuotaAndCache:
    async def test_network_call_bumps_quota_then_cache_is_free(self, tmp_path):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=_payload())

        src = ApiFootballSource(
            api_key="test-key",
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        d1, d2 = dt.date(2026, 8, 15), dt.date(2026, 8, 16)
        await src.get_fixtures("E0", "2026-27", d1, d2)
        assert src.quota_count() == 1
        await src.get_fixtures("E0", "2026-27", d1, d2)  # cached
        assert src.quota_count() == 1
        assert len(calls) == 1

    async def test_quota_limit_refuses(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload())

        src = ApiFootballSource(
            api_key="k",
            cache_dir=tmp_path,
            quota_limit=0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(DataSourceError, match="quota"):
            await src.get_fixtures("E0", "2026-27", dt.date(2026, 8, 1), dt.date(2026, 8, 2))

    async def test_no_key_raises(self, tmp_path):
        src = ApiFootballSource(api_key=None, cache_dir=tmp_path)
        assert src.has_key is False
        with pytest.raises(DataSourceError, match="key"):
            await src.get_fixtures("E0", "2026-27", dt.date(2026, 8, 1), dt.date(2026, 8, 2))

    async def test_429_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        src = ApiFootballSource(
            api_key="k",
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(DataSourceError, match="429"):
            await src.get_fixtures("E0", "2026-27", dt.date(2026, 8, 1), dt.date(2026, 8, 2))


class TestSeasonProvider:
    """The freshness ladder, driven with stub sources."""

    def _csv_stub(self, matches, error: Exception | None = None):
        class Stub:
            async def get_season(self, competition, season):
                if error is not None:
                    raise error
                return matches

        return Stub()

    def _api(self, tmp_path, payload: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        return ApiFootballSource(
            api_key="k",
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def _played(self, date, home="Man United"):
        from football_mcp.models import Match

        return Match(
            competition="E0",
            season="2026-27",
            date=date,
            home_team=home,
            away_team="Chelsea",
            home_goals=1,
            away_goals=0,
            result="H",
        )

    async def test_fresh_csv_skips_api(self, tmp_path):
        from football_mcp.models import Match

        today = dt.date.today()
        fresh = Match(
            competition="E0",
            season="2026-27",
            date=today,
            home_team="Man United",
            away_team="Chelsea",
            home_goals=1,
            away_goals=0,
            result="H",
        )
        provider = SeasonProvider(self._csv_stub([fresh]), self._api(tmp_path, _payload()))
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.api_used is False
        assert result.freshness.warning is None
        assert result.freshness.quota_remaining is None

    async def test_stale_csv_with_key_fills_gap(self, tmp_path):
        stale_date = dt.date.today() - dt.timedelta(days=3)
        provider = SeasonProvider(
            self._csv_stub([self._played(stale_date)]), self._api(tmp_path, _payload())
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.api_used is True
        assert result.freshness.quota_remaining is not None
        # Two matches: stale CSV one + fresh API one (deduped by names).
        assert len(result.matches) == 2
        assert result.freshness.warning is None

    async def test_stale_csv_without_key_warns(self):
        stale_date = dt.date.today() - dt.timedelta(days=3)
        provider = SeasonProvider(self._csv_stub([self._played(stale_date)]), None)
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.api_used is False
        assert result.freshness.warning is not None
        assert "stale" in result.freshness.warning

    async def test_unpublished_season_served_purely_from_api(self, tmp_path):
        provider = SeasonProvider(
            self._csv_stub([], error=DataSourceError("season not published")),
            self._api(tmp_path, _payload()),
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.api_used is True
        assert len(result.matches) == 1
        assert result.matches[0].home_team == "Manchester United"
