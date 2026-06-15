import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-plugin"))

from bmo_plugin.web_client import GameInfo


class WebClientGameInfoTest(unittest.TestCase):
    def test_parses_game_metadata_from_api(self) -> None:
        info = GameInfo.from_api(
            {
                "key": "cards",
                "title": "Cards",
                "description": "Private cards.",
                "min_players": 2,
                "max_players": None,
                "private_player_links": True,
            }
        )

        self.assertEqual(info.key, "cards")
        self.assertEqual(info.min_players, 2)
        self.assertIsNone(info.max_players)
        self.assertTrue(info.private_player_links)


if __name__ == "__main__":
    unittest.main()
