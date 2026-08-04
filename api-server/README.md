# ThreadForge API Server

FastAPI backend for the ThreadForge Web console: REST + SSE over the Pico
Legacy Runtime. Implements the contracts in
[`../docs/fastapi-backend-rewrite-execution.md`](../docs/fastapi-backend-rewrite-execution.md).

## Install

```powershell
python -m pip install -e .\pico-legacy-runtime
python -m pip install -e .\api-server
```

## Run (local, loopback only)

```powershell
$env:THREADFORGE_DATA_DIR = "D:\path\to\threadforge-data"
$env:THREADFORGE_WORKSPACES_FILE = "D:\path\to\workspaces.json"
$env:PICO_OPENAI_API_KEY = "..."
python -m threadforge_api
```

`python -m threadforge_api --reload` enables hot reload for development. The
launcher holds an exclusive lock on the data directory for the whole process;
running a second API process fails fast. Bypassing the launcher
(`uvicorn threadforge_api.main:create_app`) is unsupported.

## Run with Docker Compose

From the repository root:

```powershell
$env:PICO_OPENAI_API_KEY = "..."
docker compose up --build
```

The default container allowlist exposes the repository as workspace
`threadforge`; edit `api-server/workspaces.docker.json` and add matching
read-only or read-write volume mounts when additional workspaces are needed.

## Client metadata

The client reads the effective model and execution boundary from
`GET /api/v1/config`. `GET /api/v1/skills` and
`GET /api/v1/mcp/servers` are read-only compatibility endpoints in V1: they
always mark Skills as planned and MCP servers as not configured.

Packaged Electron pages have the opaque `Origin: null`. Direct desktop access
is therefore disabled by default. Enable it only for a trusted packaged client:

```powershell
$env:THREADFORGE_DESKTOP_ORIGIN_ENABLED = "true"
docker compose up -d --build
```

The desktop client must still connect through a loopback address, such as an
SSH tunnel; the API is not exposed publicly.

## Ownership foundation (V1.5)

The server creates one stable owner UUID in its data directory; deployments may
also pin it with `THREADFORGE_INSTANCE_OWNER_ID`. Existing V1 records without
ownership are claimed by that UUID at startup. The REST,
SSE, approval, cancellation, and artifact paths all enforce the configured
owner, and client-supplied identity headers are ignored. This remains a
single-owner compatibility boundary, not public multi-user authentication; see
[`../docs/multi-user-v15-roadmap.md`](../docs/multi-user-v15-roadmap.md).

GitHub OAuth can replace the single-owner actor while preserving the existing
object ownership checks. It uses PKCE, one-use state, an HttpOnly opaque session
cookie, a server-side login allowlist, and never persists the GitHub access
token. Setup is documented in
[`../docs/github-oauth-setup.md`](../docs/github-oauth-setup.md).

On Windows, `scripts\\dev.cmd` starts the configured `threadforge-server` SSH
tunnel and the Vite client, waits for both endpoints, and opens the browser.

## Tests (offline)

All tests use the deterministic `FakeModelClient` — no provider credentials or
network required.

```powershell
Push-Location .\pico-legacy-runtime
python -m pytest -q
Pop-Location
Push-Location .\api-server
python -m pytest -q
Pop-Location
```

## Notes

- Tools run in the backend process with `policy_boundary` enforcement; there is
  **no** independent container sandbox in V1. UI/API snapshots always report
  `execution_environment: backend_process` and `container_sandbox_enabled: false`.
- Loopback-only and not a public deployment. Single-owner mode is the default;
  optional GitHub OAuth supports a small allowlisted group while retaining the
  single-process, globally single-active-task execution limit.
- The Windows shell runner uses a Job Object with `KILL_ON_JOB_CLOSE` so a
  cancelled run terminates the whole process tree.
