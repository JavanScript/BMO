import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.games.base import GameInfo, GameReply
from bmo_web.games.registry import GameRegistry
from bmo_web.sessions import SessionStore


class SessionStoreTest(unittest.TestCase):
    def test_creates_wordle_session_url(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )

        session = store.create_session(
            game_key="wordle",
            lobby_id="lobby",
            room_id="!room:example.org",
            players=["@ada:example.org", "@ada:example.org"],
        )

        self.assertEqual(session.players, ["@ada:example.org"])
        self.assertEqual(
            store.public_url(session),
            f"https://bmo.example.org/game/{session.session_id}",
        )

    def test_player_links_are_signed(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )
        session = store.create_session(
            game_key="wordle",
            lobby_id="lobby",
            room_id="!room:example.org",
            players=["@ada:example.org"],
        )

        links = store.player_links(session)

        self.assertEqual(len(links), 1)
        self.assertIn("player_id=%40ada%3Aexample.org", links[0].url)
        self.assertIn("token=", links[0].url)

    def test_rejects_invalid_player_token(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )
        session = store.create_session(
            game_key="wordle",
            lobby_id="lobby",
            room_id="!room:example.org",
            players=["@ada:example.org"],
        )

        with self.assertRaises(PermissionError):
            store.require_player(session, "@ada:example.org", "wrong")

    def test_persists_game_state_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bmo.sqlite3"
            first_store = SessionStore(
                shared_secret="secret",
                public_base_url="https://bmo.example.org",
                db_path=db_path,
            )
            session = first_store.create_session(
                game_key="wordle",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=["@ada:example.org"],
            )
            token = first_store.player_token(session.session_id, "@ada:example.org")

            first_store.submit_action(
                session_id=session.session_id,
                player_id="@ada:example.org",
                token=token,
                action="guess",
                payload={"guess": "adieu"},
            )

            second_store = SessionStore(
                shared_secret="secret",
                public_base_url="https://bmo.example.org",
                db_path=db_path,
            )
            restored = second_store.get(session.session_id)

            self.assertIsNotNone(restored)
            self.assertIn("A", restored.game.board)

    def test_rejects_unknown_game(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )

        with self.assertRaises(ValueError):
            store.create_session(
                game_key="cards",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=[],
            )

    def test_rejects_empty_player_list(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )

        with self.assertRaises(ValueError):
            store.create_session(
                game_key="wordle",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=[],
            )

    def test_hokm_requires_exactly_four_players(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )

        with self.assertRaises(ValueError):
            store.create_session(
                game_key="hokm",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=["@a:example.org", "@b:example.org", "@c:example.org"],
            )

        session = store.create_session(
            game_key="hokm",
            lobby_id="lobby",
            room_id="!room:example.org",
            players=[
                "@a:example.org",
                "@b:example.org",
                "@c:example.org",
                "@d:example.org",
            ],
        )

        self.assertEqual(session.game_key, "hokm")
        self.assertEqual(len(session.players), 4)

    def test_hokm_serialization_does_not_expose_other_hands(self) -> None:
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
        )
        session = store.create_session(
            game_key="hokm",
            lobby_id="lobby",
            room_id="!room:example.org",
            players=[
                "@a:example.org",
                "@b:example.org",
                "@c:example.org",
                "@d:example.org",
            ],
        )
        hakem = session.game.hakem
        token = store.player_token(session.session_id, hakem)
        store.submit_action(
            session_id=session.session_id,
            player_id=hakem,
            token=token,
            action="choose_trump",
            payload={"suit": "spades"},
        )
        restored = store.get(session.session_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        other_player = next(player for player in restored.players if player != hakem)

        hakem_data = store.serialize(restored, player_id=hakem)
        other_data = store.serialize(restored, player_id=other_player)
        other_hand_ids = [card["id"] for card in other_data["hand"]]

        self.assertNotIn("hands", hakem_data)
        for card_id in other_hand_ids:
            self.assertNotIn(card_id, json.dumps(hakem_data))

    def test_submit_action_serializes_concurrent_updates(self) -> None:
        registry = GameRegistry()
        registry.register(SlowCounterFactory())
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
            registry=registry,
        )
        session = store.create_session(
            game_key="slow_counter",
            lobby_id="lobby",
            room_id="!room:example.org",
            players=["@ada:example.org"],
        )
        token = store.player_token(session.session_id, "@ada:example.org")
        start = threading.Event()
        errors: list[BaseException] = []

        def submit() -> None:
            start.wait(timeout=2)
            try:
                store.submit_action(
                    session_id=session.session_id,
                    player_id="@ada:example.org",
                    token=token,
                    action="increment",
                    payload={},
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        restored = store.get(session.session_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        data = store.serialize(restored, player_id="@ada:example.org")
        self.assertEqual(data["count"], 2)


class SlowCounterGame:
    key = "slow_counter"

    def __init__(self, count: int = 0) -> None:
        self.count = count

    @property
    def ended(self) -> bool:
        return False

    def handle_action(
        self,
        player_id: str,
        action: str,
        payload: dict[str, object],
    ) -> GameReply:
        current = self.count
        time.sleep(0.03)
        self.count = current + 1
        return GameReply(f"{current}->{self.count}")

    def serialize_public(self, player_id: str | None = None) -> dict[str, object]:
        return {"count": self.count}

    def to_state(self) -> dict[str, object]:
        return {"count": self.count}


class SlowCounterFactory:
    info = GameInfo(
        key="slow_counter",
        title="Slow counter",
        description="Concurrency regression test game.",
    )

    def create(self, players: list[str] | None = None) -> SlowCounterGame:
        return SlowCounterGame()

    def load(self, state: dict[str, object]) -> SlowCounterGame:
        return SlowCounterGame(count=int(state["count"]))


if __name__ == "__main__":
    unittest.main()
