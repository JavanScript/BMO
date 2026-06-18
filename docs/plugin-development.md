# BMO Plugin Development Guide

BMO plugins let you add new multiplayer games to the platform. Plugins run
in a sandboxed subprocess (stdin/stdout JSON-RPC), separate from the main web
server. Browsers connect directly to the web server for the frontend.

---

## Architecture

```
bmo-plugin (Maubot/Matrix)          -- lobby, commands, link delivery
       │  POST /api/sessions
       ▼
bmo-web (aiohttp)                   -- sessions, game state, SSE, frontend
       │  GET /game/<session_id>
       ▼
Browser                             -- HTML/JS frontend, SSE events
```

Two tiers:

- **bmo-plugin** — Matrix bot handling `!bmo` commands, creating lobbies, and
  calling `POST /api/sessions` on the web server.
- **bmo-web** — HTTP server hosting game pages, managing game state in SQLite,
  streaming real-time events via SSE. No Matrix dependency.

---

## Directory Structure

```
my-plugin/
├── manifest.yaml         # REQUIRED — metadata
├── plugin.py             # REQUIRED — game factory module
└── frontend/
    └── index.html        # OPTIONAL — browser HTML
```

---

## manifest.yaml

Flat key:value file (no nested YAML):

```yaml
key: my-game
title: My Game
description: A example game.
min_players: 2
max_players: 4
private_player_links: true
frontend: frontend/index.html
entrypoint: plugin:factory
```

### Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `key` | Yes | — | Unique identifier. Lowercase alphanumeric, `_`, `-`. 1–64 chars. |
| `title` | Yes | — | Human-readable name shown in the game list. |
| `description` | Yes | — | Short description shown in menus. |
| `min_players` | No | `1` | Minimum players to start a session. |
| `max_players` | No | unlimited | Maximum players allowed. |
| `private_player_links` | No | `false` | `true` sends signed links via Matrix DM instead of the room. |
| `frontend` | No | — | Path to the HTML frontend, relative to plugin root. |
| `module` | No | `plugin` | Python module name (without `.py`), relative to plugin root. |
| `factory` | No | `factory` | Name of the factory object/class in the module. |
| `entrypoint` | No | — | Shorthand: `module:symbol` overrides `module` + `factory`. |

---

## plugin.py — Game Contract

Your module must expose the `factory` object (or whatever `entrypoint`
specifies) implementing `GameFactory`.

### GameFactory Protocol

```python
from bmo_web.games.base import GameInfo

class MyFactory:
    info = GameInfo(
        key="my-game",
        title="My Game",
        description="...",
        min_players=2,
        max_players=4,
        private_player_links=False,
    )

    def create(self, players: list[str] | None = None) -> Game:
        """Return a fresh game instance."""
        ...

    def load(self, state: dict) -> Game:
        """Deserialize a game from a state dict (for persistence)."""
        ...

factory = MyFactory()
```

### Game Protocol

```python
class Game:
    key: str = "my-game"
    ended: bool = False  # Set to True when the game finishes

    def handle_action(
        self, player_id: str, action: str, payload: dict
    ) -> GameReply:
        """Process a player action.

        Return GameReply(message, ended).
        Raise ValueError for invalid actions.
        Raise PermissionError for out-of-turn moves.
        """
        ...

    def serialize_public(self, player_id: str | None = None) -> dict:
        """Return the state visible to this player.

        For private-hand games, filter out other players' hidden info.
        This dict is merged with session metadata before sending to the client.
        """
        ...

    def to_state(self) -> dict:
        """Return a JSON-serializable dict for SQLite persistence."""
        ...
```

### GameReply

```python
from dataclasses import dataclass

@dataclass
class GameReply:
    message: str = ""
    ended: bool = False
```

---

## Reference Games

The best way to learn is reading the built-in games:

| File | Complexity | Description |
|---|---|---|
| `bmo-web/bmo_web/games/wordle.py` | Simple | 1 player, no private state |
| `bmo-web/bmo_web/games/hokm.py` | Complex | 4 players, private hands, teams, trump |

---

## Frontend

### Token Replacement

Plugin HTML files support these tokens (replaced at serve time):

| Token | Replaced With |
|---|---|
| `__SESSION_JSON__` | JSON-encoded session ID string |
| `__SESSION_ID__` | Raw session ID |
| `__GAME_KEY__` | Game key |
| `__ASSET_BASE__` | Base URL for plugin assets (e.g. `/game/my-game/`) |

### Example minimal frontend

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>My Game</title>
</head>
<body>
  <div id="app">Loading...</div>
  <script>
    const sessionId = __SESSION_JSON__;
    const params = new URLSearchParams(window.location.search);
    const playerId = params.get("player_id") || "";
    const token = params.get("token") || "";
    const authQuery = new URLSearchParams({ player_id: playerId, token });

    async function load() {
      const res = await fetch(`/api/sessions/${sessionId}?${authQuery}`);
      const data = await res.json();
      render(data);
    }

    function render(data) {
      document.querySelector("#app").textContent = JSON.stringify(data, null, 2);
    }

    function listen() {
      const events = new EventSource(
        `/api/sessions/${sessionId}/events?${authQuery}`
      );
      events.addEventListener("state", (event) => {
        render(JSON.parse(event.data));
      });
    }

    load();
    listen();
  </script>
