# Plugin Security Analysis

BMO game plugins are trusted server-side code. A plugin zip can include
`plugin.py`, and loading that module executes Python inside the `bmo-web`
process with the same filesystem, network, environment, and database access as
the web server. The current plugin system is intended for admins installing code
they trust, not for accepting untrusted community uploads.

## Implemented Controls

- Plugin uploads are disabled by default. Set `BMO_ENABLE_PLUGIN_UPLOADS=1` only
  when trusted admins need the web upload flow.
- Admin APIs require either the shared web secret via `X-BMO-Secret` or the
  configured admin password via `X-BMO-Admin`.
- Uploaded zips are rejected when they exceed size and file-count limits.
- Zip entries are rejected for absolute paths, `..` traversal, backslashes,
  NUL bytes, symlinks, files outside the plugin root, and multiple manifests.
- Manifests are flat YAML only. Required metadata is validated before install.
- Game keys are restricted to lowercase letters, numbers, dashes, and
  underscores. Plugin keys cannot silently replace built-in game keys.
- Plugin frontend file paths are confined to the plugin directory.
- `/api/games` exposes metadata only; it does not expose sessions, player links,
  shared secrets, or signed player tokens.
- The admin summary shows recent session metadata but not persisted game state or
  player tokens.
- Matrix launch behavior can mark any game as requiring private player links,
  keeping signed links out of public rooms for private-hand games.

## Remaining Risks

- Plugin Python is not sandboxed. Malicious or buggy plugins can read secrets,
  mutate the database, make outbound requests, or affect other sessions.
- Reloading plugins imports new Python modules but does not unload old module
  objects already referenced by active sessions.
- The admin panel depends on transport security. Use HTTPS in production; do not
  send admin passwords or shared secrets over plain HTTP outside local testing.
- Admin upload/reload is not a package manager. There is no signature check,
  dependency resolver, or provenance verification.
- Plugin frontend JavaScript runs in the browser with access to the signed
  player URL that opened it. Treat frontend code as trusted too.
- Size limits reduce zip-bomb risk, but they do not prove a plugin is cheap to
  import or safe to run.

## Operating Guidance

- Keep upload disabled except during controlled installs.
- Prefer installing reviewed plugin zips into `GAME_PLUGINS_DIR`, then use the
  admin reload action or restart `bmo-web`.
- Put BMO behind HTTPS before exposing `/admin/`.
- Rotate `BMO_SHARED_SECRET` and `ADMIN_PASSWORD` if a plugin or admin password
  may have been compromised.
- Run untrusted game ideas as separate services until process/container
  isolation exists for plugins.
