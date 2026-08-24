"""Season provider: freshness ladder over CSV + API-Football.

Decision table (today = UTC today):

- CSV latest played match >= yesterday  -> CSV only, zero API cost.
- Gap (stale or unpublished season file):
    * key configured -> fetch the gap window from API-Football and merge,
    * no key -> CSV only, with an explicit staleness warning.

The returned Freshness object tells tools (and their calling agents) exactly
how fresh the data is and why.

赛季数据提供器：在 CSV 与 API-Football 之间按数据新鲜度逐级选择。

决策表（today 为当前 UTC 日期）：

- CSV 中最近一场已完赛比赛不早于昨天：仅使用 CSV，不消耗 API 配额。
- 存在数据空档（CSV 陈旧或赛季文件尚未发布）：
    * 已配置密钥：从 API-Football 获取空档时间段的数据并合并；
    * 未配置密钥：仅使用 CSV，并返回明确的数据陈旧警告。

返回的 Freshness 对象会准确告知工具及调用它们的 Agent：数据有多新，以及形成
当前新鲜度状态的原因。
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from football_mcp.models import Match
from football_mcp.sources.api_football import ApiFootballSource
from football_mcp.sources.football_data import DataSourceError, FootballDataSource
from football_mcp.sources.merge import merge_matches


class Freshness(BaseModel):
    csv_latest_played: dt.date | None = None
    api_used: bool = False
    api_latest_played: dt.date | None = None
    quota_remaining: int | None = None
    warning: str | None = None


class SeasonResult(BaseModel):
    matches: list[Match]
    freshness: Freshness


def _today() -> dt.date:
    return dt.datetime.now(tz=dt.UTC).date()


class SeasonProvider:
    def __init__(
        self,
        csv_source: FootballDataSource,
        api_source: ApiFootballSource | None = None,
    ) -> None:
        self.csv_source = csv_source
        self.api_source = api_source

    async def get_season(self, competition: str, season: str) -> SeasonResult:
        competition = competition.upper()
        base: list[Match] = []
        csv_error: str | None = None
        try:
            base = await self.csv_source.get_season(competition, season)
        except DataSourceError as exc:
            csv_error = str(exc)

        played_dates = [m.date for m in base if m.played and m.date is not None]
        csv_latest = max(played_dates) if played_dates else None
        today = _today()
        stale = csv_latest is None or csv_latest < today - dt.timedelta(days=1)

        freshness = Freshness(csv_latest_played=csv_latest)
        matches = base

        if stale and self.api_source is not None and self.api_source.has_key:
            date_from = (
                csv_latest + dt.timedelta(days=1)
                if csv_latest is not None
                else dt.date(int(season[:4]), 8, 1)
            )
            if date_from <= today:
                try:
                    extra = await self.api_source.get_fixtures(
                        competition, season, date_from, today
                    )
                except DataSourceError as exc:
                    freshness.warning = f"api-football unavailable: {exc}"
                else:
                    matches = merge_matches(base, extra)
                    api_dates = [
                        m.date for m in extra if m.played and m.date is not None
                    ]
                    freshness.api_used = True
                    freshness.api_latest_played = max(api_dates) if api_dates else None
                    freshness.quota_remaining = (
                        self.api_source.quota_limit - self.api_source.quota_count()
                    )

        if freshness.warning is None:
            latest = max(
                d for d in [freshness.csv_latest_played, freshness.api_latest_played] if d
            )
            if not matches:
                reason = f"csv unavailable ({csv_error})" if csv_error else "csv empty"
                if self.api_source is None or not self.api_source.has_key:
                    freshness.warning = (
                        f"no data available: {reason}; configure "
                        "FOOTBALL_MCP_API_FOOTBALL_KEY for fresh api-football results"
                    )
                else:
                    freshness.warning = f"no data available: {reason}; api returned nothing"
            elif latest is not None and latest < today - dt.timedelta(days=1):
                if freshness.api_used:
                    freshness.warning = (
                        f"data may be stale: latest match {latest}; "
                        "api-football returned no newer fixtures"
                    )
                else:
                    freshness.warning = (
                        f"data may be stale: latest match {latest}; "
                        "configure FOOTBALL_MCP_API_FOOTBALL_KEY for fresher results"
                    )

        return SeasonResult(matches=matches, freshness=freshness)
