import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.games.hokm import Card, HokmGame, _full_deck


PLAYERS = [
    "@ava:example.org",
    "@ben:example.org",
    "@cyra:example.org",
    "@dan:example.org",
]


def card(suit: str, rank: str) -> Card:
    return Card(suit=suit, rank=rank)


def setup_deck() -> list[Card]:
    return [card("spades", "A"), card("hearts", "2"), card("clubs", "A")]


def make_game() -> HokmGame:
    return HokmGame.new(
        PLAYERS,
        setup_deck=setup_deck(),
        deal_deck=_full_deck(),
    )


def make_play_game() -> HokmGame:
    game = make_game()
    game.choose_trump(PLAYERS[0], suit="spades")
    game.phase = "play"
    game.trump_suit = "spades"
    game.current_turn = PLAYERS[0]
    game.current_trick = []
    game.hand_tricks = {0: 0, 1: 0}
    game.hands = {
        PLAYERS[0]: [card("hearts", "A"), card("spades", "2")],
        PLAYERS[1]: [card("hearts", "K"), card("clubs", "A")],
        PLAYERS[2]: [card("hearts", "2"), card("diamonds", "A")],
        PLAYERS[3]: [card("spades", "A"), card("clubs", "2")],
    }
    return game


class HokmSetupTest(unittest.TestCase):
    def test_setup_seats_partners_opposite_and_deals_hakem_first_five(self) -> None:
        game = make_game()

        self.assertEqual(game.hakem, PLAYERS[0])
        self.assertEqual(game.seats, PLAYERS)
        self.assertEqual(game.team_of[PLAYERS[0]], game.team_of[PLAYERS[2]])
        self.assertNotEqual(game.team_of[PLAYERS[0]], game.team_of[PLAYERS[1]])
        self.assertEqual(game.phase, "choose_trump")
        self.assertEqual(len(game.hands[PLAYERS[0]]), 5)
        self.assertEqual(len(game.hands[PLAYERS[1]]), 0)
        self.assertEqual(len(game.hands[PLAYERS[2]]), 0)
        self.assertEqual(len(game.hands[PLAYERS[3]]), 0)

    def test_choose_trump_deals_all_cards(self) -> None:
        game = make_game()

        reply = game.choose_trump(PLAYERS[0], suit="hearts")

        self.assertIn("♥", reply.message)
        self.assertEqual(game.phase, "play")
        self.assertEqual(game.trump_suit, "hearts")
        self.assertEqual(game.current_turn, PLAYERS[0])
        self.assertEqual({player: len(hand) for player, hand in game.hands.items()}, {
            PLAYERS[0]: 13,
            PLAYERS[1]: 13,
            PLAYERS[2]: 13,
            PLAYERS[3]: 13,
        })
        self.assertEqual(game.undealt, [])

    def test_only_hakem_can_choose_trump(self) -> None:
        game = make_game()

        with self.assertRaises(PermissionError):
            game.choose_trump(PLAYERS[1], suit="clubs")


class HokmPlayTest(unittest.TestCase):
    def test_play_enforces_turn_and_follow_suit_then_resolves_trump_winner(self) -> None:
        game = make_play_game()

        with self.assertRaises(PermissionError):
            game.play_card(PLAYERS[1], card_id="hearts:K")

        game.play_card(PLAYERS[0], card_id="hearts:A")
        with self.assertRaises(ValueError):
            game.play_card(PLAYERS[1], card_id="clubs:A")

        game.play_card(PLAYERS[1], card_id="hearts:K")
        game.play_card(PLAYERS[2], card_id="hearts:2")
        reply = game.play_card(PLAYERS[3], card_id="spades:A")

        self.assertIn(PLAYERS[3], reply.message)
        self.assertEqual(game.hand_tricks[1], 1)
        self.assertEqual(game.current_turn, PLAYERS[3])
        self.assertEqual(game.last_trick["winner"], PLAYERS[3])

    def test_normal_hand_win_scores_one_and_keeps_hakem(self) -> None:
        game = make_play_game()
        game.hand_tricks = {0: 6, 1: 1}
        game.hands = {
            PLAYERS[0]: [card("hearts", "A")],
            PLAYERS[1]: [card("hearts", "K")],
            PLAYERS[2]: [card("hearts", "Q")],
            PLAYERS[3]: [card("hearts", "J")],
        }

        game.play_card(PLAYERS[0], card_id="hearts:A")
        game.play_card(PLAYERS[1], card_id="hearts:K")
        game.play_card(PLAYERS[2], card_id="hearts:Q")
        reply = game.play_card(PLAYERS[3], card_id="hearts:J")

        self.assertFalse(reply.ended)
        self.assertEqual(game.scores[0], 1)
        self.assertEqual(game.last_hand["points"], 1)
        self.assertEqual(game.last_hand["result"], "normal")
        self.assertEqual(game.hakem, PLAYERS[0])
        self.assertEqual(game.phase, "choose_trump")
        self.assertIsNotNone(game.last_trick)
        self.assertEqual(game.last_trick["winner"], PLAYERS[0])

    def test_hakem_koti_scores_three_and_advances_hakem_right(self) -> None:
        game = make_play_game()
        game.hand_tricks = {0: 0, 1: 6}
        game.hands = {
            PLAYERS[0]: [card("hearts", "2")],
            PLAYERS[1]: [card("hearts", "A")],
            PLAYERS[2]: [card("hearts", "3")],
            PLAYERS[3]: [card("hearts", "4")],
        }

        game.play_card(PLAYERS[0], card_id="hearts:2")
        game.play_card(PLAYERS[1], card_id="hearts:A")
        game.play_card(PLAYERS[2], card_id="hearts:3")
        game.play_card(PLAYERS[3], card_id="hearts:4")

        self.assertEqual(game.scores[1], 3)
        self.assertEqual(game.last_hand["points"], 3)
        self.assertEqual(game.last_hand["result"], "hakem_koti")
        self.assertEqual(game.hakem, PLAYERS[1])
        self.assertEqual(game.seats[0], PLAYERS[1])

    def test_match_ends_at_seven_points(self) -> None:
        game = make_play_game()
        game.scores = {0: 6, 1: 0}
        game.hand_tricks = {0: 6, 1: 1}
        game.hands = {
            PLAYERS[0]: [card("hearts", "A")],
            PLAYERS[1]: [card("hearts", "K")],
            PLAYERS[2]: [card("hearts", "Q")],
            PLAYERS[3]: [card("hearts", "J")],
        }

        game.play_card(PLAYERS[0], card_id="hearts:A")
        game.play_card(PLAYERS[1], card_id="hearts:K")
        game.play_card(PLAYERS[2], card_id="hearts:Q")
        reply = game.play_card(PLAYERS[3], card_id="hearts:J")

        self.assertTrue(reply.ended)
        self.assertTrue(game.ended)
        self.assertEqual(game.phase, "finished")
        self.assertEqual(game.winner_team, 0)


class HokmStateTest(unittest.TestCase):
    def test_persists_and_restores_state(self) -> None:
        game = make_game()
        game.choose_trump(PLAYERS[0], suit="clubs")

        restored = HokmGame.from_state(game.to_state())

        self.assertEqual(restored.hakem, game.hakem)
        self.assertEqual(restored.trump_suit, "clubs")
        self.assertEqual(restored.scores, game.scores)
        self.assertEqual(restored.hands[PLAYERS[0]], game.hands[PLAYERS[0]])

    def test_serialization_only_includes_viewers_hand(self) -> None:
        game = make_play_game()
        game.hands[PLAYERS[0]] = [card("hearts", "A")]
        game.hands[PLAYERS[1]] = [card("clubs", "9")]

        data = game.serialize_public(player_id=PLAYERS[0])

        self.assertEqual([card_data["id"] for card_data in data["hand"]], ["hearts:A"])
        self.assertNotIn("clubs:9", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
