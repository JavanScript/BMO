import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.app import (
    _mint_admin_token,
    _verify_admin_token,
    admin_debug_session,
    admin_summary,
    create_app,
    list_games,
)
from bmo_web.games.registry import GameRegistry
from bmo_web.sessions import SessionStore


class AppApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        store = SessionStore(
            shared_secret="secret",
            public_base_url="https://bmo.example.org",
            registry=GameRegistry.defaults(),
        )
        self.app = create_app(
            store=store,
            plugins_dir=Path(self.temp_dir.name),
            admin_password="admin",
            enable_plugin_uploads=False,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_lists_games_from_registry(self) -> None:
        request = make_mocked_request("GET", "/api/games", app=self.app)
        response = await list_games(request)
        data = _json(response)

        self.assertEqual(response.status, 200)
        games = {game["key"]: game for game in data["games"]}
        self.assertIn("wordle", games)
        self.assertTrue(games["hokm"]["private_player_links"])
        self.assertEqual(games["hokm"]["max_players"], 4)

    async def test_admin_summary_requires_admin_header(self) -> None:
        unauthorized = make_mocked_request(
            "GET",
            "/api/admin/summary",
            app=self.app,
        )
        with self.assertRaises(web.HTTPUnauthorized):
            await admin_summary(unauthorized)

        authorized = make_mocked_request(
            "GET",
            "/api/admin/summary",
            headers={"X-BMO-Admin": "admin"},
            app=self.app,
        )
        response = await admin_summary(authorized)
        data = _json(response)

        self.assertEqual(response.status, 200)
        self.assertFalse(data["config"]["plugin_uploads_enabled"])
        self.assertEqual(data["plugin_errors"], [])

    async def test_shared_secret_can_authorize_admin_api(self) -> None:
        request = make_mocked_request(
            "GET",
            "/api/admin/summary",
            headers={"X-BMO-Secret": "secret"},
            app=self.app,
        )
        response = await admin_summary(request)

        self.assertEqual(response.status, 200)

    async def test_debug_session_creates_playable_links(self) -> None:
        import asyncio

        loop = asyncio.get_event_loop()
        stream = StreamReader(protocol=mock.Mock(), limit=2**16, loop=loop)
        stream.feed_data(b'{"game": "hokm"}')
        stream.feed_eof()
        request = make_mocked_request(
            "POST",
            "/api/admin/debug/session",
            headers={"X-BMO-Secret": "secret", "Content-Type": "application/json"},
            payload=stream,
            app=self.app,
        )
        response = await admin_debug_session(request)
        data = _json(response)

        self.assertEqual(response.status, 200)
        self.assertEqual(len(data["player_links"]), 4)
        for link in data["player_links"]:
            self.assertIn("player_id=", link["url"])
            self.assertIn("token=", link["url"])


class AdminTokenTest(unittest.TestCase):
    def test_minted_token_verifies(self) -> None:
        token = _mint_admin_token("secret", now=1000.0)
        self.assertTrue(_verify_admin_token("secret", token, now=1000.0))

    def test_rejects_wrong_secret(self) -> None:
        token = _mint_admin_token("secret", now=1000.0)
        self.assertFalse(_verify_admin_token("other-secret", token, now=1000.0))

    def test_rejects_expired_token(self) -> None:
        token = _mint_admin_token("secret", now=1000.0)
        self.assertFalse(_verify_admin_token("secret", token, now=1000.0 + 3601))

    def test_rejects_tampered_or_garbage(self) -> None:
        self.assertFalse(_verify_admin_token("secret", "", now=1000.0))
        self.assertFalse(_verify_admin_token("secret", "9999999999", now=1000.0))
        self.assertFalse(
            _verify_admin_token("secret", "9999999999.deadbeef", now=1000.0)
        )


def _json(response: web.Response) -> dict[str, object]:
    import json

    return json.loads(response.text)


if __name__ == "__main__":
    unittest.main()
