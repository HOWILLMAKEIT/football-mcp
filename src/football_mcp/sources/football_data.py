"""football-data.co.uk season CSV source.

Design:
- Plan A parsing: one central column-mapping table; anything not in the table
  is ignored. Adding a field later means touching exactly one place.
- Freshness: past seasons are immutable and cached forever; the current season
  is revalidated after a TTL via conditional GET (If-Modified-Since), so last
  night's matches show up without re-downloading on every call.
- Old seasons have fewer columns; every mapped column is optional at parse time.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from football_mcp.models import Match, OddsTriple

BASE_URL = "https://www.football-data.co.uk/mmz4281/{code}/{div}.csv"

# Division code -> human name (main European competitions).
SUPPORTED_COMPETITIONS: dict[str, str] = {
    "E0": "England Premier League",
    "E1": "England Championship",
    "E2": "England League One",
    "E3": "England League Two",
    "SC0": "Scotland Premiership",
    "D1": "Germany Bundesliga",
    "D2": "Germany 2. Bundesliga",
    "I1": "Italy Serie A",
    "I2": "Italy Serie B",
    "SP1": "Spain La Liga",
    "SP2": "Spain Segunda",
    "F1": "France Ligue 1",
    "F2": "France Ligue 2",
    "N1": "Netherlands Eredivisie",
    "B1": "Belgium Pro League",
    "P1": "Portugal Liga",
    "T1": "Turkey Super Lig",
    "G1": "Greece Super League",
}

_DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y")


class DataSourceError(RuntimeError):
    """Raised when a data source cannot satisfy a request."""


def _to_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


# Central column map: CSV header -> (model field, parser). Plan A single point.
COLUMN_MAP: dict[str, tuple[str, Callable[[str | None], Any]]] = {
    "Time": ("kick_off", _to_str),
    "HomeTeam": ("home_team", _to_str),
    "AwayTeam": ("away_team", _to_str),
    "FTHG": ("home_goals", _to_int),
    "FTAG": ("away_goals", _to_int),
    "FTR": ("result", _to_str),
    "HTHG": ("half_time_home_goals", _to_int),
    "HTAG": ("half_time_away_goals", _to_int),
    "Referee": ("referee", _to_str),
    "HS": ("home_shots", _to_int),
    "AS": ("away_shots", _to_int),
    "HST": ("home_shots_on_target", _to_int),
    "AST": ("away_shots_on_target", _to_int),
    "HC": ("home_corners", _to_int),
    "AC": ("away_corners", _to_int),
    "HF": ("home_fouls", _to_int),
    "AF": ("away_fouls", _to_int),
    "HY": ("home_yellow_cards", _to_int),
    "AY": ("away_yellow_cards", _to_int),
    "HR": ("home_red_cards", _to_int),
    "AR": ("away_red_cards", _to_int),
}

# Odds columns are assembled into OddsTriple / flat fields after base parsing.
ODDS_COLUMNS = {
    "pinnacle_open": ("PSH", "PSD", "PSA"),
    "market_avg_open": ("AvgH", "AvgD", "AvgA"),
    "pinnacle_close": ("PSCH", "PSCD", "PSCA"),
    "market_avg_close": ("AvgCH", "AvgCD", "AvgCA"),
}
ODDS_FLAT = {
    "market_avg_close_over25": "AvgC>2.5",
    "market_avg_close_under25": "AvgC<2.5",
    "ah_close_line": "AHCh",
    "ah_close_home": "AvgCAHH",
    "ah_close_away": "AvgCAHA",
}


def season_to_code(season: str) -> str:
    """'2025-26' -> '2526' (football-data URL path segment)."""
    try:
        start, end = season.split("-")
        if len(start) != 4 or len(end) != 2:
            raise ValueError
        if int(end) != (int(start) + 1) % 100:
            raise ValueError
    except ValueError as exc:
        msg = f"invalid season label: {season!r} (expected e.g. '2025-26')"
        raise DataSourceError(msg) from exc
    return f"{start[2:]}{end}"


def _today() -> dt.date:
    return dt.datetime.now(tz=dt.UTC).date()


def current_season_label(today: dt.date | None = None) -> str:
    """European seasons run Aug..May; from July on, the new season has started."""
    today = today or _today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def is_past_season(season: str, today: dt.date | None = None) -> bool:
    today = today or _today()
    return season_to_code(season) < season_to_code(current_season_label(today))


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_season_csv(text: str, competition: str, season: str) -> list[Match]:
    """Parse one season CSV into Match models; tolerate missing columns."""
    matches: list[Match] = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        home = _to_str(row.get("HomeTeam"))
        away = _to_str(row.get("AwayTeam"))
        if not home or not away:
            continue  # footer / malformed rows
        fields: dict[str, Any] = {"competition": competition.upper(), "season": season}
        for column, (field, parser) in COLUMN_MAP.items():
            parsed = parser(row.get(column))
            if parsed is not None:
                fields[field] = parsed
        fields["date"] = parse_date(row.get("Date"))
        for target, (h, d, a) in ODDS_COLUMNS.items():
            triple = OddsTriple(
                home=_to_float(row.get(h)),
                draw=_to_float(row.get(d)),
                away=_to_float(row.get(a)),
            )
            if triple.home is not None or triple.draw is not None or triple.away is not None:
                fields[target] = triple
        for field, column in ODDS_FLAT.items():
            value = _to_float(row.get(column))
            if value is not None:
                fields[field] = value
        matches.append(Match.model_validate(fields))
    return matches


class FootballDataSource:
    """Downloads and caches football-data.co.uk season CSVs with freshness."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        ttl_seconds: float = 3600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        env_dir = os.environ.get("FOOTBALL_MCP_CACHE_DIR")
        default_dir = Path.home() / ".cache" / "football_mcp"
        self.cache_dir = Path(cache_dir or env_dir or default_dir) / "football-data"
        self.ttl_seconds = ttl_seconds
        self._client = client
        self._owns_client = client is None

    def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                follow_redirects=True,
                headers={"User-Agent": "football_mcp/0.1 (data provider MCP server)"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # -- csv cache ---------------------------------------------------------

    def _csv_path(self, competition: str, season: str) -> Path:
        return self.cache_dir / f"{season_to_code(season)}-{competition.upper()}.csv"

    def _meta_path(self, path: Path) -> Path:
        return path.with_suffix(".csv.meta.json")

    def _load_meta(self, path: Path) -> dict[str, Any]:
        meta_path = self._meta_path(path)
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, path: Path, last_modified: str | None) -> None:
        self._meta_path(path).write_text(
            json.dumps({"fetched_at": time.time(), "last_modified": last_modified})
        )

    def _cache_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        meta = self._load_meta(path)
        fetched_at = meta.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return False
        return (time.time() - fetched_at) < self.ttl_seconds

    async def ensure_csv(self, competition: str, season: str) -> Path:
        """Return a local CSV path for (competition, season), revalidating if stale.

        Past seasons are immutable: cached forever without network calls.
        The current season is rechecked with a conditional GET after the TTL.
        """
        competition = competition.upper()
        if competition not in SUPPORTED_COMPETITIONS:
            raise DataSourceError(
                f"unsupported competition code: {competition!r}; "
                f"known: {', '.join(SUPPORTED_COMPETITIONS)}"
            )
        path = self._csv_path(competition, season)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists() and (is_past_season(season) or self._cache_fresh(path)):
            return path

        headers: dict[str, str] = {}
        last_modified = self._load_meta(path).get("last_modified")
        if path.exists() and isinstance(last_modified, str):
            headers["If-Modified-Since"] = last_modified

        url = BASE_URL.format(code=season_to_code(season), div=competition)
        response = await self._client_get().get(url, headers=headers)
        if response.status_code == 304:
            self._write_meta(path, last_modified)  # refresh TTL, keep content
            return path
        if response.status_code in (300, 404):
            # football-data answers unpublished season files with a 300 redirect
            # (httpx refuses to auto-follow 300) or a plain 404.
            raise DataSourceError(
                f"season {season} for {competition} is not published on football-data.co.uk yet"
            )
        response.raise_for_status()
        path.write_text(response.text, encoding="utf-8")
        self._write_meta(path, response.headers.get("Last-Modified"))
        return path

    # -- public api --------------------------------------------------------

    async def get_season(self, competition: str, season: str) -> list[Match]:
        path = await self.ensure_csv(competition, season)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise DataSourceError(f"cannot read cached csv {path}: {exc}") from exc
        return parse_season_csv(text, competition, season)
