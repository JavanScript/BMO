from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web

from .games.debug import run_debug
from .games.plugins import (
    MAX_PLUGIN_ZIP_BYTES,
    PluginValidationError,
    _is_plugin_enabled,
    close_all_sandboxes,
    discover_plugins,
    install_plugin_zip,
    set_plugin_enabled,
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
ADMIN_TOKENS_KEY = web.AppKey("admin_tokens", dict)
ADMIN_TOKEN_TTL = 3600  # 1 hour
SSE_HEARTBEAT_SECONDS = 25


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
        shared_secret = os.environ.get("BMO_SHARED_SECRET", "change-me")
        _warn_default_secret(shared_secret)
        store = SessionStore(
            shared_secret=shared_secret,
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
    app[ADMIN_TOKENS_KEY] = {}
    app.router.add_get("/api/games", list_games)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_post("/api/sessions/{session_id}/actions", submit_action)
    app.router.add_get("/api/sessions/{session_id}/events", session_events)
    app.router.add_post("/api/admin/login", admin_login)
    app.router.add_post("/api/admin/logout", admin_logout)
    app.router.add_get("/api/admin/summary", admin_summary)
    app.router.add_post("/api/admin/plugins/upload", admin_upload_plugin)
    app.router.add_post("/api/admin/plugins/reload", admin_reload_plugins)
    app.router.add_post("/api/admin/plugins/{key}/enable", admin_enable_plugin)
    app.router.add_post("/api/admin/plugins/{key}/disable", admin_disable_plugin)
    app.router.add_post("/api/admin/debug/play", admin_debug_play)
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
    provided = request.headers.get("X-BMO-Secret", "")
    if not hmac.compare_digest(provided, store.shared_secret):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    display_names = body.get("display_names", {})
    try:
        session = store.create_session(
            game_key=body["game"],
            lobby_id=body["lobby_id"],
            room_id=body["room_id"],
            players=body.get("players", []),
            display_names=display_names,
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
            try:
                await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
                continue
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


async def admin_debug_play(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    _require_admin(request, store)
    body = await request.json() if request.can_read_body else {}
    game_key = str(body.get("game", ""))
    if not game_key:
        return web.json_response({"error": "game key required"}, status=400)
    try:
        info = store.registry.info(game_key)
        factory = store.registry.get_factory(game_key)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    players = [f"@debug-{i}" for i in range(info.min_players)]
    loop = asyncio.get_running_loop()
    log = await loop.run_in_executor(None, run_debug, factory, players)
    return web.json_response({
        "game_key": game_key,
        "players": players,
        "steps": len(log),
        "result": log[-1] if log else None,
    })


async def admin_enable_plugin(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    _require_admin(request, store)
    key = request.match_info["key"]
    set_plugin_enabled(request.app[PLUGINS_DIR_KEY], key, True)
    errors = _reload_plugins(request.app)
    return web.json_response({"ok": True, "plugin_errors": errors, "summary": _admin_summary(request.app, store)})


async def admin_disable_plugin(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    _require_admin(request, store)
    key = request.match_info["key"]
    set_plugin_enabled(request.app[PLUGINS_DIR_KEY], key, False)
    errors = _reload_plugins(request.app)
    return web.json_response({"ok": True, "plugin_errors": errors, "summary": _admin_summary(request.app, store)})


async def admin_login(request: web.Request) -> web.Response:
    body = await request.json()
    password = str(body.get("password", ""))
    if not password:
        return web.json_response({"error": "password required"}, status=400)

    store = request.app[STORE_KEY]
    admin_password = str(request.app[ADMIN_PASSWORD_KEY] or "")
    tokens = request.app[ADMIN_TOKENS_KEY]

    valid = False
    if admin_password and hmac.compare_digest(password, admin_password):
        valid = True
    elif not admin_password and hmac.compare_digest(password, store.shared_secret):
        valid = True

    if not valid:
        return web.json_response({"error": "invalid password"}, status=401)

    now = time.time()
    _prune_admin_tokens(tokens, now)
    token = secrets.token_urlsafe(32)
    tokens[token] = now + ADMIN_TOKEN_TTL
    return web.json_response({"token": token})


async def admin_logout(request: web.Request) -> web.Response:
    tokens = request.app[ADMIN_TOKENS_KEY]
    token = request.headers.get("X-BMO-Admin-Token", "")
    tokens.pop(token, None)
    return web.json_response({"ok": True})


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
    close_all_sandboxes()
    registry = GameRegistry.defaults()
    errors = _register_plugins(registry, app[PLUGINS_DIR_KEY])
    store.replace_registry(registry)
    app[PLUGIN_ERRORS_KEY] = errors
    return errors


def _admin_summary(
    app: web.Application,
    store: SessionStore,
) -> dict[str, object]:
    plugins_dir = app[PLUGINS_DIR_KEY]
    games = []
    for info in store.registry.list_games():
        d = info.to_public_dict()
        if info.source == "plugin":
            d["enabled"] = _is_plugin_enabled(plugins_dir, info.key)
        else:
            d["enabled"] = True
        games.append(d)
    return {
        "games": games,
        "sessions": store.list_sessions(limit=50),
        "config": {
            "public_base_url": store.public_base_url,
            "plugins_dir": str(plugins_dir),
            "plugin_uploads_enabled": bool(app[PLUGIN_UPLOADS_ENABLED_KEY]),
            "admin_password_configured": bool(app[ADMIN_PASSWORD_KEY]),
        },
        "plugin_errors": list(app[PLUGIN_ERRORS_KEY]),
    }


def _prune_admin_tokens(tokens: dict[str, float], now: float) -> None:
    expired = [token for token, expiry in tokens.items() if expiry <= now]
    for token in expired:
        tokens.pop(token, None)


def _require_admin(request: web.Request, store: SessionStore) -> None:
    tokens = request.app.get(ADMIN_TOKENS_KEY, {})
    now = time.time()
    token = request.headers.get("X-BMO-Admin-Token", "")
    expiry = tokens.get(token, 0)
    if expiry > now:
        return
    _prune_admin_tokens(tokens, now)

    admin_password = str(request.app[ADMIN_PASSWORD_KEY] or "")
    provided_admin = request.headers.get("X-BMO-Admin", "")
    provided_secret = request.headers.get("X-BMO-Secret", "")

    if provided_secret and hmac.compare_digest(provided_secret, store.shared_secret):
        return
    if admin_password and hmac.compare_digest(provided_admin, admin_password):
        return
    if not admin_password and hmac.compare_digest(provided_admin, store.shared_secret):
        return

    raise web.HTTPUnauthorized(
        text=json.dumps({"error": "unauthorized"}),
        content_type="application/json",
    )


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


def _warn_default_secret(secret: str) -> None:
    if secret == "change-me":
        import warnings
        warnings.warn(
            "BMO_SHARED_SECRET is set to the default value 'change-me'. "
            "This is insecure for production. Set a unique secret via "
            "the BMO_SHARED_SECRET environment variable."
        )


def _admin_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BMO Admin</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
        "SF Pro Display", "Segoe UI", system-ui, sans-serif;
      --bg-primary: #f2f2f7;
      --bg-elevated: rgba(255, 255, 255, .8);
      --bg-card: #ffffff;
      --bg-fill: rgba(120, 120, 128, .08);
      --bg-fill-strong: rgba(120, 120, 128, .14);
      --separator: rgba(60, 60, 67, .12);
      --separator-opaque: #d1d1d6;
      --text-primary: #1c1c1e;
      --text-secondary: #6c6c70;
      --text-tertiary: #aeaeb2;
      --accent: #007aff;
      --accent-hover: #0a84ff;
      --danger: #ff3b30;
      --success: #34c759;
      --warning: #ff9500;
      --shadow: 0 1px 3px rgba(0, 0, 0, .06), 0 8px 24px rgba(0, 0, 0, .06);
      --radius-lg: 18px;
      --radius-md: 12px;
      --radius-sm: 8px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg-primary: #000000;
        --bg-elevated: rgba(28, 28, 30, .8);
        --bg-card: #1c1c1e;
        --bg-fill: rgba(120, 120, 128, .18);
        --bg-fill-strong: rgba(120, 120, 128, .28);
        --separator: rgba(84, 84, 88, .4);
        --separator-opaque: #38383a;
        --text-primary: #ffffff;
        --text-secondary: #98989f;
        --text-tertiary: #6c6c70;
        --accent: #0a84ff;
        --accent-hover: #409cff;
        --danger: #ff453a;
        --success: #30d158;
        --warning: #ff9f0a;
        --shadow: 0 1px 3px rgba(0, 0, 0, .4), 0 8px 24px rgba(0, 0, 0, .5);
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg-primary);
      color: var(--text-primary);
      -webkit-font-smoothing: antialiased;
      letter-spacing: -.01em;
    }
    main {
      width: min(1040px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 0 0 48px;
    }
    .login-screen {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 24px;
    }
    .login-card {
      width: min(360px, 100%);
      padding: 32px 28px;
      background: var(--bg-card);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      text-align: center;
    }
    .login-card h1 {
      margin: 0 0 6px;
      font-size: 22px;
      font-weight: 700;
    }
    .login-card .subtitle {
      margin: 0 0 24px;
      color: var(--text-secondary);
      font-size: 14px;
    }
    .login-card .error {
      color: var(--danger);
      font-size: 13px;
      margin: 12px 0 0;
      min-height: 18px;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin: 0 -16px 8px;
      padding: 16px;
      background: var(--bg-elevated);
      backdrop-filter: saturate(180%) blur(20px);
      -webkit-backdrop-filter: saturate(180%) blur(20px);
      border-bottom: .5px solid var(--separator);
    }
    .header-left h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
    }
    .header-left div {
      color: var(--text-secondary);
      font-size: 13px;
      margin-top: 1px;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; }
    h2 {
      margin: 0 0 12px;
      font-size: 13px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .04em;
      color: var(--text-secondary);
    }
    button, input {
      min-height: 38px;
      border-radius: var(--radius-sm);
      border: none;
      font: inherit;
      letter-spacing: -.01em;
      background: transparent;
      color: var(--text-primary);
    }
    button {
      padding: 0 18px;
      background: var(--accent);
      color: #fff;
      font-weight: 600;
      cursor: pointer;
      transition: background .15s, transform .08s, opacity .15s;
    }
    button:hover { background: var(--accent-hover); }
    button:active { transform: scale(.97); }
    button:disabled { opacity: .4; cursor: not-allowed; }
    button:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    button.secondary {
      background: var(--bg-fill);
      color: var(--accent);
    }
    button.secondary:hover { background: var(--bg-fill-strong); }
    button.danger {
      background: var(--bg-fill);
      color: var(--danger);
    }
    button.danger:hover { background: rgba(255, 59, 48, .14); }
    input {
      width: 100%;
      padding: 0 14px;
      background: var(--bg-fill);
      outline: none;
      transition: box-shadow .15s;
    }
    input::placeholder { color: var(--text-tertiary); }
    input:focus { box-shadow: 0 0 0 3.5px rgba(0, 122, 255, .3); }
    input[type="file"] { padding: 8px; }
    .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
    section {
      margin-top: 24px;
    }
    .tools {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }
    .upload-zone {
      flex: 1;
      min-width: 240px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      border: 1.5px dashed var(--separator-opaque);
      border-radius: var(--radius-md);
      background: var(--bg-card);
      cursor: pointer;
      font-size: 14px;
      color: var(--text-secondary);
      transition: border-color .15s, background .15s;
    }
    .upload-zone:hover { border-color: var(--accent); }
    .upload-zone.dragover {
      border-color: var(--accent);
      background: rgba(0, 122, 255, .08);
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    .card {
      background: var(--bg-card);
      border-radius: var(--radius-lg);
      padding: 18px 20px;
      box-shadow: var(--shadow);
    }
    .table-wrap {
      background: var(--bg-card);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .table-scroll { overflow-x: auto; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      padding: 13px 16px;
      border-bottom: .5px solid var(--separator);
      text-align: left;
      vertical-align: middle;
    }
    tbody tr:last-child td { border-bottom: none; }
    th {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .03em;
      color: var(--text-secondary);
    }
    tbody tr { transition: background .12s; }
    tbody tr:hover td { background: var(--bg-fill); }
    td:first-child { font-variant-numeric: tabular-nums; }
    pre {
      margin: 0;
      min-height: 80px;
      overflow: auto;
      padding: 16px;
      border-radius: var(--radius-md);
      background: var(--bg-fill);
      color: var(--text-primary);
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 12.5px;
      line-height: 1.55;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: var(--bg-fill-strong);
      color: var(--text-secondary);
    }
    .badge-yes { background: rgba(52, 199, 89, .18); color: var(--success); }
    .badge-no { background: rgba(255, 59, 48, .16); color: var(--danger); }
    .muted { color: var(--text-secondary); }
    .error { color: var(--danger); font-weight: 600; }
    .success { color: var(--success); font-weight: 600; }
    .spinner {
      display: inline-block;
      width: 18px; height: 18px;
      border: 2px solid var(--separator);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin .6s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .hidden { display: none !important; }
    .progress-bar {
      width: 100%;
      height: 5px;
      background: var(--bg-fill-strong);
      border-radius: 999px;
      overflow: hidden;
      margin: 10px 0;
    }
    .progress-bar div {
      height: 100%;
      background: var(--accent);
      width: 0;
      border-radius: 999px;
      transition: width .2s;
    }
    @media (max-width: 760px) {
      .grid { grid-template-columns: 1fr; }
      header { margin: 0 -16px 8px; }
    }
  </style>
</head>
<body>
  <div id="login-screen" class="login-screen">
    <div class="login-card">
      <h1>BMO Admin</h1>
      <p class="subtitle">Sign in to manage games and plugins</p>
      <input id="login-password" type="password" autocomplete="current-password"
        placeholder="Admin password">
      <div class="btn-row" style="margin-top:16px">
        <button id="login-btn" style="flex:1">Unlock</button>
      </div>
      <p id="login-error" class="error"></p>
    </div>
  </div>

  <main id="dashboard" class="hidden">
    <header>
      <div class="header-left">
        <h1>BMO Admin</h1>
        <div id="status">Disconnected</div>
      </div>
      <div class="btn-row">
        <span id="session-info" class="muted" style="font-size:13px"></span>
        <button id="logout-btn" class="danger">Logout</button>
      </div>
    </header>

    <section>
      <h2>Tools</h2>
      <div class="tools">
        <button id="reload-btn" class="secondary">Reload Plugins</button>
        <div class="upload-zone" id="upload-zone">
          <span>📦</span>
          <span id="upload-label">Drop plugin zip here or click to browse</span>
          <input id="plugin-file" type="file" accept=".zip" hidden>
        </div>
      </div>
      <div id="progress-wrap" class="hidden">
        <div class="progress-bar"><div id="progress-fill"></div></div>
      </div>
      <p id="upload-note" class="muted" style="margin:8px 0 0"></p>
      <p id="debug-note" class="muted" style="margin:8px 0 0"></p>
    </section>

    <section>
      <h2>Games</h2>
      <div id="games"><div class="spinner"></div></div>
    </section>

    <section>
      <h2>Recent Sessions</h2>
      <div id="sessions"><div class="spinner"></div></div>
    </section>

    <div class="grid">
      <section>
        <h2>Config</h2>
        <div class="card"><pre id="config">Loading...</pre></div>
      </section>
      <section>
        <h2>Plugin Errors</h2>
        <div class="card"><pre id="errors">Loading...</pre></div>
      </section>
    </div>
  </main>

  <script>
    let adminToken = localStorage.getItem("bmo_admin_token") || "";
    const loginScreen = document.querySelector("#login-screen");
    const dashboard = document.querySelector("#dashboard");
    const loginPassword = document.querySelector("#login-password");
    const loginBtn = document.querySelector("#login-btn");
    const loginError = document.querySelector("#login-error");
    const statusEl = document.querySelector("#status");
    const sessionInfo = document.querySelector("#session-info");
    const logoutBtn = document.querySelector("#logout-btn");
    const reloadBtn = document.querySelector("#reload-btn");
    const gamesEl = document.querySelector("#games");
    const sessionsEl = document.querySelector("#sessions");
    const configEl = document.querySelector("#config");
    const errorsEl = document.querySelector("#errors");
    const uploadNote = document.querySelector("#upload-note");
    const uploadZone = document.querySelector("#upload-zone");
    const pluginFile = document.querySelector("#plugin-file");
    const progressWrap = document.querySelector("#progress-wrap");
    const progressFill = document.querySelector("#progress-fill");
    const debugNote = document.querySelector("#debug-note");

    function apiHeaders() {
      const h = { "Content-Type": "application/json" };
      if (adminToken) h["X-BMO-Admin-Token"] = adminToken;
      return h;
    }

    async function api(path, opts = {}) {
      const res = await fetch(path, { headers: { ...apiHeaders(), ...opts.headers }, ...opts });
      if (res.status === 401) { adminToken = ""; localStorage.removeItem("bmo_admin_token"); showLogin(); }
      return res;
    }

    function showLogin() {
      loginScreen.classList.remove("hidden");
      dashboard.classList.add("hidden");
      loginPassword.value = "";
      loginError.textContent = "";
    }

    function showDashboard() {
      loginScreen.classList.add("hidden");
      dashboard.classList.remove("hidden");
    }

    async function doLogin() {
      const password = loginPassword.value;
      if (!password) return;
      loginBtn.disabled = true;
      loginError.textContent = "";
      try {
        const res = await fetch("/api/admin/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password }),
        });
        const data = await res.json();
        if (!res.ok) { loginError.textContent = data.error || "Login failed."; return; }
        adminToken = data.token;
        localStorage.setItem("bmo_admin_token", adminToken);
        showDashboard();
        loadSummary();
      } catch (e) {
        loginError.textContent = "Connection error.";
      } finally {
        loginBtn.disabled = false;
      }
    }

    async function doLogout() {
      await fetch("/api/admin/logout", { method: "POST", headers: { "X-BMO-Admin-Token": adminToken } });
      adminToken = "";
      localStorage.removeItem("bmo_admin_token");
      showLogin();
    }

    function cell(text) {
      const td = document.createElement("td");
      td.textContent = text ?? "";
      return td;
    }

    function badge(val, yesText, noText) {
      const span = document.createElement("span");
      span.className = `badge badge-${val ? "yes" : "no"}`;
      span.textContent = val ? (yesText || "yes") : (noText || "no");
      return span;
    }

    function table(columns, rows, renderRow) {
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const scroll = document.createElement("div");
      scroll.className = "table-scroll";
      const t = document.createElement("table");
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      for (const col of columns) {
        const th = document.createElement("th");
        th.textContent = col;
        headRow.append(th);
      }
      head.append(headRow);
      const body = document.createElement("tbody");
      for (const row of rows) {
        const tr = document.createElement("tr");
        const vals = renderRow ? renderRow(row) : row;
        vals.forEach((v) => {
          if (v instanceof Node) { const td = document.createElement("td"); td.append(v); tr.append(td); }
          else { tr.append(cell(v)); }
        });
        body.append(tr);
      }
      t.append(head, body);
      scroll.append(t);
      wrap.append(scroll);
      return wrap;
    }

    function shortId(id) {
      return id.length > 12 ? id.slice(0, 12) + "..." : id;
    }

    function render(summary) {
      statusEl.textContent = "Connected";
      statusEl.className = "muted";

      uploadNote.textContent = summary.config.plugin_uploads_enabled
        ? "Plugin uploads execute trusted Python code from the zip."
        : "Uploads are disabled. Set BMO_ENABLE_PLUGIN_UPLOADS=1 to enable them.";

      function actionBtn(text, cls, fn) {
        const b = document.createElement("button");
        b.textContent = text;
        b.className = cls;
        b.style.cssText = "padding:4px 8px;min-height:28px;font-size:11px";
        b.addEventListener("click", fn);
        return b;
      }

      gamesEl.replaceChildren(table(
        ["Key", "Title", "Players", "Private", "Source", "Debug", ""],
        summary.games,
        (g) => {
          const cells = [
            g.key, g.title,
            `${g.min_players}-${g.max_players || "∞"}`,
            badge(g.private_player_links),
            g.source,
          ];
          const debugBtn = actionBtn("Debug", "secondary", async () => {
            debugBtn.disabled = true;
            debugBtn.textContent = "Running...";
            const res = await api("/api/admin/debug/play", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ game: g.key }),
            });
            const data = await res.json();
            debugNote.textContent = res.ok
              ? `${g.key}: ${data.steps} steps, ended=${!!(data.result && data.result.ended)}`
              : data.error || "Debug failed";
            debugNote.className = res.ok ? "success" : "error";
            debugBtn.disabled = false;
            debugBtn.textContent = "Debug";
          });
          cells.push(debugBtn);
          if (g.source === "plugin") {
            const toggleBtn = actionBtn(g.enabled ? "Disable" : "Enable", g.enabled ? "danger" : "secondary", async () => {
              const action = g.enabled ? "disable" : "enable";
              toggleBtn.disabled = true;
              const res = await api(`/api/admin/plugins/${g.key}/${action}`, { method: "POST" });
              const data = await res.json();
              if (res.ok && data.summary) render(data.summary);
              else toggleBtn.disabled = false;
            });
            cells.push(toggleBtn);
          } else {
            cells.push(badge(true, "builtin", ""));
          }
          return cells;
        }
      ));

      sessionsEl.replaceChildren(table(
        ["Session", "Game", "Players", "Updated"],
        summary.sessions,
        (s) => [
          shortId(s.session_id),
          s.game,
          String(s.players.length),
          String(s.updated_at || "").slice(0, 19).replace("T", " "),
        ]
      ));

      configEl.textContent = JSON.stringify(summary.config, null, 2);
      errorsEl.textContent = summary.plugin_errors.length
        ? summary.plugin_errors.join("\\n")
        : "No plugin errors.";
    }

    async function loadSummary() {
      try {
        const res = await api("/api/admin/summary");
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Could not load.");
        render(data);
      } catch (e) {
        statusEl.textContent = e.message;
        statusEl.className = "error";
      }
    }

    loginBtn.addEventListener("click", doLogin);
    loginPassword.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
    logoutBtn.addEventListener("click", doLogout);

    reloadBtn.addEventListener("click", async () => {
      reloadBtn.disabled = true;
      try {
        const res = await api("/api/admin/plugins/reload", { method: "POST" });
        const data = await res.json();
        if (res.ok && data.summary) render(data.summary);
      } finally { reloadBtn.disabled = false; }
    });

    uploadZone.addEventListener("click", () => pluginFile.click());
    uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("dragover"); });
    uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragover"));
    uploadZone.addEventListener("drop", (e) => {
      e.preventDefault();
      uploadZone.classList.remove("dragover");
      if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
    });
    pluginFile.addEventListener("change", () => {
      if (pluginFile.files.length) uploadFile(pluginFile.files[0]);
    });

    async function uploadFile(file) {
      progressWrap.classList.remove("hidden");
      progressFill.style.width = "0";
      uploadNote.textContent = `Uploading ${file.name}...`;
      uploadNote.className = "muted";
      const form = new FormData();
      form.append("plugin", file);
      try {
        const xhr = new XMLHttpRequest();
        xhr.upload.addEventListener("progress", (e) => {
          if (e.lengthComputable) progressFill.style.width = `${(e.loaded / e.total) * 100}%`;
        });
        const result = await new Promise((resolve, reject) => {
          xhr.open("POST", "/api/admin/plugins/upload");
          xhr.setRequestHeader("X-BMO-Admin-Token", adminToken);
          xhr.onload = () => resolve({ ok: xhr.status < 400, data: JSON.parse(xhr.responseText || "{}") });
          xhr.onerror = () => reject(new Error("Upload failed"));
          xhr.send(form);
        });
        progressFill.style.width = "100%";
        if (!result.ok) {
          uploadNote.textContent = result.data.error || "Upload failed.";
          uploadNote.className = "error";
          return;
        }
        uploadNote.textContent = `Plugin "${result.data.game.title}" uploaded.`;
        uploadNote.className = "success";
        if (result.data.summary) render(result.data.summary);
      } catch (e) {
        uploadNote.textContent = e.message;
        uploadNote.className = "error";
      }
    }

    if (adminToken) {
      showDashboard();
      loadSummary();
    } else {
      showLogin();
    }
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
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system,
        BlinkMacSystemFont, sans-serif;
      --exact: #2f9e6f;
      --present: #c9a227;
      --absent: #2a3f46;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: start center;
      background: #102025;
      color: #f7fbfb;
    }}
    main {{
      width: min(480px, calc(100vw - 32px));
      padding: 28px 0 40px;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 28px;
      letter-spacing: 0;
      text-align: center;
    }}
    #identity {{
      color: #9bc7c3;
      overflow-wrap: anywhere;
      text-align: center;
      font-size: 13px;
      margin: 0 0 4px;
    }}
    #players {{
      color: #6f8f8d;
      text-align: center;
      font-size: 13px;
      margin: 0 0 20px;
    }}
    #board {{
      display: grid;
      grid-template-rows: repeat(6, 1fr);
      gap: 6px;
      width: min(330px, 100%);
      margin: 0 auto 20px;
    }}
    .row {{
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 6px;
    }}
    .tile {{
      aspect-ratio: 1;
      display: grid;
      place-items: center;
      border: 2px solid #2a3f46;
      border-radius: 6px;
      font-size: 28px;
      font-weight: 800;
      text-transform: uppercase;
      color: #f7fbfb;
      transition: background .2s, border-color .2s;
    }}
    .tile.filled {{ border-color: #45656b; }}
    .tile.exact {{ background: var(--exact); border-color: var(--exact); }}
    .tile.present {{ background: var(--present); border-color: var(--present); }}
    .tile.absent {{ background: var(--absent); border-color: var(--absent); }}
    form {{
      display: flex;
      gap: 8px;
      margin: 0 0 14px;
    }}
    input, button {{
      min-height: 48px;
      border-radius: 8px;
      border: 1px solid #7aa7a5;
      font: inherit;
    }}
    input {{
      flex: 1;
      min-width: 0;
      padding: 0 14px;
      background: #f7fbfb;
      color: #102025;
      letter-spacing: 4px;
      text-transform: uppercase;
      font-weight: 700;
    }}
    button {{
      padding: 0 20px;
      background: #f4c95d;
      color: #102025;
      font-weight: 700;
      cursor: pointer;
      border: none;
      transition: opacity .15s;
    }}
    button:hover:not(:disabled) {{ opacity: .88; }}
    button:disabled, input:disabled {{
      opacity: .5;
      cursor: not-allowed;
    }}
    #banner {{
      min-height: 0;
      text-align: center;
      font-weight: 700;
      border-radius: 8px;
      padding: 0;
      transition: padding .15s;
    }}
    #banner.show {{ padding: 12px; margin-bottom: 12px; }}
    #banner.win {{ background: rgba(47, 158, 111, .2); color: #6fe0a8; }}
    #banner.lose {{ background: rgba(198, 40, 56, .18); color: #ff9ba6; }}
    #message {{
      color: #f4c95d;
      min-height: 22px;
      text-align: center;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>BMO Wordle</h1>
    <p id="identity"></p>
    <p id="players"></p>
    <div id="banner"></div>
    <div id="board" aria-label="Wordle board"></div>
    <form id="guess-form">
      <input id="guess" maxlength="5" autocomplete="off" autocapitalize="characters"
        spellcheck="false" placeholder="guess" aria-label="Your guess">
      <button>Guess</button>
    </form>
    <p id="message" role="status" aria-live="polite"></p>
  </main>
  <script>
    const sessionId = {session_json};
    const params = new URLSearchParams(window.location.search);
    const playerId = params.get("player_id") || "";
    const token = params.get("token") || "";
    const authQuery = new URLSearchParams({{ player_id: playerId, token }});
    const WORD_LENGTH = 5;
    const MAX_GUESSES = 6;
    const MARK_CLASS = {{ EXACT: "exact", PRESENT: "present", ABSENT: "absent" }};
    const board = document.querySelector("#board");
    const players = document.querySelector("#players");
    const identity = document.querySelector("#identity");
    const message = document.querySelector("#message");
    const banner = document.querySelector("#banner");
    const form = document.querySelector("#guess-form");
    const input = document.querySelector("#guess");
    const button = form.querySelector("button");

    function setDisabled(disabled) {{
      input.disabled = disabled;
      button.disabled = disabled;
    }}

    function renderBoard(rows, length) {{
      board.replaceChildren();
      const maxGuesses = MAX_GUESSES;
      for (let r = 0; r < maxGuesses; r++) {{
        const rowEl = document.createElement("div");
        rowEl.className = "row";
        const row = rows[r];
        for (let c = 0; c < length; c++) {{
          const tile = document.createElement("div");
          tile.className = "tile";
          if (row) {{
            const letter = row.guess[c] || "";
            tile.textContent = letter;
            if (letter) tile.classList.add("filled");
            const mark = row.marks[c];
            if (mark) tile.classList.add(MARK_CLASS[mark] || "");
          }}
          rowEl.append(tile);
        }}
        board.append(rowEl);
      }}
    }}

    function showBanner(text, kind) {{
      banner.textContent = text;
      banner.className = text ? `show ${{kind}}` : "";
    }}

    function render(data) {{
      const length = data.word_length || WORD_LENGTH;
      renderBoard(data.rows || [], length);
      input.maxLength = length;
      const count = data.players ? data.players.length : 0;
      players.textContent = `${{count}} player(s) from Matrix`;
      identity.textContent = data.player_id ? `Playing as ${{data.player_id}}` : "";
      if (data.ended) {{
        setDisabled(true);
        if (data.solved) {{
          showBanner(`Solved in ${{data.guess_count}}/${{data.max_guesses}}!`, "win");
        }} else {{
          showBanner(`The word was ${{data.answer || "?"}}.`, "lose");
        }}
      }} else {{
        setDisabled(false);
        showBanner("", "");
      }}
    }}

    function showLoadError(text) {{
      renderBoard([], WORD_LENGTH);
      message.textContent = text;
      setDisabled(true);
    }}

    async function refresh() {{
      if (!playerId || !token) {{
        showLoadError("Open your signed player link from Matrix.");
        return;
      }}
      try {{
        const res = await fetch(`/api/sessions/${{sessionId}}?${{authQuery}}`);
        const data = await res.json();
        if (!res.ok) {{
          showLoadError(data.error || "Could not load game.");
          return;
        }}
        render(data);
      }} catch (e) {{
        showLoadError("Connection error.");
      }}
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
      if (!guess) return;
      message.textContent = "";
      input.value = "";
      try {{
        const res = await fetch(`/api/sessions/${{sessionId}}/actions?${{authQuery}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "guess", payload: {{ guess }} }}),
        }});
        const data = await res.json();
        message.textContent = data.error || data.message || "";
        if (data.session) render(data.session);
      }} catch (e) {{
        message.textContent = "Connection error.";
      }}
    }});

    renderBoard([], WORD_LENGTH);
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
    web.run_app(create_app(), host="0.0.0.0", port=port)  # nosec B104
