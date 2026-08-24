"""football_mcp server: MCP tools over the dual-source season provider.

Built on the MCP Python SDK v2 (MCPServer, protocol 2026-07-28 revision).

Design rules for tool payloads:
- Compact by default: one match = one short dict; odds limited to the
  research-grade subset (pinnacle/market closing, O/U 2.5, Asian handicap)
  unless odds_detail=true. A full CSV row is 130+ columns and would drown a
  client model's context.
- Team matching is canonical and case-insensitive ("Man United" finds
  "Manchester United").
- Every get_matches response carries its freshness audit so agents can see
  how fresh the data is and why.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from mcp.server import MCPServer

from football_mcp.derive import (
    compute_head_to_head,
    compute_standings,
    compute_team_form,
)
from football_mcp.models import Match
from football_mcp.names import canonical
from football_mcp.sources.espn import CUP_SLUGS, EspnSource
from football_mcp.sources.football_data import (
    SUPPORTED_COMPETITIONS,
    DataSourceError,
    FootballDataSource,
    season_to_code,
)
from football_mcp.sources.season_provider import SeasonProvider, SeasonResult

MAX_ROWS = 100
DEFAULT_ROWS = 50

mcp = MCPServer(
    "football-mcp",
    instructions=(
        "Football data server: results, fixtures, stats and odds for 18 "
        "European leagues, seasons 1993-94 to today, minute-fresh for the "
        "current season. Competition codes: E0 PL, E1 Championship, D1 "
        "Bundesliga, I1 Serie A, SP1 La Liga, F1 Ligue 1, ... (call "
        "list_competitions). Seasons look like '2025-26'. Dates are "
        "YYYY-MM-DD. Team names match loosely (short or full forms)."
    ),
)

_provider: SeasonProvider | None = None


def get_provider() -> SeasonProvider:
    global _provider
    if _provider is None:
        _provider = SeasonProvider(FootballDataSource(), EspnSource())
    return _provider


def _parse_date(value: str | None, name: str) -> dt.date | None:
    if value is None or value == "":
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _odds_view(match: Match, detail: bool) -> dict[str, Any] | None:
    """Compact odds view; None when the row carries no odds at all."""
    if match.pinnacle_close is None and match.market_avg_close is None:
        return None
    view: dict[str, Any] = {
        "pinnacle_close": match.pinnacle_close.model_dump() if match.pinnacle_close else None,
        "market_avg_close": (
            match.market_avg_close.model_dump() if match.market_avg_close else None
        ),
        "over_under_2_5_close": (
            [match.market_avg_close_over25, match.market_avg_close_under25]
            if match.market_avg_close_over25 is not None
            else None
        ),
        "asian_handicap_close": (
            {
                "line": match.ah_close_line,
                "home": match.ah_close_home,
                "away": match.ah_close_away,
            }
            if match.ah_close_line is not None
            else None
        ),
    }
    if detail:
        view["pinnacle_open"] = (
            match.pinnacle_open.model_dump() if match.pinnacle_open else None
        )
        view["market_avg_open"] = (
            match.market_avg_open.model_dump() if match.market_avg_open else None
        )
    return view


def _stats_view(match: Match) -> dict[str, Any] | None:
    stats = {
        "shots": [match.home_shots, match.away_shots],
        "shots_on_target": [match.home_shots_on_target, match.away_shots_on_target],
        "corners": [match.home_corners, match.away_corners],
        "fouls": [match.home_fouls, match.away_fouls],
        "yellow_cards": [match.home_yellow_cards, match.away_yellow_cards],
        "red_cards": [match.home_red_cards, match.away_red_cards],
        "possession_pct": [match.home_possession, match.away_possession],
        "assists": [match.home_assists, match.away_assists],
    }
    return stats if any(v != [None, None] for v in stats.values()) else None


def _match_view(
    match: Match,
    include_stats: bool,
    include_odds: bool,
    odds_detail: bool,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "date": match.date.isoformat() if match.date else None,
        "kickoff_utc": match.kick_off,
        "home": match.home_team,
        "away": match.away_team,
        "played": match.played,
    }
    if match.played:
        view["score"] = f"{match.home_goals}-{match.away_goals}"
        view["result"] = match.result
        view["half_time"] = (
            f"{match.half_time_home_goals}-{match.half_time_away_goals}"
            if match.half_time_home_goals is not None
            else None
        )
    if match.referee:
        view["referee"] = match.referee
    if match.note:
        view["note"] = match.note  # cup semantics: legs, penalty shootouts
    if include_odds:
        odds = _odds_view(match, odds_detail)
        if odds is not None:
            view["odds"] = odds
    if include_stats and match.played:
        stats = _stats_view(match)
        if stats is not None:
            view["stats"] = stats
    return view


def _freshness_view(result: SeasonResult) -> dict[str, Any]:
    f = result.freshness
    return {
        "csv_latest_played": f.csv_latest_played.isoformat() if f.csv_latest_played else None,
        "enhancement_used": f.enhancement_used,
        "enhancement_source": f.enhancement_name,
        "warning": f.warning,
    }


@mcp.tool()
def list_competitions() -> dict[str, str]:
    """List every supported competition as {code: name}.

    Codes are used in every other tool (e.g. 'E0' = English Premier League).
    """
    return dict(SUPPORTED_COMPETITIONS)


@mcp.tool()
async def list_teams(competition: str, season: str) -> dict[str, Any]:
    """List teams recorded in a competition season, with per-team match counts.

    Use this for team-name discovery before filtering by team in get_matches:
    names here are exactly the names that source rows carry (e.g. 'Man United').
    """
    competition = competition.upper()
    if competition not in SUPPORTED_COMPETITIONS:
        raise ValueError(
            f"unsupported competition code {competition!r}; "
            "call list_competitions for the valid codes"
        )
    provider = get_provider()
    try:
        result = await provider.get_season(competition, season)
    except DataSourceError as exc:
        raise ValueError(str(exc)) from exc
    counts: dict[str, int] = {}
    for m in result.matches:
        counts[m.home_team] = counts.get(m.home_team, 0) + 1
        counts[m.away_team] = counts.get(m.away_team, 0) + 1
    return {
        "teams": sorted(counts),
        "counts": dict(sorted(counts.items())),
        "freshness": _freshness_view(result),
    }


@mcp.tool()
async def get_standings(
    competition: str,
    season: str,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """League table replayed from played matches (leakage-safe).

    Args:
        competition: league code, e.g. "E0".
        season: season label, e.g. "2025-26".
        as_of_date: optional "YYYY-MM-DD" cutoff; the table then reflects
            only matches played up to that date (no future leakage).

    Note: administrative points deductions are not in the data; tables for
    deduction seasons can differ from official ones.
    """
    competition = competition.upper()
    if competition not in SUPPORTED_COMPETITIONS:
        raise ValueError(f"unsupported competition code {competition!r}")
    cutoff = _parse_date(as_of_date, "as_of_date")
    provider = get_provider()
    try:
        result = await provider.get_season(competition, season)
    except DataSourceError as exc:
        raise ValueError(str(exc)) from exc
    rows = compute_standings(result.matches, as_of_date=cutoff)
    return {
        "rows": [r.model_dump(mode="json") for r in rows],
        "count": len(rows),
        "as_of_date": cutoff.isoformat() if cutoff else None,
        "freshness": _freshness_view(result),
    }


@mcp.tool()
async def get_team_form(
    team: str,
    competition: str,
    season: str,
    last: int = 10,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Recent form of one team: last N played matches, newest first, + summary.

    Args:
        team: team name; loose matching ("Man United" finds "Manchester Utd").
        competition: league code, e.g. "E0".
        season: season label, e.g. "2025-26".
        last: how many recent matches to return (1-20, default 10).
        as_of_date: optional cutoff for leakage-safe historical analysis.
    """
    competition = competition.upper()
    if competition not in SUPPORTED_COMPETITIONS:
        raise ValueError(f"unsupported competition code {competition!r}")
    last = max(1, min(int(last), 20))
    cutoff = _parse_date(as_of_date, "as_of_date")
    provider = get_provider()
    try:
        result = await provider.get_season(competition, season)
    except DataSourceError as exc:
        raise ValueError(str(exc)) from exc
    form = compute_team_form(team, result.matches, last=last, as_of_date=cutoff)
    if form is None:
        raise ValueError(
            f"no played matches found for team {team!r} in "
            f"{competition} {season}; call list_teams to see valid names"
        )
    return {
        "team": form.team,
        "entries": [e.model_dump(mode="json") for e in form.entries],
        "summary": form.summary,
        "as_of_date": cutoff.isoformat() if cutoff else None,
        "freshness": _freshness_view(result),
    }


