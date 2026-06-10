from __future__ import annotations

from .base import Game, GameFactory, GameInfo, JsonDict
from .wordle import WordleFactory


class GameRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, GameFactory] = {}

    @classmethod
    def defaults(cls) -> "GameRegistry":
        registry = cls()
        registry.register(WordleFactory())
        return registry

    def register(self, factory: GameFactory) -> None:
        self._factories[factory.info.key] = factory

    def create(self, key: str) -> Game:
        return self._get_factory(key).create()

    def load(self, key: str, state: JsonDict) -> Game:
        return self._get_factory(key).load(state)

    def info(self, key: str) -> GameInfo:
        return self._get_factory(key).info

    def list_games(self) -> list[GameInfo]:
        return sorted(
            (factory.info for factory in self._factories.values()),
            key=lambda info: info.title,
        )

    def _get_factory(self, key: str) -> GameFactory:
        normalized = key.lower().strip()
        try:
            return self._factories[normalized]
        except KeyError as exc:
            raise ValueError(f"Unknown game: {key}") from exc

