from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4


@dataclass
class Lobby:
    lobby_id: str
    room_id: str
    host_id: str
    game_key: str
    message_id: str
    min_players: int
    max_players: int | None = None
    ready_users: set[str] = field(default_factory=set)

    @property
    def players(self) -> list[str]:
        return sorted(self.ready_users)

    @property
    def ready_count(self) -> int:
        return len(self.players)

    @property
    def can_launch(self) -> bool:
        if self.max_players is not None and self.min_players >= self.max_players:
            return self.ready_count == self.max_players
        return self.ready_count >= self.min_players

    @property
    def is_full(self) -> bool:
        return self.max_players is not None and self.ready_count >= self.max_players


@dataclass(frozen=True)
class Reaction:
    room_id: str
    sender: str
    event_id: str
    key: str


class LobbyManager:
    def __init__(self, ready_reaction: str = "👍") -> None:
        self.ready_reaction = ready_reaction
        self._by_room: dict[str, Lobby] = {}
        self._by_message: dict[str, Lobby] = {}

    def create(
        self,
        *,
        room_id: str,
        host_id: str,
        game_key: str,
        message_id: str,
        min_players: int,
        max_players: int | None = None,
    ) -> Lobby:
        if room_id in self._by_room:
            raise LobbyAlreadyExists(self._by_room[room_id])

        lobby = Lobby(
            lobby_id=uuid4().hex,
            room_id=room_id,
            host_id=host_id,
            game_key=game_key.lower().strip(),
            message_id=message_id,
            min_players=max(1, min_players),
            max_players=max_players,
            ready_users=set(),
        )
        self._by_room[room_id] = lobby
        self._by_message[message_id] = lobby
        return lobby

    def get_for_room(self, room_id: str) -> Lobby | None:
        return self._by_room.get(room_id)

    def mark_ready(self, reaction: Reaction) -> Lobby | None:
        if reaction.key != self.ready_reaction:
            return None

        lobby = self._by_message.get(reaction.event_id)
        if not lobby or lobby.room_id != reaction.room_id:
            return None

        if reaction.sender in lobby.ready_users:
            return None
        if lobby.is_full:
            return None

        lobby.ready_users.add(reaction.sender)
        return lobby

    def mark_unready(self, room_id: str, user_id: str) -> Lobby | None:
        lobby = self._by_room.get(room_id)
        if not lobby:
            return None
        lobby.ready_users.discard(user_id)
        return lobby

    def remove_for_room(self, room_id: str) -> Lobby | None:
        lobby = self._by_room.pop(room_id, None)
        if lobby:
            self._by_message.pop(lobby.message_id, None)
        return lobby

    def render_new_lobby(
        self,
        *,
        game_key: str,
        host_id: str,
        min_players: int,
        max_players: int | None = None,
    ) -> str:
        target = _ready_target(min_players=min_players, max_players=max_players)
        return (
            f"🎮 **BMO lobby: {game_key}**\n"
            f"Host: **{host_id}**\n"
            f"Ready: 0/{target}\n"
            f"\n"
            f"Tap {self.ready_reaction} under this message to ready up.\n"
            "The host can launch with **!bmo launch**."
        )

    def render_lobby(self, lobby: Lobby) -> str:
        target = _ready_target(
            min_players=lobby.min_players,
            max_players=lobby.max_players,
        )
        players_section = (
            "\n## Players\n" + "\n".join(f"• **{p}** ✅" for p in lobby.players) + "\n"
            if lobby.players
            else ""
        )
        return (
            f"🎮 **BMO lobby: {lobby.game_key}**\n"
            f"Host: **{lobby.host_id}**\n"
            f"Ready: {lobby.ready_count}/{target}\n"
            f"{players_section}"
            f"\n"
            f"Tap {self.ready_reaction} under this message to ready up.\n"
            "The host can launch with **!bmo launch**."
        )


class LobbyAlreadyExists(Exception):
    def __init__(self, lobby: Lobby) -> None:
        super().__init__(f"{lobby.game_key} lobby already exists in this room")
        self.lobby = lobby


def reaction_from_event(evt: Any) -> Reaction | None:
    content = _as_mapping(getattr(evt, "content", None))
    relates_to = _as_mapping(content.get("m.relates_to"))
    if not relates_to:
        relates_to = _as_mapping(getattr(content.get("relates_to"), "__dict__", None))

    event_id = relates_to.get("event_id")
    key = relates_to.get("key")
    rel_type = relates_to.get("rel_type")
    if rel_type and str(rel_type) != "m.annotation":
        return None
    if not event_id or not key:
        return None

    return Reaction(
        room_id=str(getattr(evt, "room_id")),
        sender=str(getattr(evt, "sender")),
        event_id=str(event_id),
        key=str(key),
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _ready_target(*, min_players: int, max_players: int | None) -> int:
    if max_players is not None and min_players >= max_players:
        return max_players
    return max(1, min_players)
