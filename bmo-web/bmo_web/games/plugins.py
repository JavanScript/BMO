from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from .base import GameInfo, GamePlugin
from .sandbox import PluginSandbox, SandboxError, SandboxedFactory


MANIFEST_NAMES = {"manifest.yaml", "manifest.yml"}
MAX_PLUGIN_ZIP_BYTES = 5 * 1024 * 1024
MAX_PLUGIN_EXTRACTED_BYTES = 25 * 1024 * 1024
MAX_PLUGIN_FILES = 256
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PLUGIN_DB_FILENAME = "plugin_db.json"
_SANDBOXES: dict[str, PluginSandbox] = {}


class PluginValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PluginDiscovery:
    plugins: list[GamePlugin]
    errors: list[str]


def _plugin_db_path(plugins_dir: Path) -> Path:
    return plugins_dir.parent / PLUGIN_DB_FILENAME


def _load_plugin_db(plugins_dir: Path) -> dict[str, bool]:
    try:
        return json.loads(_plugin_db_path(plugins_dir).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_plugin_db(plugins_dir: Path, db: dict[str, bool]) -> None:
    path = _plugin_db_path(plugins_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db, indent=2) + "\n")


def _is_plugin_enabled(plugins_dir: Path, key: str) -> bool:
    return _load_plugin_db(plugins_dir).get(key, True)


def set_plugin_enabled(plugins_dir: Path, key: str, enabled: bool) -> None:
    db = _load_plugin_db(plugins_dir)
    db[key] = enabled
    _save_plugin_db(plugins_dir, db)


def discover_plugins(directory: Path) -> PluginDiscovery:
    if not directory.exists():
        return PluginDiscovery(plugins=[], errors=[])

    plugins: list[GamePlugin] = []
    errors: list[str] = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            plugins.append(load_plugin_directory(child))
        except PluginValidationError as exc:
            errors.append(f"{child.name}: {exc}")
        except Exception as exc:
            errors.append(f"{child.name}: unexpected error: {exc}")
    return PluginDiscovery(plugins=plugins, errors=errors)


def load_plugin_directory(root: Path) -> GamePlugin:
    root = root.resolve()
    manifest_path = _manifest_path(root)
    manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    info = _info_from_manifest(manifest)
    factory = _sandboxed_factory_from_manifest(root, manifest, info)
    frontend_path = _frontend_path(root, manifest)
    return GamePlugin(
        info=info,
        factory=factory,
        root=root,
        frontend_path=frontend_path,
    )


