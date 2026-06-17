from __future__ import annotations

import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from urllib.parse import urlencode
from uuid import uuid4

from .games.base import Game, GameReply, JsonDict
from .games.registry import GameRegistry


@dataclass(frozen=True)
class PlayerLink:
    player_id: str
    url: str


@dataclass
class GameSession:
    session_id: str
    lobby_id: str
    room_id: str
    players: list[str]
    game_key: str
    game: Game
    public_base_url: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ActionResult:
    reply: GameReply
    session: GameSession


class SessionStore:
    def __init__(
        self,
        *,
        shared_secret: str,
        public_base_url: str,
        db_path: str | Path = ":memory:",
        registry: GameRegistry | None = None,
    ) -> None:
        self.shared_secret = shared_secret
        self.public_base_url = public_base_url.rstrip("/")
        self.registry = registry or GameRegistry.defaults()
        self.db_path = str(db_path)
        self._lock = RLock()
        self._db = _connect(db_path)
        self._migrate()

    def create_session(
        self,
        *,
        game_key: str,
        lobby_id: str,
        room_id: str,
        players: list[str],
        display_names: dict[str, str] | None = None,
        public_base_url: str | None = None,
    ) -> GameSession:
        normalized_game = game_key.lower().strip()
        clean_players = sorted({player for player in players if player})
        game_info = self.registry.info(normalized_game)
        if len(clean_players) < game_info.min_players:
            raise ValueError(
                f"{game_info.title} requires at least {game_info.min_players} player(s)."
            )
        if game_info.max_players is not None and len(clean_players) > game_info.max_players:
            raise ValueError(
                f"{game_info.title} allows at most {game_info.max_players} player(s)."
            )
        game = self.registry.create(normalized_game, players=clean_players)
        session_id = uuid4().hex
        now = _now()
        base_url = (public_base_url or self.public_base_url).rstrip("/")

        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO sessions (
                    session_id, lobby_id, room_id, game_key, public_base_url,
                    game_state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    lobby_id,
                    room_id,
                    normalized_game,
                    base_url,
                    json.dumps(game.to_state()),
                    now,
                    now,
                ),
            )
            self._replace_players(session_id, clean_players, display_names)

        return GameSession(
            session_id=session_id,
            lobby_id=lobby_id,
            room_id=room_id,
            players=clean_players,
            game_key=normalized_game,
            game=game,
            public_base_url=base_url,
            created_at=now,
            updated_at=now,
        )

    def get(self, session_id: str) -> GameSession | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            players, _ = self._players_with_names(session_id)
        return self._session_from_row(row, players)

    def submit_action(
        self,
        *,
        session_id: str,
        player_id: str,
        token: str,
        action: str,
        payload: JsonDict,
    ) -> ActionResult:
        with self._lock, self._db:
            session = self.get(session_id)
            if not session:
                raise LookupError("session not found")
            self.require_player(session, player_id, token)

            reply = session.game.handle_action(
                player_id=player_id,
                action=action,
                payload=payload,
            )
            session.updated_at = _now()
            self._db.execute(
                """
                UPDATE sessions
                SET game_state = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    json.dumps(session.game.to_state()),
                    session.updated_at,
                    session.session_id,
                ),
            )
        return ActionResult(reply=reply, session=session)

    def require_player(self, session: GameSession, player_id: str, token: str) -> None:
        if player_id not in session.players:
            raise PermissionError("player is not in this session")
        expected = self.player_token(session.session_id, player_id)
        if not hmac.compare_digest(expected, token):
            raise PermissionError("invalid player token")

    def player_token(self, session_id: str, player_id: str) -> str:
        message = f"{session_id}\0{player_id}".encode("utf-8")
        return hmac.new(self.shared_secret.encode("utf-8"), message, sha256).hexdigest()

    def public_url(self, session: GameSession) -> str:
        return f"{session.public_base_url}/game/{session.session_id}"

    def player_url(self, session: GameSession, player_id: str) -> str:
        query = urlencode(
            {
                "player_id": player_id,
                "token": self.player_token(session.session_id, player_id),
            }
        )
        return f"{self.public_url(session)}?{query}"

    def player_links(self, session: GameSession) -> list[PlayerLink]:
        return [
            PlayerLink(player_id=player_id, url=self.player_url(session, player_id))
            for player_id in session.players
        ]

    def serialize(
        self,
        session: GameSession,
        *,
        player_id: str | None = None,
    ) -> dict[str, object]:
        game_info = self.registry.info(session.game_key)
        display_names = self._display_names(session.session_id)
        data: dict[str, object] = {
            "session_id": session.session_id,
            "lobby_id": session.lobby_id,
            "room_id": session.room_id,
            "players": session.players,
            "display_names": display_names,
            "player_id": player_id,
            "game": session.game_key,
            "title": game_info.title,
            "description": game_info.description,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
        data.update(session.game.serialize_public(player_id=player_id))
        return data

    def list_sessions(self, *, limit: int = 50) -> list[dict[str, object]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT session_id, lobby_id, room_id, game_key, public_base_url,
                       created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
            session_ids = [str(row["session_id"]) for row in rows]
            players_by_session, names_by_session = self._players_for_sessions(
                session_ids
            )
            sessions = []
            for row in rows:
                session_id = str(row["session_id"])
                sessions.append(
                    {
                        "session_id": session_id,
                        "lobby_id": str(row["lobby_id"]),
                        "room_id": str(row["room_id"]),
                        "game": str(row["game_key"]),
                        "url": f"{row['public_base_url']}/game/{session_id}",
                        "players": players_by_session.get(session_id, []),
                        "display_names": names_by_session.get(session_id, {}),
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                    }
                )
            return sessions

    def _players_for_sessions(
        self, session_ids: list[str]
    ) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
        players_by_session: dict[str, list[str]] = {sid: [] for sid in session_ids}
        names_by_session: dict[str, dict[str, str]] = {sid: {} for sid in session_ids}
        if not session_ids:
            return players_by_session, names_by_session
        placeholders = ",".join("?" for _ in session_ids)
        rows = self._db.execute(
            f"""
            SELECT session_id, player_id, display_name FROM players
            WHERE session_id IN ({placeholders})
            ORDER BY session_id, player_id
            """,
            session_ids,
        ).fetchall()
        for row in rows:
            sid = str(row["session_id"])
            player_id = str(row["player_id"])
            players_by_session[sid].append(player_id)
            names_by_session[sid][player_id] = str(row["display_name"] or "")
        return players_by_session, names_by_session

    def _display_names(self, session_id: str) -> dict[str, str]:
        _, display_names = self._players_with_names(session_id)
        return display_names

    def _players_with_names(
        self, session_id: str
    ) -> tuple[list[str], dict[str, str]]:
        rows = self._db.execute(
            """
            SELECT player_id, display_name FROM players
            WHERE session_id = ?
            ORDER BY player_id
            """,
            (session_id,),
        ).fetchall()
        players = [str(row["player_id"]) for row in rows]
        display_names = {
            str(row["player_id"]): str(row["display_name"] or "")
            for row in rows
        }
        return players, display_names

    def replace_registry(self, registry: GameRegistry) -> None:
        with self._lock:
            self.registry = registry

    def _replace_players(
        self,
        session_id: str,
        players: list[str],
        display_names: dict[str, str] | None = None,
    ) -> None:
        self._db.execute("DELETE FROM players WHERE session_id = ?", (session_id,))
        display_names = display_names or {}
        self._db.executemany(
            """
            INSERT INTO players (session_id, player_id, display_name)
            VALUES (?, ?, ?)
            """,
            [
                (session_id, player, display_names.get(player, ""))
                for player in players
            ],
        )

    def _session_from_row(
        self,
        row: sqlite3.Row,
        players: list[str],
    ) -> GameSession:
        game_key = str(row["game_key"])
        game = self.registry.load(game_key, json.loads(str(row["game_state"])))
        return GameSession(
            session_id=str(row["session_id"]),
            lobby_id=str(row["lobby_id"]),
            room_id=str(row["room_id"]),
            players=players,
            game_key=game_key,
            game=game,
            public_base_url=str(row["public_base_url"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    lobby_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    game_key TEXT NOT NULL,
                    public_base_url TEXT NOT NULL,
                    game_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    session_id TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (session_id, player_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
                """
            )
            self._db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                ON sessions (updated_at DESC)
                """
            )
            self._db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_players_session_id
                ON players (session_id)
                """
            )
            self._migrate_display_names()

    def _migrate_display_names(self) -> None:
        info = self._db.execute("PRAGMA table_info(players)").fetchall()
        columns = {str(row["name"]) for row in info}
        if "display_name" not in columns:
            self._db.execute(
                "ALTER TABLE players ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
            )


def _connect(db_path: str | Path) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
