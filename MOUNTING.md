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
# Prefix all dashboard routes with /reviewer
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

## Path Configuration

When mounted at a sub-path, set `APP_BASE_URL` so dashboard links in the review summary point to the correct URL:

```bash
# .env
APP_BASE_URL=https://myplatform.com/reviewer
```

The dashboard's static frontend makes API calls to `/dashboard/api/...` (relative paths). If you mount at `/reviewer`, the static files will call `/reviewer/dashboard/api/...` — this works automatically because the static files are served from within the mounted sub-app.

---

## Database Isolation

By default Dev Companion writes to `./agent_data/dashboard.db` relative to the working directory. When running alongside other apps, point it at a dedicated directory:

```bash
# .env
AGENT_DATA_DIR=/var/data/code-reviewer
```

Or set it programmatically before importing:

```python
import os
os.environ["AGENT_DATA_DIR"] = "/var/data/code-reviewer"

# Now import Dev Companion modules
from dashboard.api import dashboard_app
```

---

## Authentication

Dev Companion uses Chainlit's built-in password auth (`CHAINLIT_USER` / `CHAINLIT_PASSWORD`). This is independent of your host app's auth.

**Option A — Keep separate auth (simplest)**  
Users log in to Dev Companion with the Chainlit credentials. Your host app has its own auth. No integration needed.

**Option B — Bypass Chainlit auth (trusted internal tool)**  
Set `CHAINLIT_AUTH_SECRET` to a random value and remove the `@cl.password_auth_callback` decorator from `reviewer_ui.py`. Chainlit will skip the login screen. Protect the `/reviewer` path with your host app's middleware instead.

**Option C — Custom auth callback**  
Replace the `auth_callback` in `reviewer_ui.py` with one that validates against your own user store:

```python
@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    # Call your own auth service
    if await my_auth_service.verify(username, password):
        return cl.User(identifier=username, metadata={"role": "user"})
    return None
```

---

## Static Files

The dashboard's static files are in `code-reviewer/dashboard/static/`. They are mounted by `dashboard/api.py` at startup:

```python
dashboard_app.mount(
    "/dashboard/static",
    StaticFiles(directory=os.path.join(_HERE, "static"), html=True),
    name="static",
)
```

`_HERE` is resolved relative to `dashboard/api.py`, so the path is always correct regardless of the working directory. No action needed.

---

## Lifespan Merging

Dev Companion's lifespan closes the SQLite checkpointer connection on shutdown. Merge it with your host app's lifespan as shown in the Quick Start example above.

If your host app already has a lifespan, add the Dev Companion cleanup inside it:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Your startup logic
    await my_db.connect()

    yield

    # Your shutdown logic
    await my_db.disconnect()

    # Dev Companion cleanup
    try:
        import reviewer_ui
        if reviewer_ui._checkpointer_conn is not None:
            await reviewer_ui._checkpointer_conn.close()
    except Exception:
        pass
```

---

## Known Limitations

**Single process required**  
Chainlit uses WebSockets and in-process state. Both the host app and Dev Companion must run in the same uvicorn process. You cannot run them as separate services and proxy between them.

**Single replica**  
Dev Companion uses SQLite for persistence. SQLite does not support concurrent writes from multiple processes. Keep the deployment to a single container replica. If you need horizontal scaling, migrate `dashboard/db.py` to use PostgreSQL.

**WebSocket path**  
Chainlit's WebSocket connects to `/reviewer/ws` when mounted at `/reviewer`. Ensure your reverse proxy (nginx, Azure Application Gateway, etc.) passes WebSocket upgrade headers for this path.

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

**Working directory**  
Run uvicorn from the `code-reviewer` directory (or set `PYTHONPATH` appropriately). Chainlit resolves `reviewer_ui.py` relative to the working directory when using a relative path in `mount_chainlit`.

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
        storageName: my-azure-files-storage   # Azure Files share name
```

The Azure Files share at `/app/agent_data` persists `dashboard.db`, `checkpoints_lg.db`, and `chainlit_ui.db` across container restarts and redeployments.
