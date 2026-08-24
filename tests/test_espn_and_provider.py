"""Offline tests: espn source parsing, caching; season provider ladder."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from football_mcp.models import Match
from football_mcp.sources.espn import EspnSource, parse_scoreboard
from football_mcp.sources.football_data import DataSourceError
from football_mcp.sources.season_provider import SeasonProvider


def _espn_event(
    date_iso: str,
    state: str,
    completed: bool,
    home: str,
    away: str,
    home_score: str,
    away_score: str,
    with_stats: bool = False,
) -> dict:
    home_stats = [
        {"name": "totalShots", "displayValue": "20"},
        {"name": "shotsOnTarget", "displayValue": "6"},
        {"name": "wonCorners", "displayValue": "8"},
        {"name": "foulsCommitted", "displayValue": "10"},
        {"name": "goalAssists", "displayValue": "2"},
        {"name": "possessionPct", "displayValue": "64.5"},
    ]
    away_stats = [
        {"name": "totalShots", "displayValue": "10"},
        {"name": "shotsOnTarget", "displayValue": "3"},
        {"name": "wonCorners", "displayValue": "2"},
        {"name": "foulsCommitted", "displayValue": "12"},
        {"name": "goalAssists", "displayValue": "0"},
        {"name": "possessionPct", "displayValue": "35.5"},
    ]
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
                        "statistics": home_stats if with_stats else [],
                    },
                    {
                        "homeAway": "away",
                        "team": {"displayName": away},
                        "score": away_score,
                        "statistics": away_stats if with_stats else [],
                    },
                ]
            }
        ],
    }


def _ft_event(date: dt.date, home="Arsenal", away="Coventry City") -> dict:
    return _espn_event(
        f"{date.isoformat()}T19:00Z", "post", True, home, away, "3", "0", with_stats=True
    )


def _pre_event(date: dt.date, home="Fulham", away="Chelsea") -> dict:
    # Real-data trap: pre matches already carry score "0".
    return _espn_event(
        f"{date.isoformat()}T19:00Z", "pre", False, home, away, "0", "0", with_stats=True
    )


class TestEspnParsing:
    def test_finished_and_pre(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        matches = parse_scoreboard(
            {"events": [_ft_event(yesterday), _pre_event(tomorrow)]}, "E0", "2026-27"
        )
        assert len(matches) == 2
        played, pre = matches
        assert played.played is True
        assert played.home_goals == 3
        assert played.result == "H"
        assert played.kick_off == "19:00"
        assert pre.played is False
        assert pre.home_goals is None  # the "0" score must NOT become a draw
        assert pre.home_team == "Fulham"

    def test_statistics_align_with_match_fields(self):
        """ESPN stat names land on the same fields CSV columns populate."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        matches = parse_scoreboard({"events": [_ft_event(yesterday)]}, "E0", "2026-27")
        played = matches[0]
        # aligned with CSV semantics (HS/AS, HST/AST, HC/AC, HF/AF)
        assert played.home_shots == 20
        assert played.home_shots_on_target == 6
        assert played.home_corners == 8
        assert played.home_fouls == 10
        # ESPN-only fields
        assert played.home_possession == 64.5
        assert played.away_possession == 35.5
        assert played.home_assists == 2

    def test_partial_stats_on_pre_never_leak(self):
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        matches = parse_scoreboard({"events": [_pre_event(tomorrow)]}, "E0", "2026-27")
        pre = matches[0]
        assert pre.home_possession is None
        assert pre.home_shots is None

    def test_malformed_rows_skipped(self):
        yesterday = dt.date.today() - dt.timedelta(days=1)
        events = [_ft_event(yesterday), {"id": "2", "date": "garbage"}]
        matches = parse_scoreboard({"events": events}, "E0", "2026-27")
        assert len(matches) == 1


