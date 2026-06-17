from __future__ import annotations

import json
import os
import resource
import signal
import subprocess  # nosec B404
import sys
from pathlib import Path
from .base import GameInfo, GameReply, JsonDict

PLUGIN_RUNNER_PATH = Path(__file__).resolve().parent / "plugin_runner.py"
SANDBOX_TIMEOUT = 10
SANDBOX_MEMORY_MB = 128


def _restrict_child() -> None:
    os.setsid()
    resource.setrlimit(
        resource.RLIMIT_AS,
        (SANDBOX_MEMORY_MB * 1024 * 1024, SANDBOX_MEMORY_MB * 1024 * 1024),
    )
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    os.nice(5)


class SandboxError(RuntimeError):
    pass


class PluginSandbox:
    def __init__(self, plugin_root: Path) -> None:
        self._plugin_root = plugin_root.resolve()
        self._process: subprocess.Popen | None = None
        self._next_id = 0

    def start(self) -> None:
        cwd = PLUGIN_RUNNER_PATH.parent.parent.parent
        self._process = subprocess.Popen(  # nosec B603
            [sys.executable, str(PLUGIN_RUNNER_PATH), str(self._plugin_root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_restrict_child,
            cwd=str(cwd),
        )
        self._check_alive()

    def call(self, method: str, params: JsonDict | None = None) -> JsonDict:
        if not self._process:
            raise SandboxError("Sandbox not started")
        req_id = self._next_id
        self._next_id += 1
        request = json.dumps({"id": req_id, "method": method, "params": params or {}})
        try:
            assert self._process.stdin is not None
            self._process.stdin.write((request + "\n").encode("utf-8"))
            self._process.stdin.flush()
            assert self._process.stdout is not None
            line = self._process.stdout.readline()
            if not line:
                stderr = self._read_stderr()
                raise SandboxError(f"Sandbox process died: {stderr}")
            data = json.loads(line.decode("utf-8"))
            if not isinstance(data, dict):
                raise SandboxError(f"Invalid sandbox response: {data}")
            if "error" in data:
                raise SandboxError(str(data["error"]))
            result = data.get("result", {})
            return result
        except (OSError, json.JSONDecodeError) as exc:
            self.close()
            raise SandboxError(f"Sandbox communication error: {exc}") from exc

    def _check_alive(self) -> None:
        if self._process and self._process.poll() is not None:
            stderr = self._read_stderr()
            raise SandboxError(f"Sandbox exited early: {stderr}")

    def _read_stderr(self) -> str:
        if self._process and self._process.stderr:
            try:
                return self._process.stderr.read(4096).decode("utf-8", errors="replace")
            except OSError:
                return ""
        return ""

    def close(self) -> None:
        if self._process:
            try:
                pgid = os.getpgid(self._process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                self._process.kill()
            self._process = None


class SandboxedGame:
    def __init__(self, sandbox: PluginSandbox, key: str, state: JsonDict) -> None:
        self._sandbox = sandbox
        self.key = key
        self._state = state
        self.ended = bool(state.get("ended", False))

    def handle_action(
        self, player_id: str, action: str, payload: JsonDict
    ) -> GameReply:
        result = self._sandbox.call("handle_action", {
            "state": self._state,
            "player_id": player_id,
            "action": action,
            "payload": payload,
        })
        self._state = result.get("state", {})
        reply = result.get("reply", {})
        message = str(reply.get("message", ""))
        ended = bool(reply.get("ended", False))
        self.ended = ended
        return GameReply(message=message, ended=ended)

    def serialize_public(self, player_id: str | None = None) -> JsonDict:
        result = self._sandbox.call("serialize_public", {
            "state": self._state,
            "player_id": player_id,
        })
        return result.get("public", {})

    def to_state(self) -> JsonDict:
        return dict(self._state)


class SandboxedFactory:
    def __init__(self, sandbox: PluginSandbox, info: GameInfo) -> None:
        self._sandbox = sandbox
        self.info = info

    def create(self, players: list[str] | None = None) -> SandboxedGame:
        result = self._sandbox.call("create", {"players": players or []})
        state = result.get("state", {})
        return SandboxedGame(self._sandbox, self.info.key, state)

    def load(self, state: JsonDict) -> SandboxedGame:
        return SandboxedGame(self._sandbox, self.info.key, state)