def _shift_season(season: str, back: int) -> str:
    """'2025-26' shifted back n years -> '2023-24'."""
    start = int(season[:4]) - back
    return f"{start}-{str(start + 1)[2:]}"


@mcp.tool()
async def get_head_to_head(
    team_a: str,
    team_b: str,
    competition: str,
    season: str,
    last: int = 10,
    seasons_back: int = 0,
) -> dict[str, Any]:
    """Head-to-head meetings between two teams, one or many seasons.

    Args:
        team_a, team_b: team names; loose matching on both sides.
        competition: league code (both teams must play in it).
        season: season label, e.g. "2025-26".
        last: how many recent meetings to return (1-20, default 10),
            newest first across the whole span.
        seasons_back: seasons to reach back beyond `season` (0 = single
            season). E.g. season="2025-26", seasons_back=9 analyzes the
            last 10 seasons. League meetings only (no cup competitions).

    Returns per-season details plus an aggregated summary (wins_a/wins_b/
    draws/goals over every season loaded). Seasons whose data is missing
    are skipped and listed in skipped_seasons.
    """
    competition = competition.upper()
    if competition not in SUPPORTED_COMPETITIONS:
        raise ValueError(f"unsupported competition code {competition!r}")
    last = max(1, min(int(last), 20))
    seasons_back = max(0, min(int(seasons_back), 30))
    provider = get_provider()

    per_season: list[dict[str, Any]] = []
    totals = {"matches": 0, "wins_a": 0, "wins_b": 0, "draws": 0, "goals_a": 0, "goals_b": 0}
    all_rows: list[dict] = []
    skipped: list[str] = []
    last_freshness: dict[str, Any] | None = None
    found_any = False

    for back in range(seasons_back + 1):
        season_label = _shift_season(season, back)
        try:
            result = await provider.get_season(competition, season_label)
        except DataSourceError:
            skipped.append(season_label)
            continue
        h2h = compute_head_to_head(team_a, team_b, result.matches, last=20)
        if h2h is None:
            # Season loads but holds no meetings between the two teams
            # (or the season file was unavailable and degraded to a warning
            # inside the provider): count as skipped, never fatal.
            skipped.append(season_label)
            continue
        found_any = True
        last_freshness = _freshness_view(result)
        per_season.append(
            {"season": season_label, "summary": h2h.summary}
        )
        for key in totals:
            totals[key] += h2h.summary.get(key, 0)
        all_rows.extend(dict(row, season=season_label) for row in h2h.matches)

    if not found_any:
        span = f"{_shift_season(season, seasons_back)}..{season}" if seasons_back else season
        raise ValueError(
            f"no played meetings between {team_a!r} and {team_b!r} in "
            f"{competition} {span}"
        )

    all_rows.sort(key=lambda r: r["date"], reverse=True)
    return {
        "team_a": team_a,
        "team_b": team_b,
        "span": f"{_shift_season(season, seasons_back)}..{season}",
        "per_season": per_season,
        "matches": all_rows[:last],
        "summary": totals,
        "skipped_seasons": skipped,
        "freshness": last_freshness,
    }


