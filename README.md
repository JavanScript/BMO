# BMO

BMO is a Dockerized Matrix game bot. Matrix is the lobby and social layer; the actual games run in the browser.

## Architecture

- `bmo-plugin/` is the maubot plugin. It handles Matrix commands, lobby messages, ready reactions, and launching games.
- `bmo-web/` is the browser game server. It creates sessions, serves game pages, owns game state, persists sessions to SQLite, and streams live updates to browsers.
- `docker-compose.yml` runs maubot and the BMO web service together.

The first vertical slice is browser Wordle. The Matrix room creates the lobby, players mark themselves ready with a reaction, then the bot posts signed browser links for the ready players.

## Matrix Flow

Commands are grouped under `!bmo`:

- `!bmo` - show help
- `!bmo games` - list games
- `!bmo start wordle` - create a room lobby
- `!bmo launch` - launch the active lobby and post the game URL
- `!bmo status` - show ready count
- `!bmo cancel` - cancel the lobby

The default ready reaction key is `ready` to keep local config ASCII-only. Change `ready_reaction` in the maubot plugin config to an emoji or any Matrix reaction key you prefer.

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

If `docker` requires `sudo`, you can either keep using `sudo docker ...` or explicitly add your user to the `docker` group. That group effectively grants root-level host access to Docker users, so only do this on machines where you trust your user account:

```sh
sudo usermod -aG docker "$USER"
```

Log out and back in before running Docker without `sudo`.

Copy the example environment file and edit secrets/URLs:

```sh
cp .env.example .env
```

Start the services:

```sh
docker compose up --build
```

Maubot will be reachable on port `29316` and BMO web on port `8000` unless you put them behind your reverse proxy. BMO web stores SQLite data under `./data/bmo-web`.

The maubot Docker image stores its runtime config in `./data/maubot`. On first run, maubot creates its base config there. Configure the Matrix client and management UI as usual, then upload the BMO plugin bundle.

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
ready_reaction: ready
bmo_web_url: http://bmo-web:8000
public_game_url: https://bmo.example.com
shared_secret: same-secret-as-BMO_SHARED_SECRET
min_players:
  wordle: 1
```

`shared_secret` must match `BMO_SHARED_SECRET`. The web service uses it to authorize maubot session creation and sign per-player browser links.

## Game Server Model

Games implement a small contract:

- metadata: key, title, description, min/max players
- `create()` for new sessions
- `load(state)` for persisted sessions
- `handle_action(player_id, action, payload)` for browser actions
- `serialize_public()` for browser-visible state
- `to_state()` for SQLite persistence

The browser currently uses Server-Sent Events from `/api/sessions/<id>/events` so all open players see state changes without refreshing.

## Test

The unit tests cover lobby behavior, session creation, and Wordle logic without needing Matrix or Docker:

```sh
python3 -m unittest discover
```
