from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from bmo_web.games.base import GameInfo, GameReply, JsonDict


ROWS, COLS = 6, 7
EMPTY, P1, P2 = 0, 1, 2
DIRS = [(0, 1), (1, 0), (1, 1), (1, -1)]
DEFAULT_WIN = 4
DEFAULT_TIME_LIMIT = 0


@dataclass
class ConnectGame:
    board: list[list[int]]
    current_player: int
    players: list[str]
    winner: int
    turn_count: int
    started: bool
    win_length: int
    time_limit: int
    player_times: list[float]
    move_deadline: float
    playing: bool = False
    time_mode: str = "move"
    key: str = "connect"
    settings_lock: bool = False

    @property
    def ended(self) -> bool:
        return self.winner != 0 or (
            self.playing
            and all(self.board[r][c] != EMPTY for r in range(ROWS) for c in range(COLS))
        )

    @staticmethod
    def new(players: list[str] | None = None) -> ConnectGame:
        return ConnectGame(
            board=[[EMPTY] * COLS for _ in range(ROWS)],
            current_player=P1,
            players=players or ["Player 1", "Player 2"],
            winner=EMPTY,
            turn_count=0,
            started=False,
            win_length=DEFAULT_WIN,
            time_limit=DEFAULT_TIME_LIMIT,
            player_times=[0.0, 0.0],
            move_deadline=0.0,
        )

    def handle_action(self, player_id: str, action: str, payload: JsonDict) -> GameReply:
        player_num = self._player_num(player_id)
        if player_num == 0:
            return GameReply(message="You are not in this game.")

        if action == "setup":
            return self._setup(player_num, payload)
        if action == "start":
            return self._start(player_num, payload)
        if action == "drop":
            return self._handle_drop(player_num, payload)
        if action == "play":
            return self._play(player_num)
        if action == "forfeit":
            return self._forfeit(player_num)
        if action == "reset":
            return self._reset()
        return GameReply(message=f"Unknown action: {action}")

    def _player_num(self, player_id: str) -> int:
        try:
            return self.players.index(player_id) + 1
        except ValueError:
            return 0

    def _setup(self, player_num: int, payload: JsonDict) -> GameReply:
        if self.started:
            return GameReply(message="Game already started.")
        if player_num != P1:
            return GameReply(message="Only Player 1 can configure settings.")
        self.settings_lock = True
        return GameReply(message="Settings locked. Press Start to begin.")

    def _start(self, player_num: int, payload: JsonDict) -> GameReply:
        if self.started:
            return GameReply(message="Game already started.")
        if player_num != P1:
            return GameReply(message="Only Player 1 can start the game.")
        wl = int(payload.get("win_length", DEFAULT_WIN))
        self.win_length = 5 if wl == 5 else 4
        tl = int(payload.get("time_limit", DEFAULT_TIME_LIMIT))
        self.time_limit = max(0, tl)
        self.time_mode = payload.get("time_mode", "move")
        self.playing = False
        self.started = True
        return GameReply(message="Game configured! Press Play to start.")

    def _play(self, player_num: int) -> GameReply:
        if not self.started:
            return GameReply(message="Game has not been configured yet.")
        if self.playing:
            return GameReply(message="Game is already playing.")
        self.playing = True
        if self.time_limit > 0:
            self.player_times = [float(self.time_limit), float(self.time_limit)]
            self.move_deadline = time.time() + self.time_limit
        return GameReply(message="Game on!")

    def _handle_drop(self, player_num: int, payload: JsonDict) -> GameReply:
        if not self.started:
            return GameReply(message="Game has not started yet.")
        if not self.playing:
            return GameReply(message="The game hasn't begun yet.")
        if self.ended:
            return GameReply(message="Game is already over.")
        if player_num != self.current_player:
            return GameReply(message="Not your turn.")
        if self._timed_out(player_num):
            self.winner = P2 if player_num == P1 else P1
            return GameReply(
                message=f"{self._name(player_num)} ran out of time! {self._name(self.winner)} wins!",
                ended=True,
            )
        col = int(payload.get("column", -1))
        if col < 0 or col >= COLS:
            return GameReply(message="Invalid column.")
        for row in range(ROWS - 1, -1, -1):
            if self.board[row][col] == EMPTY:
                self.board[row][col] = player_num
                self.turn_count += 1
                if self._check_win(row, col):
                    self.winner = player_num
                    return GameReply(
                        message=f"{self._name(player_num)} wins!",
                        ended=True,
                    )
                if all(self.board[r][c] != EMPTY for r in range(ROWS) for c in range(COLS)):
                    return GameReply(message="Draw!", ended=True)
                self.current_player = P2 if player_num == P1 else P1
                self._charge_time(player_num)
                return GameReply(message=f"{self._current_name()}'s turn.")
        return GameReply(message="Column is full.")

    def _forfeit(self, player_num: int) -> GameReply:
        if self.ended:
            return GameReply(message="Game is already over.")
        if not self.playing:
            return GameReply(message="Game hasn't started yet.")
        self.winner = P2 if player_num == P1 else P1
        return GameReply(
            message=f"{self._name(player_num)} forfeits. {self._name(self.winner)} wins!",
            ended=True,
        )

    def _reset(self) -> GameReply:
        current = self.to_state()
        new = ConnectGame.new(current.get("players"))
        for attr in ("board", "current_player", "winner", "turn_count",
                     "started", "playing", "win_length", "time_limit",
                     "player_times", "move_deadline", "settings_lock"):
            setattr(self, attr, getattr(new, attr))
        return GameReply(message="Game reset.")

    def _current_name(self) -> str:
        return self._name(self.current_player)

    def _name(self, player_num: int) -> str:
        idx = 0 if player_num == P1 else 1
        return self.players[idx] if idx < len(self.players) else f"Player {player_num}"

    def _timed_out(self, player_num: int) -> bool:
        if self.time_limit <= 0:
            return False
        return self.move_deadline > 0 and time.time() > self.move_deadline + 1.0

    def _charge_time(self, player_num: int) -> None:
        if self.time_limit <= 0:
            return
        idx = 0 if player_num == P1 else 1
        if self.time_mode == "total":
            elapsed = time.time() - (self.move_deadline - self.player_times[idx])
            self.player_times[idx] = max(0.0, self.player_times[idx] - elapsed)
        next_idx = 0 if self.current_player == P1 else 1
        base = self.player_times[next_idx] if self.time_mode == "total" else self.time_limit
        self.move_deadline = time.time() + base

    def _check_win(self, row: int, col: int) -> bool:
        player = self.board[row][col]
        for dr, dc in DIRS:
            count = 1
            for sign in (1, -1):
                r, c = row + dr * sign, col + dc * sign
                while 0 <= r < ROWS and 0 <= c < COLS and self.board[r][c] == player:
                    count += 1
                    r += dr * sign
                    c += dc * sign
            if count >= self.win_length:
                return True
        return False

    def _win_cells(self) -> list[list[int]]:
        for r in range(ROWS):
            for c in range(COLS):
                player = self.board[r][c]
                if player == EMPTY:
                    continue
                for dr, dc in DIRS:
                    cells = [[r, c]]
                    for sign in (1, -1):
                        rr, cc = r + dr * sign, c + dc * sign
                        while 0 <= rr < ROWS and 0 <= cc < COLS and self.board[rr][cc] == player:
                            cells.append([rr, cc])
                            rr += dr * sign
                            cc += dc * sign
                    if len(cells) >= self.win_length:
                        return cells
        return []

    def serialize_public(self, player_id: str | None = None) -> JsonDict:
        now = time.time()
        pn = self._player_num(player_id) if player_id else 0
        remaining = 0.0
        if self.time_limit > 0 and self.started and not self.ended:
            idx = 0 if self.current_player == P1 else 1
            base = self.player_times[idx] if self.time_mode == "total" else self.time_limit
            remaining = max(0.0, base - (now - (self.move_deadline - base)))
        return {
            "board": self.board,
            "current_player": self.current_player,
            "winner": self.winner,
            "ended": self.ended,
            "players": self.players,
            "player_number": pn,
            "turn_count": self.turn_count,
            "win_cells": self._win_cells() if self.winner else [],
            "started": self.started,
            "playing": self.playing,
            "win_length": self.win_length,
            "time_limit": self.time_limit,
            "time_mode": self.time_mode,
            "player_times": list(self.player_times),
            "move_deadline": self.move_deadline,
            "remaining": round(remaining, 1),
            "settings_lock": self.settings_lock,
        }

    def to_state(self) -> JsonDict:
        return {
            "board": self.board,
            "current_player": self.current_player,
            "players": self.players,
            "winner": self.winner,
            "turn_count": self.turn_count,
            "started": self.started,
            "playing": self.playing,
            "win_length": self.win_length,
            "time_limit": self.time_limit,
            "time_mode": self.time_mode,
            "player_times": list(self.player_times),
            "move_deadline": self.move_deadline,
            "settings_lock": self.settings_lock,
        }

    @staticmethod
    def from_state(state: JsonDict) -> ConnectGame:
        return ConnectGame(**state)


class ConnectFactory:
    info = GameInfo(
        key="connect",
        title="Connect Four",
        description="Connect four of your tokens in a row.",
        min_players=2,
        max_players=2,
        source="plugin",
    )

    def create(self, players: list[str] | None = None):
        return ConnectGame.new(players)

    def load(self, state: JsonDict):
        return ConnectGame.from_state(state)


factory = ConnectFactory()
