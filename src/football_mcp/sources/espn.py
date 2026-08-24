"""ESPN public scoreboard source (site.api.espn.com, no key required).

Role: default enhancement source in the freshness ladder. Free, minute-level
freshness, covers every competition we support. Unofficial and undocumented:
ESPN may change it at any time -- callers must treat DataSourceError as an
expected, non-fatal outcome and degrade to CSV-only data.

Quota: none. Be polite instead: responses are cached for a TTL (default 15
min) and requests carry a descriptive User-Agent.

Parsing rule learned from real data: *pre* matches already carry score "0";
only status.type.completed == true marks a finished match.
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

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"

# Our competition code -> ESPN slug (verified 2026-08-24: all respond).
SLUGS: dict[str, str] = {
    "E0": "eng.1",
    "E1": "eng.2",
    "E2": "eng.3",
    "E3": "eng.4",
    "SC0": "sco.1",
    "D1": "ger.1",
    "D2": "ger.2",
    "I1": "ita.1",
    "I2": "ita.2",
    "SP1": "esp.1",
    "SP2": "esp.2",
    "F1": "fra.1",
    "F2": "fra.2",
    "N1": "ned.1",
    "B1": "bel.1",
    "P1": "por.1",
    "T1": "tur.1",
    "G1": "gre.1",
}


def _result_letter(home: int, away: int) -> str:
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_scoreboard(payload: dict[str, Any], competition: str, season: str) -> list[Match]:
    """Parse an ESPN scoreboard response into Match models."""
    matches: list[Match] = []
    for event in payload.get("events") or []:
        iso = event.get("date")
        if not iso or len(iso) < 16:
            continue
        status_type = ((event.get("status") or {}).get("type") or {})
        completed = bool(status_type.get("completed"))
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = {c.get("homeAway"): c for c in competitions[0].get("competitors") or []}
        home = competitors.get("home") or {}
        away = competitors.get("away") or {}
        home_name = ((home.get("team") or {}).get("displayName")) or None
        away_name = ((away.get("team") or {}).get("displayName")) or None
        if not home_name or not away_name:
            continue
        fields: dict[str, Any] = {
            "competition": competition.upper(),
            "season": season,
            "date": dt.date.fromisoformat(iso[:10]),
            "kick_off": iso[11:16],  # UTC kickoff
            "home_team": home_name,
            "away_team": away_name,
        }
        if completed:
            home_goals = _to_int(home.get("score"))
            away_goals = _to_int(away.get("score"))
            if home_goals is not None and away_goals is not None:
                fields["home_goals"] = home_goals
                fields["away_goals"] = away_goals
                fields["result"] = _result_letter(home_goals, away_goals)
        matches.append(Match.model_validate(fields))
    return matches


class EspnSource:
    """Keyless ESPN scoreboard enhancement source with TTL caching."""

    name = "espn"

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        ttl_seconds: float = 900.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        env_dir = os.environ.get("FOOTBALL_MCP_CACHE_DIR")
        default_dir = Path.home() / ".cache" / "football_mcp"
        self.cache_dir = Path(cache_dir or env_dir or default_dir) / "espn"
        self.ttl_seconds = ttl_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def has_key(self) -> bool:
        return True  # keyless by design

    def quota_remaining(self) -> int | None:
        return None  # no quota concept

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
                headers={"User-Agent": "football_mcp/0.1 (MCP data server; polite caching)"},
            )
        return self._client

    def _cache_path(
        self, competition: str, date_from: dt.date, date_to: dt.date
    ) -> Path:
        return self.cache_dir / f"{competition.upper()}-{date_from}-{date_to}.json"

    async def get_fixtures(
        self,
        competition: str,
        season: str,
        date_from: dt.date,
        date_to: dt.date,
    ) -> list[Match]:
        """Fixtures (finished and scheduled) for a league in a date window."""
        competition = competition.upper()
        slug = SLUGS.get(competition)
        if slug is None:
            raise DataSourceError(f"no espn slug for competition {competition!r}")
        if date_from > date_to:
            return []

        cache_path = self._cache_path(competition, date_from, date_to)
        if cache_path.exists():
            if time.time() - cache_path.stat().st_mtime < self.ttl_seconds:
                return parse_scoreboard(
                    json.loads(cache_path.read_text()), competition, season
                )

        response = await self._client_get().get(
            BASE_URL.format(slug=slug),
            params={"dates": f"{date_from:%Y%m%d}-{date_to:%Y%m%d}"},
        )
        if response.status_code == 404:
            raise DataSourceError(f"espn returned 404 for slug {slug!r}")
        response.raise_for_status()
        payload = response.json()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload))
        return parse_scoreboard(payload, competition, season)
