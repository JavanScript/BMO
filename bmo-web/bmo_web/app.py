from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from aiohttp import web

from .realtime import EventBroker
from .sessions import GameSession, SessionStore


def create_app() -> web.Application:
    app = web.Application()
    app["store"] = SessionStore(
        shared_secret=os.environ.get("BMO_SHARED_SECRET", "change-me"),
        public_base_url=os.environ.get("BMO_PUBLIC_BASE_URL", "http://localhost:8000"),
        db_path=os.environ.get("BMO_DB_PATH", "data/bmo-web.sqlite3"),
    )
    app["broker"] = EventBroker()
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_post("/api/sessions/{session_id}/actions", submit_action)
    app.router.add_post("/api/sessions/{session_id}/guess", submit_guess)
    app.router.add_get("/api/sessions/{session_id}/events", session_events)
    app.router.add_get("/game/{session_id}", game_page)
    return app


async def create_session(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    if request.headers.get("X-BMO-Secret") != store.shared_secret:
        return web.json_response({"error": "unauthorized"}, status=401)

    body = await request.json()
    try:
        session = store.create_session(
            game_key=body["game"],
            lobby_id=body["lobby_id"],
            room_id=body["room_id"],
            players=body.get("players", []),
            public_base_url=body.get("public_base_url"),
        )
    except (KeyError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    return web.json_response(
        {
            "session_id": session.session_id,
            "url": store.public_url(session),
            "player_links": [
                {"player_id": link.player_id, "url": link.url}
                for link in store.player_links(session)
            ],
        },
        status=201,
    )


async def get_session(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    session, player_id = _authorized_session(request, store)
    return web.json_response(store.serialize(session, player_id=player_id))


async def submit_action(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        payload = _ensure_dict(body.get("payload", {}))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    return await _submit_action(
        request,
        action=str(body.get("action", "")),
        payload=payload,
    )


async def submit_guess(request: web.Request) -> web.Response:
    body = await request.json()
    return await _submit_action(
        request,
        action="guess",
        payload={"guess": body.get("guess", "")},
    )


async def _submit_action(
    request: web.Request,
    *,
    action: str,
    payload: dict[str, Any],
) -> web.Response:
    store: SessionStore = request.app["store"]
    broker: EventBroker = request.app["broker"]
    session_id = request.match_info["session_id"]
    player_id = request.query.get("player_id", "")
    token = request.query.get("token", "")

    try:
        result = store.submit_action(
            session_id=session_id,
            player_id=player_id,
            token=token,
            action=action,
            payload=payload,
        )
    except LookupError:
        return web.json_response({"error": "not found"}, status=404)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    payload = store.serialize(result.session)
    await broker.publish(session_id, payload)
    return web.json_response(
        {
            "message": result.reply.message,
            "ended": result.reply.ended,
            "session": store.serialize(result.session, player_id=player_id),
        }
    )


async def session_events(request: web.Request) -> web.StreamResponse:
    store: SessionStore = request.app["store"]
    broker: EventBroker = request.app["broker"]
    session, _player_id = _authorized_session(request, store)
    queue = broker.subscribe(session.session_id)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)
    await _write_sse(response, store.serialize(session), event="state")

    try:
        while True:
            message = await queue.get()
            await response.write(f"event: state\ndata: {message}\n\n".encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        broker.unsubscribe(session.session_id, queue)

    return response


async def game_page(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    session_id = request.match_info["session_id"]
    if not store.get(session_id):
        return web.Response(text="Game not found", status=404)
    return web.Response(text=_html(session_id), content_type="text/html")


def _authorized_session(
    request: web.Request,
    store: SessionStore,
) -> tuple[GameSession, str]:
    session = store.get(request.match_info["session_id"])
    if not session:
        raise web.HTTPNotFound(text=json.dumps({"error": "not found"}), content_type="application/json")

    player_id = request.query.get("player_id", "")
    token = request.query.get("token", "")
    try:
        store.require_player(session, player_id, token)
    except PermissionError as exc:
        raise web.HTTPForbidden(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json",
        ) from exc
    return session, player_id


async def _write_sse(
    response: web.StreamResponse,
    payload: dict[str, Any],
    *,
    event: str,
) -> None:
    await response.write(
        f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
    )


def _ensure_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise ValueError("payload must be an object")


def _html(session_id: str) -> str:
    session_json = json.dumps(session_id)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BMO Game</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #102025;
      color: #f7fbfb;
    }}
    main {{
      width: min(680px, calc(100vw - 32px));
      padding: 32px 0;
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    form {{
      display: flex;
      gap: 8px;
      margin: 20px 0;
    }}
    input, button {{
      min-height: 44px;
      border-radius: 6px;
      border: 1px solid #7aa7a5;
      font: inherit;
    }}
    input {{
      flex: 1;
      min-width: 0;
      padding: 0 12px;
      background: #f7fbfb;
      color: #102025;
    }}
    button {{
      padding: 0 16px;
      background: #f4c95d;
      color: #102025;
      font-weight: 700;
      cursor: pointer;
    }}
    button:disabled, input:disabled {{
      opacity: .6;
      cursor: not-allowed;
    }}
    pre {{
      min-height: 180px;
      padding: 16px;
      border: 1px solid #33575d;
      border-radius: 8px;
      background: #172d33;
      overflow: auto;
      line-height: 1.7;
      font-size: 18px;
    }}
    #message {{
      color: #f4c95d;
      min-height: 24px;
    }}
    #identity {{
      color: #9bc7c3;
      overflow-wrap: anywhere;
    }}
  </style>
</head>
<body>
  <main>
    <h1>BMO Wordle</h1>
    <p id="identity"></p>
    <p id="players"></p>
    <pre id="board">Loading...</pre>
    <form id="guess-form">
      <input id="guess" maxlength="5" autocomplete="off" placeholder="crane">
      <button>Guess</button>
    </form>
    <p id="message"></p>
  </main>
  <script>
    const sessionId = {session_json};
    const params = new URLSearchParams(window.location.search);
    const playerId = params.get("player_id") || "";
    const token = params.get("token") || "";
    const authQuery = new URLSearchParams({{ player_id: playerId, token }});
    const board = document.querySelector("#board");
    const players = document.querySelector("#players");
    const identity = document.querySelector("#identity");
    const message = document.querySelector("#message");
    const form = document.querySelector("#guess-form");
    const input = document.querySelector("#guess");
    const button = form.querySelector("button");

    function setDisabled(disabled) {{
      input.disabled = disabled;
      button.disabled = disabled;
    }}

    function render(data) {{
      board.textContent = data.board || "No guesses yet.";
      players.textContent = `${{data.players.length}} player(s) from Matrix`;
      identity.textContent = data.player_id ? `Playing as ${{data.player_id}}` : "";
      if (data.ended) {{
        setDisabled(true);
      }}
    }}

    async function refresh() {{
      if (!playerId || !token) {{
        board.textContent = "Open your signed player link from Matrix.";
        setDisabled(true);
        return;
      }}
      const res = await fetch(`/api/sessions/${{sessionId}}?${{authQuery}}`);
      const data = await res.json();
      if (!res.ok) {{
        board.textContent = data.error || "Could not load game.";
        setDisabled(true);
        return;
      }}
      render(data);
    }}

    function connectEvents() {{
      if (!playerId || !token) return;
      const events = new EventSource(`/api/sessions/${{sessionId}}/events?${{authQuery}}`);
      events.addEventListener("state", (event) => {{
        render(JSON.parse(event.data));
      }});
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const guess = input.value.trim();
      input.value = "";
      const res = await fetch(`/api/sessions/${{sessionId}}/actions?${{authQuery}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ action: "guess", payload: {{ guess }} }}),
      }});
      const data = await res.json();
      message.textContent = data.error || data.message || "";
      if (data.session) render(data.session);
    }});

    refresh();
    connectEvents();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
