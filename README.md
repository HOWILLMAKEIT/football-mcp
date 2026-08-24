# football-mcp

A data-provider MCP server for football: results, match stats, odds and
derived analytics (standings, form, head-to-head) for 18 European leagues —
seasons 1993-94 to today — served over stdio to any MCP client.

No API keys required. No opinions baked in: it serves data and server-side
aggregates; the intelligence lives in the calling agent.

## Data sources

| | [football-data.co.uk] (base) | [ESPN scoreboard] (enhancement) |
|---|---|---|
| Role | Depth: 33 seasons of history + full odds | Freshness: minute-level + cups |
| Coverage | 18 leagues, 1993-94 → (E0 PL, SP1 La Liga, I1 Serie A, D1 Bundesliga, F1 Ligue 1, …) | 8 cups (FA, LC, CDR, CI, DFB, CDF, UCL, UEL), ~2001 → |
| Per match | score, HT score, referee, shots/corners/fouls/cards, ~20 bookmakers' 1X2 open+close, Pinnacle, O/U 2.5, Asian handicap | score, kickoff, team stats, possession, assists |
| Freshness | hours-to-a-day after full time | minutes after full time, live state |

The season provider merges both automatically: football-data rows win on
conflicts (official stats + odds), ESPN fills freshness gaps (last night's
matches, not-yet-published season files) and contributes possession/assists.
Team names are canonicalized across sources (`Man United` ≡ `Manchester
United`). Past seasons are cached forever; the current season revalidates
with conditional GETs and fixture-aware staleness detection (kickoff + 2h
with no result → refresh) — idle break weeks cost zero network calls.

## Tools

| Tool | What it does |
|---|---|
| `list_competitions()` | 18 league codes → names |
| `list_teams(competition, season)` | team-name discovery (use before team filters) |
| `get_matches(...)` | results/fixtures with filters (team, dates, played/upcoming), optional stats & compact odds views |
| `get_standings(competition, season, as_of_date?)` | replayed league table; `as_of_date` gives leakage-safe historical views |
| `get_team_form(team, ...)` | last N matches + W/D/L, goals, points-per-game summary |
| `get_head_to_head(a, b, ..., seasons_back?, scope?)` | meetings with wins/draws/goals; `seasons_back=9` covers 10 seasons in one call; `scope`: `league` / `domestic_cups` / `europe` / `all` / a concrete cup code (`UCL`, `UEL`, `CDR`, …), with `by_scope` + `by_competition` breakdowns; if `scope` is omitted the user is asked via MCP elicitation |
| `list_cup_competitions()` | 8 cup codes → names |
| `get_cup_matches(...)` | cup ties incl. penalty-shootout notes (`advance 4-3 on penalties`) and two-legged tie notes (`1st Leg`) |

Semantics worth knowing: penalty shootouts count as final wins in head-to-head
(`pen_wins_a/b` disclose them); goals count regulation/extra time only;
league tables do not include administrative points deductions (source data
has none).

## Install

```bash
# from PyPI (once published)
uvx football-mcp            # runs the server over stdio
# or from a local checkout
uv run football-mcp
```

### Claude Desktop

```json
{
  "mcpServers": {
    "football": {
      "command": "uvx",
      "args": ["football-mcp"]
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "football": {
      "command": "uvx",
      "args": ["football-mcp"]
    }
  }
}
```

### DeepSeek Harness (dsh)

```bash
dsh plugin --profile web add dsh-football-mcp
```

Tools surface as `mcp__football__get_matches`, … in every dsh session.
Requires `uv` on `PATH` (the server runs as `uvx football-mcp`). To hack on
a local checkout instead, override the entry in your profile's
`cordis.patch.yml`:

```yaml
- id: mcp-football
  config:
    command: uv
    args: ['--directory', '/path/to/football_mcp', 'run', 'football-mcp']
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `FOOTBALL_MCP_CACHE_DIR` | `~/.cache/football_mcp` | CSV/ESPN cache location |

No API keys needed for any source.

## Example

```
User:  过去 10 个赛季国家德比，巴萨对皇马胜率？含杯赛
Agent: get_head_to_head("Barcelona", "Real Madrid", "SP1", "2025-26",
                        seasons_back=9, scope="all")
→      25 meetings: Barcelona 12 (48%), Real Madrid 9 (36%), 4 draws
       by_scope: league 20 (9-8-3) · domestic_cups 5 (3-1-1)
```

## Development

```bash
uv sync --extra dev
uv run pytest      # 76 offline tests
uv run ruff check .
```

## License

MIT

[football-data.co.uk]: https://www.football-data.co.uk/
[ESPN scoreboard]: https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard
