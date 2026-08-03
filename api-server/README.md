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
- Single user, loopback only. No login, no public deployment.
- The Windows shell runner uses a Job Object with `KILL_ON_JOB_CLOSE` so a
  cancelled run terminates the whole process tree.