@mcp.tool()
def list_cup_competitions() -> dict[str, str]:
    """List supported cup competitions as {code: name}.

    Cups are served purely from ESPN (no odds, no football-data CSV base).
    Codes: FA Cup, LC League Cup, CDR Copa del Rey, CI Coppa Italia,
    DFB Pokal, CDF Coupe de France, UCL Champions League, UEL Europa League.
    """
    return {code: name for code, (_slug, name) in CUP_SLUGS.items()}


@mcp.tool()
async def get_cup_matches(
    competition: str,
    season: str,
    team: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only: str = "all",
    limit: int = DEFAULT_ROWS,
) -> dict[str, Any]:
    """Query matches in a cup competition for one season (ESPN source).

    Includes ties decided on penalties: the recorded score is the
    regulation/extra-time score and `note` carries e.g. "advance 4-3 on
    penalties" or "1st Leg" for two-legged ties. No odds and no shot stats
    for most cup matches; ESPN history reaches back to the early 2000s.

    Args:
        competition: cup code (see list_cup_competitions), e.g. "FA", "UCL".
        season: season label covering the whole campaign, e.g. "2025-26"
            (an Aug..Jul span; the 2025-26 FA Cup final is May 2026).
        team: optional team filter; loose matching.
        date_from / date_to: optional inclusive "YYYY-MM-DD" bounds.
        only: "all" (default), "played", or "upcoming".
        limit: max matches returned, 1-100 (default 50), newest first.
    """
    if only not in ("all", "played", "upcoming"):
        raise ValueError(f"only must be all|played|upcoming, got {only!r}")
    limit = max(1, min(int(limit), MAX_ROWS))
    lo = _parse_date(date_from, "date_from")
    hi = _parse_date(date_to, "date_to")

    espn = get_provider().enhancement
    if not isinstance(espn, EspnSource):
        raise ValueError("cup support requires the espn source")
    try:
        matches = await espn.get_cup_season(competition, season)
    except DataSourceError as exc:
        raise ValueError(str(exc)) from exc

    team_key = canonical(team) if team else None
    rows: list[Match] = []
    for match in matches:
        if match.date is None:
            continue
        if lo is not None and match.date < lo:
            continue
        if hi is not None and match.date > hi:
            continue
        if team_key is not None and team_key not in (
            canonical(match.home_team),
            canonical(match.away_team),
        ):
            continue
        if only == "played" and not match.played:
            continue
        if only == "upcoming" and match.played:
            continue
        rows.append(match)

    rows.sort(key=lambda m: m.date or dt.date.min, reverse=True)
    total = len(rows)
    selected = rows[:limit]
    played_dates = [m.date for m in matches if m.played and m.date]
    return {
        "matches": [
            _match_view(m, include_stats=False, include_odds=False, odds_detail=False)
            for m in selected
        ],
        "count": len(selected),
        "total_matching": total,
        "source": "espn",
        "latest_played": max(played_dates).isoformat() if played_dates else None,
    }


