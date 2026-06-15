from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web

from .games.plugins import (
    MAX_PLUGIN_ZIP_BYTES,
    PluginValidationError,
    discover_plugins,
    install_plugin_zip,
)
from .games.registry import GameRegistry
from .realtime import EventBroker
from .sessions import GameSession, SessionStore


STORE_KEY = web.AppKey("store", SessionStore)
BROKER_KEY = web.AppKey("broker", EventBroker)
PLUGINS_DIR_KEY = web.AppKey("plugins_dir", Path)
PLUGIN_ERRORS_KEY = web.AppKey("plugin_errors", list)
ADMIN_PASSWORD_KEY = web.AppKey("admin_password", object)
PLUGIN_UPLOADS_ENABLED_KEY = web.AppKey("plugin_uploads_enabled", bool)


def create_app(
    *,
    store: SessionStore | None = None,
    plugins_dir: str | Path | None = None,
    admin_password: str | None = None,
    enable_plugin_uploads: bool | None = None,
) -> web.Application:
    app = web.Application()
    plugin_root = Path(plugins_dir or os.environ.get("GAME_PLUGINS_DIR", "plugins"))
    if store is None:
        registry = GameRegistry.defaults()
        plugin_errors = _register_plugins(registry, plugin_root)
        store = SessionStore(
            shared_secret=os.environ.get("BMO_SHARED_SECRET", "change-me"),
            public_base_url=os.environ.get(
                "BMO_PUBLIC_BASE_URL",
                "http://localhost:8000",
            ),
            db_path=os.environ.get("BMO_DB_PATH", "data/bmo-web.sqlite3"),
            registry=registry,
        )
    else:
        plugin_errors = []

    app[STORE_KEY] = store
    app[BROKER_KEY] = EventBroker()
    app[PLUGINS_DIR_KEY] = plugin_root
    app[PLUGIN_ERRORS_KEY] = plugin_errors
    app[ADMIN_PASSWORD_KEY] = admin_password if admin_password is not None else (
        os.environ.get("ADMIN_PASSWORD") or os.environ.get("BMO_ADMIN_PASSWORD")
    )
    app[PLUGIN_UPLOADS_ENABLED_KEY] = (
        _truthy(os.environ.get("BMO_ENABLE_PLUGIN_UPLOADS", ""))
        if enable_plugin_uploads is None
        else enable_plugin_uploads
    )
    app.router.add_get("/api/games", list_games)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_post("/api/sessions/{session_id}/actions", submit_action)
    app.router.add_post("/api/sessions/{session_id}/guess", submit_guess)
    app.router.add_get("/api/sessions/{session_id}/events", session_events)
    app.router.add_get("/api/admin/summary", admin_summary)
    app.router.add_post("/api/admin/plugins/upload", admin_upload_plugin)
    app.router.add_post("/api/admin/plugins/reload", admin_reload_plugins)
    app.router.add_get("/admin", admin_redirect)
    app.router.add_get("/admin/", admin_page)
    app.router.add_get("/game/{game_key}/", game_frontend_resource)
    app.router.add_get("/game/{game_key}/{path:.+}", game_frontend_resource)
    app.router.add_get("/game/{session_id}", game_page)
    return app


