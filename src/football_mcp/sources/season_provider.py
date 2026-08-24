"""Season provider: freshness ladder over CSV + API-Football.

Staleness is fixture-aware, not calendar-aware:

- The CSV itself lists scheduled fixtures (rows with teams but no result).
  A fixture whose kickoff is >2h in the past but still has no result is
  "overdue": the data is stale *for that match*, and only then do we refresh
  the gap window from API-Football. This detects a finished match the same
  evening (kickoff + ~2h), instead of guessing whether "last night had games".
- If no fixture is overdue, data is complete for every scheduled match:
  no API call at all -- even mid-break when the latest result is weeks old
  (a calendar heuristic would false-positive here and burn quota).
- Fallbacks: files that list no future fixtures fall back to day-based
  staleness; a current-season file silent for >45 days is treated as
  concluded (summer break). Past seasons are immutable: never refreshed.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from football_mcp.models import Match
from football_mcp.sources.api_football import ApiFootballSource
from football_mcp.sources.football_data import (
    DataSourceError,
    FootballDataSource,
    is_past_season,
)
from football_mcp.sources.merge import merge_matches

# A football match: 90 min + halftime + stoppage. The CSV kick_off is UK local
# time; treating it as UTC is off by at most 1h in summer, which the margin
# absorbs comfortably.
MATCH_DURATION = dt.timedelta(hours=2)


class Freshness(BaseModel):
    csv_latest_played: dt.date | None = None
    api_used: bool = False
    api_latest_played: dt.date | None = None
    quota_remaining: int | None = None
    warning: str | None = None


class SeasonResult(BaseModel):
    matches: list[Match]
    freshness: Freshness


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _kickoff_utc(match: Match) -> dt.datetime | None:
    if match.date is None or not match.kick_off:
        return None
    try:
        clock = dt.time.fromisoformat(match.kick_off.strip())
    except ValueError:
        return None
    return dt.datetime.combine(match.date, clock, tzinfo=dt.UTC)


def overdue_fixtures(matches: list[Match], now: dt.datetime) -> list[Match]:
    """Scheduled matches that should have finished by `now` but lack a result."""
    overdue: list[Match] = []
    for match in matches:
        if match.played or match.date is None:
            continue
        if match.date < now.date():
            overdue.append(match)  # any earlier date is unambiguously past
            continue
        kickoff = _kickoff_utc(match)
        if kickoff is not None and now >= kickoff + MATCH_DURATION:
            overdue.append(match)
    return overdue


def _latest_played(matches: list[Match]) -> dt.date | None:
    dates = [m.date for m in matches if m.played and m.date is not None]
    return max(dates) if dates else None


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

        now = _now()
        today = now.date()
        latest_played = _latest_played(base)
        freshness = Freshness(csv_latest_played=latest_played)
        matches = base

        overdue = overdue_fixtures(base, now)
        has_future_rows = any(
            (not m.played) and m.date is not None and m.date >= today for m in base
        )
        season_over = is_past_season(season) or (
            latest_played is not None
            and not has_future_rows
            and (today - latest_played) > dt.timedelta(days=45)
        )
        # Day-based fallback only for files that never list future fixtures.
        day_stale = (
            not has_future_rows and latest_played is not None and latest_played < today
        )
        needs_refresh = not season_over and (
            csv_error is not None or not base or bool(overdue) or day_stale
        )

        if needs_refresh and self.api_source is not None and self.api_source.has_key:
            if overdue:
                date_from = min(m.date for m in overdue)
            elif latest_played is not None:
                date_from = latest_played + dt.timedelta(days=1)
            else:
                date_from = dt.date(int(season[:4]), 8, 1)
            if date_from <= today:
                try:
                    extra = await self.api_source.get_fixtures(
                        competition, season, date_from, today
                    )
                except DataSourceError as exc:
                    freshness.warning = f"api-football unavailable: {exc}"
                else:
                    matches = merge_matches(base, extra)
                    api_dates = [m.date for m in extra if m.played and m.date]
                    freshness.api_used = True
                    freshness.api_latest_played = max(api_dates) if api_dates else None
                    freshness.quota_remaining = (
                        self.api_source.quota_limit - self.api_source.quota_count()
                    )

        if freshness.warning is None:
            still_overdue = overdue_fixtures(matches, now)
            if not matches:
                reason = f"csv unavailable ({csv_error})" if csv_error else "csv empty"
                if self.api_source is None or not self.api_source.has_key:
                    freshness.warning = (
                        f"no data available: {reason}; configure "
                        "FOOTBALL_MCP_API_FOOTBALL_KEY for fresh api-football results"
                    )
                else:
                    freshness.warning = f"no data available: {reason}; api returned nothing"
            elif still_overdue:
                first = still_overdue[0]
                label = f"{first.home_team} vs {first.away_team} on {first.date}"
                if freshness.api_used:
                    freshness.warning = (
                        f"{len(still_overdue)} scheduled match(es) still missing "
                        f"results after api refresh (e.g. {label})"
                    )
                else:
                    freshness.warning = (
                        f"data may be stale: {len(still_overdue)} match(es) look "
                        f"finished but have no result (e.g. {label}); configure "
                        "FOOTBALL_MCP_API_FOOTBALL_KEY"
                    )

        return SeasonResult(matches=matches, freshness=freshness)
