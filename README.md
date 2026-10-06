# Enterprise Multi-Source AI Agent

Phase 0/1 implementation only. Canonical behavior remains in `docs/contracts/`.
This increment provides infrastructure and an internal mock Graph skeleton, not a business demo.

## Windows startup

Requires Docker Desktop (Linux containers), Python 3.12, uv, and Node >=22.12.
MySQL and Milvus run in Docker; no host database or LLM is required.

```powershell
./scripts/init-env.ps1
uv sync --frozen
docker compose config --quiet
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
$env:SMOKE_BASE_URL = 'http://127.0.0.1:5173'
uv run pytest
uv run pytest tests/unit
uv run pytest tests/integration
uv run pytest tests/e2e -m smoke
uv run ruff check .
uv run ruff format --check .
```

Open http://127.0.0.1:5173. The status page checks Backend, MySQL, and Milvus.
`GET /api/v1/health/live` checks the application process;
`GET /api/v1/health` returns 503 if a dependency is unavailable.
Only health APIs exist in this phase; no Graph, SQL, Tool, or Checkpoint HTTP access is exposed.

## Local development

```powershell
uv run uvicorn backend.api.main:app --host 127.0.0.1 --port 8001 --no-access-log
cd frontend
npm ci
$env:BACKEND_URL = 'http://127.0.0.1:8001'
npm run dev -- --port 5174
npm run build
```

Host processes do not resolve Compose DNS. Dependency health is expected to be degraded
outside Docker unless explicit reachable dependency settings are supplied.
Use the Compose runtime for full dependency acceptance.

## Configuration and operations

`.env.example` contains no credentials. `scripts/init-env.ps1` generates random private
local credentials once and preserves an existing `.env`. Never commit or print `.env`,
resolved Compose environment, or raw dependency diagnostics. Credentials initialize
MySQL/MinIO on first startup; changing `.env` does not rotate existing volume credentials.

Container connections use `mysql`, `milvus`, `etcd`, `minio`, and `backend` service names.
Loopback URLs inside health checks probe the same container, not another service.
Backend/frontend ports bind only to host loopback; storage services have no public ports.
Persistent named volumes hold MySQL, Milvus, etcd, MinIO, and SQLite Checkpoints.

```powershell
docker compose logs --tail 50 backend
docker compose stop
docker compose up -d
```

Keep volumes when stopping. Do not use `down -v` unless intentionally deleting local data.
Phase 2 and later provider/routing, RAG, SQL safety, HITL, auth and SSE work is deferred.
