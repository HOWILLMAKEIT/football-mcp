"""Offline tests: api-football source parsing, quota, cache; season provider."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from football_mcp.sources.api_football import ApiFootballSource, parse_fixtures
from football_mcp.sources.espn import EspnSource, parse_scoreboard
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider


def _payload(
    status: str = "FT",
    home: int | None = 2,
    away: int | None = 1,
    date: dt.date | None = None,
) -> dict:
    # Default fixture sits "last night": inside any gap window.
    day = date or dt.date.today() - dt.timedelta(days=1)
    return {
        "results": 1,
        "errors": [],
        "response": [
            {
                "fixture": {
                    "id": 1,
                    "date": f"{day.isoformat()}T19:00:00+00:00",
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
    """The fixture-aware freshness ladder, driven with stub sources."""

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

    def _played(self, date, home="Man United", away="Chelsea"):
        from football_mcp.models import Match

        return Match(
            competition="E0",
            season="2026-27",
            date=date,
            home_team=home,
            away_team=away,
            home_goals=1,
            away_goals=0,
            result="H",
        )

    def _fixture(self, date, home="Man United", away="Brighton", kick_off=None):
        from football_mcp.models import Match

        return Match(
            competition="E0",
            season="2026-27",
            date=date,
            kick_off=kick_off,
            home_team=home,
            away_team=away,
        )

    async def test_fresh_csv_skips_api(self, tmp_path):
        today = dt.date.today()
        base = [self._played(today)]
        provider = SeasonProvider(self._csv_stub(base), self._api(tmp_path, _payload()))
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is False
        assert result.freshness.warning is None
        assert result.freshness.quota_remaining is None

    async def test_break_weeks_waste_no_quota(self, tmp_path):
        """No matches during a break -> nothing overdue -> zero API calls."""
        base = [
            self._played(dt.date.today() - dt.timedelta(days=5)),
            self._fixture(dt.date.today() + dt.timedelta(days=3)),
        ]
        provider = SeasonProvider(self._csv_stub(base), self._api(tmp_path, _payload()))
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is False
        assert result.freshness.warning is None

    async def test_just_finished_match_detected_same_evening(self, tmp_path):
        """Kickoff + 2h with no result -> refresh immediately, same day."""
        kickoff = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=3)).strftime("%H:%M")
        base = [
            self._played(dt.date.today() - dt.timedelta(days=5)),
            self._fixture(dt.date.today(), kick_off=kickoff),
        ]
        provider = SeasonProvider(
            self._csv_stub(base), self._api(tmp_path, _payload(date=dt.date.today()))
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        # The overdue CSV row must be superseded by the API result row.
        today_rows = [m for m in result.matches if m.date == dt.date.today()]
        assert len(today_rows) == 1
        assert today_rows[0].played is True
        assert result.freshness.warning is None

    async def test_stale_csv_with_key_fills_gap(self, tmp_path):
        base = [
            self._played(dt.date.today() - dt.timedelta(days=3)),
            self._fixture(dt.date.today() - dt.timedelta(days=1)),
        ]
        provider = SeasonProvider(self._csv_stub(base), self._api(tmp_path, _payload()))
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        assert result.freshness.quota_remaining is not None
        # Old played row + overdue row replaced by the fresh API result.
        assert len(result.matches) == 2
        assert result.freshness.warning is None

    async def test_stale_csv_without_key_warns(self):
        base = [
            self._played(dt.date.today() - dt.timedelta(days=3)),
            self._fixture(dt.date.today() - dt.timedelta(days=1)),
        ]
        provider = SeasonProvider(self._csv_stub(base), None)
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is False
        assert result.freshness.warning is not None
        assert "enhancement source" in result.freshness.warning

    async def test_unpublished_season_served_purely_from_api(self, tmp_path):
        provider = SeasonProvider(
            self._csv_stub([], error=DataSourceError("season not published")),
            self._api(tmp_path, _payload()),
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        assert len(result.matches) == 1
        assert result.matches[0].home_team == "Manchester United"


def _espn_event(
    date_iso: str,
    state: str,
    completed: bool,
    home: str,
    away: str,
    home_score: str,
    away_score: str,
) -> dict:
    return {
        "id": "1",
        "date": date_iso,
        "name": f"{home} vs {away}",
        "status": {"type": {"state": state, "completed": completed, "detail": "FT"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"displayName": home},
                        "score": home_score,
                    },
                    {
                        "homeAway": "away",
                        "team": {"displayName": away},
                        "score": away_score,
                    },
                ]
            }
        ],
    }


class TestEspnSource:
    """Parsing and caching for the keyless ESPN scoreboard source."""

    def _payload(self) -> dict:
        yesterday = dt.date.today() - dt.timedelta(days=1)
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        return {
            "events": [
                _espn_event(
                    f"{yesterday}T19:00Z", "post", True,
                    "Arsenal", "Coventry City", "3", "0",
                ),
                # Real-data trap: pre matches already carry score "0".
                _espn_event(
                    f"{tomorrow}T19:00Z", "pre", False,
                    "Fulham", "Chelsea", "0", "0",
                ),
            ]
        }

    def test_parse_finished_and_pre(self):
        matches = parse_scoreboard(self._payload(), "E0", "2026-27")
        assert len(matches) == 2
        played, pre = matches
        assert played.played is True
        assert played.home_goals == 3
        assert played.result == "H"
        assert played.kick_off == "19:00"
        assert pre.played is False
        assert pre.home_goals is None  # the "0" score must NOT become a draw
        assert pre.home_team == "Fulham"

    async def test_get_fixtures_parses_and_caches(self, tmp_path):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=self._payload())

        src = EspnSource(
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert src.has_key is True
        assert src.quota_remaining() is None
        d1, d2 = dt.date(2026, 8, 21), dt.date(2026, 8, 24)
        matches = await src.get_fixtures("E0", "2026-27", d1, d2)
        assert len(matches) == 2
        await src.get_fixtures("E0", "2026-27", d1, d2)  # cached
        assert len(calls) == 1

    async def test_unknown_slug_raises(self, tmp_path):
        src = EspnSource(cache_dir=tmp_path)
        with pytest.raises(DataSourceError, match="slug"):
            await src.get_fixtures("XX", "2026-27", dt.date(2026, 8, 1), dt.date(2026, 8, 2))

    async def test_provider_with_espn_serves_unpublished_season(self, tmp_path):
        """The exact production situation of E0 2026-27 today: no CSV at all."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._payload())

        class CsvUnavailable:
            async def get_season(self, competition, season):
                raise DataSourceError("season not published")

        provider = SeasonProvider(
            CsvUnavailable(),
            EspnSource(
                cache_dir=tmp_path,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ),
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        assert result.freshness.enhancement_name == "espn"
        assert result.freshness.quota_remaining is None
        assert result.freshness.warning is None
        assert len(result.matches) == 2  # yesterday's result + tomorrow's fixture
