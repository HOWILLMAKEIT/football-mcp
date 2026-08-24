"""Offline tests: name canonicalization and merge dedup."""

from __future__ import annotations

import datetime as dt

from football_mcp.models import Match
from football_mcp.names import canonical, same_team
from football_mcp.sources.merge import merge_matches


class TestNames:
    def test_football_data_short_names(self):
        assert canonical("Man United") == canonical("Manchester United")
        assert canonical("Ath Madrid") == canonical("Atlético Madrid")
        assert canonical("M'gladbach") == canonical("Borussia Mönchengladbach")
        assert canonical("Nott'm Forest") == canonical("Nottingham Forest")
        assert canonical("Paris SG") == canonical("Paris Saint-Germain")

    def test_accents_and_hyphens(self):
        assert same_team("Celta", "Celta Vigo")
        assert same_team("Betis", "Real Betis")
        assert same_team("Köln", "FC Koln")

    def test_unknown_names_passthrough(self):
        assert canonical("Some New FC") == "some new fc"


class TestMerge:
    def _csv_match(self) -> Match:
        return Match(
            competition="E0",
            season="2026-27",
            date=dt.date(2026, 8, 15),
            home_team="Man United",
            away_team="Brighton",
            home_goals=2,
            away_goals=1,
            result="H",
        )

    def _api_match(
        self, home: str = "Manchester United", away: str = "Brighton and Hove Albion"
    ) -> Match:
        return Match(
            competition="E0",
            season="2026-27",
            date=dt.date(2026, 8, 15),
            home_team=home,
            away_team=away,
            home_goals=2,
            away_goals=1,
            result="H",
        )

    def test_dedup_same_match_different_names(self):
        merged = merge_matches([self._csv_match()], [self._api_match()])
        assert len(merged) == 1
        assert merged[0].home_team == "Man United"  # base row wins
        assert merged[0].home_shots is None  # base is a plain row here, still preferred

    def test_different_date_kept(self):
        api = self._api_match()
        api.date = dt.date(2026, 8, 16)
        merged = merge_matches([self._csv_match()], [api])
        assert len(merged) == 2

    def test_extra_only_rows_appended_and_sorted(self):
        api_new = self._api_match(home="Arsenal", away="Chelsea")
        api_new.date = dt.date(2026, 8, 14)
        merged = merge_matches([self._csv_match()], [api_new])
        assert [m.date for m in merged] == [dt.date(2026, 8, 14), dt.date(2026, 8, 15)]