</body>
</html>
```

### API Endpoints for Frontends

| Method | Route | Auth | Description |
|---|---|---|---|
| GET | `/api/sessions/{id}?player_id=...&token=...` | HMAC token | Get session + game state |
| POST | `/api/sessions/{id}/actions?player_id=...&token=...` | HMAC token | Submit an action `{ action, payload }` |
| GET | `/api/sessions/{id}/events?player_id=...&token=...` | HMAC token | SSE stream of state updates |

Player links are signed with `HMAC-SHA256(secret, session_id + "\0" + player_id)`.

### Real-time Updates (SSE)

Connect to the events endpoint for live state changes:

```javascript
const events = new EventSource(`/api/sessions/${sessionId}/events?${authQuery}`);
events.addEventListener("state", (event) => {
  const data = JSON.parse(event.data);
  render(data);
});
```

The server sends:
1. Immediate `event: state` with the current state
2. `event: state` on every action from any player
3. Heartbeat (`: keepalive`) every 25 seconds

---

## Session Lifecycle

1. **Create** — `bmo-plugin` calls `POST /api/sessions` with `{ game, players }`.
   Returns `{ session_id, player_links: [{ player_id, url }] }`.
2. **Join** — Players open their signed URLs. The frontend loads and fetches state.
3. **Play** — Players submit actions via `POST /actions`. State updates stream via SSE.
4. **End** — Game sets `ended = True`. No more actions accepted.

### Session Data Shape

The `serialize_public()` return value is merged with:

```python
{
    "player_id": ...,       # The authenticated player
    "session_id": ...,
    "game": ...,            # Game key
    "phase": ...,           # From game.to_state()
    "players": [...],       # All player IDs
    "seats": [...],         # Ordered player IDs
    "display_names": {...}, # Player → display name mapping
    "teams": [...],         # From game.serialize_public()
    "hand": [...],          # Player's hand (from game)
    "current_turn": ...,    # Player ID who acts next
    "playable_card_ids": [...],
    "hakem": ...,           # Current hakem/leader
    "trump_suit": ...,      # Current trump suit
    "trump_symbol": ...,    # Display symbol for trump
    "trump_options": [...], # Available trump choices
    "can_choose_trump": bool,
    "hand_number": ...,
    "current_trick": [...], # Cards played this trick
    "last_trick": {...},    # Previous completed trick
    "last_hand": {...},     # Previous completed hand info
    "winner_team": ...,     # Winning team (game over)
}
```

Your `serialize_public()` dict is merged into this — return the fields your
frontend needs.

---

## Plugin Sandbox

Plugin Python code runs in a **subprocess** with restrictions:

- 128 MB memory limit
- 30 second CPU time limit
- 1 MB file size limit
- `os.nice(5)` for low priority
- `os.setsid()` for process group kill

Communication is JSON-RPC over stdin/stdout — one JSON request per line,
one JSON response per line.

The sandbox is transparent: you write a normal `GameFactory`/`Game` as shown
above. The `SandboxedFactory` and `SandboxedGame` wrappers handle the IPC.

---

## Installation

### Manual

Drop the plugin directory into the plugins folder:

```bash
cp -r my-plugin bmo-web/plugins/
```

Then reload via admin panel or restart the server.

### Upload

If `BMO_ENABLE_PLUGIN_UPLOADS=1`, admins can upload `.zip` files via the
admin panel. The zip root must contain `manifest.yaml` and `plugin.py`.

### Reload

```http
POST /api/admin/plugins/reload
```

Or use the admin panel "Reload Plugins" button.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BMO_SHARED_SECRET` | `change-me` | Shared secret between bot and web server |
| `BMO_PUBLIC_BASE_URL` | `http://localhost:8000` | Public base URL for game links |
| `BMO_DB_PATH` | `data/bmo-web.sqlite3` | SQLite database path |
| `GAME_PLUGINS_DIR` | `plugins/` | Directory for game plugins |
| `BMO_ENABLE_PLUGIN_UPLOADS` | `0` | Allow zip upload via admin panel |
| `BMO_ADMIN_PASSWORD` | — | Admin password for `/admin/` panel |

---

## Security

See `docs/plugin-security.md` for the full threat model. Key points:

- Plugins are sandboxed subprocesses with resource limits
- The sandbox does **not** provide strong security isolation — only resource
  limits and OS-level process separation
- Player tokens are HMAC-signed server-side; frontends cannot forge them
- `X-BMO-Secret` must match between bot and web server
- Frontend assets are served with path traversal protection
