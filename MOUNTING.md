# Mounting Dev Companion in another FastAPI application

Dev Companion ships a single `create_app()` factory in `run.py`. Call it, get a FastAPI app back, mount it. That's it.

---

## Quick Start

```python
# your_app.py
from fastapi import FastAPI
from run import create_app          # adjust import path as needed

app = FastAPI(title="My Platform")

# Your existing routes
@app.get("/api/status")
def status():
    return {"ok": True}

# Mount Dev Companion at /reviewer
reviewer = create_app(
    config_file="/etc/reviewer/config.json",   # optional — see Config section
)
app.mount("/reviewer", reviewer)
```

Run:
```bash
uvicorn your_app:app --host 0.0.0.0 --port 8000
```

That's all. Dev Companion is now live at:

| URL | What |
|-----|------|
| `http://localhost:8000/reviewer/` | Chat UI |
| `http://localhost:8000/reviewer/dashboard` | Dashboard |
| `http://localhost:8000/reviewer/dashboard/api/findings` | REST API |
| `http://localhost:8000/reviewer/health` | Health check |

---

## How it works

`create_app()` returns a FastAPI app with routes at:
- `/health`
- `/dashboard`
- `/dashboard/api/...`
- `/` (Chainlit UI)

When you do `your_app.mount("/reviewer", reviewer)`, FastAPI automatically prepends `/reviewer` to all paths. No manual path manipulation needed.

The dashboard frontend detects the mount prefix at runtime — the server injects `window.API_BASE` into the HTML so all API calls go to the correct prefixed path automatically.

---

## `create_app()` signature

```python
def create_app(
    config_file: str | None = None,
    title: str = "Dev Companion",
) -> FastAPI:
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `config_file` | `None` | Absolute path to a `config.json`. Sets `CONFIG_FILE` env var before any module is imported, so all Dev Companion config is isolated from the host app. |
| `title` | `"Dev Companion"` | FastAPI app title (shows in `/docs`). |

---

## Config isolation via config.json

The cleanest way to configure Dev Companion when mounting is a `config.json` file:

```python
reviewer = create_app(config_file="/etc/reviewer/config.json")
```

Minimal `config.json` (copy from `config.json.example` for the full list):

```json
{
  "AZURE_OPENAI_API_KEY": "...",
  "AZURE_OPENAI_ENDPOINT": "https://your-resource.openai.azure.com/",
  "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o",
  "AGENT_DATA_DIR": "/var/data/reviewer/agent_data",
  "WORKSPACE_BASE_DIR": "/tmp/reviewer/workspaces",
  "APP_BASE_URL": "https://myplatform.com/reviewer",
  "CHAINLIT_USER": "admin",
  "CHAINLIT_PASSWORD": "your-secure-password",
  "CLEANUP_WORKSPACE_ON_EXIT": true
}
```

Config lookup order: `config.json` → environment variables → built-in defaults.

---

## Standalone (no host app)

`run.py` also exposes a `standalone` app object for running Dev Companion on its own:

```bash
uvicorn run:standalone --host 0.0.0.0 --port 8001
```

Or in Python:
```python
import uvicorn
from run import standalone
uvicorn.run(standalone, host="0.0.0.0", port=8001)
```

---

## Lifespan merging

`create_app()` manages its own lifespan (closes the SQLite checkpointer on shutdown). If your host app also has a lifespan, merge them:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from run import create_app

reviewer_app = create_app()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await my_db.connect()
    yield
    await my_db.disconnect()
    # Dev Companion cleanup
    try:
        import reviewer_ui
        if reviewer_ui._checkpointer_conn is not None:
            await reviewer_ui._checkpointer_conn.close()
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)
app.mount("/reviewer", reviewer_app)
```

---

## sys.path

`run.py` automatically adds its own directory to `sys.path` so all Dev Companion imports resolve correctly regardless of where your host app runs from. No manual `sys.path` manipulation needed.

---

## Reverse proxy (nginx / Azure Application Gateway)

When mounted at `/reviewer`, WebSocket connections go to `/reviewer/ws`. Make sure your proxy passes WebSocket upgrade headers:

```nginx
location /reviewer/ {
    proxy_pass http://localhost:8000/reviewer/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

---

## Azure App Service

Set these in **Configuration → Application settings**:

| Key | Value |
|-----|-------|
| `AGENT_DATA_DIR` | `/home/agent_data` (persistent storage on App Service) |
| `WORKSPACE_BASE_DIR` | `/tmp/workspaces` (ephemeral) |
| `APP_BASE_URL` | `https://your-app.azurewebsites.net/reviewer` |
| `CLEANUP_WORKSPACE_ON_EXIT` | `true` |
| `AZURE_OPENAI_API_KEY` | your key |
| `AZURE_OPENAI_ENDPOINT` | your endpoint |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | your deployment |
| `CHAINLIT_USER` | your username |
| `CHAINLIT_PASSWORD` | your password |

Or use a `config.json` stored in `/home/` (persistent) and pass its path to `create_app()`.

---

## Known limitations

- **Single process** — Chainlit requires both the UI and API to run in the same process.
- **Single replica** — SQLite doesn't support concurrent writes from multiple instances. Use one replica, or migrate `dashboard/db.py` to PostgreSQL for horizontal scaling.
- **Lifespan** — The `standalone` object's lifespan calls `os._exit(0)` on shutdown to force-kill uvicorn. When using `create_app()` inside a host app, the lifespan only closes the DB connection — clean shutdown is left to the host.
