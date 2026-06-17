from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .base import Game, GameFactory, GameInfo, GamePlugin, JsonDict
from .hokm import HokmFactory
from .wordle import WordleFactory


@dataclass(frozen=True)
class RegisteredGame:
    factory: GameFactory
    info: GameInfo
    frontend_path: Path | None = None


class GameRegistry:
    def __init__(self) -> None:
        self._games: dict[str, RegisteredGame] = {}

    @classmethod
    def defaults(cls) -> "GameRegistry":
        registry = cls()
        registry.register(HokmFactory())
        registry.register(WordleFactory())
        return registry

    def register(
        self,
        factory: GameFactory,
        *,
        info: GameInfo | None = None,
        source: str = "builtin",
        frontend_path: Path | None = None,
        replace_existing: bool = False,
    ) -> None:
        registry_info = _registry_info(info or factory.info, source=source)
        if registry_info.key in self._games and not replace_existing:
            raise ValueError(f"Game already registered: {registry_info.key}")
        self._games[registry_info.key] = RegisteredGame(
            factory=factory,
            info=registry_info,
            frontend_path=frontend_path,
        )

    def register_plugin(
        self,
        plugin: GamePlugin,
        *,
        replace_existing: bool = False,
    ) -> None:
        self.register(
            plugin.factory,
            info=plugin.info,
            source=plugin.info.source,
            frontend_path=plugin.frontend_path,
            replace_existing=replace_existing,
        )

    def create(self, key: str, players: list[str] | None = None) -> Game:
        return self._get_factory(key).create(players=players)

    def load(self, key: str, state: JsonDict) -> Game:
        return self._get_factory(key).load(state)

    def info(self, key: str) -> GameInfo:
        return self._get_game(key).info

    def list_games(self) -> list[GameInfo]:
        return sorted(
            (game.info for game in self._games.values()),
            key=lambda info: info.title,
        )

    def frontend_path(self, key: str) -> Path | None:
        return self._get_game(key).frontend_path

    def get_factory(self, key: str) -> GameFactory:
        return self._get_game(key).factory

    def _get_factory(self, key: str) -> GameFactory:
        return self._get_game(key).factory

    def _get_game(self, key: str) -> RegisteredGame:
        normalized = key.lower().strip()
        try:
            return self._games[normalized]
        except KeyError as exc:
            raise ValueError(f"Unknown game: {key}") from exc


def _registry_info(info: GameInfo, *, source: str) -> GameInfo:
    normalized = info.key.lower().strip()
    if normalized != info.key or info.source != source:
        return replace(info, key=normalized, source=source)
    return info
