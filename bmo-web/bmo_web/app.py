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

    await broker.publish(session_id)
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
    session, player_id = _authorized_session(request, store)
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
    await _write_sse(response, store.serialize(session, player_id=player_id), event="state")

    try:
        while True:
            await queue.get()
            current_session = store.get(session.session_id)
            if not current_session:
                break
            payload = store.serialize(current_session, player_id=player_id)
            await _write_sse(response, payload, event="state")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        broker.unsubscribe(session.session_id, queue)

    return response


async def game_page(request: web.Request) -> web.Response:
    store: SessionStore = request.app["store"]
    session_id = request.match_info["session_id"]
    session = store.get(session_id)
    if not session:
        return web.Response(text="Game not found", status=404)
    if session.game_key == "hokm":
        return web.Response(text=_hokm_html(session_id), content_type="text/html")
    return web.Response(text=_wordle_html(session_id), content_type="text/html")


def _authorized_session(
    request: web.Request,
    store: SessionStore,
) -> tuple[GameSession, str]:
    session = store.get(request.match_info["session_id"])
    if not session:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "not found"}),
            content_type="application/json",
        )

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


def _wordle_html(session_id: str) -> str:
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
      font-family: Inter, ui-sans-serif, system-ui, -apple-system,
        BlinkMacSystemFont, sans-serif;
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


