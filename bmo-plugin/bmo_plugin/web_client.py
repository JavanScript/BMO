from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from aiohttp import ClientError, ClientSession


@dataclass(frozen=True)
class PlayerLink:
    player_id: str
    url: str


@dataclass(frozen=True)
class CreatedSession:
    session_id: str
    url: str
    player_links: tuple[PlayerLink, ...]


@dataclass(frozen=True)
class GameInfo:
    key: str
    title: str
    description: str
    min_players: int = 1
    max_players: int | None = None
    private_player_links: bool = False

    @classmethod
    def from_api(cls, data: dict[str, object]) -> "GameInfo":
        max_players = data.get("max_players")
        return cls(
            key=str(data["key"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            min_players=int(data.get("min_players", 1)),
            max_players=None if max_players is None else int(max_players),
            private_player_links=bool(data.get("private_player_links", False)),
        )


class WebSessionError(RuntimeError):
    pass


class BmoWebClient:
    def __init__(self, *, base_url: str, public_base_url: str, shared_secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.shared_secret = shared_secret
        self._session: ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def create_session(
        self,
        *,
        game_key: str,
        lobby_id: str,
        room_id: str,
        players: Iterable[str],
        display_names: dict[str, str] | None = None,
    ) -> CreatedSession:
        payload = {
            "game": game_key,
            "lobby_id": lobby_id,
            "room_id": room_id,
            "players": list(players),
            "public_base_url": self.public_base_url,
        }
        if display_names:
            payload["display_names"] = display_names
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/api/sessions",
                json=payload,
                headers={"X-BMO-Secret": self.shared_secret},
            ) as response:
                data = await response.json()
                if response.status >= 400:
                    raise WebSessionError(data.get("error", f"HTTP {response.status}"))
        except ClientError as exc:
            raise WebSessionError(str(exc)) from exc

        return CreatedSession(
            session_id=data["session_id"],
            url=data["url"],
            player_links=tuple(
                PlayerLink(player_id=link["player_id"], url=link["url"])
                for link in data.get("player_links", [])
            ),
        )

    async def list_games(self) -> tuple[GameInfo, ...]:
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/api/games") as response:
                data = await response.json()
                if response.status >= 400:
                    raise WebSessionError(data.get("error", f"HTTP {response.status}"))
        except ClientError as exc:
            raise WebSessionError(str(exc)) from exc

        return tuple(
            GameInfo.from_api(game)
            for game in data.get("games", [])
            if isinstance(game, dict)
        )

    async def _get_session(self) -> ClientSession:
        if not self._session:
            self._session = ClientSession()
        return self._session
