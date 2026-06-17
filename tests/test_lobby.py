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
        self.assertFalse(lobby.can_launch)
        self.assertEqual(lobby.players, ["@ada:example.org"])

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

        self.assertIn("Host: **@host:example.org**", text)
        self.assertIn("Ready: 1/2", text)
        self.assertIn("@ada:example.org", text)
        self.assertIn("• **", text)
        self.assertIn("✅", text)
        self.assertIn("Tap 👍", text)

    def test_lobby_with_max_players_launches_only_when_full(self) -> None:
        manager = LobbyManager(ready_reaction="👍")
        lobby = manager.create(
            room_id="!room:example.org",
            host_id="@host:example.org",
            game_key="hokm",
            message_id="$message",
            min_players=4,
            max_players=4,
        )

        self.assertFalse(lobby.can_launch)
        for player in (
            "@host:example.org",
            "@a:example.org",
            "@b:example.org",
            "@c:example.org",
        ):
            manager.mark_ready(_reaction(player))

        self.assertTrue(lobby.can_launch)
        self.assertEqual(lobby.ready_count, 4)

    def test_lobby_ignores_extra_ready_reactions_after_full(self) -> None:
        manager = LobbyManager(ready_reaction="👍")
        lobby = manager.create(
            room_id="!room:example.org",
            host_id="@host:example.org",
            game_key="hokm",
            message_id="$message",
            min_players=4,
            max_players=4,
        )

        for player in (
            "@host:example.org",
            "@a:example.org",
            "@b:example.org",
            "@c:example.org",
        ):
            manager.mark_ready(_reaction(player))

        manager.mark_ready(_reaction("@extra:example.org"))

        self.assertEqual(lobby.ready_count, 4)
        self.assertNotIn("@extra:example.org", lobby.players)
        self.assertIn("4/4", manager.render_lobby(lobby))

class ReactionParsingTest(unittest.TestCase):
    def test_parses_typed_relates_to_object(self) -> None:
        # maubot delivers m.relates_to as a typed object (RelatesTo), not a
        # dict. reaction_from_event must coerce it via .serialize().
        class RelatesTo:
            def serialize(self):
                return {
                    "rel_type": "m.annotation",
                    "event_id": "$message",
                    "key": "👍",
                }

        reaction = reaction_from_event(
            SimpleNamespace(
                room_id="!room:example.org",
                sender="@ada:example.org",
                content={"m.relates_to": RelatesTo()},
            )
        )

        self.assertIsNotNone(reaction)
        self.assertEqual(reaction.event_id, "$message")
        self.assertEqual(reaction.key, "👍")

    def test_parses_relates_to_via_dunder_dict(self) -> None:
        relates_to = SimpleNamespace(
            rel_type="m.annotation", event_id="$message", key="👍"
        )
        reaction = reaction_from_event(
            SimpleNamespace(
                room_id="!room:example.org",
                sender="@ada:example.org",
                content={"m.relates_to": relates_to},
            )
        )

        self.assertIsNotNone(reaction)
        self.assertEqual(reaction.key, "👍")


def _reaction(sender: str):
    return reaction_from_event(
        SimpleNamespace(
            room_id="!room:example.org",
            sender=sender,
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message",
                    "key": "👍",
                }
            },
        )
    )


if __name__ == "__main__":
    unittest.main()
