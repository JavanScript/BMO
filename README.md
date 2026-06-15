# BMO

BMO is a Dockerized Matrix game bot. Matrix is the lobby and social layer; the actual games run in the browser.

## Architecture

- `bmo-plugin/` is the maubot plugin. It handles Matrix commands, lobby messages, ready reactions, and launching games.
- `bmo-web/` is the browser game server. It creates sessions, serves game pages, owns game state, persists sessions to SQLite, and streams live updates to browsers.
- `docker-compose.yml` runs the BMO web service for local browser testing. Maubot can run separately on the server, with the BMO plugin installed into that maubot instance.

BMO currently includes browser Wordle and standard four-player Hokm / حکم. The Matrix room creates the lobby, players mark themselves ready with a reaction, then the bot launches browser game links for the ready players. Games with private hands, like Hokm, send signed player links privately instead of posting them in the room.

## Matrix Flow

Commands are grouped under `!bmo`:

- `!bmo` - show help
- `!bmo games` - list games
- `!bmo start wordle` - create a room lobby
- `!bmo start hokm` - create a four-player Hokm / حکم lobby
- `!bmo launch` - launch the active lobby and share the game URL
- `!bmo status` - show ready count
- `!bmo sync` - refresh the game list from the web server
- `!bmo cancel` - cancel the lobby

The default ready reaction is `👍`. The bot seeds that reaction under the lobby message, so players can tap it to join/ready up. You can change `ready_reaction` in the maubot plugin config to any Matrix reaction key you prefer.

Hokm uses exactly four Matrix players. Extra ready reactions are ignored once the table has four players. The room only receives the unsigned game URL; each player gets their signed Hokm link in a private Matrix room.

## Docker

Docker Engine and the Compose plugin are required. On Ubuntu, follow Docker's official apt repository install path and install these packages:

```sh
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Validate the install:

```sh
sudo docker run hello-world
docker compose version
```

If `docker info` shows the client but says it cannot connect to `/var/run/docker.sock`, the Docker daemon is installed but your shell cannot access it. You can either keep using `sudo docker ...` / `sudo docker compose ...`, or explicitly add your user to the `docker` group. That group effectively grants root-level host access to Docker users, so only do this on machines where you trust your user account:

```sh
sudo usermod -aG docker "$USER"
```

Log out and back in before running Docker without `sudo`.

If previous `sudo docker ...` runs created root-owned files in this repo, fix ownership once:

```sh
sudo chown -R "$USER:$USER" .
```

Copy the example environment file and edit secrets/URLs:

```sh
cp .env.example .env
```

Set the UID/GID values in `.env` to your user so containers write files as you instead of root:

```sh
BMO_UID=$(id -u)
BMO_GID=$(id -g)
```

You can write those values into `.env` with:

```sh
printf 'BMO_UID=%s\nBMO_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

Start the browser game server:

```sh
docker compose up --build bmo-web
```

If your shell needs sudo for Docker:

```sh
sudo docker compose up --build bmo-web
```

BMO web will be reachable on `http://localhost:8000`. It stores SQLite data under `./data/bmo-web`.

Optional plugin/admin environment:

```sh
GAME_PLUGINS_DIR=/data/plugins
BMO_ENABLE_PLUGIN_UPLOADS=0
BMO_ADMIN_PASSWORD=change-this-admin-password
```

`GAME_PLUGINS_DIR` is where extracted game plugins are discovered. Uploads are disabled by default because plugin zips execute trusted Python code in the web server process.

If Docker times out while pulling `python:3.12-slim`, the issue is the registry pull, not the BMO build. Try pre-pulling the base image first:

```sh
docker pull python:3.12-slim
docker compose up --build bmo-web
```

If that fails while fetching a token from `auth.docker.io`, Docker Hub is slow or blocked on your network. Use a registry that mirrors the official Python image without contacting Docker Hub:

```sh
BMO_PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim docker compose up --build bmo-web
```

Another mirror to try:

```sh
BMO_PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim
docker compose up --build bmo-web
```

You can also put the working image in `.env`:

```sh
BMO_PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
```

## Local Browser Test

With `bmo-web` running, create a local Wordle session:

```sh
curl -sS -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H 'X-BMO-Secret: change-me' \
  -d '{
    "game": "wordle",
    "lobby_id": "local-lobby",
    "room_id": "!local:localhost",
    "players": ["@alice:localhost", "@bob:localhost"],
    "public_base_url": "http://localhost:8000"
  }'
```

