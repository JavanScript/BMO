from __future__ import annotations

from bmo_web.games.base import GameInfo, JsonDict
from bmo_web.games.wordle import WordleGame


DEFAULT_ANSWERS = (
    "adieu", "brave", "cider", "delta", "ember", "fjord", "glade",
    "honey", "ivory", "jolly", "karma", "lemon", "mango", "noble",
    "ocean", "pearl", "quilt", "raven", "sugar", "tiger", "umbra",
    "vivid", "whale", "xenon", "yacht", "zebra",
)


class WordlePluginFactory:
    info = GameInfo(
        key="wordle",
        title="Wordle",
        description="Guess a five-letter word in six tries.",
        min_players=1,
        source="plugin",
    )

    def create(self, players: list[str] | None = None):
        from random import SystemRandom
        return WordleGame(answer=SystemRandom().choice(DEFAULT_ANSWERS))

    def load(self, state: JsonDict):
        return WordleGame.from_state(state)


factory = WordlePluginFactory()
