"""Team-name canonicalization for cross-source deduplication.

football-data.co.uk uses short names ("Man United", "Ath Madrid", "M'gladbach")
while API-Football uses full names ("Manchester United", "Atlético Madrid",
"Borussia Mönchengladbach"). Both sides are folded onto one canonical form so
matches from the two sources can be deduplicated safely.
"""

from __future__ import annotations

import re
import unicodedata


def _normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace(".", " ").replace("-", " ").replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


# source variant (any side) -> canonical form. Keys are matched normalized.
_ALIASES: dict[str, str] = {
    # England
    "man united": "manchester united",
    "man city": "manchester city",
    "sheff united": "sheffield united",
    "sheffield utd": "sheffield united",
    "sheff wed": "sheffield wednesday",
    "nottm forest": "nottingham forest",
    "tottenham": "tottenham hotspur",
    "west ham": "west ham united",
    "wolves": "wolverhampton wanderers",
    "brighton": "brighton and hove albion",
    "brighton and hove": "brighton and hove albion",
    "leicester": "leicester city",
    "ipswich": "ipswich town",
    "newcastle": "newcastle united",
    "bournemouth": "afc bournemouth",
    "leeds": "leeds united",
    "wba": "west bromwich albion",
    "stoke": "stoke city",
    "preston": "preston north end",
    "norwich": "norwich city",
    "swansea": "swansea city",
    "blackburn": "blackburn rovers",
    "derby": "derby county",
    "plymouth": "plymouth argyle",
    "luton": "luton town",
    "charlton": "charlton athletic",
    "birmingham": "birmingham city",
    "wigan": "wigan athletic",
    "bristol city": "bristol city",
    "hull city": "hull city",
    "coventry": "coventry city",
    "qpr": "queens park rangers",
    # Spain
    "ath madrid": "atletico madrid",
    "atl madrid": "atletico madrid",
    "ath bilbao": "athletic bilbao",
    "betis": "real betis",
    "sociedad": "real sociedad",
    "espanol": "espanyol",
    "celta": "celta vigo",
    "vallecano": "rayo vallecano",
    "alaves": "deportivo alaves",
    "valladolid": "real valladolid",
    # Italy
    "milan": "ac milan",
    "roma": "as roma",
    "verona": "hellas verona",
    "inter milano": "inter",
    # Germany
    "bayern munich": "bayern munich",
    "bayern": "bayern munich",
    "dortmund": "borussia dortmund",
    "leverkusen": "bayer leverkusen",
    "bayer 04 leverkusen": "bayer leverkusen",
    "m gladbach": "borussia monchengladbach",
    "mgladbach": "borussia monchengladbach",
    "ein frankfurt": "eintracht frankfurt",
    "werder bremen": "werder bremen",
    "koln": "fc koln",
    "1 fc koln": "fc koln",
    "mainz": "mainz 05",
    "hofenheim": "tsg hoffenheim",
    "augsburg": "fc augsburg",
    "stuttgart": "vfb stuttgart",
    "freiburg": "sc freiburg",
    "wolfsburg": "vfl wolfsburg",
    "bochum": "vfl bochum",
    "heidenheim": "fc heidenheim",
    "st pauli": "fc st pauli",
    "hertha": "hertha berlin",
    "darmstadt": "sv darmstadt 98",
    "greuther furth": "greuther furth",
    # France
    "paris sg": "paris saint germain",
    "paris fc": "paris fc",
    "saint etienne": "saint etienne",
}


def canonical(name: str) -> str:
    """Fold a team name from either source onto its canonical form."""
    normalized = _normalize(name)
    return _ALIASES.get(normalized, normalized)


def same_team(a: str, b: str) -> bool:
    return canonical(a) == canonical(b)
