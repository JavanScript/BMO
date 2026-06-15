from __future__ import annotations

import re
from dataclasses import replace
from typing import Type

from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig

from .config import Config
from .link_delivery import (
    private_player_message,
    public_launch_message,
)
from .lobby import LobbyManager, reaction_from_event
from .web_client import BmoWebClient, GameInfo, PlayerLink, WebSessionError

REACTION_EVENT = EventType.find("m.reaction", t_class=EventType.Class.MESSAGE)
REDACTION_EVENT = EventType.ROOM_REDACTION


class BMO(Plugin):
    async def start(self) -> None:
        self.config.load_and_update()
        self.lobbies = LobbyManager(ready_reaction=self.config["ready_reaction"])
        self._reaction_map: dict[str, tuple[str, str]] = {}
        self.games: dict[str, GameInfo] = {}
        self.web = BmoWebClient(
            base_url=self.config["bmo_web_url"],
            public_base_url=self.config["public_game_url"],
            shared_secret=self.config["shared_secret"],
        )
        synced, message = await self._sync_games()
        if not synced:
            self.log.warning(message)

    async def stop(self) -> None:
        await self.web.close()

    def _format_html(self, text: str) -> str:
        lines = text.split("\n")
        result = []
        for line in lines:
            if line.startswith("## "):
                line = f"<h4>{line[3:]}</h4>"
            parts = line.split("**")
            out = []
            for i, p in enumerate(parts):
                out.append(p if i % 2 == 0 else f"<b>{p}</b>")
            line = "".join(out)
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', line)
            result.append(line)
        return "<br>".join(result)

    def _strip_formatting(self, text: str) -> str:
        lines = text.split("\n")
        result = []
        for line in lines:
            if line.startswith("## "):
                line = line[3:]
            line = line.replace("**", "")
            line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
            result.append(line)
        return "\n".join(result)

    async def _send_html(self, room_id: str, text: str) -> str:
        resp = await self.client.send_message(room_id, {
            "msgtype": "m.text",
            "body": self._strip_formatting(text),
            "format": "org.matrix.custom.html",
            "formatted_body": self._format_html(text),
        })
        return str(resp["event_id"]) if isinstance(resp, dict) else str(resp)

    async def _edit_html(self, room_id: str, event_id: str, text: str) -> None:
        plain = self._strip_formatting(text)
        html = self._format_html(text)
        await self.client.send_message(room_id, {
            "msgtype": "m.text",
            "body": f" * {plain}",
            "format": "org.matrix.custom.html",
            "formatted_body": html,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
            "m.new_content": {
                "msgtype": "m.text",
                "body": plain,
                "format": "org.matrix.custom.html",
                "formatted_body": html,
            },
        })

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    def command_name(self) -> str:
        return self.config["command_prefix"]

    async def _sync_games(self) -> tuple[bool, str]:
        try:
            games = await self.web.list_games()
        except WebSessionError as exc:
            if self.games:
                return False, f"Could not sync BMO games; using cached catalog: {exc}"
            return False, f"Could not sync BMO games: {exc}"

        if not games:
            if self.games:
                return False, "BMO web returned no games; using cached catalog."
            return False, "BMO web returned no games."

        self.games = {
            game.key: self._apply_game_overrides(game)
            for game in games
        }
        return True, f"Synced {len(self.games)} BMO game(s)."

    def _apply_game_overrides(self, game: GameInfo) -> GameInfo:
        overrides = self._mapping_config("game_overrides").get(game.key, {})
        if not isinstance(overrides, dict):
            overrides = {}

        min_players = max(
            game.min_players,
            _optional_int(
                self._mapping_config("min_players").get(game.key),
                game.min_players,
            ),
        )
        max_players = _optional_int(overrides.get("max_players"), game.max_players)
        private_links = _optional_bool(
            overrides.get("private_player_links"),
            game.private_player_links,
        )
        return replace(
            game,
            title=str(overrides.get("title", game.title)),
            description=str(overrides.get("description", game.description)),
            min_players=min_players,
            max_players=max_players,
            private_player_links=private_links,
        )

    def _mapping_config(self, key: str) -> dict[str, object]:
        value = self.config[key]
        if hasattr(value, "items"):
            return dict(value.items())
        return {}

    @command.new(name=command_name, help="Show BMO game commands", require_subcommand=False)
    async def bmo(self, evt: MessageEvent) -> None:
        game_examples = "• **!bmo start <game>** — Create a game lobby"
        if self.games:
            examples = ", ".join(f"**{key}**" for key in sorted(self.games))
            game_examples = (
                f"• **!bmo start <game>** — Create a game lobby ({examples})"
            )
        await evt.reply(self._format_html(
            "🎮 **BMO Games**\n"
            "Available commands:\n"
            "• **!bmo games** — List available games\n"
            f"{game_examples}\n"
            "• **!bmo launch** — Launch the active lobby\n"
            "• **!bmo status** — Show lobby status\n"
            "• **!bmo sync** — Refresh the game catalog\n"
            "• **!bmo cancel** — Cancel the active lobby"
        ), allow_html=True)

    @bmo.subcommand("games", help="List available games")
    async def games_command(self, evt: MessageEvent) -> None:
        synced, message = await self._sync_games()
        if not self.games:
            await evt.reply(self._format_html(
                f"❌ No games are available yet. {message}"
            ), allow_html=True)
            return

        games = "\n".join(
            f"• **{game.key}**: {game.description}"
            for game in sorted(self.games.values(), key=lambda item: item.key)
        )
        suffix = "" if synced else f"\n\n{message}"
        await evt.reply(self._format_html(
            f"🎮 **Available Games**\n{games}{suffix}"
        ), allow_html=True)

    @bmo.subcommand("sync", help="Refresh game metadata from BMO web")
    async def sync_command(self, evt: MessageEvent) -> None:
        synced, message = await self._sync_games()
        prefix = "✅" if synced else "⚠️"
        await evt.reply(self._format_html(
            f"{prefix} {message}"
        ), allow_html=True)

    @bmo.subcommand("start", help="Create a game lobby")
    @command.argument("game_key", required=False)
    async def start_command(self, evt: MessageEvent, game_key: str | None = None) -> None:
        game_key = (game_key or "").lower().strip()
        if not game_key:
            await evt.reply(self._format_html(
                "❌ Pick a game, like **!bmo start wordle**."
            ), allow_html=True)
            return
        if game_key not in self.games:
            await self._sync_games()
        game_info = self.games.get(game_key)
        if not game_info:
            await evt.reply(self._format_html(
                f"❌ I do not know **{game_key}**. Try **!bmo games**."
            ), allow_html=True)
            return

        existing_lobby = self.lobbies.get_for_room(evt.room_id)
        if existing_lobby:
            await evt.reply(self._format_html(
                f"❌ **{existing_lobby.game_key}** already has a lobby here. "
                "Use **!bmo cancel** before starting another."
            ), allow_html=True)
            return

        min_players = game_info.min_players
        max_players = game_info.max_players
        message_id = await evt.reply(
            self._format_html(self.lobbies.render_new_lobby(
                game_key=game_key,
                host_id=str(evt.sender),
                min_players=min_players,
                max_players=max_players,
            )),
            allow_html=True,
        )
        self.lobbies.create(
            room_id=evt.room_id,
            host_id=str(evt.sender),
            game_key=game_key,
            message_id=str(message_id),
            min_players=min_players,
            max_players=max_players,
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
            await evt.reply(self._format_html(
                "❌ No lobby is active here. Start one with **!bmo start hokm**."
            ), allow_html=True)
            return
        if str(evt.sender) != lobby.host_id:
            await evt.reply(self._format_html(
                "❌ Only the lobby host can launch the game."
            ), allow_html=True)
            return
        if not lobby.can_launch:
            await self._edit_html(
                lobby.room_id,
                lobby.message_id,
                self.lobbies.render_lobby(lobby),
            )
            await evt.reply(self._format_html(
                f"🛑 Not enough players ready yet."
            ), allow_html=True)
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
            await evt.reply(self._format_html(
                f"❌ I could not create the browser game session: {exc}"
            ), allow_html=True)
            return

        self.lobbies.remove_for_room(evt.room_id)
        game_info = self.games.get(lobby.game_key)
        private_player_links = (
            game_info.private_player_links
            if game_info is not None
            else False
        )
        if private_player_links:
            failed = await self._send_private_player_links(
                game_key=lobby.game_key,
                player_links=session.player_links,
            )
            await evt.reply(
                public_launch_message(
                    game_key=lobby.game_key,
                    session_url=session.url,
                    private_links_sent=True,
                    private_player_links=True,
                    failed_player_ids=failed,
                )
            )
            return

        links = "\n".join(
            f"• [{link.player_id}]({link.url})"
            for link in session.player_links
        )
        await evt.reply(self._format_html(
            f"🚀 **Game launched!**\n"
            f"Tap your link to open your game session.\n\n"
            f"{links}"
        ), allow_html=True)

    async def _send_private_player_links(
        self,
        *,
        game_key: str,
        player_links: tuple[PlayerLink, ...],
    ) -> list[str]:
        failed: list[str] = []
        for link in player_links:
            try:
                room_id = await self.client.create_room(
                    is_direct=True,
                    invitees=[link.player_id],
                    name="BMO Game",
                )
                await self.client.send_text(
                    room_id,
                    private_player_message(game_key, link.url),
                )
            except Exception:
                self.log.exception("Failed to send private game link to %s", link.player_id)
                failed.append(link.player_id)
        return failed

    @bmo.subcommand("status", help="Show active lobby status")
    async def status_command(self, evt: MessageEvent) -> None:
        lobby = self.lobbies.get_for_room(evt.room_id)
        if not lobby:
            await evt.reply(self._format_html(
                "❌ No lobby is active here."
            ), allow_html=True)
            return
        await evt.reply(self._format_html(
            self.lobbies.render_lobby(lobby)
        ), allow_html=True)

    @bmo.subcommand("cancel", aliases=["end"], help="Cancel active lobby")
    async def cancel_command(self, evt: MessageEvent) -> None:
        lobby = self.lobbies.get_for_room(evt.room_id)
        if not lobby:
            await evt.reply(self._format_html(
                "❌ No lobby is active here."
            ), allow_html=True)
            return
        if str(evt.sender) != lobby.host_id:
            await evt.reply(self._format_html(
                "❌ Only the lobby host can cancel it."
            ), allow_html=True)
            return

        self.lobbies.remove_for_room(evt.room_id)
        await evt.reply(self._format_html(
            f"✅ Cancelled **{lobby.game_key}**."
        ), allow_html=True)

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

        self._reaction_map[str(evt.event_id)] = (lobby.room_id, reaction.sender)
        await self._edit_html(
            lobby.room_id,
            lobby.message_id,
            self.lobbies.render_lobby(lobby),
        )

    @event.on(REDACTION_EVENT)
    async def handle_redaction(self, evt) -> None:
        if evt.sender == evt.client.mxid:
            return

        redacted = str(getattr(evt, "redacts", "") or "")
        if not redacted:
            return

        info = self._reaction_map.pop(redacted, None)
        if not info:
            return

        room_id, user_id = info
        lobby = self.lobbies.mark_unready(room_id, user_id)
        if not lobby:
            return

        await self._edit_html(
            lobby.room_id,
            lobby.message_id,
            self.lobbies.render_lobby(lobby),
        )


def _optional_int(value: object, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    return int(value)


def _optional_bool(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).lower().strip()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default
