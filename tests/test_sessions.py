import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

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
        store = SessionStore(shared_secret="secret", public_base_url="https://bmo.example.org")

        with self.assertRaises(ValueError):
            store.create_session(
                game_key="cards",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=[],
            )

    def test_rejects_empty_player_list(self) -> None:
        store = SessionStore(shared_secret="secret", public_base_url="https://bmo.example.org")

        with self.assertRaises(ValueError):
            store.create_session(
                game_key="wordle",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=[],
            )


if __name__ == "__main__":
    unittest.main()
