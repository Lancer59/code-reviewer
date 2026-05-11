"""
Production entry point for Dev Companion.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8001

Development (Chainlit only):
    chainlit run reviewer_ui.py
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from chainlit.utils import mount_chainlit
from dashboard.api import dashboard_app


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
    import os as _os
    _os._exit(0)


app = FastAPI(title="Dev Companion", lifespan=lifespan)


@app.get("/health", tags=["ops"])
async def health():
    """Liveness probe for container orchestration (Docker, Kubernetes, Azure Container Apps)."""
    return {"status": "ok", "version": "3.0"}


# Mount dashboard routes
for route in dashboard_app.routes:
    app.routes.append(route)

# Mount Chainlit UI at root
mount_chainlit(app=app, target="reviewer_ui.py", path="/")
