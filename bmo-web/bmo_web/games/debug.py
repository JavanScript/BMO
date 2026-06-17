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


def _default_player(state: JsonDict, player_id: str) -> tuple[str, JsonDict] | None:
    if state.get("ended"):
        return None
    if state.get("can_choose_trump"):
        return _auto_choose_trump(state, player_id)
    playable = state.get("playable_card_ids", [])
    if playable:
        return _auto_play_card(state, player_id)
    return None


def run_debug(factory: GameFactory, players: list[str]) -> list[JsonDict]:
    game = factory.create(players)
    log: list[JsonDict] = [game.serialize_public()]

    auto_player = _get_auto_player(factory)

    while not game.ended:
        state = game.serialize_public()
        player_id = state.get("current_turn")
        if player_id:
            action = auto_player(state, player_id)
            if action:
                try:
                    game.handle_action(player_id, action[0], action[1])
                except (ValueError, PermissionError):
                    break
                log.append(game.serialize_public())
                continue

        hakem = state.get("hakem")
        if hakem and state.get("phase") == "choose_trump":
            action = auto_player(state, hakem)
            if action:
                try:
                    game.handle_action(hakem, action[0], action[1])
                except (ValueError, PermissionError):
                    break
                log.append(game.serialize_public())
                continue

        break

    log.append(game.serialize_public())
    return log


def _get_auto_player(factory: GameFactory) -> AutoPlayer:
    if hasattr(factory, "debug_player") and callable(factory.debug_player):
        return factory.debug_player
    return _default_player