def _hokm_html(session_id: str) -> str:
    session_json = json.dumps(session_id)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BMO Hokm</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system,
        BlinkMacSystemFont, sans-serif;
      background: #0d2f25;
      color: #f7f3e8;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(232, 184, 74, .16), transparent 34rem),
        linear-gradient(135deg, #0d2f25 0%, #174537 54%, #421f2a 100%);
    }}
    main {{
      width: min(1180px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 24px 0 32px;
    }}
    h1 {{
      margin: 0;
      font-size: 30px;
      letter-spacing: 0;
    }}
    button {{
      min-height: 40px;
      border: 1px solid rgba(247, 243, 232, .34);
      border-radius: 7px;
      font: inherit;
      cursor: pointer;
    }}
    button:disabled {{
      cursor: not-allowed;
      opacity: .45;
    }}
    .topbar {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }}
    .identity {{
      color: #d7c89e;
      overflow-wrap: anywhere;
      text-align: right;
    }}
    .table {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 14px;
      align-items: start;
    }}
    .surface {{
      min-height: 420px;
      border: 1px solid rgba(247, 243, 232, .2);
      border-radius: 8px;
      background: rgba(9, 38, 30, .76);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .04);
      padding: 18px;
    }}
    .panel {{
      border: 1px solid rgba(247, 243, 232, .2);
      border-radius: 8px;
      background: rgba(247, 243, 232, .08);
      padding: 14px;
      margin-bottom: 12px;
    }}
    .panel h2 {{
      margin: 0 0 10px;
      font-size: 16px;
      letter-spacing: 0;
    }}
    .status {{
      min-height: 28px;
      color: #f2c766;
      font-weight: 700;
    }}
    .teams {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .team {{
      border-radius: 8px;
      background: rgba(255, 255, 255, .08);
      padding: 10px;
    }}
    .team.active {{
      outline: 2px solid #f2c766;
    }}
    .players {{
      color: #d9decf;
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.45;
    }}
    .stats {{
      display: flex;
      gap: 12px;
      margin-top: 8px;
      color: #f7f3e8;
      font-weight: 700;
    }}
    .trump-buttons {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .trump-buttons button {{
      min-width: 64px;
      background: #f7f3e8;
      color: #1d2a26;
      font-size: 22px;
      font-weight: 800;
    }}
    .trick {{
      display: grid;
      grid-template-columns: repeat(4, minmax(82px, 1fr));
      gap: 10px;
      margin-top: 18px;
    }}
    .play {{
      min-height: 112px;
      border: 1px dashed rgba(247, 243, 232, .28);
      border-radius: 8px;
      display: grid;
      place-items: center;
      padding: 8px;
      text-align: center;
      color: #d9decf;
    }}
    .play strong {{
      display: block;
      font-size: 34px;
      margin-top: 4px;
    }}
    .hand {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(68px, 1fr));
      gap: 8px;
      margin-top: 16px;
    }}
    .card {{
      aspect-ratio: 5 / 7;
      background: #fbfaf5;
      color: #161b18;
      border: 1px solid #d8d0bd;
      box-shadow: 0 4px 10px rgba(0, 0, 0, .22);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 8px;
      font-weight: 800;
    }}
    .card.red {{
      color: #ba2636;
    }}
    .card span {{
      align-self: flex-start;
      font-size: 18px;
    }}
    .card strong {{
      align-self: center;
      font-size: 34px;
      line-height: 1;
    }}
    .log {{
      color: #d9decf;
      line-height: 1.45;
      min-height: 44px;
    }}
    @media (max-width: 820px) {{
      .table {{
        grid-template-columns: 1fr;
      }}
      .topbar {{
        display: block;
      }}
      .identity {{
        text-align: left;
        margin-top: 8px;
      }}
      .trick {{
        grid-template-columns: repeat(2, minmax(82px, 1fr));
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div>
        <h1>Hokm / حکم</h1>
        <div id="status" class="status">Loading...</div>
      </div>
      <div id="identity" class="identity"></div>
    </div>
    <div class="table">
      <section class="surface">
        <div id="teams" class="teams"></div>
        <div id="trump-panel" class="panel" hidden>
          <h2>Trump / حکم</h2>
          <div id="trump-buttons" class="trump-buttons"></div>
        </div>
        <div class="trick" id="current-trick"></div>
        <h2>Your hand / دست شما</h2>
        <div id="hand" class="hand"></div>
      </section>
      <aside>
        <div class="panel">
          <h2>Table / میز</h2>
          <div id="meta" class="log"></div>
        </div>
        <div class="panel">
          <h2>Last hand / دست قبلی</h2>
          <div id="last-hand" class="log"></div>
        </div>
        <div class="panel">
          <h2>Message / پیام</h2>
          <div id="message" class="log"></div>
        </div>
      </aside>
    </div>
  </main>
  <script>
    const sessionId = {session_json};
    const params = new URLSearchParams(window.location.search);
    const playerId = params.get("player_id") || "";
    const token = params.get("token") || "";
    const authQuery = new URLSearchParams({{ player_id: playerId, token }});
    const statusEl = document.querySelector("#status");
    const identityEl = document.querySelector("#identity");
    const teamsEl = document.querySelector("#teams");
    const metaEl = document.querySelector("#meta");
    const lastHandEl = document.querySelector("#last-hand");
    const messageEl = document.querySelector("#message");
    const trumpPanel = document.querySelector("#trump-panel");
    const trumpButtons = document.querySelector("#trump-buttons");
    const trickEl = document.querySelector("#current-trick");
    const handEl = document.querySelector("#hand");

    function teamName(id) {{
      return `Team ${{Number(id) + 1}}`;
    }}

    function render(data) {{
      identityEl.textContent = data.player_id ? `Playing as ${{data.player_id}}` : "";
      renderStatus(data);
      renderTeams(data.teams || []);
      renderMeta(data);
      renderTrumpControls(data);
      renderTrick(data.current_trick || []);
      renderHand(data.hand || [], new Set(data.playable_card_ids || []));
      renderLastHand(data.last_hand);
    }}

    function renderStatus(data) {{
      if (data.phase === "finished") {{
        statusEl.textContent = `${{teamName(data.winner_team)}} wins the match.`;
      }} else if (data.phase === "choose_trump") {{
        statusEl.textContent = data.can_choose_trump
          ? "Choose trump / حکم را انتخاب کنید"
          : `Waiting for Hâkem / حاکم: ${{data.hakem}}`;
      }} else if (data.current_turn === data.player_id) {{
        statusEl.textContent = "Your turn / نوبت شما";
      }} else {{
        statusEl.textContent = `Turn / نوبت: ${{data.current_turn || ""}}`;
      }}
    }}

    function renderTeams(teams) {{
      teamsEl.replaceChildren(...teams.map((team) => {{
        const item = document.createElement("div");
        item.className = `team${{team.is_hakem_team ? " active" : ""}}`;
        const title = document.createElement("strong");
        title.textContent = `${{team.name}}${{team.is_hakem_team ? " · Hâkem" : ""}}`;
        const players = document.createElement("div");
        players.className = "players";
        players.textContent = team.players.join(" / ");
        const stats = document.createElement("div");
        stats.className = "stats";
        stats.textContent = `Score ${{team.score}} · Tricks ${{team.tricks}}`;
        item.append(title, players, stats);
        return item;
      }}));
    }}

    function renderMeta(data) {{
      const trump = data.trump_symbol ? data.trump_symbol : "not chosen";
      metaEl.textContent =
        `Hand ${{data.hand_number}} · Hâkem / حاکم: ${{data.hakem}}` +
        ` · Trump / حکم: ${{trump}}`;
    }}

    function renderTrumpControls(data) {{
      trumpPanel.hidden = data.phase !== "choose_trump";
      trumpButtons.replaceChildren();
      if (data.phase !== "choose_trump") return;
      for (const option of data.trump_options || []) {{
        const button = document.createElement("button");
        button.textContent = option.symbol;
        button.disabled = !data.can_choose_trump;
        button.addEventListener("click", () => {{
          sendAction("choose_trump", {{ suit: option.suit }});
        }});
        trumpButtons.append(button);
      }}
    }}

    function renderTrick(plays) {{
      const cells = [];
      for (let index = 0; index < 4; index += 1) {{
        const play = plays[index];
        const cell = document.createElement("div");
        cell.className = "play";
        if (play) {{
          const player = document.createElement("span");
          player.textContent = play.player_id;
          const card = document.createElement("strong");
          card.textContent = play.card.label;
          if (play.card.color === "red") card.style.color = "#ff6b7b";
          cell.append(player, card);
        }} else {{
          cell.textContent = "Waiting";
        }}
        cells.push(cell);
      }}
      trickEl.replaceChildren(...cells);
    }}

    function renderHand(cards, playable) {{
      if (!cards.length) {{
        const empty = document.createElement("div");
        empty.className = "log";
        empty.textContent = "No cards visible yet.";
        handEl.replaceChildren(empty);
        return;
      }}
      handEl.replaceChildren(...cards.map((card) => {{
        const button = document.createElement("button");
        button.className = `card ${{card.color}}`;
        button.disabled = !playable.has(card.id);
        const rank = document.createElement("span");
        rank.textContent = card.rank;
        const symbol = document.createElement("strong");
        symbol.textContent = card.symbol;
        button.append(rank, symbol);
        button.addEventListener("click", () => {{
          sendAction("play_card", {{ card: card.id }});
        }});
        return button;
      }}));
    }}

    function renderLastHand(lastHand) {{
      if (!lastHand) {{
        lastHandEl.textContent = "No completed hand yet.";
        return;
      }}
      lastHandEl.textContent =
        `${{teamName(lastHand.winner_team)}} · ${{lastHand.result}}` +
        ` · +${{lastHand.points}}`;
    }}

    async function sendAction(action, payload) {{
      const res = await fetch(`/api/sessions/${{sessionId}}/actions?${{authQuery}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ action, payload }}),
      }});
      const data = await res.json();
      messageEl.textContent = data.error || data.message || "";
      if (data.session) render(data.session);
    }}

    async function refresh() {{
      if (!playerId || !token) {{
        statusEl.textContent = "Open your signed player link from Matrix.";
        return;
      }}
      const res = await fetch(`/api/sessions/${{sessionId}}?${{authQuery}}`);
      const data = await res.json();
      if (!res.ok) {{
        statusEl.textContent = data.error || "Could not load game.";
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

    refresh();
    connectEvents();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
