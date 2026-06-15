from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class GameInfo:
    key: str
    title: str
    description: str
    min_players: int = 1
    max_players: int | None = None


@dataclass(frozen=True)
class GameReply:
    message: str
    ended: bool = False


class Game(Protocol):
    key: str

    @property
    def ended(self) -> bool:
        raise NotImplementedError

    def handle_action(self, player_id: str, action: str, payload: JsonDict) -> GameReply:
        raise NotImplementedError

    def serialize_public(self, player_id: str | None = None) -> JsonDict:
        raise NotImplementedError

    def to_state(self) -> JsonDict:
        raise NotImplementedError


class GameFactory(Protocol):
    info: GameInfo

    def create(self, players: list[str] | None = None) -> Game:
        raise NotImplementedError

    def load(self, state: JsonDict) -> Game:
        raise NotImplementedError
