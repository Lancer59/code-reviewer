"""
Dev Companion — single-import entry point.

Usage in your existing FastAPI application
------------------------------------------

    from code_reviewer.run import create_app   # if installed as a package
    # OR if on sys.path:
    from run import create_app

    # Mount at a sub-path (recommended)
    reviewer = create_app(mount_path="/reviewer", config_file="/path/to/reviewer-config.json")
    your_app.mount("/reviewer", reviewer)

    # Mount at root (standalone)
    reviewer = create_app()
    your_app.mount("/", reviewer)

Standalone (no host app)
------------------------

    uvicorn run:standalone --host 0.0.0.0 --port 8001

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

# ---------------------------------------------------------------------------
# Ensure this directory is importable regardless of where the host app runs
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def create_app(
    mount_path: str = "",
    config_file: Optional[str] = None,
    title: str = "Dev Companion",
) -> FastAPI:
    """
    Build and return a self-contained Dev Companion FastAPI application.

    Parameters
    ----------
    mount_path : str
        The URL prefix this app will be mounted at in the host application.
        Examples: "/reviewer", "/code-review", ""  (empty = root mount)
        Used to prefix dashboard API routes so they resolve correctly.
        Default: "" (root mount — use when running standalone).

    config_file : str | None
        Absolute path to a config.json file.
        If provided, sets CONFIG_FILE env var before loading any modules.
        If None, falls back to CONFIG_FILE env var, then ./config.json, then env vars.

    title : str
        FastAPI app title (shows in /docs).

    Returns
    -------
    FastAPI
        A fully configured FastAPI app with:
        - Chainlit UI mounted at `mount_path + "/"`
        - Dashboard API routes at `mount_path + "/dashboard/..."`
        - Health check at `mount_path + "/health"`

    Example
    -------
        # host_app.py
        from fastapi import FastAPI
        from run import create_app

        app = FastAPI()

        @app.get("/")
        def root():
            return {"service": "my-platform"}

        reviewer = create_app(mount_path="/reviewer", config_file="/etc/reviewer/config.json")
        app.mount("/reviewer", reviewer)
    """
    # Set config file path before importing any Dev Companion module
    if config_file:
        os.environ["CONFIG_FILE"] = str(config_file)

    # Import here (after CONFIG_FILE is set) so config is loaded correctly
    from chainlit.utils import mount_chainlit
    from dashboard.api import dashboard_app

    # Normalise mount_path — strip trailing slash, keep leading slash
    mount_path = ("/" + mount_path.strip("/")).rstrip("/") if mount_path.strip("/") else ""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Graceful shutdown: close SQLite checkpointer connection
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

    # Dashboard routes — prefix each route with mount_path so they resolve
    # correctly when the app is mounted at a sub-path in the host.
    for route in dashboard_app.routes:
        # Avoid double-prefixing if called multiple times
        if mount_path and not route.path.startswith(mount_path):
            route.path = mount_path + route.path
        app.routes.append(route)

    # Chainlit UI — always mounted at "/" within this sub-app.
    # The host app's mount() call handles the prefix.
    _ui_target = str(_HERE / "reviewer_ui.py")
    mount_chainlit(app=app, target=_ui_target, path="/")

    return app


# ---------------------------------------------------------------------------
# Standalone entry point — `uvicorn run:standalone --host 0.0.0.0 --port 8001`
# ---------------------------------------------------------------------------
standalone = create_app(mount_path="", title="Dev Companion")
