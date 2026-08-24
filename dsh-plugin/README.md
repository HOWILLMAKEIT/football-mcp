# dsh-football-mcp

[DeepSeek Harness](https://github.com/deepseek-ai/dsh) plugin that connects
the [`football-mcp`](https://github.com/HOWILLMAKEIT/football-mcp) server —
a data-provider MCP for football (18 European leagues since 1993-94, 8 cup
competitions, results/stats/odds + derived standings/form/head-to-head) —
to any dsh agent.

## Install

```bash
dsh plugin --profile web add dsh-football-mcp
```

Requirements: [`uv`](https://docs.astral.sh/uv/) on `PATH` (the server is
spawned as `uvx football-mcp` from the PyPI wheel). No API keys needed.

## What you get

Every tool lands in the agent's tool list as `mcp__football__<name>`:

- `mcp__football__list_competitions`, `list_teams`, `list_cup_competitions`
- `mcp__football__get_matches` (results/fixtures, stats, compact odds)
- `mcp__football__get_standings` (leakage-safe `as_of_date`), `get_team_form`
- `mcp__football__get_head_to_head` (cross-season, scoped league/cups/europe)
- `mcp__football__get_cup_matches` (FA/LC/CDR/CI/DFB/CDF/UCL/UEL, shootout notes)

Example: *"过去 10 个赛季国家德比，巴萨对皇马的胜率？含杯赛"* — the agent
calls `get_head_to_head(seasons_back=9, scope=all)` and answers with
per-competition breakdowns.

## Run a local checkout instead

Add to your profile's `cordis.patch.yml` (overrides this bundle's entry):

```yaml
- id: mcp-football
  config:
    command: uv
    args: ['--directory', '/path/to/football_mcp', 'run', 'football-mcp']
```

## Uninstall

```bash
dsh plugin --profile web remove dsh-football-mcp
```
