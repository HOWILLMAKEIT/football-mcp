"""Merge two match lists, preferring the (richer) base rows on duplicates.

合并两份比赛列表；遇到重复比赛时，优先保留信息更丰富的基础数据行。
"""

from __future__ import annotations

from football_mcp.models import Match
from football_mcp.names import canonical


def _key(match: Match) -> tuple:
    return (match.date, canonical(match.home_team), canonical(match.away_team))


def merge_matches(base: list[Match], extra: list[Match]) -> list[Match]:
    """Merge two match lists, deduplicated by date + canonical teams.

    Base (football-data CSV) rows win because they carry stats and odds. One
    exception: when the base row is an *unplayed* fixture (stale CSV) and the
    extra row carries the finished result, the extra row replaces it in place.
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
                merged[position] = match  # overdue CSV row superseded by result
        else:
            if key is not None:
                index[key] = len(merged)
            merged.append(match)
    merged.sort(key=lambda m: (m.date is None, m.date or m.home_team))
    return merged
