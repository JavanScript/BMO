import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-plugin"))

from bmo_plugin.lobby import LobbyAlreadyExists, LobbyManager, reaction_from_event


class LobbyManagerTest(unittest.TestCase):
    def test_create_tracks_one_lobby_per_room(self) -> None:
        manager = LobbyManager()
        manager.create(
            room_id="!room:example.org",
            host_id="@host:example.org",
            game_key="wordle",
            message_id="$message",
            min_players=2,
        )

        with self.assertRaises(LobbyAlreadyExists):
            manager.create(
                room_id="!room:example.org",
                host_id="@host:example.org",
                game_key="wordle",
                message_id="$other",
                min_players=2,
            )

    def test_ready_reaction_marks_player_ready(self) -> None:
        manager = LobbyManager(ready_reaction="👍")
        lobby = manager.create(
            room_id="!room:example.org",
            host_id="@host:example.org",
            game_key="wordle",
            message_id="$message",
            min_players=2,
        )

        updated = manager.mark_ready(
            reaction_from_event(
                SimpleNamespace(
                    room_id="!room:example.org",
                    sender="@ada:example.org",
                    content={
                        "m.relates_to": {
                            "rel_type": "m.annotation",
                            "event_id": "$message",
                            "key": "👍",
                        }
                    },
                )
            )
        )

        self.assertIs(updated, lobby)
        self.assertTrue(lobby.can_launch)
        self.assertEqual(lobby.players, ["@ada:example.org", "@host:example.org"])

    def test_duplicate_ready_reaction_is_ignored(self) -> None:
        manager = LobbyManager(ready_reaction="👍")
        manager.create(
            room_id="!room:example.org",
            host_id="@host:example.org",
            game_key="wordle",
            message_id="$message",
            min_players=2,
        )
        event = SimpleNamespace(
            room_id="!room:example.org",
            sender="@ada:example.org",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message",
                    "key": "👍",
                }
            },
        )

        first = manager.mark_ready(reaction_from_event(event))
        second = manager.mark_ready(reaction_from_event(event))

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_render_lobby_includes_players(self) -> None:
        manager = LobbyManager(ready_reaction="👍")
        lobby = manager.create(
            room_id="!room:example.org",
            host_id="@host:example.org",
            game_key="wordle",
            message_id="$message",
            min_players=2,
        )
        lobby.ready_users.add("@ada:example.org")

        text = manager.render_lobby(lobby)

        self.assertIn("Host: @host:example.org", text)
        self.assertIn("Ready: 2/2", text)
        self.assertIn("@ada:example.org", text)
        self.assertIn("Tap 👍", text)


if __name__ == "__main__":
    unittest.main()
