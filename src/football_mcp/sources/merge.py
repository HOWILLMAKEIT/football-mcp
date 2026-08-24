"""Merge two match lists, preferring the (richer) base rows on duplicates.

合并两份比赛列表；遇到重复比赛时，优先保留信息更丰富的基础数据行。
"""

from __future__ import annotations

from football_mcp.models import Match
from football_mcp.names import canonical


def _key(match: Match) -> tuple:
    return (match.date, canonical(match.home_team), canonical(match.away_team))


def merge_matches(base: list[Match], extra: list[Match]) -> list[Match]:
    """Add `extra` rows not already in `base`, deduplicated by date + teams.

    Base (football-data CSV) rows win because they carry stats and odds; the
    API rows only fill the freshness gap. Rows without a date are appended.
    """
    seen = {_key(m) for m in base if m.date is not None}
    merged = list(base)
    for match in extra:
        if match.date is None or _key(match) not in seen:
            merged.append(match)
            if match.date is not None:
                seen.add(_key(match))
    merged.sort(key=lambda m: (m.date is None, m.date or m.home_team))
    return merged
