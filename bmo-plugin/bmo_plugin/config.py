from __future__ import annotations

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("command_prefix")
        helper.copy("ready_reaction")
        helper.copy("bmo_web_url")
        helper.copy("public_game_url")
        helper.copy("shared_secret")
        helper.copy("min_players")

