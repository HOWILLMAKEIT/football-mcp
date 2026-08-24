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
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    Resolve,
)
from pydantic import BaseModel, Field

from football_mcp.derive import (
    compute_head_to_head,
    compute_standings,
    compute_team_form,
)
from football_mcp.models import Match
from football_mcp.names import canonical
from football_mcp.sources.espn import (
    CUP_SLUGS,
    EUROPEAN_CUPS,
    LEAGUE_TO_DOMESTIC_CUPS,
    EspnSource,
)
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


SCOPE_VALUES = ("league", "domestic_cups", "europe", "all")
# A scope is either one of the four semantic groups or one concrete cup code.
VALID_SCOPES = frozenset((*SCOPE_VALUES, *CUP_SLUGS))


class ScopeConfirm(BaseModel):
    """Elicitation form: which competition scope an analysis should cover."""

    scope: Literal[
        "league",
        "domestic_cups",
        "europe",
        "all",
        "FA",
        "LC",
        "CDR",
        "CI",
        "DFB",
        "CDF",
        "UCL",
        "UEL",
    ] = Field(
        description=(
            "league: league only; domestic_cups: national cups of the league's "
            "country; europe: UCL+UEL; all: everything; or one concrete cup "
            "(UCL Champions League, UEL Europa League, CDR Copa del Rey, "
            "FA Cup, ...)"
        )
    )


async def _resolve_h2h_scope(
    team_a: str, team_b: str, competition: str, scope: str | None = None
) -> ScopeConfirm | Elicit[ScopeConfirm]:
    """Ask which scope to analyze when the caller did not specify one.

    Runs before the tool body (SDK resolver). If the model already passed
    `scope`, answer directly with zero round-trips.
    """
    if scope is not None:
        normalized = scope.lower() if scope.lower() in SCOPE_VALUES else scope.upper()
        if normalized not in VALID_SCOPES:
            raise ValueError(
                f"invalid scope {scope!r}; expected one of "
                f"{', '.join(sorted(VALID_SCOPES))}"
            )
        return ScopeConfirm(scope=normalized)  # type: ignore[arg-type]
    return Elicit(
        f"Which meetings should the {team_a} vs {team_b} head-to-head "
        f"({competition}) include?",
        ScopeConfirm,
    )


def _scope_bucket(code: str) -> str:
    if code in EUROPEAN_CUPS:
        return "europe"
    if code in CUP_SLUGS:
        return "domestic_cups"
    return "league"