def install_plugin_zip(data: bytes, plugins_dir: Path) -> GamePlugin:
    if len(data) > MAX_PLUGIN_ZIP_BYTES:
        raise PluginValidationError("Plugin zip is too large.")

    plugins_dir.mkdir(parents=True, exist_ok=True)
    plugins_root = plugins_dir.resolve()
    temp_dir = Path(tempfile.mkdtemp(prefix=".upload-", dir=plugins_root))

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            members, manifest_info = _validated_zip_members(archive)
            manifest = parse_manifest(
                archive.read(manifest_info).decode("utf-8")
            )
            info = _info_from_manifest(manifest)

            for member, relative_path in members:
                target = (temp_dir / relative_path.as_posix()).resolve()
                if not target.is_relative_to(temp_dir.resolve()):
                    raise PluginValidationError("Plugin zip contains unsafe paths.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest)

        _validate_entrypoint(temp_dir, manifest, info)

        manifest_key = info.key
        target_dir = plugins_root / manifest_key
        if target_dir.exists():
            shutil.rmtree(target_dir)
        temp_dir.rename(target_dir)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise PluginValidationError("Upload is not a valid zip file.") from exc
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    plugin = load_plugin_directory(target_dir)
    if plugin.info.key != manifest_key:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise PluginValidationError("Manifest key changed during extraction.")
    set_plugin_enabled(plugins_root, plugin.info.key, True)
    return plugin


def parse_manifest(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            raise PluginValidationError(
                f"Unsupported nested manifest value on line {line_number}."
            )
        key, separator, value = raw_line.partition(":")
        if not separator:
            raise PluginValidationError(f"Invalid manifest line {line_number}.")
        data[key.strip()] = _parse_scalar(value.strip())
    return data


def _manifest_path(root: Path) -> Path:
    for name in sorted(MANIFEST_NAMES):
        path = root / name
        if path.is_file():
            return path
    raise PluginValidationError("Missing manifest.yaml.")


def _info_from_manifest(manifest: dict[str, Any]) -> GameInfo:
    key = _game_key(_required_string(manifest, "key"))
    min_players = _positive_int(manifest.get("min_players", 1), "min_players")
    max_players = _optional_positive_int(manifest.get("max_players"), "max_players")
    if max_players is not None and max_players < min_players:
        raise PluginValidationError("max_players cannot be lower than min_players.")

    return GameInfo(
        key=key,
        title=_required_string(manifest, "title"),
        description=_required_string(manifest, "description"),
        min_players=min_players,
        max_players=max_players,
        private_player_links=_bool_value(
            manifest.get("private_player_links", False),
            "private_player_links",
        ),
        source="plugin",
    )


def _sandboxed_factory_from_manifest(
    root: Path,
    manifest: dict[str, Any],
    info: GameInfo,
) -> SandboxedFactory:
    _validate_entrypoint(root, manifest, info)
    sandbox = _get_sandbox(info.key, root)
    try:
        sandbox.call("ping")
    except SandboxError as exc:
        raise PluginValidationError(f"Plugin sandbox validation failed: {exc}") from exc
    return SandboxedFactory(sandbox=sandbox, info=info)


def _get_sandbox(key: str, root: Path) -> PluginSandbox:
    if key in _SANDBOXES:
        old = _SANDBOXES[key]
        try:
            old.close()
        except Exception:  # nosec B110
            pass
    sandbox = PluginSandbox(root)
    try:
        sandbox.start()
    except SandboxError as exc:
        raise PluginValidationError(f"Failed to start plugin sandbox: {exc}") from exc
    _SANDBOXES[key] = sandbox
    return sandbox


def close_all_sandboxes() -> None:
    for key, sandbox in list(_SANDBOXES.items()):
        try:
            sandbox.close()
        except Exception:  # nosec B110
            pass
    _SANDBOXES.clear()


def _validate_entrypoint(
    root: Path,
    manifest: dict[str, Any],
    info: GameInfo,
) -> tuple[str, str]:
    entrypoint = str(manifest.get("entrypoint", "")).strip()
    if entrypoint:
        module_name, separator, symbol = entrypoint.partition(":")
        if not separator:
            raise PluginValidationError("entrypoint must look like module:symbol.")
    else:
        module_name = str(manifest.get("module", "plugin")).strip()
        symbol = str(manifest.get("factory", "factory")).strip()

    if not module_name or not symbol:
        raise PluginValidationError("Plugin factory entrypoint is incomplete.")

    module_path = (root / f"{module_name.replace('.', '/')}.py").resolve()
    if not module_path.is_relative_to(root) or not module_path.is_file():
        raise PluginValidationError(f"Missing plugin module: {module_name}.")

    return module_name, symbol


def _frontend_path(root: Path, manifest: dict[str, Any]) -> Path | None:
    value = str(manifest.get("frontend", "")).strip()
    if not value:
        return None

    relative = _safe_manifest_path(value)
    path = (root / relative.as_posix()).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise PluginValidationError("Frontend file does not exist.")
    return path


def _validated_zip_members(
    archive: zipfile.ZipFile,
) -> tuple[list[tuple[zipfile.ZipInfo, PurePosixPath]], zipfile.ZipInfo]:
    files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    total_size = 0

    for member in archive.infolist():
        path = _safe_zip_path(member.filename)
        if member.is_dir():
            continue
        _reject_zip_symlink(member)
        files.append((member, path))
        total_size += member.file_size
        if len(files) > MAX_PLUGIN_FILES:
            raise PluginValidationError("Plugin zip contains too many files.")
        if total_size > MAX_PLUGIN_EXTRACTED_BYTES:
            raise PluginValidationError("Plugin zip expands to too much data.")

    manifest_files = [
        (member, path)
        for member, path in files
        if path.name in MANIFEST_NAMES
    ]
    if not manifest_files:
        raise PluginValidationError("Plugin zip is missing manifest.yaml.")

    root_manifest = [
        (member, path)
        for member, path in manifest_files
        if len(path.parts) == 1
    ]
    if root_manifest:
        manifest_member, manifest_path = root_manifest[0]
    elif len(manifest_files) == 1:
        manifest_member, manifest_path = manifest_files[0]
    else:
        raise PluginValidationError("Plugin zip has multiple manifests.")

    root = manifest_path.parent
    normalized: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    for member, path in files:
        if not path.is_relative_to(root):
            raise PluginValidationError("Plugin zip has files outside plugin root.")
        relative_path = path.relative_to(root)
        if relative_path.name == "":
            continue
        normalized.append((member, relative_path))
    return normalized, manifest_member


def _safe_zip_path(filename: str) -> PurePosixPath:
    if "\\" in filename or "\x00" in filename:
        raise PluginValidationError("Plugin zip contains unsafe paths.")
    path = PurePosixPath(filename)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginValidationError("Plugin zip contains unsafe paths.")
    return path


def _safe_manifest_path(filename: str) -> PurePosixPath:
    path = PurePosixPath(filename)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginValidationError("Manifest contains an unsafe path.")
    return path


def _reject_zip_symlink(member: zipfile.ZipInfo) -> None:
    mode = member.external_attr >> 16
    if mode & 0o170000 == 0o120000:
        raise PluginValidationError("Plugin zip cannot contain symlinks.")


def _parse_scalar(value: str) -> Any:
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


def _required_string(manifest: dict[str, Any], key: str) -> str:
    value = str(manifest.get(key, "")).strip()
    if not value:
        raise PluginValidationError(f"Missing manifest field: {key}.")
    return value


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PluginValidationError(f"{field} must be a positive integer.") from exc
    if parsed < 1:
        raise PluginValidationError(f"{field} must be a positive integer.")
    return parsed


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value in {None, ""}:
        return None
    return _positive_int(value, field)


def _bool_value(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise PluginValidationError(f"{field} must be true or false.")


def _game_key(value: str) -> str:
    key = value.lower().strip()
    if not KEY_RE.fullmatch(key):
        raise PluginValidationError(
            "Game keys may use lowercase letters, numbers, dashes, and underscores."
        )
    return key


def _looks_like_factory(value: object) -> bool:
    return callable(getattr(value, "create", None)) and callable(
        getattr(value, "load", None)
    )
