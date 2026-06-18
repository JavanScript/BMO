from __future__ import annotations

import random
from typing import Callable

from .base import GameFactory, JsonDict


AutoPlayer = Callable[[JsonDict, str], tuple[str, JsonDict] | None]


def _auto_play_card(state: JsonDict, player_id: str) -> tuple[str, JsonDict] | None:
    playable = state.get("playable_card_ids", [])
    if playable:
        return "play_card", {"card_id": random.choice(playable)}  # nosec B311
    return None


def _auto_choose_trump(state: JsonDict, player_id: str) -> tuple[str, JsonDict] | None:
    if state.get("can_choose_trump"):
        options = state.get("trump_options", [])
        if options:
            return "choose_trump", {"suit": random.choice(options)["suit"]}  # nosec B311
    return None


_WORDS = ("crane", "adieu", "stork", "vivid", "lemon", "ghost", "plumb", "waltz")


def _auto_guess(state: JsonDict, player_id: str) -> tuple[str, JsonDict] | None:
    # A turnless guessing game (Wordle) exposes a board/max_guesses rather
    # than current_turn or playable cards.
    if "max_guesses" not in state:
        return None
    return "guess", {"guess": random.choice(_WORDS)}  # nosec B311


def _default_player(state: JsonDict, player_id: str) -> tuple[str, JsonDict] | None:
    if state.get("ended"):
        return None
    if state.get("can_choose_trump"):
        return _auto_choose_trump(state, player_id)
    playable = state.get("playable_card_ids", [])
    if playable:
        return _auto_play_card(state, player_id)
    return _auto_guess(state, player_id)


MAX_DEBUG_STEPS = 2000


def run_debug(factory: GameFactory, players: list[str]) -> list[JsonDict]:
    game = factory.create(players)
    log: list[JsonDict] = [game.serialize_public()]

    auto_player = _get_auto_player(factory)

    for _ in range(MAX_DEBUG_STEPS):
        if game.ended:
            break

        # The actor is whoever must move: the player to act, or the Hâkem
        # while trump is being chosen. Per-player fields like
        # playable_card_ids / can_choose_trump are only populated when the
        # state is serialized from that player's perspective, so serialize
        # for the actor rather than the global spectator view.
        global_state = game.serialize_public()
        actor = global_state.get("current_turn")
        if not actor and global_state.get("phase") == "choose_trump":
            actor = global_state.get("hakem")
        if not actor:
            # Turnless game (e.g. Wordle): any player may move.
            actor = players[0] if players else None
        if not actor:
            break

        state = game.serialize_public(player_id=actor)
        action = auto_player(state, actor)
        if not action:
            break
        try:
            game.handle_action(actor, action[0], action[1])
        except (ValueError, PermissionError):
            break
        log.append(game.serialize_public())

    log.append(game.serialize_public())
    return log


def _get_auto_player(factory: GameFactory) -> AutoPlayer:
    if hasattr(factory, "debug_player") and callable(factory.debug_player):
        return factory.debug_player
    return _default_player
