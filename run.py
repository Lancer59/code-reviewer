"""
Dev Companion — single-import entry point.

Usage in your existing FastAPI application
------------------------------------------

    from run import create_app

    # Mount at a sub-path (recommended)
    reviewer = create_app(config_file="/path/to/reviewer-config.json")
    your_app.mount("/reviewer", reviewer)

    # Mount at root (standalone)
    reviewer = create_app()
    your_app.mount("/", reviewer)

Standalone (no host app)
------------------------

    uvicorn run:standalone --host 0.0.0.0 --port 8001

How it works
------------
create_app() returns a FastAPI app with routes at:
    /health
    /dashboard
    /dashboard/api/...
    /  (Chainlit UI)

When you do `your_app.mount("/reviewer", reviewer)`, FastAPI automatically
prepends /reviewer to all paths, giving you:
    /reviewer/health
    /reviewer/dashboard
    /reviewer/dashboard/api/...
    /reviewer/  (Chainlit UI)

The dashboard frontend detects the mount prefix at runtime via the
window.API_BASE variable injected into the HTML by the server.

Environment / config
--------------------
All configuration is read from (in priority order):
  1. config.json  — path set via `config_file` arg or CONFIG_FILE env var
  2. Environment variables
  3. Built-in defaults

See config.json.example for all supported keys.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

# Ensure this directory is importable regardless of where the host app runs
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def create_app(
    config_file: Optional[str] = None,
    title: str = "Dev Companion",
) -> FastAPI:
    """
    Build and return a self-contained Dev Companion FastAPI application.

    Parameters
    ----------
    config_file : str | None
        Absolute path to a config.json file.
        Sets CONFIG_FILE env var before loading any modules so all Dev Companion
        config is isolated from the host app's environment.

    title : str
        FastAPI app title (shows in /docs).

    Returns
    -------
    FastAPI
        A fully configured FastAPI app with routes at:
            /health, /dashboard, /dashboard/api/..., / (Chainlit UI)

        Mount it in your host app:
            reviewer = create_app()
            your_app.mount("/reviewer", reviewer)

        FastAPI's mount() automatically prepends /reviewer to all paths.

    Example
    -------
        from fastapi import FastAPI
        from run import create_app

        app = FastAPI()

        @app.get("/api/status")
        def status():
            return {"ok": True}

        reviewer = create_app(config_file="/etc/reviewer/config.json")
        app.mount("/reviewer", reviewer)
    """
    if config_file:
        os.environ["CONFIG_FILE"] = str(config_file)

    # Import after CONFIG_FILE is set
    from chainlit.utils import mount_chainlit
    from dashboard.api import dashboard_app

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        try:
            import reviewer_ui
            if reviewer_ui._checkpointer_conn is not None:
                await reviewer_ui._checkpointer_conn.close()
        except Exception:
            pass

    app = FastAPI(title=title, lifespan=lifespan)

    # Health check
    @app.get("/health", tags=["ops"], include_in_schema=False)
    async def health():
        return {"status": "ok", "service": "dev-companion", "version": "3.0"}

    # Include dashboard routes as-is (paths are already /dashboard/...).
    # Do NOT prefix them — FastAPI's mount() in the host app handles the prefix.
    for route in dashboard_app.routes:
        app.routes.append(route)

    # Chainlit UI at root
    _ui_target = str(_HERE / "reviewer_ui.py")
    mount_chainlit(app=app, target=_ui_target, path="/")

    return app


# ---------------------------------------------------------------------------
# Standalone entry point — `uvicorn run:standalone --host 0.0.0.0 --port 8001`
# ---------------------------------------------------------------------------
standalone = create_app(title="Dev Companion")