async def list_games(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    return web.json_response(
        {
            "games": [
                info.to_public_dict()
                for info in store.registry.list_games()
            ]
        }
    )


async def create_session(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
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
    store = request.app[STORE_KEY]
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
    store = request.app[STORE_KEY]
    broker = request.app[BROKER_KEY]
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
    store = request.app[STORE_KEY]
    broker = request.app[BROKER_KEY]
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
    store = request.app[STORE_KEY]
    session_id = request.match_info["session_id"]
    session = store.get(session_id)
    if not session:
        return web.Response(text="Game not found", status=404)
    frontend_path = store.registry.frontend_path(session.game_key)
    if frontend_path:
        return _plugin_frontend_response(
            session_id=session_id,
            game_key=session.game_key,
            frontend_path=frontend_path,
        )
    if session.game_key == "hokm":
        return web.Response(text=_hokm_html(session_id), content_type="text/html")
    return web.Response(text=_wordle_html(session_id), content_type="text/html")


async def game_frontend_resource(request: web.Request) -> web.StreamResponse:
    store = request.app[STORE_KEY]
    game_key = request.match_info["game_key"]
    try:
        frontend_path = store.registry.frontend_path(game_key)
    except ValueError as exc:
        raise web.HTTPNotFound(text="Game not found") from exc
    if not frontend_path:
        raise web.HTTPNotFound(text="No plugin frontend for this game")

    path = request.match_info.get("path", "")
    if not path:
        return _plugin_frontend_response(
            session_id="",
            game_key=game_key,
            frontend_path=frontend_path,
        )

    relative_path = _safe_web_path(path)
    root = frontend_path.parent.resolve()
    target = (root / relative_path.as_posix()).resolve()
    if not target.is_relative_to(root):
        raise web.HTTPForbidden(text="Forbidden")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise web.HTTPNotFound(text="Asset not found")
    if target == frontend_path.resolve():
        return _plugin_frontend_response(
            session_id="",
            game_key=game_key,
            frontend_path=frontend_path,
        )
    return web.FileResponse(target)


async def admin_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound("/admin/")


async def admin_page(request: web.Request) -> web.Response:
    return web.Response(text=_admin_html(), content_type="text/html")


async def admin_summary(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    _require_admin(request, store)
    return web.json_response(_admin_summary(request.app, store))


async def admin_upload_plugin(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    _require_admin(request, store)
    if not request.app[PLUGIN_UPLOADS_ENABLED_KEY]:
        return web.json_response(
            {
                "error": (
                    "Plugin uploads are disabled. Set "
                    "BMO_ENABLE_PLUGIN_UPLOADS=1 to allow trusted admin uploads."
                )
            },
            status=403,
        )

    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return web.json_response({"error": "expected multipart upload"}, status=400)

    data: bytes | None = None
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name != "plugin":
            continue
        data = await _read_upload(field)
        break

    if not data:
        return web.json_response({"error": "missing plugin file"}, status=400)

    try:
        plugin = install_plugin_zip(data, request.app[PLUGINS_DIR_KEY])
    except PluginValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    errors = _reload_plugins(request.app)
    return web.json_response(
        {
            "game": plugin.info.to_public_dict(),
            "plugin_errors": errors,
            "summary": _admin_summary(request.app, store),
        }
    )


async def admin_reload_plugins(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    _require_admin(request, store)
    errors = _reload_plugins(request.app)
    return web.json_response(
        {
            "plugin_errors": errors,
            "summary": _admin_summary(request.app, store),
        }
    )


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


def _register_plugins(registry: GameRegistry, plugins_dir: Path) -> list[str]:
    discovery = discover_plugins(plugins_dir)
    errors = list(discovery.errors)
    for plugin in discovery.plugins:
        try:
            registry.register_plugin(plugin)
        except ValueError as exc:
            errors.append(f"{plugin.info.key}: {exc}")
    return errors


def _reload_plugins(app: web.Application) -> list[str]:
    store = app[STORE_KEY]
    registry = GameRegistry.defaults()
    errors = _register_plugins(registry, app[PLUGINS_DIR_KEY])
    store.replace_registry(registry)
    app[PLUGIN_ERRORS_KEY] = errors
    return errors


def _admin_summary(
    app: web.Application,
    store: SessionStore,
) -> dict[str, object]:
    return {
        "games": [
            info.to_public_dict()
            for info in store.registry.list_games()
        ],
        "sessions": store.list_sessions(limit=50),
        "config": {
            "public_base_url": store.public_base_url,
            "db_path": store.db_path,
            "plugins_dir": str(app[PLUGINS_DIR_KEY]),
            "plugin_uploads_enabled": bool(app[PLUGIN_UPLOADS_ENABLED_KEY]),
            "admin_password_configured": bool(app[ADMIN_PASSWORD_KEY]),
        },
        "plugin_errors": list(app[PLUGIN_ERRORS_KEY]),
    }


def _require_admin(request: web.Request, store: SessionStore) -> None:
    admin_password = str(request.app[ADMIN_PASSWORD_KEY] or "")
    provided_admin = request.headers.get("X-BMO-Admin", "")
    provided_secret = request.headers.get("X-BMO-Secret", "")

    if provided_secret and hmac_compare(provided_secret, store.shared_secret):
        return
    if admin_password and hmac_compare(provided_admin, admin_password):
        return
    if not admin_password and hmac_compare(provided_admin, store.shared_secret):
        return

    raise web.HTTPUnauthorized(
        text=json.dumps({"error": "unauthorized"}),
        content_type="application/json",
    )


def hmac_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


async def _read_upload(field: Any) -> bytes:
    chunks = bytearray()
    while True:
        chunk = await field.read_chunk()
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_PLUGIN_ZIP_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_PLUGIN_ZIP_BYTES,
                actual_size=len(chunks),
            )
    return bytes(chunks)


def _plugin_frontend_response(
    *,
    session_id: str,
    game_key: str,
    frontend_path: Path,
) -> web.Response:
    text = frontend_path.read_text(encoding="utf-8")
    asset_base = f"/game/{game_key}/"
    replacements = {
        "__SESSION_JSON__": json.dumps(session_id),
        "__SESSION_ID__": session_id,
        "__GAME_KEY__": game_key,
        "__ASSET_BASE__": asset_base,
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    return web.Response(text=text, content_type="text/html")


def _safe_web_path(path: str) -> PurePosixPath:
    if "\\" in path or "\x00" in path:
        raise web.HTTPForbidden(text="Forbidden")
    relative = PurePosixPath(path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise web.HTTPForbidden(text="Forbidden")
    return relative


def _truthy(value: str) -> bool:
    return value.lower().strip() in {"1", "true", "yes", "on"}


def _admin_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BMO Admin</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system,
        BlinkMacSystemFont, sans-serif;
      background: #f5f7fb;
      color: #17202f;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      background: #f5f7fb;
    }
    main {
      width: min(1120px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 24px 0 36px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 10px;
      font-size: 17px;
      letter-spacing: 0;
    }
    button,
    input {
      min-height: 40px;
      border-radius: 6px;
      border: 1px solid #b7c2d4;
      font: inherit;
    }
    button {
      padding: 0 14px;
      background: #1b6ed6;
      color: white;
      font-weight: 800;
      cursor: pointer;
    }
    button.secondary {
      background: #eef3fa;
      color: #17202f;
    }
    input[type="password"] {
      width: min(360px, 100%);
      padding: 0 12px;
      background: white;
      color: #17202f;
    }
    input[type="file"] {
      padding: 7px;
      background: white;
    }
    section {
      border-top: 1px solid #d7deea;
      padding: 18px 0;
    }
    .auth,
    .tools {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }
    .table {
      width: 100%;
      border-collapse: collapse;
      background: white;
      border: 1px solid #d7deea;
      border-radius: 8px;
      overflow: hidden;
    }
    th,
    td {
      padding: 10px;
      border-bottom: 1px solid #e5ebf3;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }
    th {
      background: #eef3fa;
      font-size: 12px;
      text-transform: uppercase;
    }
    pre {
      min-height: 120px;
      overflow: auto;
      padding: 12px;
      border: 1px solid #d7deea;
      border-radius: 8px;
      background: #17202f;
      color: #edf3ff;
    }
    .muted {
      color: #607086;
    }
    .error {
      color: #b42335;
      font-weight: 800;
    }
    @media (max-width: 760px) {
      header,
      .grid {
        display: block;
      }
      .tools {
        margin-top: 12px;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>BMO Admin</h1>
        <div id="status" class="muted">Locked</div>
      </div>
      <div class="auth">
        <input id="secret" type="password" autocomplete="current-password"
          placeholder="Admin password or shared secret">
        <button id="load">Unlock</button>
      </div>
    </header>

    <section>
      <div class="tools">
        <button id="reload" class="secondary">Reload Plugins</button>
        <form id="upload-form" class="tools">
          <input id="plugin-file" name="plugin" type="file" accept=".zip">
          <button>Upload Plugin</button>
        </form>
      </div>
      <p id="upload-note" class="muted"></p>
    </section>

    <div class="grid">
      <section>
        <h2>Games</h2>
        <div id="games"></div>
      </section>
      <section>
        <h2>Config</h2>
        <pre id="config">No data loaded.</pre>
      </section>
    </div>

    <section>
      <h2>Recent Sessions</h2>
      <div id="sessions"></div>
    </section>

    <section>
      <h2>Plugin Errors</h2>
      <pre id="errors">No data loaded.</pre>
    </section>
  </main>
  <script>
    const secret = document.querySelector("#secret");
    const statusEl = document.querySelector("#status");
    const gamesEl = document.querySelector("#games");
    const sessionsEl = document.querySelector("#sessions");
    const configEl = document.querySelector("#config");
    const errorsEl = document.querySelector("#errors");
    const uploadNote = document.querySelector("#upload-note");
    const uploadForm = document.querySelector("#upload-form");
    const pluginFile = document.querySelector("#plugin-file");

    function headers() {
      return { "X-BMO-Admin": secret.value };
    }

    function cell(text) {
      const td = document.createElement("td");
      td.textContent = text ?? "";
      return td;
    }

    function table(columns, rows) {
      const tableEl = document.createElement("table");
      tableEl.className = "table";
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      for (const column of columns) {
        const th = document.createElement("th");
        th.textContent = column;
        headRow.append(th);
      }
      head.append(headRow);
      const body = document.createElement("tbody");
      for (const row of rows) {
        const tr = document.createElement("tr");
        row.forEach((value) => tr.append(cell(value)));
        body.append(tr);
      }
      tableEl.append(head, body);
      return tableEl;
    }

    function render(summary) {
      statusEl.textContent = "Connected";
      statusEl.className = "muted";
      uploadNote.textContent = summary.config.plugin_uploads_enabled
        ? "Plugin uploads execute trusted Python code from the zip."
        : "Uploads are disabled. Set BMO_ENABLE_PLUGIN_UPLOADS=1 to enable them.";
      gamesEl.replaceChildren(table(
        ["Key", "Title", "Players", "Private", "Source"],
        summary.games.map((game) => [
          game.key,
          game.title,
          `${game.min_players}-${game.max_players || "∞"}`,
          game.private_player_links ? "yes" : "no",
          game.source,
        ])
      ));
      sessionsEl.replaceChildren(table(
        ["Session", "Game", "Players", "Updated"],
        summary.sessions.map((session) => [
          session.session_id,
          session.game,
          session.players.length,
          session.updated_at,
        ])
      ));
      configEl.textContent = JSON.stringify(summary.config, null, 2);
      errorsEl.textContent = summary.plugin_errors.length
        ? summary.plugin_errors.join("\\n")
        : "No plugin errors.";
    }

    async function loadSummary() {
      const res = await fetch("/api/admin/summary", { headers: headers() });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not load admin data.");
      render(data);
    }

    document.querySelector("#load").addEventListener("click", async () => {
      try {
        await loadSummary();
      } catch (error) {
        statusEl.textContent = error.message;
        statusEl.className = "error";
      }
    });

    document.querySelector("#reload").addEventListener("click", async () => {
      const res = await fetch("/api/admin/plugins/reload", {
        method: "POST",
        headers: headers(),
      });
      const data = await res.json();
      if (res.ok) render(data.summary);
    });

    uploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!pluginFile.files.length) return;
      const form = new FormData();
      form.append("plugin", pluginFile.files[0]);
      const res = await fetch("/api/admin/plugins/upload", {
        method: "POST",
        headers: headers(),
        body: form,
      });
      const data = await res.json();
      if (!res.ok) {
        uploadNote.textContent = data.error || "Upload failed.";
        uploadNote.className = "error";
        return;
      }
      uploadNote.className = "muted";
      render(data.summary);
    });
  </script>
</body>
</html>"""


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


def _hokm_html_legacy(session_id: str) -> str:
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


def _hokm_html(session_id: str) -> str:
    session_json = json.dumps(session_id)
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BMO Hokm</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system,
        BlinkMacSystemFont, sans-serif;
      background: #081423;
      color: #e7eef8;
    }
    * {
      box-sizing: border-box;
    }
    html,
    body {
      max-width: 100%;
      overflow-x: hidden;
    }
    body {
      margin: 0;
      min-height: 100vh;
      background: #081423;
    }
    main {
      width: min(1240px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 18px 0 28px;
      min-width: 0;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      letter-spacing: 0;
    }
    h2 {
      margin: 0;
      font-size: 15px;
      letter-spacing: 0;
    }
    button {
      min-height: 40px;
      border: 1px solid rgba(247, 240, 223, .34);
      border-radius: 7px;
      font: inherit;
      cursor: pointer;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: .45;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }
    .status-line {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    .badge {
      min-height: 30px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(247, 240, 223, .2);
      border-radius: 999px;
      padding: 5px 10px;
      background: rgba(15, 31, 52, .72);
      color: #c9d7ea;
      font-size: 13px;
      font-weight: 700;
    }
    .badge.hot {
      background: #f0b84d;
      border-color: #f0b84d;
      color: #081423;
    }
    .identity {
      color: #aebed4;
      overflow-wrap: anywhere;
      text-align: right;
      font-size: 14px;
    }
    .game-shell {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 300px;
      gap: 14px;
      align-items: start;
      min-width: 0;
    }
    .felt {
      min-height: 590px;
      border: 1px solid rgba(247, 240, 223, .2);
      border-radius: 8px;
      background: #0c1b2d;
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, .05),
        0 14px 36px rgba(0, 0, 0, .22);
      padding: 18px;
      min-width: 0;
      overflow: hidden;
    }
    .table-grid {
      min-height: 360px;
      display: grid;
      grid-template-columns: 185px minmax(250px, 1fr) 185px;
      grid-template-rows: 116px minmax(170px, 1fr) 122px;
      gap: 12px;
      align-items: stretch;
      min-width: 0;
    }
    .seat {
      min-width: 0;
      border: 1px solid rgba(247, 240, 223, .18);
      border-radius: 8px;
      background: rgba(45, 68, 104, .38);
      padding: 11px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 8px;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .03);
      overflow: hidden;
    }
    .seat.current {
      border-color: #f0b84d;
      box-shadow: 0 0 0 2px rgba(240, 184, 77, .34);
    }
    .seat.viewer {
      background: rgba(75, 126, 190, .22);
    }
    .seat.top {
      grid-column: 2;
      grid-row: 1;
    }
    .seat.left {
      grid-column: 1;
      grid-row: 2;
    }
    .seat.right {
      grid-column: 3;
      grid-row: 2;
    }
    .seat.bottom {
      grid-column: 2;
      grid-row: 3;
    }
    .seat-name {
      color: #f4f8ff;
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .seat-id {
      color: #9eb0c8;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .seat-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .tag {
      border-radius: 999px;
      padding: 3px 8px;
      background: rgba(8, 20, 35, .72);
      color: #c9d7ea;
      font-size: 12px;
      font-weight: 800;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tag.hakem {
      background: #f0b84d;
      color: #081423;
    }
    .tag.turn {
      background: #64d2ff;
      color: #061220;
    }
    .table-center {
      grid-column: 2;
      grid-row: 2;
      min-width: 0;
      border: 1px solid rgba(201, 215, 234, .2);
      border-radius: 8px;
      background: rgba(5, 15, 29, .68);
      padding: 14px;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 12px;
    }
    .center-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      min-width: 0;
    }
    .status {
      min-width: 0;
      color: #f0b84d;
      font-weight: 900;
      overflow-wrap: anywhere;
    }
    .trump-mark {
      min-width: 56px;
      text-align: center;
      border-radius: 8px;
      background: #dce8f7;
      color: #081423;
      padding: 8px;
      font-size: 26px;
      font-weight: 900;
      line-height: 1;
    }
    .trick {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      align-content: center;
    }
    .play {
      min-height: 82px;
      border: 1px dashed rgba(201, 215, 234, .24);
      border-radius: 8px;
      padding: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: #c9d7ea;
      min-width: 0;
    }
    .play.winner {
      border-style: solid;
      border-color: #f0b84d;
      box-shadow: 0 0 0 1px rgba(240, 184, 77, .25);
    }
    .play .name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 12px;
      font-weight: 700;
    }
    .mini-card {
      width: 46px;
      height: 64px;
      border-radius: 7px;
      border: 1px solid #cbd7e6;
      background: #f8fbff;
      color: #081423;
      display: grid;
      place-items: center;
      font-size: 18px;
      font-weight: 900;
      flex: 0 0 auto;
      box-shadow: 0 5px 12px rgba(0, 0, 0, .2);
    }
    .mini-card.red {
      color: #b62438;
    }
    .trump-panel {
      display: none;
      border-top: 1px solid rgba(201, 215, 234, .16);
      padding-top: 12px;
    }
    .trump-panel.active {
      display: block;
    }
    .trump-buttons {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      margin-top: 8px;
    }
    .trump-buttons button {
      min-width: 0;
      min-height: 54px;
      background: #dce8f7;
      color: #081423;
      font-size: 24px;
      font-weight: 900;
    }
    .trump-buttons button.red {
      color: #b62438;
    }
    .hand-wrap {
      margin-top: 18px;
      min-width: 0;
      max-width: 100%;
    }
    .hand-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }
    .hand {
      min-height: 148px;
      display: flex;
      gap: 8px;
      overflow-x: auto;
      overflow-y: hidden;
      overscroll-behavior-x: contain;
      padding: 2px 2px 12px;
      scroll-snap-type: x proximity;
      width: 100%;
      max-width: 100%;
      min-width: 0;
      -webkit-overflow-scrolling: touch;
    }
    .card {
      width: clamp(68px, 7vw, 88px);
      min-width: clamp(68px, 7vw, 88px);
      flex: 0 0 clamp(68px, 7vw, 88px);
      aspect-ratio: 5 / 7;
      background: #f8fbff;
      color: #081423;
      border: 1px solid #cbd7e6;
      box-shadow: 0 6px 14px rgba(0, 0, 0, .25);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 9px;
      font-weight: 900;
      scroll-snap-align: start;
      transition: transform .14s ease, box-shadow .14s ease, opacity .14s ease;
    }
    .card.playable {
      transform: translateY(-6px);
      border-color: #f0b84d;
      box-shadow: 0 10px 18px rgba(240, 184, 77, .28);
    }
    .card.red {
      color: #b62438;
    }
    .card span {
      align-self: flex-start;
      font-size: 18px;
    }
    .card strong {
      align-self: center;
      font-size: 34px;
      line-height: 1;
    }
    .side-panel {
      border: 1px solid rgba(247, 240, 223, .2);
      border-radius: 8px;
      background: rgba(15, 31, 52, .72);
      padding: 14px;
      margin-bottom: 12px;
    }
    .team {
      border: 1px solid rgba(247, 240, 223, .13);
      border-radius: 8px;
      background: rgba(45, 68, 104, .32);
      padding: 10px;
      margin-top: 10px;
    }
    .team.active {
      border-color: rgba(240, 184, 77, .95);
    }
    .players {
      color: #c9d7ea;
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.45;
      margin-top: 6px;
    }
    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 8px;
      color: #e7eef8;
      font-weight: 700;
    }
    .log {
      color: #c9d7ea;
      line-height: 1.45;
      min-height: 44px;
      overflow-wrap: anywhere;
    }
    .message {
      color: #f0b84d;
      font-weight: 700;
    }
    .empty {
      min-height: 120px;
      display: grid;
      place-items: center;
      border: 1px dashed rgba(201, 215, 234, .22);
      border-radius: 8px;
      color: #9eb0c8;
    }
    .danger {
      color: #ffb7c0;
    }
    @media (max-width: 900px) {
      .game-shell {
        grid-template-columns: 1fr;
      }
      .felt {
        min-height: 0;
      }
    }
    @media (max-width: 720px) {
      body {
        min-height: 100dvh;
        background: #081423;
      }
      main {
        width: 100%;
        padding: 8px 8px 12px;
      }
      .topbar {
        position: sticky;
        top: 0;
        z-index: 5;
        display: block;
        margin: 0 -8px 8px;
        padding: 8px 8px 10px;
        background: #081423;
        border-bottom: 1px solid rgba(201, 215, 234, .14);
      }
      h1 {
        font-size: 22px;
      }
      h2 {
        font-size: 14px;
      }
      .status-line {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 6px;
      }
      .badge {
        min-height: 28px;
        justify-content: center;
        padding: 4px 7px;
        font-size: 12px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .identity {
        text-align: left;
        margin-top: 6px;
        font-size: 12px;
      }
      .game-shell {
        gap: 10px;
      }
      .felt {
        min-height: 0;
        padding: 10px;
        border-radius: 0;
        border-left: 0;
        border-right: 0;
      }
      .table-grid {
        min-height: 0;
        display: grid;
        grid-template-columns: minmax(64px, .72fr) minmax(128px, 1.36fr)
          minmax(64px, .72fr);
        grid-template-rows: 68px minmax(160px, auto) 74px;
        gap: 6px;
      }
      .seat.top,
      .seat.left,
      .seat.right,
      .seat.bottom {
        min-height: 0;
        padding: 8px;
      }
      .seat.top {
        grid-column: 2;
        grid-row: 1;
      }
      .seat.left {
        grid-column: 1;
        grid-row: 2;
      }
      .seat.right {
        grid-column: 3;
        grid-row: 2;
      }
      .seat.bottom {
        grid-column: 2;
        grid-row: 3;
      }
      .table-center {
        grid-column: 2;
        grid-row: 2;
        min-height: 0;
        padding: 10px;
        gap: 8px;
      }
      .seat-name {
        font-size: 14px;
      }
      .seat-id {
        display: none;
      }
      .seat-tags {
        gap: 4px;
      }
      .tag {
        padding: 3px 6px;
        font-size: 11px;
      }
      .center-top {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 44px;
        align-items: start;
      }
      .status {
        font-size: 15px;
        line-height: 1.25;
      }
      .trump-mark {
        min-width: 48px;
        font-size: 24px;
        padding: 7px;
      }
      .trick {
        gap: 6px;
      }
      .play {
        min-height: 48px;
        justify-content: center;
        padding: 5px;
      }
      .play .name {
        display: none;
      }
      .mini-card {
        width: 34px;
        height: 46px;
        font-size: 15px;
        border-radius: 6px;
      }
      .trump-buttons {
        grid-template-columns: repeat(2, 1fr);
      }
      .trump-buttons button {
        min-height: 48px;
      }
      .hand-wrap {
        position: sticky;
        bottom: 0;
        z-index: 4;
        margin: 10px 0 0;
        padding: 10px 0 6px;
        background: #0c1b2d;
        border-top: 1px solid rgba(201, 215, 234, .16);
        overflow: hidden;
      }
      .hand-head {
        margin-bottom: 8px;
      }
      .hand {
        min-height: 118px;
        gap: 7px;
        padding-bottom: 8px;
      }
      .card {
        width: 64px;
        min-width: 64px;
        flex-basis: 64px;
        padding: 7px;
      }
      .card span {
        font-size: 16px;
      }
      .card strong {
        font-size: 30px;
      }
      .card.playable {
        transform: translateY(-4px);
      }
      .side-panel {
        margin-bottom: 8px;
        padding: 10px;
        border-radius: 0;
        border-left: 0;
        border-right: 0;
      }
      .team {
        padding: 8px;
      }
      .players,
      .stats,
      .log {
        font-size: 13px;
      }
    }
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div>
        <h1>Hokm / حکم</h1>
        <div class="status-line">
          <span id="phase-badge" class="badge">Loading</span>
          <span id="turn-badge" class="badge">Turn</span>
          <span id="trump-badge" class="badge">Trump</span>
        </div>
      </div>
      <div id="identity" class="identity"></div>
    </div>

    <div class="game-shell">
      <section class="felt">
        <div class="table-grid">
          <div id="seat-top" class="seat top"></div>
          <div id="seat-left" class="seat left"></div>
          <div id="seat-right" class="seat right"></div>
          <div id="seat-bottom" class="seat bottom"></div>
          <div class="table-center">
            <div class="center-top">
              <div id="status" class="status">Loading...</div>
              <div id="trump-mark" class="trump-mark">?</div>
            </div>
            <div class="trick" id="current-trick"></div>
            <div id="trump-panel" class="trump-panel">
              <h2>Trump / حکم</h2>
              <div id="trump-buttons" class="trump-buttons"></div>
            </div>
          </div>
        </div>

        <div class="hand-wrap">
          <div class="hand-head">
            <h2>Your hand / دست شما</h2>
            <span id="hand-count" class="badge">0 cards</span>
          </div>
          <div id="hand" class="hand"></div>
        </div>
      </section>

      <aside>
        <div class="side-panel">
          <h2>Teams / تیم‌ها</h2>
          <div id="teams"></div>
        </div>
        <div class="side-panel">
          <h2>Table / میز</h2>
          <div id="meta" class="log"></div>
        </div>
        <div class="side-panel">
          <h2>Last hand / دست قبلی</h2>
          <div id="last-hand" class="log"></div>
        </div>
        <div class="side-panel">
          <h2>Message / پیام</h2>
          <div id="message" class="log message"></div>
        </div>
      </aside>
    </div>
  </main>
  <script>
    const sessionId = __SESSION_JSON__;
    const params = new URLSearchParams(window.location.search);
    const playerId = params.get("player_id") || "";
    const token = params.get("token") || "";
    const authQuery = new URLSearchParams({ player_id: playerId, token });
    const statusEl = document.querySelector("#status");
    const identityEl = document.querySelector("#identity");
    const phaseBadge = document.querySelector("#phase-badge");
    const turnBadge = document.querySelector("#turn-badge");
    const trumpBadge = document.querySelector("#trump-badge");
    const trumpMark = document.querySelector("#trump-mark");
    const teamsEl = document.querySelector("#teams");
    const metaEl = document.querySelector("#meta");
    const lastHandEl = document.querySelector("#last-hand");
    const messageEl = document.querySelector("#message");
    const trumpPanel = document.querySelector("#trump-panel");
    const trumpButtons = document.querySelector("#trump-buttons");
    const trickEl = document.querySelector("#current-trick");
    const handEl = document.querySelector("#hand");
    const handCountEl = document.querySelector("#hand-count");
    const seatEls = {
      top: document.querySelector("#seat-top"),
      right: document.querySelector("#seat-right"),
      bottom: document.querySelector("#seat-bottom"),
      left: document.querySelector("#seat-left"),
    };

    function teamName(id) {
      return `Team ${Number(id) + 1}`;
    }

    function shortName(player) {
      return (player || "").replace(/^@/, "").split(":")[0] || player || "";
    }

    function teamFor(data, player) {
      return (data.teams || []).find((team) => team.players.includes(player));
    }

    function positionedSeats(data) {
      const seats = data.seats || [];
      if (seats.length !== 4) return {};
      const viewerIndex = seats.indexOf(data.player_id);
      if (viewerIndex === -1) {
        return {
          top: seats[0],
          right: seats[1],
          bottom: seats[2],
          left: seats[3],
        };
      }
      return {
        bottom: seats[viewerIndex],
        right: seats[(viewerIndex + 1) % 4],
        top: seats[(viewerIndex + 2) % 4],
        left: seats[(viewerIndex + 3) % 4],
      };
    }

    function node(tag, className, text) {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined) element.textContent = text;
      return element;
    }

    function render(data) {
      identityEl.textContent = data.player_id ? `Playing as ${data.player_id}` : "";
      statusEl.classList.remove("danger");
      renderStatus(data);
      renderSeats(data);
      renderTeams(data.teams || []);
      renderMeta(data);
      renderTrumpControls(data);
      renderTrick(data);
      renderHand(data.hand || [], new Set(data.playable_card_ids || []));
      renderLastHand(data.last_hand);
    }

    function renderStatus(data) {
      phaseBadge.textContent = data.phase === "choose_trump"
        ? "Trump / حکم"
        : data.phase === "finished"
          ? "Finished"
          : `Hand ${data.hand_number}`;
      phaseBadge.classList.toggle("hot", data.phase === "choose_trump");
      turnBadge.textContent = data.current_turn
        ? `Turn: ${shortName(data.current_turn)}`
        : "Turn";
      turnBadge.classList.toggle("hot", data.current_turn === data.player_id);
      trumpBadge.textContent = data.trump_symbol
        ? `Trump ${data.trump_symbol}`
        : "Trump ?";
      trumpBadge.classList.toggle("hot", Boolean(data.trump_symbol));
      trumpMark.textContent = data.trump_symbol || "?";

      if (data.phase === "finished") {
        statusEl.textContent = `${teamName(data.winner_team)} wins the match.`;
      } else if (data.phase === "choose_trump") {
        statusEl.textContent = data.can_choose_trump
          ? "Choose trump / حکم را انتخاب کنید"
          : `Waiting for Hâkem / حاکم: ${shortName(data.hakem)}`;
      } else if (data.current_turn === data.player_id) {
        statusEl.textContent = "Your turn / نوبت شما";
      } else {
        statusEl.textContent = `Turn / نوبت: ${shortName(data.current_turn)}`;
      }
    }

    function renderSeats(data) {
      const positions = positionedSeats(data);
      for (const [position, player] of Object.entries(positions)) {
        const seat = seatEls[position];
        const team = teamFor(data, player);
        seat.className = `seat ${position}`;
        seat.classList.toggle("current", player === data.current_turn);
        seat.classList.toggle("viewer", player === data.player_id);

        const main = node("div");
        main.append(
          node("div", "seat-name", shortName(player)),
          node("div", "seat-id", player)
        );

        const tags = node("div", "seat-tags");
        if (team) tags.append(node("span", "tag", teamName(team.id)));
        if (player === data.hakem) {
          tags.append(node("span", "tag hakem", "Hâkem / حاکم"));
        }
        if (player === data.current_turn) {
          tags.append(node("span", "tag turn", "Turn"));
        }
        seat.replaceChildren(main, tags);
      }
    }

    function renderTeams(teams) {
      teamsEl.replaceChildren(...teams.map((team) => {
        const item = node("div", `team${team.is_hakem_team ? " active" : ""}`);
        const title = node(
          "strong",
          "",
          `${team.name}${team.is_hakem_team ? " · Hâkem" : ""}`
        );
        const players = node("div", "players", team.players.map(shortName).join(" / "));
        const stats = node("div", "stats", `Score ${team.score} · Tricks ${team.tricks}`);
        item.append(title, players, stats);
        return item;
      }));
    }

    function renderMeta(data) {
      const trump = data.trump_symbol ? data.trump_symbol : "not chosen";
      metaEl.textContent =
        `Hand ${data.hand_number} · Hâkem / حاکم: ${shortName(data.hakem)}` +
        ` · Trump / حکم: ${trump}`;
    }

    function renderTrumpControls(data) {
      trumpPanel.classList.toggle("active", data.phase === "choose_trump");
      trumpButtons.replaceChildren();
      if (data.phase !== "choose_trump") return;
      for (const option of data.trump_options || []) {
        const button = node("button", ["hearts", "diamonds"].includes(option.suit)
          ? "red"
          : "");
        button.textContent = option.symbol;
        button.disabled = !data.can_choose_trump;
        button.addEventListener("click", () => {
          sendAction("choose_trump", { suit: option.suit });
        });
        trumpButtons.append(button);
      }
    }

    function visibleTrick(data) {
      const current = data.current_trick || [];
      if (current.length) {
        return { plays: current, winner: "" };
      }
      const last = data.last_trick || {};
      return { plays: last.cards || [], winner: last.winner || "" };
    }

    function renderTrick(data) {
      const { plays, winner } = visibleTrick(data);
      const byPlayer = new Map(plays.map((play) => [play.player_id, play]));
      const positions = positionedSeats(data);
      const cells = ["top", "right", "bottom", "left"].map((position) => {
        const player = positions[position];
        const play = byPlayer.get(player);
        const cell = node("div", `play${player === winner ? " winner" : ""}`);
        cell.append(node("span", "name", shortName(player)));
        if (play) {
          cell.append(node("div", `mini-card ${play.card.color}`, play.card.label));
        } else {
          cell.append(node("div", "mini-card", "·"));
        }
        return cell;
      });
      trickEl.replaceChildren(...cells);
    }

    function renderHand(cards, playable) {
      handCountEl.textContent = `${cards.length} card${cards.length === 1 ? "" : "s"}`;
      if (!cards.length) {
        handEl.replaceChildren(node("div", "empty", "Waiting for deal"));
        return;
      }
      handEl.replaceChildren(...cards.map((card) => {
        const isPlayable = playable.has(card.id);
        const button = node("button", `card ${card.color}${isPlayable ? " playable" : ""}`);
        button.disabled = !isPlayable;
        button.append(node("span", "", card.rank), node("strong", "", card.symbol));
        button.addEventListener("click", () => {
          sendAction("play_card", { card: card.id });
        });
        return button;
      }));
    }

    function renderLastHand(lastHand) {
      if (!lastHand) {
        lastHandEl.textContent = "No completed hand.";
        return;
      }
      lastHandEl.textContent =
        `${teamName(lastHand.winner_team)} · ${lastHand.result}` +
        ` · +${lastHand.points}`;
    }

    async function sendAction(action, payload) {
      const res = await fetch(`/api/sessions/${sessionId}/actions?${authQuery}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, payload }),
      });
      const data = await res.json();
      messageEl.textContent = data.error || data.message || "";
      if (data.session) render(data.session);
    }

    async function refresh() {
      if (!playerId || !token) {
        statusEl.textContent = "Open your signed player link from Matrix.";
        phaseBadge.textContent = "Signed link";
        turnBadge.textContent = "Waiting";
        trumpBadge.textContent = "Trump ?";
        return;
      }
      const res = await fetch(`/api/sessions/${sessionId}?${authQuery}`);
      const data = await res.json();
      if (!res.ok) {
        statusEl.textContent = data.error || "Could not load game.";
        statusEl.classList.add("danger");
        return;
      }
      render(data);
    }

    function connectEvents() {
      if (!playerId || !token) return;
      const events = new EventSource(`/api/sessions/${sessionId}/events?${authQuery}`);
      events.addEventListener("state", (event) => {
        render(JSON.parse(event.data));
      });
    }

    refresh();
    connectEvents();
  </script>
</body>
</html>""".replace("__SESSION_JSON__", session_json)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
