"""
Minimal host app to test mounting Dev Companion at /reviewer.
Run: uvicorn _host_test_app:app --port 8080
Then check: http://localhost:8080/reviewer/dashboard
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from run import create_app

app = FastAPI(title="Host App")

@app.get("/")
def root():
    return {"service": "host-app", "reviewer_at": "/reviewer"}

@app.get("/api/status")
def status():
    return {"ok": True}

reviewer = create_app()
app.mount("/reviewer", reviewer)
