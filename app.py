"""
Production entry point for Dev Companion.

Run with:
    uvicorn app:app --host 127.0.0.1 --port 8001

Development (Chainlit only):
    chainlit run reviewer_ui.py
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
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
    import os
    os._exit(0)


app = FastAPI(title="Dev Companion", lifespan=lifespan)

for route in dashboard_app.routes:
    app.routes.append(route)

mount_chainlit(app=app, target="reviewer_ui.py", path="/")
