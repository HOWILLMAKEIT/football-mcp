"""API-Football (api-sports.io v3) enhancement source.

Purpose: freshness. football-data.co.uk CSVs lag hours to a day behind and a
new season's file may not exist at all during the first rounds; API-Football
reports finished fixtures within minutes of full-time. This source fills the
gap window ("latest CSV match + 1 day" .. today).

Quota: the free plan allows 100 requests/day. Every real network call bumps a
persisted daily counter; responses are cached for a TTL so repeat calls are
free. The counter refuses to exceed `quota_limit` (default 95, a safety
margin below the plan cap).

Without an API key the source stays inert: `has_key` is False and callers
degrade gracefully to CSV-only data with a staleness warning.

API-Football（api-sports.io v3）增强数据源。

用途：提升数据新鲜度。football-data.co.uk 的 CSV 通常会延迟数小时到一天，
而且新赛季开始的前几轮甚至可能尚未发布赛季文件；API-Football 通常会在比赛
结束后几分钟内提供完场数据。该数据源用于填补“CSV 中最近一场比赛的次日”到
今天之间的数据空档。

配额：免费套餐每天允许 100 次请求。每次真实网络调用都会增加持久化的每日
计数器；响应会按 TTL 缓存，因此重复调用不会消耗配额。计数器拒绝超过
`quota_limit`（默认 95），在套餐上限之下留出安全余量。

未配置 API 密钥时，该数据源保持停用：`has_key` 为 False，调用方会平稳降级为
仅使用 CSV 数据，并附带数据陈旧警告。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from football_mcp.models import Match
from football_mcp.sources.football_data import DataSourceError

BASE_URL = "https://v3.football.api-sports.io"

# Our competition code -> API-Football league id.
LEAGUE_IDS: dict[str, int] = {
    "E0": 39,
    "E1": 40,
    "E2": 41,
    "E3": 42,
    "SC0": 71,
    "D1": 78,
    "D2": 79,
    "I1": 135,
    "I2": 136,
    "SP1": 140,
    "SP2": 141,
    "F1": 61,
    "F2": 62,
    "N1": 88,
    "B1": 144,
    "P1": 94,
    "T1": 203,
    "G1": 197,
}

# Fixture status codes (API-Football `fixture.status.short`).
FINISHED = {"FT", "AET", "PEN"}
LIVE = {"1H", "2H", "HT", "ET", "BT", "P", "SUSP", "INT", "LIVE"}


def _season_year(season: str) -> int:
    return int(season[:4])


def _result_letter(home: int, away: int) -> str:
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def parse_fixtures(payload: dict[str, Any], competition: str, season: str) -> list[Match]:
    """Parse an API-Football /fixtures response into Match models."""
    errors = payload.get("errors")
    if errors:
        raise DataSourceError(f"api-football returned errors: {errors}")
    matches: list[Match] = []
    for item in payload.get("response", []):
        fixture = item.get("fixture", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {}) or {}
        status = (fixture.get("status") or {}).get("short")
        iso = fixture.get("date")  # e.g. "2026-08-14T19:00:00+00:00" (UTC)
        home_name = (teams.get("home") or {}).get("name")
        away_name = (teams.get("away") or {}).get("name")
        if not home_name or not away_name or not iso:
            continue
        home_goals = goals.get("home")
        away_goals = goals.get("away")
        finished = status in FINISHED
        fields: dict[str, Any] = {
            "competition": competition.upper(),
            "season": season,
            "date": dt.date.fromisoformat(iso[:10]),
            # UTC time; football-data kick_off is UK local. Good enough for v1.
            "kick_off": iso[11:16] if len(iso) >= 16 else None,
            "home_team": home_name,
            "away_team": away_name,
        }
        if finished and home_goals is not None and away_goals is not None:
            fields["home_goals"] = int(home_goals)
            fields["away_goals"] = int(away_goals)
            fields["result"] = _result_letter(int(home_goals), int(away_goals))
        matches.append(Match.model_validate(fields))
    return matches


class ApiFootballSource:
    """Fresh fixtures from api-football.api-sports.io with quota + cache."""

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | str | None = None,
        quota_limit: int = 95,
        ttl_seconds: float = 1800.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("FOOTBALL_MCP_API_FOOTBALL_KEY")
        env_dir = os.environ.get("FOOTBALL_MCP_CACHE_DIR")
        default_dir = Path.home() / ".cache" / "football_mcp"
        self.cache_dir = Path(cache_dir or env_dir or default_dir) / "api-football"
        self.quota_limit = quota_limit
        self.ttl_seconds = ttl_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                headers={
                    "x-apisports-key": self.api_key or "",
                    "User-Agent": "football_mcp/0.1 (data provider MCP server)",
                },
            )
        return self._client

    # -- quota --------------------------------------------------------------

    @property
    def _quota_path(self) -> Path:
        return self.cache_dir / "quota.json"

    def quota_count(self) -> int:
        try:
            state = json.loads(self._quota_path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        if state.get("date") != dt.date.today().isoformat():
            return 0
        return int(state.get("count", 0))

    def _bump_quota(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        count = self.quota_count()
        self._quota_path.write_text(
            json.dumps({"date": dt.date.today().isoformat(), "count": count + 1})
        )

    # -- fixtures -----------------------------------------------------------

    def _cache_path(
        self, competition: str, season: str, date_from: dt.date, date_to: dt.date
    ) -> Path:
        return self.cache_dir / (
            f"{competition.upper()}-{_season_year(season)}-{date_from}-{date_to}.json"
        )

    async def get_fixtures(
        self,
        competition: str,
        season: str,
        date_from: dt.date,
        date_to: dt.date,
    ) -> list[Match]:
        """Fixtures (played and scheduled) for a league/season in a date window."""
        competition = competition.upper()
        league_id = LEAGUE_IDS.get(competition)
        if league_id is None:
            raise DataSourceError(f"no api-football league id for {competition!r}")
        if not self.has_key:
            raise DataSourceError("api-football key not configured")
        if date_from > date_to:
            return []

        cache_path = self._cache_path(competition, season, date_from, date_to)
        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < self.ttl_seconds:
                return parse_fixtures(json.loads(cache_path.read_text()), competition, season)

        if self.quota_count() >= self.quota_limit:
            raise DataSourceError(
                f"api-football daily quota exhausted ({self.quota_limit} requests)"
            )

        params = {
            "league": league_id,
            "season": _season_year(season),
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        }
        response = await self._client_get().get(f"{BASE_URL}/fixtures", params=params)
        if response.status_code == 429:
            raise DataSourceError("api-football rate limit hit (HTTP 429)")
        response.raise_for_status()
        payload = response.json()
        self._bump_quota()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload))
        return parse_fixtures(payload, competition, season)
