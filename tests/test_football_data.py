"""Offline tests for the football-data source: parsing, seasons, cache."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from football_mcp.sources.football_data import (
    DataSourceError,
    FootballDataSource,
    current_season_label,
    is_past_season,
    parse_date,
    parse_season_csv,
    season_to_code,
)

# Minimal but realistic CSV: BOM + subset of real columns; one played match
# with odds, one future fixture with blanks.
SAMPLE_CSV = (
    "\ufeffDiv,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,"
    "PSH,PSD,PSA,AvgCH,AvgCD,AvgCA,AvgC>2.5,AvgC<2.5,AHCh,AvgCAHH,AvgCAHA\n"
    "E0,15/08/2025,20:00,Liverpool,Bournemouth,4,2,H,19,10,"
    "1.28,6.56,9.07,1.31,6.6,9.5,3.2,1.41,-1.75,2.03,1.78\n"
    "E0,22/08/2025,20:00,Arsenal,Chelsea,,,,,,"
    ",,,1.5,4.8,6.2,,,1.0,1.95,1.95\n"
)


class TestParsing:
    def test_parses_played_match(self):
        matches = parse_season_csv(SAMPLE_CSV, "E0", "2025-26")
        assert len(matches) == 2
        played = matches[0]
        assert played.home_team == "Liverpool"
        assert played.away_team == "Bournemouth"
        assert played.home_goals == 4
        assert played.away_goals == 2
        assert played.result == "H"
        assert played.home_shots == 19
        assert played.away_shots == 10
        assert played.date == dt.date(2025, 8, 15)
        assert played.kick_off == "20:00"
        assert played.played is True

    def test_parses_odds_blocks(self):
        played = parse_season_csv(SAMPLE_CSV, "E0", "2025-26")[0]
        assert played.pinnacle_open is not None
        assert played.pinnacle_open.home == 1.28
        assert played.pinnacle_open.away == 9.07
        assert played.market_avg_close is not None
        assert played.market_avg_close.home == 1.31
        assert played.market_avg_close_over25 == 3.2
        assert played.ah_close_line == -1.75
        # no closing pinnacle columns in the sample -> absent
        assert played.pinnacle_close is None

    def test_future_fixture_has_teams_but_no_score(self):
        fixture = parse_season_csv(SAMPLE_CSV, "E0", "2025-26")[1]
        assert fixture.home_team == "Arsenal"
        assert fixture.away_team == "Chelsea"
        assert fixture.home_goals is None
        assert fixture.played is False
        assert fixture.market_avg_close is not None  # odds may exist pre-match

    def test_missing_columns_tolerated(self):
        header = "\ufeffDiv,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        row = "E0,15/08/2025,Liverpool,Bournemouth,4,2,H\n"
        match = parse_season_csv(header + row, "E0", "2025-26")[0]
        assert match.home_goals == 4
        assert match.home_shots is None
        assert match.referee is None

    def test_old_date_format(self):
        assert parse_date("15/08/99") == dt.date(1999, 8, 15)
        assert parse_date("15/08/2025") == dt.date(2025, 8, 15)
        assert parse_date("") is None


class TestSeasonHelpers:
    def test_current_season_july_starts_new(self):
        assert current_season_label(dt.date(2026, 7, 17)) == "2026-27"
        assert current_season_label(dt.date(2026, 2, 1)) == "2025-26"

    def test_season_codes(self):
        assert season_to_code("2025-26") == "2526"
        assert season_to_code("1993-94") == "9394"
        with pytest.raises(DataSourceError):
            season_to_code("2025-27")
        with pytest.raises(DataSourceError):
            season_to_code("banana")

    def test_past_vs_current(self):
        today = dt.date(2026, 7, 17)
        assert is_past_season("2025-26", today) is True
        assert is_past_season("2026-27", today) is False


class TestCacheAndFreshness:
    async def test_downloads_once_then_304(self, tmp_path):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if "If-Modified-Since" in request.headers:
                return httpx.Response(304)
            return httpx.Response(
                200,
                text=SAMPLE_CSV,
                headers={"Last-Modified": "Sat, 16 Aug 2025 14:00:00 GMT"},
            )

        source = FootballDataSource(
            cache_dir=tmp_path,
            ttl_seconds=0.0,  # always stale -> exercise conditional path
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        first = await source.ensure_csv("E0", "2026-27")
        assert first.exists()
        assert "If-Modified-Since" not in calls[0].headers

        # Force apparent staleness by making meta old, then re-request.
        await source.ensure_csv("E0", "2026-27")
        assert len(calls) == 2
        assert "If-Modified-Since" in calls[1].headers
        assert calls[1].headers["If-Modified-Since"] == "Sat, 16 Aug 2025 14:00:00 GMT"

    async def test_past_season_never_hits_network(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("past season should not trigger a download")

        source = FootballDataSource(
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        # Seed the cache with a past-season file + meta.
        path = source._csv_path("E0", "2024-25")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SAMPLE_CSV, encoding="utf-8")
        source._write_meta(path, None)

        got = await source.ensure_csv("E0", "2024-25")
        assert got == path
        matches = await source.get_season("E0", "2024-25")
        assert len(matches) == 2

    async def test_ttl_keeps_cache_without_network(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("fresh cache should not trigger a download")

        source = FootballDataSource(
            cache_dir=tmp_path,
            ttl_seconds=3600.0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        path = source._csv_path("E0", "2026-27")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SAMPLE_CSV, encoding="utf-8")
        source._write_meta(path, None)
        await source.ensure_csv("E0", "2026-27")  # within TTL: no request fired

    async def test_404_raises_clear_error(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        source = FootballDataSource(
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(DataSourceError, match="not published"):
            await source.ensure_csv("E1", "2050-51")
