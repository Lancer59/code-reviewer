"""
Dashboard DB for Dev Companion.
Tracks LLM calls, tool invocations, and review findings with criticality.
"""

import datetime
import json
import logging
import os
import aiosqlite

from config import cfg

_AGENT_DATA_DIR = cfg(
    "AGENT_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent_data"),
)
DB_PATH = os.path.join(_AGENT_DATA_DIR, "dashboard.db")
logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """You are an expert code reviewer. Review the target repository and record all findings using record_finding."""
DEFAULT_ITERATION_LIMIT = 80
DEFAULT_LLM_PROVIDER = "azure"
DEFAULT_MODEL_NAME = cfg("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT, timestamp TEXT, model TEXT,
                prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
                agent_name TEXT
            );
            CREATE TABLE IF NOT EXISTS tool_invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT, tool_name TEXT, timestamp TEXT, duration_ms REAL, status TEXT
            );
            CREATE TABLE IF NOT EXISTS review_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT, timestamp TEXT,
                file_path TEXT, line_number INTEGER,
                criticality TEXT, category TEXT,
                title TEXT, description TEXT, suggestion TEXT,
                finding_id INTEGER,
                estimated_fix_tokens INTEGER,
                status TEXT DEFAULT 'open',
                agent_name TEXT,
                workspace TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_config (
                id INTEGER PRIMARY KEY,
                system_prompt TEXT, iteration_limit INTEGER,
                enabled_tools TEXT, llm_provider TEXT, model_name TEXT,
                all_known_tools TEXT
            );
            CREATE TABLE IF NOT EXISTS review_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                commit_hash TEXT,
                scope TEXT,
                total_findings INTEGER,
                model TEXT
            );
        """)
        # Migrate existing tables — add columns if missing
        for col, defn in [
            ("finding_id", "INTEGER"),
            ("estimated_fix_tokens", "INTEGER"),
            ("status", "TEXT DEFAULT 'open'"),
            ("agent_name", "TEXT"),
            ("workspace", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE review_findings ADD COLUMN {col} {defn}")
            except Exception:
                pass
        for col, defn in [("agent_name", "TEXT")]:
            try:
                await db.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} {defn}")
            except Exception:
                pass
        await db.commit()


def _defaults() -> dict:
    return {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "iteration_limit": DEFAULT_ITERATION_LIMIT,
        "enabled_tools": ["record_finding"],
        "llm_provider": DEFAULT_LLM_PROVIDER,
        "model_name": DEFAULT_MODEL_NAME,
        "all_known_tools": ["record_finding"],
    }


async def get_config() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("ALTER TABLE agent_config ADD COLUMN all_known_tools TEXT")
            await db.commit()
        except Exception:
            pass
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM agent_config WHERE id = 1") as cur:
            row = await cur.fetchone()
    if row is None:
        d = _defaults()
        await save_config(d)
        return d
    all_known_raw = row["all_known_tools"] if "all_known_tools" in row.keys() else None
    all_known = json.loads(all_known_raw) if all_known_raw else json.loads(row["enabled_tools"])
    return {
        "system_prompt": row["system_prompt"],
        "iteration_limit": row["iteration_limit"],
        "enabled_tools": json.loads(row["enabled_tools"]),
        "all_known_tools": all_known,
        "llm_provider": row["llm_provider"],
        "model_name": row["model_name"],
    }


async def save_config(config: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("ALTER TABLE agent_config ADD COLUMN all_known_tools TEXT")
            await db.commit()
        except Exception:
            pass
        all_known = config.get("all_known_tools", config.get("enabled_tools", []))
        await db.execute(
            """INSERT OR REPLACE INTO agent_config
               (id, system_prompt, iteration_limit, enabled_tools, llm_provider, model_name, all_known_tools)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (config["system_prompt"], config["iteration_limit"],
             json.dumps(config["enabled_tools"]),
             config["llm_provider"], config["model_name"], json.dumps(all_known))
        )
        await db.commit()


async def record_llm_call(thread_id, model, prompt_tokens, completion_tokens, total_tokens):
    prompt_tokens = prompt_tokens or 0
    completion_tokens = completion_tokens or 0
    total_tokens = total_tokens or 0
    ts = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO llm_calls (thread_id,timestamp,model,prompt_tokens,completion_tokens,total_tokens) VALUES (?,?,?,?,?,?)",
            (thread_id, ts, model, prompt_tokens, completion_tokens, total_tokens))
        await db.commit()


async def record_tool_invocation_start(thread_id: str, tool_name: str) -> int:
    ts = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tool_invocations (thread_id,tool_name,timestamp,status) VALUES (?,?,?,'pending')",
            (thread_id, tool_name, ts))
        await db.commit()
        return cur.lastrowid


async def record_tool_invocation_end(invocation_id: int, duration_ms: float, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tool_invocations SET duration_ms=?, status=? WHERE id=?",
            (duration_ms, status, invocation_id))
        await db.commit()


async def record_finding(thread_id: str, file_path: str, line_number: int,
                          criticality: str, category: str, title: str,
                          description: str, suggestion: str,
                          finding_id: int = None, estimated_fix_tokens: int = None,
                          agent_name: str = None, workspace: str = None) -> int:
    ts = datetime.datetime.utcnow().isoformat()
    # Derive a clean project name from the workspace path (last path component)
    project = os.path.basename(workspace.rstrip("/\\")) if workspace else None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO review_findings
               (thread_id,timestamp,file_path,line_number,criticality,category,
                title,description,suggestion,finding_id,estimated_fix_tokens,status,agent_name,workspace)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
            (thread_id, ts, file_path, line_number, criticality, category,
             title, description, suggestion, finding_id, estimated_fix_tokens, agent_name, project))
        await db.commit()
        return cur.lastrowid


async def record_review_session(thread_id: str, workspace: str, commit_hash: str = None,
                                 scope: str = "full", total_findings: int = 0, model: str = None):
    ts = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO review_sessions (thread_id,workspace,timestamp,commit_hash,scope,total_findings,model)
               VALUES (?,?,?,?,?,?,?)""",
            (thread_id, workspace, ts, commit_hash, scope, total_findings, model))
        await db.commit()


async def get_last_review_session(workspace: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM review_sessions WHERE workspace=? ORDER BY timestamp DESC LIMIT 1",
            (workspace,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def update_finding_status(finding_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE review_findings SET status=? WHERE id=?", (status, finding_id))
        await db.commit()