The response includes `player_links`. Open one of those URLs in your browser. Each link is signed for a specific Matrix player, so the plain `/game/<session-id>` URL will load but will ask for a signed player link before allowing play.

For a local Hokm table, create a session with exactly four players:

```sh
curl -sS -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -H 'X-BMO-Secret: change-me' \
  -d '{
    "game": "hokm",
    "lobby_id": "local-hokm",
    "room_id": "!local:localhost",
    "players": [
      "@alice:localhost",
      "@bob:localhost",
      "@cyrus:localhost",
      "@darya:localhost"
    ],
    "public_base_url": "http://localhost:8000"
  }'
```

List available games and metadata:

```sh
curl -sS http://localhost:8000/api/games
```

## Plugin Build

Build the maubot plugin bundle with the helper service:

```sh
docker compose --profile tools run --rm bmo-plugin-build
```

That writes:

```text
dist/dev.bmo.games.mbp
```

Upload that bundle in the maubot admin UI, then set the plugin config values:

```yaml
command_prefix: bmo
ready_reaction: "👍"
bmo_web_url: http://bmo-web:8000
public_game_url: https://bmo.example.com
shared_secret: same-secret-as-BMO_SHARED_SECRET
min_players:
  hokm: 4
  wordle: 1
game_overrides:
  custom-cards:
    min_players: 2
    max_players: 4
    private_player_links: true
```

`shared_secret` must match `BMO_SHARED_SECRET`. The web service uses it to authorize maubot session creation and sign per-player browser links.

On startup and with `!bmo sync`, the maubot plugin calls `GET /api/games` and caches the web server's game list. If the web server is temporarily unreachable, the bot keeps using the last successful game list.

If maubot runs outside this Compose project on the same Docker host, set `bmo_web_url` to whatever address that maubot process can use to reach BMO web. For a host-level maubot process, that is usually `http://localhost:8000`. For maubot in another Docker network, publish BMO web through your reverse proxy or attach both services to a shared Docker network.

## Game Server Model

Games implement a small contract:

- metadata: key, title, description, min/max players
- `create(players)` for new sessions
- `load(state)` for persisted sessions
- `handle_action(player_id, action, payload)` for browser actions
- `serialize_public(player_id)` for browser-visible state, including private per-player views when needed
- `to_state()` for SQLite persistence

The browser currently uses Server-Sent Events from `/api/sessions/<id>/events` so all open players see state changes without refreshing. SSE events are serialized per signed player link, so games with private hands only reveal the current player's cards.

`GET /api/games` exposes the registry metadata used by maubot:

```json
{
  "games": [
    {
      "key": "wordle",
      "title": "Wordle",
      "description": "Guess a five-letter word in six tries.",
      "min_players": 1,
      "max_players": null,
      "private_player_links": false,
      "source": "builtin"
    }
  ]
}
```

## Game Plugins

At startup, BMO discovers extracted plugins under `GAME_PLUGINS_DIR`. A plugin directory or uploaded zip uses this shape:

```text
manifest.yaml
plugin.py
frontend/index.html
```

Example manifest:

```yaml
key: custom-cards
title: Custom Cards
description: Private-card table for Matrix players.
min_players: 2
max_players: 4
private_player_links: true
frontend: frontend/index.html
```

`plugin.py` must expose a `factory` object, class, or function with `create(players)` and `load(state)` methods. The resulting game object follows the same contract as built-in games.

Plugin frontends are full HTML files served for signed session URLs. BMO replaces these tokens:

- `__SESSION_JSON__` - JSON string containing the session id
- `__SESSION_ID__` - raw session id
- `__GAME_KEY__` - game key
- `__ASSET_BASE__` - base path for plugin frontend resources, such as `/game/custom-cards/`

Plugin assets are served from `/game/<game-key>/<path>`.

## Admin Panel

Open `/admin/` on the web server to view registered games, recent sessions, plugin load errors, and configuration. Admin API calls accept either:

- `X-BMO-Secret: <BMO_SHARED_SECRET>`
- `X-BMO-Admin: <ADMIN_PASSWORD or BMO_ADMIN_PASSWORD>`

The admin panel can reload plugins from `GAME_PLUGINS_DIR`. Uploading plugin zips through the panel requires `BMO_ENABLE_PLUGIN_UPLOADS=1`.

Read [docs/plugin-security.md](docs/plugin-security.md) before enabling uploads. BMO validates zip structure and manifests, but plugin Python is trusted code and is not sandboxed.

## Test

The unit tests cover lobby behavior, session creation, Wordle logic, and Hokm rules without needing Matrix or Docker:

```sh
python3 -m unittest discover
```
