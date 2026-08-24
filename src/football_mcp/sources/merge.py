"""Merge two match lists, preferring the (richer) base rows on duplicates.

合并两份比赛列表；遇到重复比赛时，优先保留信息更丰富的基础数据行。
"""

from __future__ import annotations

from football_mcp.models import Match
from football_mcp.names import canonical


def _key(match: Match) -> tuple:
    return (match.date, canonical(match.home_team), canonical(match.away_team))


# Fields that may be filled into a base row from a duplicate enhancement row
# when the base value is missing (e.g. ESPN possession/assists on a CSV row).
ENRICHABLE_FIELDS = (
    "home_shots",
    "away_shots",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_corners",
    "away_corners",
    "home_fouls",
    "away_fouls",
    "home_yellow_cards",
    "away_yellow_cards",
    "home_red_cards",
    "away_red_cards",
    "home_possession",
    "away_possession",
    "home_assists",
    "away_assists",
)


def _enrich(base: Match, extra: Match) -> Match:
    """Fill missing stat fields of `base` from `extra`; base values win."""
    updates = {
        field: getattr(extra, field)
        for field in ENRICHABLE_FIELDS
        if getattr(base, field) is None and getattr(extra, field) is not None
    }
    return base.model_copy(update=updates) if updates else base


def merge_matches(base: list[Match], extra: list[Match]) -> list[Match]:
    """Merge two match lists, deduplicated by date + canonical teams.

    Rules on duplicate (date, home, away):
    1. base unplayed + extra played  -> extra replaces base (stale CSV row
       superseded by the finished result).
    2. both played -> keep the (richer) base row but fill its missing stat
       fields from extra (e.g. CSV keeps odds; ESPN contributes possession).
       Conflicting values resolve in favor of base.
    3. otherwise -> base row stands (odds/stats win over bare scores).

    Rows without a date are appended.
    """
    index: dict[tuple, int] = {}
    merged: list[Match] = []
    for match in base:
        if match.date is not None:
            index.setdefault(_key(match), len(merged))
        merged.append(match)
    for match in extra:
        key = _key(match) if match.date is not None else None
        if key is not None and key in index:
            position = index[key]
            if not merged[position].played and match.played:
                merged[position] = match  # rule 1: result supersedes fixture
            elif merged[position].played and match.played:
                merged[position] = _enrich(merged[position], match)  # rule 2
        else:
            if key is not None:
                index[key] = len(merged)
            merged.append(match)
    merged.sort(key=lambda m: (m.date is None, m.date or m.home_team))
    return merged
