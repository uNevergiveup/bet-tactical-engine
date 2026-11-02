import unittest

from generate_context import (
    APITeam,
    BwinParser,
    ContextualTeamResolver,
    TeamResolver,
    team_name_similarity,
)


class FakeAPI:
    def get(self, endpoint, params):
        if endpoint == "teams":
            direct = {
                133: "Vasco DA Gama",
                131: "Corinthians",
            }
            if params.get("id") in direct:
                team_id = params["id"]
                return {"response": [{
                    "team": {
                        "id": team_id,
                        "name": direct[team_id],
                        "country": "Brazil",
                        "national": False,
                    },
                    "venue": {},
                }]}
            if params.get("search") == "cusco":
                return {"response": [{
                    "team": {"id": 10013, "name": "Cusco", "country": "Peru", "national": False},
                    "venue": {},
                }]}
            return {"response": []}
        if endpoint == "fixtures":
            opponents = [(10013, "Cusco"), (22543, "FC Cajamarca")]
            return {"response": [
                {
                    "fixture": {"id": fixture_id, "status": {"short": "NS"}},
                    "teams": {
                        "home": {"id": 2546, "name": "Sporting Cristal"},
                        "away": {"id": team_id, "name": name},
                    },
                    "league": {"name": "Primera División"},
                }
                for fixture_id, (team_id, name) in enumerate(opponents, start=1549520)
            ]} if "next" in params else {"response": []}
        raise AssertionError(f"Endpoint inesperado: {endpoint}")


class TeamResolutionTests(unittest.TestCase):
    def test_bwin_slug_preserves_red_bull_bragantino(self):
        url = (
            "https://www.bwin.es/es/sports/eventos/"
            "palmeiras-red-bull-bragantino-2:7885508?market=-1"
        )
        match = BwinParser().parse(url, "<html><title>Bwin</title></html>")
        self.assertEqual(match.home_name, "palmeiras")
        self.assertEqual(match.away_name, "red bull bragantino")

    def test_bwin_slug_preserves_vasco_da_gama(self):
        url = (
            "https://www.bwin.es/es/sports/eventos/"
            "vasco-da-gama-corinthians-sp-2:7885513?market=-1"
        )
        match = BwinParser().parse(url, "<html><title>Bwin</title></html>")
        self.assertEqual(match.home_name, "vasco da gama")
        self.assertEqual(match.away_name, "corinthians sp")

    def test_brazilian_teams_resolve_by_validated_ids(self):
        resolver = TeamResolver(FakeAPI())
        self.assertEqual(resolver.resolve("vasco da gama", "male").id, 133)
        self.assertEqual(resolver.resolve("corinthians sp", "male").id, 131)

    def test_cusco_fc_resolves_to_cusco(self):
        self.assertEqual(TeamResolver(FakeAPI()).resolve("cusco fc", "male").id, 10013)

    def test_context_does_not_confuse_fc_cajamarca(self):
        self.assertEqual(team_name_similarity("cusco fc", "FC Cajamarca"), 0)
        anchor = APITeam(2546, "Sporting Cristal", None, "Peru", None, False, "male", None, None)
        result = ContextualTeamResolver(FakeAPI()).resolve_from_opponent_fixtures(
            "cusco fc", "male", anchor
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.id, 10013)


if __name__ == "__main__":
    unittest.main()