async def _gather_scope_groups(
    competition: str,
    season: str,
    seasons_back: int,
    scope: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load matches grouped per (competition, season) for the chosen scope.

    League data comes from the CSV provider; cup data from ESPN
    (two-window season fetch). Cups unavailable for a country are skipped.
    """
    provider = get_provider()
    enhancement = provider.enhancement
    espn = enhancement if hasattr(enhancement, "get_cup_season") else None
    scope = scope.lower() if scope in SCOPE_VALUES else scope.upper()
    want_league = scope in ("league", "all")
    want_domestic = scope in ("domestic_cups", "all")
    want_europe = scope in ("europe", "all")

    cup_codes: list[str] = []
    if scope in CUP_SLUGS:
        cup_codes.append(scope)  # one concrete cup, e.g. "UCL" or "CDR"
    else:
        if want_domestic:
            cup_codes.extend(LEAGUE_TO_DOMESTIC_CUPS.get(competition.upper(), ()))
        if want_europe:
            cup_codes.extend(EUROPEAN_CUPS)

    groups: list[dict[str, Any]] = []
    skipped: list[str] = []

    for back in range(seasons_back + 1):
        season_label = _shift_season(season, back)
        if want_league:
            # The provider degrades fetch failures to empty lists + warning,
            # so detect "no data at all" rather than catching exceptions.
            season_result = await provider.get_season(competition, season_label)
            degraded = (
                not season_result.matches
                and season_result.freshness.warning is not None
                and "no data available" in season_result.freshness.warning
            )
            if degraded:
                skipped.append(f"{competition}/{season_label}")
            else:
                groups.append(
                    {
                        "code": competition.upper(),
                        "name": SUPPORTED_COMPETITIONS[competition.upper()],
                        "season": season_label,
                        "matches": season_result.matches,
                    }
                )
        for code in cup_codes:
            if espn is None:
                skipped.append(f"{code}/{season_label}")
                continue
            try:
                cup_matches = await espn.get_cup_season(code, season_label)
            except DataSourceError:
                skipped.append(f"{code}/{season_label}")
                continue
            groups.append(
                {
                    "code": code,
                    "name": CUP_SLUGS[code][1],
                    "season": season_label,
                    "matches": cup_matches,
                }
            )
    return groups, skipped


@mcp.tool()
async def get_head_to_head(
    team_a: str,
    team_b: str,
    competition: str,
    season: str,
    last: int = 10,
    seasons_back: int = 0,
    scope: str | None = None,
    scope_confirm: Annotated[
        ElicitationResult[ScopeConfirm], Resolve(_resolve_h2h_scope)
    ] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Head-to-head meetings between two teams across chosen competitions.

    Args:
        team_a, team_b: team names; loose matching on both sides.
        competition: league code (both teams must play in it); anchors which
            country's domestic cups "domestic_cups" means.
        season: season label, e.g. "2025-26".
        last: how many recent meetings to return (1-20, default 10),
            newest first across the whole span.
        seasons_back: seasons to reach back beyond `season` (0 = single
            season). E.g. seasons_back=9 analyzes the last 10 seasons.
        scope: "league" | "domestic_cups" | "europe" | "all", or one
            concrete cup code ("UCL", "UEL", "CDR", "FA", "LC", "CI",
            "DFB", "CDF") to analyze exactly that competition. If omitted,
            the user is asked which scope to analyze (elicitation); a
            declined question falls back to "league".

    Notes: cup ties decided on penalties count as wins for the shootout
    winner (`pen_wins_a`/`pen_wins_b` disclose how many wins came via
    penalties; goals count regulation/extra time only). Two-legged
    ties count each leg as one meeting. Cups have no odds. The response
    breaks totals down both `by_scope` (league/domestic_cups/europe) and
    `by_competition` (e.g. UCL vs UEL separately).
    """
    competition = competition.upper()
    if competition not in SUPPORTED_COMPETITIONS:
        raise ValueError(f"unsupported competition code {competition!r}")
    last = max(1, min(int(last), 20))
    seasons_back = max(0, min(int(seasons_back), 30))

    scope_note = None
    chosen_scope = scope
    if isinstance(scope_confirm, AcceptedElicitation) and scope_confirm.data:
        chosen_scope = scope_confirm.data.scope
    elif scope is None:
        # declined / cancelled elicitation: safe default, stated openly
        chosen_scope = "league"
        scope_note = "scope not specified and question declined; defaulted to league"
    raw_scope = str(chosen_scope)
    chosen_scope = next(
        (c for c in (raw_scope.lower(), raw_scope.upper()) if c in VALID_SCOPES), None
    )
    if chosen_scope is None:
        raise ValueError(
            f"invalid scope {raw_scope!r}; expected one of "
            f"{', '.join(sorted(VALID_SCOPES))}"
        )

    groups, skipped = await _gather_scope_groups(
        competition, season, seasons_back, chosen_scope or "league"
    )

    per_group: list[dict[str, Any]] = []
    by_scope: dict[str, dict[str, int]] = {}
    by_competition: dict[str, dict[str, Any]] = {}
    totals = {"matches": 0, "wins_a": 0, "wins_b": 0, "draws": 0, "goals_a": 0, "goals_b": 0,
              "pen_wins_a": 0, "pen_wins_b": 0}
    all_rows: list[dict] = []
    found_any = False

    for group in groups:
        h2h = compute_head_to_head(team_a, team_b, group["matches"], last=20)
        if h2h is None:
            # No meetings between the two teams in this competition/season:
            # a valid zero, not a data gap -- not recorded in skipped.
            continue
        found_any = True
        per_group.append(
            {
                "competition": group["code"],
                "name": group["name"],
                "season": group["season"],
                "scope": _scope_bucket(group["code"]),
                "summary": h2h.summary,
            }
        )
        bucket = _scope_bucket(group["code"])
        bucket_totals = by_scope.setdefault(
            bucket, {"matches": 0, "wins_a": 0, "wins_b": 0, "draws": 0,
                     "goals_a": 0, "goals_b": 0, "pen_wins_a": 0, "pen_wins_b": 0}
        )
        comp_entry = by_competition.setdefault(
            group["code"], {"name": group["name"], "scope": bucket,
                            "matches": 0, "wins_a": 0, "wins_b": 0, "draws": 0,
                            "goals_a": 0, "goals_b": 0,
                            "pen_wins_a": 0, "pen_wins_b": 0}
        )
        for key in totals:
            totals[key] += h2h.summary.get(key, 0)
            bucket_totals[key] += h2h.summary.get(key, 0)
            comp_entry[key] += h2h.summary.get(key, 0)
        all_rows.extend(
            dict(row, season=group["season"], competition=group["code"])
            for row in h2h.matches
        )

    if not found_any:
        span = f"{_shift_season(season, seasons_back)}..{season}" if seasons_back else season
        raise ValueError(
            f"no played meetings between {team_a!r} and {team_b!r} in "
            f"{competition} {span} (scope={chosen_scope})"
        )

    all_rows.sort(key=lambda r: r["date"], reverse=True)
    result: dict[str, Any] = {
        "team_a": team_a,
        "team_b": team_b,
        "scope": chosen_scope,
        "span": f"{_shift_season(season, seasons_back)}..{season}",
        "per_season": per_group,
        "by_scope": by_scope,
        "by_competition": by_competition,
        "matches": all_rows[:last],
        "summary": totals,
        "skipped_seasons": skipped,
    }
    if scope_note:
        result["scope_note"] = scope_note
    return result


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
