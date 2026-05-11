# Mounting Dev Companion inside another FastAPI application

Dev Companion exposes two sub-applications:

- **Chainlit UI** — the chat interface (WebSocket + HTTP)
- **Dashboard API** — REST endpoints at `/dashboard/api/...` + static files at `/dashboard/static/`

Both can be mounted inside a host FastAPI app using Starlette's sub-application mounting.

---

## Prerequisites

- Python 3.11+
- Both apps run in the **same process** (Chainlit's WebSocket handling requires this)
- FastAPI ≥ 0.100, Chainlit ≥ 1.0, Starlette ≥ 0.27
- The `code-reviewer` directory must be importable (add it to `PYTHONPATH` or install as a package)

---

## Quick Start — Mount at `/reviewer`

```python
# host_app.py
import sys
import os

# Make code-reviewer importable
sys.path.insert(0, "/path/to/code-reviewer")

# Point config at a dedicated config file BEFORE importing any Dev Companion module
os.environ["CONFIG_FILE"] = "/path/to/reviewer-config.json"

from contextlib import asynccontextmanager
from fastapi import FastAPI
from chainlit.utils import mount_chainlit
from dashboard.api import dashboard_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Dev Companion cleanup on shutdown
    try:
        import reviewer_ui
        if reviewer_ui._checkpointer_conn is not None:
            await reviewer_ui._checkpointer_conn.close()
    except Exception:
        pass


host_app = FastAPI(title="My Platform", lifespan=lifespan)


# ── Your existing routes ──────────────────────────────────────────────────────
@host_app.get("/api/v1/status")
async def status():
    return {"ok": True}


# ── Mount Dev Companion dashboard at /reviewer/dashboard ─────────────────────
for route in dashboard_app.routes:
    route.path = "/reviewer" + route.path
host_app.routes.extend(dashboard_app.routes)


# ── Mount Chainlit UI at /reviewer ────────────────────────────────────────────
mount_chainlit(
    app=host_app,
    target="/path/to/code-reviewer/reviewer_ui.py",
    path="/reviewer",
)
```

Run with:
```bash
uvicorn host_app:host_app --host 0.0.0.0 --port 8000
```

The app is now available at:
- Chat UI: `http://localhost:8000/reviewer`
- Dashboard: `http://localhost:8000/reviewer/dashboard`
- Dashboard API: `http://localhost:8000/reviewer/dashboard/api/findings`

---

## Configuration via config.json

When mounting inside a host app, use `config.json` to isolate Dev Companion's configuration from the host app's environment variables:

```python
# Set this BEFORE importing any Dev Companion module
import os
os.environ["CONFIG_FILE"] = "/path/to/reviewer-config.json"
```

Then create `/path/to/reviewer-config.json` (copy from `config.json.example`):

```json
{
  "AZURE_OPENAI_API_KEY": "...",
  "AZURE_OPENAI_ENDPOINT": "https://your-resource.openai.azure.com/",
  "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o",
  "AGENT_DATA_DIR": "/var/data/reviewer/agent_data",
  "WORKSPACE_BASE_DIR": "/var/data/reviewer/workspaces",
  "APP_BASE_URL": "https://myplatform.com/reviewer",
  "CHAINLIT_USER": "admin",
  "CHAINLIT_PASSWORD": "your-secure-password"
}
```

Config lookup order: `config.json` → environment variables → built-in defaults.

---

## Path Configuration

When mounted at a sub-path, set `APP_BASE_URL` so dashboard links in the review summary point to the correct URL:

```json
{
  "APP_BASE_URL": "https://myplatform.com/reviewer"
}
```

---

## Database Isolation

By default Dev Companion writes SQLite databases to `./agent_data/`. When running alongside other apps, point it at a dedicated directory via `config.json`:

```json
{
  "AGENT_DATA_DIR": "/var/data/reviewer/agent_data"
}
```

---

## Authentication

Dev Companion uses Chainlit's built-in password auth (`CHAINLIT_USER` / `CHAINLIT_PASSWORD`). This is independent of your host app's auth.

**Option A — Keep separate auth (simplest)**
Users log in to Dev Companion with the Chainlit credentials. No integration needed.

**Option B — Bypass Chainlit auth (trusted internal tool)**
Remove the `@cl.password_auth_callback` decorator from `reviewer_ui.py`. Chainlit will skip the login screen. Protect the `/reviewer` path with your host app's middleware instead.

**Option C — Custom auth callback**
Replace the `auth_callback` in `reviewer_ui.py` with one that validates against your own user store:

```python
@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    if await my_auth_service.verify(username, password):
        return cl.User(identifier=username, metadata={"role": "user"})
    return None
```

---

## Lifespan Merging

Dev Companion's lifespan closes the SQLite checkpointer connection on shutdown. Merge it with your host app's lifespan as shown in the Quick Start example above.

---

## Known Limitations

**Single process required**
Chainlit uses WebSockets and in-process state. Both the host app and Dev Companion must run in the same uvicorn process.

**Single replica**
Dev Companion uses SQLite. SQLite does not support concurrent writes from multiple processes. Keep the deployment to a single container replica.

**WebSocket path**
Chainlit's WebSocket connects to `/reviewer/ws` when mounted at `/reviewer`. Ensure your reverse proxy passes WebSocket upgrade headers for this path.

Nginx example:
```nginx
location /reviewer/ {
    proxy_pass http://localhost:8000/reviewer/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

---

## Azure Container Apps — Recommended Setup

```yaml
# container-app.yaml (simplified)
properties:
  template:
    containers:
      - name: code-reviewer
        image: myregistry.azurecr.io/code-reviewer:latest
        env:
          - name: AGENT_DATA_DIR
            value: /app/agent_data
          - name: WORKSPACE_BASE_DIR
            value: /app/workspaces
          - name: APP_BASE_URL
            value: https://code-reviewer.myenv.azurecontainerapps.io
          - name: CLEANUP_WORKSPACE_ON_EXIT
            value: "true"
        volumeMounts:
          - volumeName: agent-data
            mountPath: /app/agent_data
    volumes:
      - name: agent-data
        storageType: AzureFile
        storageName: my-azure-files-storage
```

The Azure Files share at `/app/agent_data` persists `dashboard.db`, `checkpoints_lg.db`, and `chainlit_ui.db` across container restarts and redeployments. The `workspaces/` directory stays ephemeral in-container — cloned repos are deleted on session end.