@mcp.tool()
async def get_matches(
    competition: str,
    season: str,
    team: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only: str = "all",
    include_stats: bool = False,
    include_odds: bool = False,
    odds_detail: bool = False,
    order: str = "desc",
    limit: int = DEFAULT_ROWS,
) -> dict[str, Any]:
    """Query matches (results and fixtures) for one competition season.

    Args:
        competition: league code, e.g. "E0" (see list_competitions).
        season: season label, e.g. "2025-26" or "2026-27".
        team: optional team filter; short or full names both work
            ("Man United" matches "Manchester United").
        date_from: optional inclusive lower bound, "YYYY-MM-DD".
        date_to: optional inclusive upper bound, "YYYY-MM-DD".
        only: "all" (default), "played", or "upcoming".
        include_stats: include shots/corners/fouls/possession per match.
        include_odds: include closing odds (pinnacle + market average,
            over/under 2.5, Asian handicap) when available.
        odds_detail: also include opening odds (requires include_odds).
        order: "desc" (newest first, default) or "asc".
        limit: max matches returned, 1-100 (default 50).

    Returns:
        {matches: [...], count, total_matching, freshness: {...}}.
        Freshness reports how current the data is and any staleness warning.
    """
    if only not in ("all", "played", "upcoming"):
        raise ValueError(f"only must be all|played|upcoming, got {only!r}")
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be asc|desc, got {order!r}")
    competition = competition.upper()
    if competition not in SUPPORTED_COMPETITIONS:
        raise ValueError(
            f"unsupported competition code {competition!r}; "
            "call list_competitions for the valid codes"
        )
    try:
        season_to_code(season)
    except DataSourceError as exc:
        raise ValueError(str(exc)) from exc
    limit = max(1, min(int(limit), MAX_ROWS))
    lo = _parse_date(date_from, "date_from")
    hi = _parse_date(date_to, "date_to")

    provider = get_provider()
    try:
        result = await provider.get_season(competition, season)
    except DataSourceError as exc:
        raise ValueError(str(exc)) from exc

    team_key = canonical(team) if team else None
    rows: list[Match] = []
    for match in result.matches:
        if match.date is None:
            continue
        if lo is not None and match.date < lo:
            continue
        if hi is not None and match.date > hi:
            continue
        if team_key is not None and team_key not in (
            canonical(match.home_team),
            canonical(match.away_team),
        ):
            continue
        if only == "played" and not match.played:
            continue
        if only == "upcoming" and match.played:
            continue
        rows.append(match)

    rows.sort(key=lambda m: m.date or dt.date.min, reverse=(order == "desc"))
    total = len(rows)
    selected = rows[:limit]
    return {
        "matches": [
            _match_view(
                m,
                include_stats=include_stats,
                include_odds=include_odds,
                odds_detail=odds_detail,
            )
            for m in selected
        ],
        "count": len(selected),
        "total_matching": total,
        "freshness": _freshness_view(result),
    }


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
