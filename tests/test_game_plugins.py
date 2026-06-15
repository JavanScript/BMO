import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.games.plugins import PluginValidationError, install_plugin_zip
from bmo_web.games.registry import GameRegistry
from bmo_web.sessions import SessionStore


PLUGIN_CODE = """
from bmo_web.games.base import GameReply


class EchoGame:
    key = "echo"

    def __init__(self, text=""):
        self.text = text

    @property
    def ended(self):
        return False

    def handle_action(self, player_id, action, payload):
        if action != "say":
            raise ValueError("unsupported")
        self.text = str(payload.get("text", ""))
        return GameReply(self.text)

    def serialize_public(self, player_id=None):
        return {"echo": self.text}

    def to_state(self):
        return {"text": self.text}


class EchoFactory:
    def create(self, players=None):
        return EchoGame()

    def load(self, state):
        return EchoGame(text=str(state.get("text", "")))


factory = EchoFactory()
"""


class GamePluginTest(unittest.TestCase):
    def test_installs_zip_and_registers_game_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = install_plugin_zip(_plugin_zip(), Path(directory))
            registry = GameRegistry.defaults()
            registry.register_plugin(plugin)
            store = SessionStore(
                shared_secret="secret",
                public_base_url="https://bmo.example.org",
                registry=registry,
            )
            session = store.create_session(
                game_key="echo",
                lobby_id="lobby",
                room_id="!room:example.org",
                players=["@ada:example.org"],
            )
            token = store.player_token(session.session_id, "@ada:example.org")

            result = store.submit_action(
                session_id=session.session_id,
                player_id="@ada:example.org",
                token=token,
                action="say",
                payload={"text": "hello"},
            )
            restored = store.get(session.session_id)

            self.assertEqual(result.reply.message, "hello")
            self.assertTrue(plugin.info.private_player_links)
            self.assertEqual(plugin.frontend_path.name, "index.html")
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(
                store.serialize(restored, player_id="@ada:example.org")["echo"],
                "hello",
            )
            self.assertTrue((Path(directory) / "echo" / "manifest.yaml").is_file())

    def test_rejects_zip_path_traversal(self) -> None:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("manifest.yaml", _manifest())
            archive.writestr("../escape.py", "")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PluginValidationError):
                install_plugin_zip(data.getvalue(), Path(directory))


def _plugin_zip() -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("bundle/manifest.yaml", _manifest())
        archive.writestr("bundle/plugin.py", PLUGIN_CODE)
        archive.writestr(
            "bundle/frontend/index.html",
            "<script>const sessionId = __SESSION_JSON__;</script>",
        )
    return data.getvalue()


def _manifest() -> str:
    return "\n".join(
        [
            "key: echo",
            "title: Echo",
            "description: Echo test plugin.",
            "min_players: 1",
            "max_players: 2",
            "private_player_links: true",
            "frontend: frontend/index.html",
        ]
    )


if __name__ == "__main__":
    unittest.main()
