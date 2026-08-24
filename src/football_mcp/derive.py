"""Derived analytics computed server-side from match lists.

Everything is replay-based: standings/form/H2H are accumulated from played
matches only, optionally cut at `as_of_date` (leakage-safe semantics: an
agent asking "standings as of Christmas" never sees a match after that date).

Known bias, documented in the knowledge base: football-data CSVs do not carry
points deductions; replay-derived standings can differ from official tables
for seasons with administrative deductions.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from football_mcp.models import Match
from football_mcp.names import canonical


class TableRow(BaseModel):
    position: int
    team: str  # display name as recorded on the first match row seen
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_difference: int
    points: int
    form: str  # last 5 results within the window, oldest -> newest, e.g. "WWDWL"


class TeamFormEntry(BaseModel):
    date: dt.date
    opponent: str
    venue: str  # "H" | "A"
    goals_for: int
    goals_against: int
    outcome: str  # "W" | "D" | "L"


class TeamForm(BaseModel):
    team: str
    entries: list[TeamFormEntry]
    summary: dict[str, int | float]  # W/D/L, GF, GA, points-per-game


class HeadToHead(BaseModel):
    team_a: str
    team_b: str
    matches: list[dict]  # compact entries: date, home, away, score, winner
    summary: dict[str, int]  # wins_a, wins_b, draws, goals_a, goals_b


def _outcome(home_goals: int, away_goals: int, *, home_is_a: bool) -> str:
    if home_goals == away_goals:
        return "D"
    home_won = home_goals > away_goals
    if home_is_a:
        return "W" if home_won else "L"
    return "L" if home_won else "W"


def compute_standings(
    matches: list[Match],
    as_of_date: dt.date | None = None,
    form_length: int = 5,
) -> list[TableRow]:
    """Replay played matches (optionally up to as_of_date) into a table."""
    stats: dict[str, dict] = {}
    display: dict[str, str] = {}

    def bucket(name: str) -> dict:
        key = canonical(name)
        display.setdefault(key, name)
        return stats.setdefault(
            key,
            {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "Pts": 0, "seq": []},
        )

    for match in sorted(matches, key=lambda m: m.date or dt.date.min):
        if not match.played or match.date is None:
            continue
        if as_of_date is not None and match.date > as_of_date:
            continue
        assert match.home_goals is not None and match.away_goals is not None
        home = bucket(match.home_team)
        away = bucket(match.away_team)
        hg, ag = match.home_goals, match.away_goals
        home["P"] += 1
        away["P"] += 1
        home["GF"] += hg
        home["GA"] += ag
        away["GF"] += ag
        away["GA"] += hg
        outcome = _outcome(hg, ag, home_is_a=True)
        if outcome == "W":
            home["W"] += 1
            home["Pts"] += 3
            away["L"] += 1
        elif outcome == "D":
            home["D"] += 1
            away["D"] += 1
            home["Pts"] += 1
            away["Pts"] += 1
        else:
            away["W"] += 1
            away["Pts"] += 3
            home["L"] += 1
        home["seq"].append(outcome)
        away["seq"].append(_outcome(hg, ag, home_is_a=False))

    rows = []
    for key, s in stats.items():
        rows.append(
            TableRow(
                position=0,  # assigned after sorting
                team=display[key],
                played=s["P"],
                won=s["W"],
                drawn=s["D"],
                lost=s["L"],
                goals_for=s["GF"],
                goals_against=s["GA"],
                goal_difference=s["GF"] - s["GA"],
                points=s["Pts"],
                form="".join(s["seq"][-form_length:]),
            )
        )
    rows.sort(
        key=lambda r: (-r.points, -r.goal_difference, -r.goals_for, r.team)
    )
    for i, row in enumerate(rows, start=1):
        row.position = i
    return rows


def compute_team_form(
    team: str,
    matches: list[Match],
    last: int = 10,
    as_of_date: dt.date | None = None,
) -> TeamForm | None:
    """Recent matches of one team, newest first, with an aggregate summary."""
    team_key = canonical(team)
    entries: list[TeamFormEntry] = []
    for match in sorted(matches, key=lambda m: m.date or dt.date.min):
        if not match.played or match.date is None:
            continue
        if as_of_date is not None and match.date > as_of_date:
            continue
        assert match.home_goals is not None and match.away_goals is not None
        home_key = canonical(match.home_team)
        away_key = canonical(match.away_team)
        if team_key not in (home_key, away_key):
            continue
        at_home = team_key == home_key
        gf = match.home_goals if at_home else match.away_goals
        ga = match.away_goals if at_home else match.home_goals
        outcome = _outcome(
            match.home_goals, match.away_goals, home_is_a=at_home
        )
        entries.append(
            TeamFormEntry(
                date=match.date,
                opponent=match.away_team if at_home else match.home_team,
                venue="H" if at_home else "A",
                goals_for=gf,
                goals_against=ga,
                outcome=outcome,
            )
        )
    if not entries:
        return None
    selected = entries[-last:]
    w = sum(1 for e in selected if e.outcome == "W")
    d = sum(1 for e in selected if e.outcome == "D")
    losses = sum(1 for e in selected if e.outcome == "L")
    gf = sum(e.goals_for for e in selected)
    ga = sum(e.goals_against for e in selected)
    points = 3 * w + d
    summary: dict[str, int | float] = {
        "matches": len(selected),
        "W": w,
        "D": d,
        "L": losses,
        "goals_for": gf,
        "goals_against": ga,
        "points": points,
        "points_per_game": round(points / len(selected), 2),
    }
    return TeamForm(team=team, entries=list(reversed(selected)), summary=summary)


def compute_head_to_head(
    team_a: str,
    team_b: str,
    matches: list[Match],
    last: int = 10,
) -> HeadToHead | None:
    """Meetings between two teams, newest first.

    Ties decided on penalties count as wins for the shootout winner (win
    rate reflects the final outcome); `pen_wins_a`/`pen_wins_b` disclose how
    many wins came that way, so the regulation-only view can be recovered.
    Goals always count the regulation/extra-time score only.
    """
    key_a, key_b = canonical(team_a), canonical(team_b)
    rows: list[dict] = []
    wins_a = wins_b = draws = goals_a = goals_b = 0
    pen_wins_a = pen_wins_b = 0
    for match in sorted(matches, key=lambda m: m.date or dt.date.min):
        if not match.played or match.date is None:
            continue
        assert match.home_goals is not None and match.away_goals is not None
        home_key = canonical(match.home_team)
        away_key = canonical(match.away_team)
        if {home_key, away_key} != {key_a, key_b}:
            continue
        a_is_home = home_key == key_a
        ga = match.home_goals if a_is_home else match.away_goals
        gb = match.away_goals if a_is_home else match.home_goals
        winner: str | None
        if match.shootout_winner:
            sw = canonical(match.shootout_winner)
            winner = "A" if sw == key_a else ("B" if sw == key_b else None)
            if winner == "A":
                pen_wins_a += 1
            elif winner == "B":
                pen_wins_b += 1
        else:
            winner = "A" if ga > gb else ("B" if gb > ga else "draw")
        if winner == "A":
            wins_a += 1
        elif winner == "B":
            wins_b += 1
        else:
            draws += 1
        goals_a += ga
        goals_b += gb
        row = {
            "date": match.date.isoformat(),
            "home": match.home_team,
            "away": match.away_team,
            "score": f"{match.home_goals}-{match.away_goals}",
            "winner": winner or "draw",
        }
        if match.note:
            row["note"] = match.note
        rows.append(row)
    if not rows:
        return None
    selected = rows[-last:]
    return HeadToHead(
        team_a=team_a,
        team_b=team_b,
        matches=list(reversed(selected)),
        summary={
            "matches": len(rows),
            "wins_a": wins_a,
            "wins_b": wins_b,
            "draws": draws,
            "goals_a": goals_a,
            "goals_b": goals_b,
            "pen_wins_a": pen_wins_a,
            "pen_wins_b": pen_wins_b,
        },
    )
