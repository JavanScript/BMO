import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-plugin"))

from bmo_plugin.link_delivery import (
    private_player_message,
    public_launch_message,
    requires_private_player_links,
)


class LinkDeliveryTest(unittest.TestCase):
    def test_hokm_requires_private_player_links(self) -> None:
        self.assertTrue(requires_private_player_links("hokm"))
        self.assertFalse(requires_private_player_links("wordle"))

    def test_hokm_public_launch_message_has_no_player_link(self) -> None:
        message = public_launch_message(
            game_key="hokm",
            session_url="https://bmo.example.org/game/session",
            private_links_sent=True,
            failed_player_ids=["@ada:example.org"],
        )

        self.assertIn("Game launched", message)
        self.assertIn("private Matrix room", message)
        self.assertNotIn("token=", message)
        self.assertNotIn("player_id=", message)

    def test_private_player_message_warns_not_to_share_link(self) -> None:
        message = private_player_message(
            "hokm",
            "https://bmo.example.org/game/session?player_id=%40ada&token=secret",
        )

        self.assertIn("token=secret", message)
        self.assertIn("Do not share", message)

    def test_dynamic_private_game_public_message_has_no_player_link(self) -> None:
        message = public_launch_message(
            game_key="custom-cards",
            session_url="https://bmo.example.org/game/session",
            private_links_sent=True,
            private_player_links=True,
        )

        self.assertIn("private Matrix room", message)
        self.assertNotIn("token=", message)
        self.assertNotIn("player_id=", message)


if __name__ == "__main__":
    unittest.main()
