from __future__ import annotations

from bmo_web.games.base import GameInfo
from bmo_web.games.hokm import HokmFactory


class HokmPluginFactory:
    info = GameInfo(
        key="hokm-example",
        title="Hokm / حکم (Plugin Example)",
        description="Four-player Iranian trick-taking with Hâkem and trump. Example plugin.",
        min_players=4,
        max_players=4,
        private_player_links=True,
        source="plugin",
    )

    def create(self, players=None):
        return HokmFactory().create(players)

    def load(self, state):
        return HokmFactory().load(state)


factory = HokmPluginFactory()
