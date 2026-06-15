from __future__ import annotations

from typing import Type

from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig

from .config import Config
from .lobby import LobbyManager, reaction_from_event
from .web_client import BmoWebClient, WebSessionError


GAME_DESCRIPTIONS = {
    "wordle": "Browser Wordle lobby for the room.",
}

REACTION_EVENT = EventType.find("m.reaction", t_class=EventType.Class.MESSAGE)


class BMO(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.lobbies = LobbyManager(ready_reaction=self.config["ready_reaction"])
        self.web = BmoWebClient(
            base_url=self.config["bmo_web_url"],
            public_base_url=self.config["public_game_url"],
            shared_secret=self.config["shared_secret"],
        )

    async def stop(self) -> None:
        await self.web.close()

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    def command_name(self) -> str:
        return self.config["command_prefix"]

    @command.new(name=command_name, help="Show BMO game commands", require_subcommand=False)
    async def bmo(self, evt: MessageEvent) -> None:
        await evt.reply(
            "BMO commands:\n"
            "!bmo games - list games\n"
            "!bmo start wordle - create a lobby\n"
            "!bmo launch - launch the active lobby\n"
            "!bmo status - show lobby status\n"
            "!bmo cancel - cancel the active lobby"
        )

    @bmo.subcommand("games", help="List available games")
    async def games_command(self, evt: MessageEvent) -> None:
        games = "\n".join(
            f"- {key}: {description}" for key, description in sorted(GAME_DESCRIPTIONS.items())
        )
        await evt.reply(games)

    @bmo.subcommand("start", help="Create a game lobby")
    @command.argument("game_key", required=False)
    async def start_command(self, evt: MessageEvent, game_key: str | None = None) -> None:
        game_key = (game_key or "").lower().strip()
        if not game_key:
            await evt.reply("Pick a game, like !bmo start wordle.")
            return
        if game_key not in GAME_DESCRIPTIONS:
            await evt.reply(f"I do not know {game_key!r}. Try !bmo games.")
            return

        existing_lobby = self.lobbies.get_for_room(evt.room_id)
        if existing_lobby:
            await evt.reply(
                f"{existing_lobby.game_key} already has a lobby here. "
                "Use !bmo cancel before starting another."
            )
            return

        min_players = int(self.config["min_players"].get(game_key, 1))
        message_id = await evt.reply(
            self.lobbies.render_new_lobby(
                game_key=game_key,
                host_id=str(evt.sender),
                min_players=min_players,
            )
        )
        self.lobbies.create(
            room_id=evt.room_id,
            host_id=str(evt.sender),
            game_key=game_key,
            message_id=str(message_id),
            min_players=min_players,
        )
        await self.client.react(
            evt.room_id,
            message_id,
            self.config["ready_reaction"],
        )

    @bmo.subcommand("launch", help="Launch the active lobby")
    async def launch_command(self, evt: MessageEvent) -> None:
        lobby = self.lobbies.get_for_room(evt.room_id)
        if not lobby:
            await evt.reply("No lobby is active here. Start one with !bmo start wordle.")
            return
        if str(evt.sender) != lobby.host_id:
            await evt.reply("Only the lobby host can launch the game.")
            return
        if not lobby.can_launch:
            await evt.reply(self.lobbies.render_lobby(lobby))
            return

        try:
            session = await self.web.create_session(
                game_key=lobby.game_key,
                lobby_id=lobby.lobby_id,
                room_id=lobby.room_id,
                players=lobby.players,
            )
        except WebSessionError as exc:
            self.log.exception("Failed to create BMO web session")
            await evt.reply(f"I could not create the browser game session: {exc}")
            return

        self.lobbies.remove_for_room(evt.room_id)
        links = "\n".join(
            f"- {link.player_id}: {link.url}" for link in session.player_links
        )
        await evt.reply(
            f"Game launched: {session.url}\n\n"
            "Player links:\n"
            f"{links}"
        )

    @bmo.subcommand("status", help="Show active lobby status")
    async def status_command(self, evt: MessageEvent) -> None:
        lobby = self.lobbies.get_for_room(evt.room_id)
        if not lobby:
            await evt.reply("No lobby is active here.")
            return
        await evt.reply(self.lobbies.render_lobby(lobby))

    @bmo.subcommand("cancel", aliases=["end"], help="Cancel active lobby")
    async def cancel_command(self, evt: MessageEvent) -> None:
        lobby = self.lobbies.get_for_room(evt.room_id)
        if not lobby:
            await evt.reply("No lobby is active here.")
            return
        if str(evt.sender) != lobby.host_id:
            await evt.reply("Only the lobby host can cancel it.")
            return

        self.lobbies.remove_for_room(evt.room_id)
        await evt.reply(f"Cancelled {lobby.game_key}.")

    @event.on(REACTION_EVENT)
    async def handle_reaction(self, evt) -> None:
        if evt.sender == evt.client.mxid:
            return

        reaction = reaction_from_event(evt)
        if not reaction:
            return

        lobby = self.lobbies.mark_ready(reaction)
        if not lobby:
            return

        await self.client.send_text(lobby.room_id, self.lobbies.render_lobby(lobby))
