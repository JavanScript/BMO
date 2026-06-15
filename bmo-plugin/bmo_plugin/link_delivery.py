from __future__ import annotations

from collections.abc import Iterable


PRIVATE_LINK_GAMES = {"hokm"}


def requires_private_player_links(game_key: str) -> bool:
    return game_key.lower().strip() in PRIVATE_LINK_GAMES


def private_player_message(game_key: str, game_url: str) -> str:
    title = "Hokm / حکم" if game_key.lower().strip() == "hokm" else game_key
    return (
        f"Your private BMO {title} player link:\n"
        f"{game_url}\n\n"
        "Do not share this link; it opens your personal game view."
    )


def public_launch_message(
    *,
    game_key: str,
    session_url: str,
    private_links_sent: bool,
    failed_player_ids: Iterable[str] = (),
) -> str:
    failed = list(failed_player_ids)
    if requires_private_player_links(game_key):
        message = (
            f"Game launched: {session_url}\n\n"
            "I sent each signed player link in a private Matrix room. "
            "Open your BMO DM to play."
        )
        if failed:
            failed_lines = "\n".join(f"- {player_id}" for player_id in failed)
            message += (
                "\n\nI could not send private links to:\n"
                f"{failed_lines}\n"
                "No private player links were posted here."
            )
        return message

    if private_links_sent:
        return f"Game launched: {session_url}\n\nPlayer links were sent privately."
    return f"Game launched: {session_url}"
