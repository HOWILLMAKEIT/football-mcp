"""Offline tests: name canonicalization and merge dedup."""

from __future__ import annotations

import datetime as dt

from football_mcp.models import Match, OddsTriple
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

    def test_espn_full_names_align_with_csv_short_names(self):
        assert same_team("Brighton & Hove Albion", "Brighton")  # & folding
        assert same_team("Hull City", "Hull")
        assert same_team("Nottingham Forest", "Nott'm Forest")
        assert same_team("AFC Bournemouth", "Bournemouth")
        assert same_team("Manchester United", "Man United")

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

    def test_played_api_row_replaces_overdue_csv_fixture(self):
        """Stale CSV fixture (no result) must yield to the API result row."""
        csv_row = self._csv_match()
        csv_row.home_goals = None
        csv_row.away_goals = None
        csv_row.result = None
        merged = merge_matches([csv_row], [self._api_match()])
        assert len(merged) == 1
        assert merged[0].played is True
        assert merged[0].home_goals == 2

    def test_played_rows_enrich_missing_fields(self):
        """CSV row keeps its values and odds; ESPN fills only what is missing."""
        csv_row = self._csv_match()
        csv_row.home_shots = 19  # CSV official value
        csv_row.market_avg_close = OddsTriple(home=1.4, draw=5.0, away=8.0)
        api_row = self._api_match()
        api_row.home_shots = 20  # conflicting: CSV wins
        api_row.home_shots_on_target = 7  # missing in CSV: filled from ESPN
        api_row.home_possession = 61.2  # ESPN-only: filled
        merged = merge_matches([csv_row], [api_row])
        assert len(merged) == 1
        row = merged[0]
        assert row.home_shots == 19  # base wins on conflict
        assert row.home_shots_on_target == 7
        assert row.home_possession == 61.2
        assert row.market_avg_close is not None  # base-only richness preserved
        assert row.home_team == "Man United"  # identity preserved