class TestEspnSource:
    """Caching and error behaviour of the keyless ESPN source."""

    def _source(self, tmp_path, events) -> EspnSource:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"events": events})

        return EspnSource(
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    async def test_parses_and_caches(self, tmp_path):
        calls: list[int] = []
        yesterday = dt.date.today() - dt.timedelta(days=1)

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"events": [_ft_event(yesterday)]})

        src = EspnSource(
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert src.has_key is True
        assert src.quota_remaining() is None
        d1, d2 = dt.date(2026, 8, 21), dt.date(2026, 8, 24)
        matches = await src.get_fixtures("E0", "2026-27", d1, d2)
        assert len(matches) == 1
        await src.get_fixtures("E0", "2026-27", d1, d2)  # cached
        assert len(calls) == 1

    async def test_unknown_slug_raises(self, tmp_path):
        src = EspnSource(cache_dir=tmp_path)
        with pytest.raises(DataSourceError, match="slug"):
            await src.get_fixtures(
                "XX", "2026-27", dt.date(2026, 8, 1), dt.date(2026, 8, 2)
            )

    async def test_inverted_window_returns_empty(self, tmp_path):
        src = self._source(tmp_path, [])
        matches = await src.get_fixtures(
            "E0", "2026-27", dt.date(2026, 8, 5), dt.date(2026, 8, 1)
        )
        assert matches == []


class TestSeasonProvider:
    """The fixture-aware freshness ladder, driven with stub sources."""

    def _csv_stub(self, matches, error: Exception | None = None):
        class Stub:
            async def get_season(self, competition, season):
                if error is not None:
                    raise error
                return matches

        return Stub()

    def _espn(self, tmp_path, events) -> EspnSource:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"events": events})

        return EspnSource(
            cache_dir=tmp_path,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def _played(self, date, home="Man United", away="Chelsea") -> Match:
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

    def _fixture(self, date, home="Man United", away="Brighton", kick_off=None) -> Match:
        return Match(
            competition="E0",
            season="2026-27",
            date=date,
            kick_off=kick_off,
            home_team=home,
            away_team=away,
        )

    async def test_fresh_csv_skips_enhancement(self, tmp_path):
        base = [self._played(dt.date.today())]
        provider = SeasonProvider(
            self._csv_stub(base), self._espn(tmp_path, [_ft_event(dt.date.today())])
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is False
        assert result.freshness.warning is None

    async def test_break_weeks_waste_no_calls(self, tmp_path):
        """No matches during a break -> nothing overdue -> zero API calls."""
        base = [
            self._played(dt.date.today() - dt.timedelta(days=5)),
            self._fixture(dt.date.today() + dt.timedelta(days=3)),
        ]
        provider = SeasonProvider(self._csv_stub(base), self._espn(tmp_path, []))
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
            self._csv_stub(base),
            # enhancement answers under different name forms: canonical dedup
            self._espn(
                tmp_path,
                [
                    _ft_event(
                        dt.date.today(),
                        home="Manchester United",
                        away="Brighton and Hove Albion",
                    )
                ],
            ),
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        today_rows = [m for m in result.matches if m.date == dt.date.today()]
        assert len(today_rows) == 1  # deduped by canonical names
        assert today_rows[0].played is True
        assert result.freshness.warning is None

    async def test_stale_csv_fills_gap(self, tmp_path):
        base = [
            self._played(dt.date.today() - dt.timedelta(days=3)),
            self._fixture(dt.date.today() - dt.timedelta(days=1)),
        ]
        provider = SeasonProvider(
            self._csv_stub(base),
            # enhancement answers under different name forms: canonical dedup
            self._espn(
                tmp_path,
                [
                    _ft_event(
                        dt.date.today() - dt.timedelta(days=1),
                        home="Manchester United",
                        away="Brighton and Hove Albion",
                    )
                ],
            ),
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        assert result.freshness.enhancement_name == "espn"
        assert result.freshness.quota_remaining is None  # espn has no quota
        assert len(result.matches) == 2
        assert result.freshness.warning is None

    async def test_stale_csv_without_enhancement_warns(self):
        base = [
            self._played(dt.date.today() - dt.timedelta(days=3)),
            self._fixture(dt.date.today() - dt.timedelta(days=1)),
        ]
        provider = SeasonProvider(self._csv_stub(base), None)
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is False
        assert result.freshness.warning is not None
        assert "enhancement source" in result.freshness.warning

    async def test_unpublished_season_served_purely_from_espn(self, tmp_path):
        """The exact production situation of E0 2026-27: no CSV at all."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        tomorrow = dt.date.today() + dt.timedelta(days=1)
        provider = SeasonProvider(
            self._csv_stub([], error=DataSourceError("season not published")),
            self._espn(tmp_path, [_ft_event(yesterday), _pre_event(tomorrow)]),
        )
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is True
        assert result.freshness.enhancement_name == "espn"
        assert result.freshness.warning is None
        assert len(result.matches) == 2  # yesterday's result + tomorrow's fixture

    async def test_enhancement_failure_degrades_to_warning(self, tmp_path):
        """An unhealthy enhancement source must never break the CSV path."""
        base = [
            self._played(dt.date.today() - dt.timedelta(days=3)),
            self._fixture(dt.date.today() - dt.timedelta(days=1)),
        ]

        class Broken:
            name = "espn"
            has_key = True

            async def get_fixtures(self, competition, season, date_from, date_to):
                raise DataSourceError("espn exploded")

            def quota_remaining(self):
                return None

        provider = SeasonProvider(self._csv_stub(base), Broken())
        result = await provider.get_season("E0", "2026-27")
        assert result.freshness.enhancement_used is False
        assert "unavailable" in (result.freshness.warning or "")
        assert len(result.matches) == 2  # CSV rows still served
