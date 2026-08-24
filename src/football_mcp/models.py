"""Domain models shared across football_mcp sources and tools.

football_mcp 数据源与工具共享的领域模型。
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel


class OddsTriple(BaseModel):
    """Decimal odds for home / draw / away (1X2)."""

    home: float | None = None
    draw: float | None = None
    away: float | None = None


class Match(BaseModel):
    """One football match as served by football_mcp.

    Scores and stats are optional: rows for not-yet-played fixtures carry
    team names and (sometimes) odds but no result.
    """

    competition: str  # football-data division code, e.g. "E0"
    season: str  # label, e.g. "2025-26"
    date: dt.date | None = None
    kick_off: str | None = None  # HH:MM local time as recorded by the source
    home_team: str
    away_team: str

    home_goals: int | None = None
    away_goals: int | None = None
    result: Literal["H", "D", "A"] | None = None
    note: str | None = None  # cup semantics: round leg, penalty shootouts, etc.
    half_time_home_goals: int | None = None
    half_time_away_goals: int | None = None
    referee: str | None = None

    home_shots: int | None = None
    away_shots: int | None = None
    home_shots_on_target: int | None = None
    away_shots_on_target: int | None = None
    home_corners: int | None = None
    away_corners: int | None = None
    home_fouls: int | None = None
    away_fouls: int | None = None
    home_yellow_cards: int | None = None
    away_yellow_cards: int | None = None
    home_red_cards: int | None = None
    away_red_cards: int | None = None

    # ESPN-only enrichments; football-data CSVs never carry these, so they
    # stay None on CSV rows and may be filled by the ESPN enhancement source.
    home_possession: float | None = None  # percent, e.g. 64.5
    away_possession: float | None = None
    home_assists: int | None = None  # goal assists
    away_assists: int | None = None

    # Odds (decimal). Opening vs closing; Pinnacle vs market average.
    pinnacle_open: OddsTriple | None = None
    market_avg_open: OddsTriple | None = None
    pinnacle_close: OddsTriple | None = None
    market_avg_close: OddsTriple | None = None
    market_avg_close_over25: float | None = None
    market_avg_close_under25: float | None = None
    ah_close_line: float | None = None  # Asian handicap line, home perspective
    ah_close_home: float | None = None
    ah_close_away: float | None = None

    @property
    def played(self) -> bool:
        return self.home_goals is not None and self.away_goals is not None
