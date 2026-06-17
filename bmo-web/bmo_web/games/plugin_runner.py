from __future__ import annotations

import importlib.util
import json
import sys
import traceback
from pathlib import Path
from uuid import uuid4


_script_dir = Path(__file__).resolve().parent
_bmo_web_pkg = _script_dir.parent
_bmo_web_root = _bmo_web_pkg.parent
for _p in (_bmo_web_root, _bmo_web_pkg):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))



MANIFEST_NAMES = {"manifest.yaml", "manifest.yml"}


def _load_plugin_module(plugin_root: Path):
    root = plugin_root.resolve()
    manifest_path = _manifest_path(root)
    manifest = _parse_manifest(manifest_path.read_text(encoding="utf-8"))
    entrypoint = str(manifest.get("entrypoint", "")).strip()
    if entrypoint:
        module_name, separator, symbol = entrypoint.partition(":")
        if not separator:
            raise ValueError("entrypoint must look like module:symbol.")
    else:
        module_name = str(manifest.get("module", "plugin")).strip()
        symbol = str(manifest.get("factory", "factory")).strip()
    module_path = (root / f"{module_name.replace('.', '/')}.py").resolve()
    if not module_path.is_file():
        raise ValueError(f"Missing plugin module: {module_name}.")

    loaded_name = f"_bmo_sandbox_plugin_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(loaded_name, module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load plugin module: {module_name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loaded_name] = module
    sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(f"Plugin module failed to import: {exc}") from exc
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass
    try:
        candidate = getattr(module, symbol)
    except AttributeError as exc:
        raise ValueError(f"Missing plugin factory: {symbol}.") from exc
    if isinstance(candidate, type):
        factory = candidate()
    elif callable(candidate):
        factory = candidate() if not _looks_like_factory(candidate) else candidate
    else:
        factory = candidate
    return factory


def _manifest_path(root: Path) -> Path:
    for name in sorted(MANIFEST_NAMES):
        path = root / name
        if path.is_file():
            return path
    raise ValueError("Missing manifest.yaml.")


def _parse_manifest(text: str) -> dict:
    data = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            raise ValueError(f"Unsupported nested manifest value on line {line_number}.")
        key, separator, value = raw_line.partition(":")
        if not separator:
            raise ValueError(f"Invalid manifest line {line_number}.")
        data[key.strip()] = _parse_scalar(value.strip())
    return data


def _parse_scalar(value: str):
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _looks_like_factory(value: object) -> bool:
    return callable(getattr(value, "create", None)) and callable(
        getattr(value, "load", None)
    )


def main() -> None:
    plugin_root = Path(sys.argv[1])
    factory = _load_plugin_module(plugin_root)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        req_id = request["id"]
        method = request["method"]
        params = request.get("params", {})

        try:
            result = _dispatch(factory, method, params)
            response = {"id": req_id, "result": result}
        except Exception as exc:
            tb = traceback.format_exc()
            response = {"id": req_id, "error": f"{type(exc).__name__}: {exc}\n{tb}"}

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def _dispatch(factory, method: str, params: dict) -> dict:
    if method == "ping":
        return {"ok": True}

    if method == "create":
        game = factory.create(params.get("players"))
        return {"state": game.to_state()}

    if method == "load":
        state = params["state"]
        game = factory.load(state)
        return {"state": game.to_state()}

    state = params["state"]
    game = factory.load(state)

    if method == "handle_action":
        reply = game.handle_action(
            player_id=str(params["player_id"]),
            action=str(params["action"]),
            payload=params.get("payload", {}),
        )
        return {
            "state": game.to_state(),
            "reply": {"message": reply.message, "ended": reply.ended},
        }

    if method == "serialize_public":
        public = game.serialize_public(player_id=params.get("player_id"))
        return {"public": public}

    if method == "to_state":
        return {"state": game.to_state()}

    raise ValueError(f"Unknown method: {method}")


if __name__ == "__main__":
    main()
