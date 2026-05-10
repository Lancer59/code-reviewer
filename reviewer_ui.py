"""Chainlit UI for Dev Companion — rich activity display, finding cards, approvals."""

import chainlit as cl
import os
import asyncio
import logging
import uuid
import time

import engineio
engineio.payload.Payload.max_decode_packets = 100000

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from langgraph.types import Command

from review_agent import create_review_agent
from dashboard.db import (
    init_db, get_config,
    record_llm_call, record_tool_invocation_start,
    record_tool_invocation_end, record_finding as db_record_finding,
    record_review_session, get_last_review_session,
    update_finding_status as db_update_finding_status,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# Keep other loggers quieter
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("httpcore").setLevel(logging.WARNING)
# logging.getLogger("chainlit").setLevel(logging.INFO)
# logging.getLogger("uvicorn").setLevel(logging.INFO)

logger = logging.getLogger("ReviewerUI")
_db_initialized = False
_checkpointer = None
_checkpointer_conn = None
_store = InMemoryStore()

# Criticality display
CRIT_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
CRIT_LABEL = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW", "info": "INFO"}

# Agent display names
AGENT_DISPLAY = {
    "git-agent":          ("🔧", "Git Agent"),
    "file-scanner":       ("🔍", "File Scanner"),
    "security-scanner":   ("🔒", "Security Scanner"),
    "report-generator":   ("📊", "Report Generator"),
    "general-purpose":    ("🤖", "General Purpose"),
}

def _finding_label(raw: str) -> str:
    """Parse compact finding string and return a descriptive step label."""
    n = (cl.user_session.get("finding_count") or 0) + 1
    if not raw:
        return f"📝 Recording finding #{n}"
    # Format: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
    parts = raw.split("|", 5)
    if len(parts) < 4:
        return f"📝 Recording finding #{n}"
    file_line = parts[0]                          # e.g. auth.py:42
    crit_code = parts[1].strip()                  # C H M L I
    title = parts[3].strip() if len(parts) > 3 else ""
    fname = file_line.split(":")[0] if ":" in file_line else file_line
    crit_emoji = {"C": "🔴", "H": "🟠", "M": "🟡", "L": "🔵", "I": "⚪"}.get(crit_code, "📝")
    label = f"{crit_emoji} Finding #{n}"
    if fname:
        label += f" · `{fname}`"
    if title:
        label += f" — {title[:50]}"
    return label


# Tool display labels — dynamic where possible
def _tool_label(tool_name: str, tool_input) -> str:
    inp = tool_input or {}
    if isinstance(inp, str):
        inp = {}

    def _get(*keys):
        for k in keys:
            v = inp.get(k, "")
            if v:
                return str(v)
        return ""

    labels = {
        "ls":                  lambda: f"📂 Listing {_get('path') or 'files'}",
        "read_file":           lambda: f"📄 Reading {_get('file_path', 'path') or 'file'}",
        "view_file":           lambda: f"📄 Reading {_get('file_path', 'path') or 'file'}",
        "write_file":          lambda: f"📝 Creating {_get('file_path', 'path') or 'file'}",
        "edit_file":           lambda: f"✏️ Editing {_get('file_path', 'path') or 'file'}",
        "grep_search":         lambda: f"🔍 Scanning for '{_get('pattern', 'query') or 'pattern'}' in {_get('path') or 'codebase'}",
        "glob":                lambda: f"🗂️ Globbing {_get('pattern') or 'files'}",
        "write_todos":         lambda: "📋 Updating plan",
        "execute":             lambda: f"⚡ Running: {str(_get('command') or 'command')[:60]}",
        "f":                   lambda: _finding_label(inp.get("finding", "")),
        "record_finding":      lambda: _finding_label(inp.get("finding", "")),
        "git_status":          lambda: "🔍 Checking git status",
        "git_diff":            lambda: f"📊 Reading diff{' — ' + _get('file_path') if _get('file_path') else ''}",
        "git_log":             lambda: "📜 Reading git log",
        "git_blame":           lambda: f"🔎 Blaming {_get('file_path') or 'file'}",
        "git_create_branch":   lambda: f"🌿 Creating branch {_get('branch_name') or ''}",
        "git_commit":          lambda: f"💾 Committing: {str(_get('message') or '')[:50]}",
        "git_stash":           lambda: f"📦 Stashing ({_get('action') or 'push'})",
        "task":                lambda: f"🤖 Delegating to {_get('subagent_name', 'name') or 'subagent'}",
    }
    fn = labels.get(tool_name)
    try:
        return fn() if fn else f"🔧 {tool_name}"
    except Exception:
        return f"🔧 {tool_name}"


@cl.data_layer
def get_data_layer():
    return SQLAlchemyDataLayer(conninfo="sqlite+aiosqlite:///agent_data/chainlit_ui.db")


@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    if username == os.getenv("CHAINLIT_USER", "admin") and password == os.getenv("CHAINLIT_PASSWORD", "admin"):
        user = cl.User(identifier=username, metadata={"role": "admin", "provider": "credentials"})
        from chainlit.data import get_data_layer as _dl
        dl = _dl()
        if dl and not await dl.get_user(identifier=username):
            await dl.create_user(user)
        return user
    return None


async def get_checkpointer():
    global _checkpointer, _checkpointer_conn
    if _checkpointer is None:
        import aiosqlite
        _checkpointer_conn = await aiosqlite.connect("agent_data/checkpoints_lg.db")
        _checkpointer = AsyncSqliteSaver(_checkpointer_conn)
        await _checkpointer.setup()
    return _checkpointer


@cl.on_chat_start
async def start():
    global _db_initialized
    if not _db_initialized:
        try:
            await init_db()
            _db_initialized = True
        except Exception as e:
            logger.warning(f"init_db failed: {e}")

    res = await cl.AskUserMessage(
        content="🔍 **Dev Companion**\n\nEnter the project folder name to review (sibling to code-reviewer):"
    ).send()
    if not res:
        return

    project_folder = res["output"].strip()
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    workspace = os.path.abspath(os.path.join(parent_dir, project_folder))
    cl.user_session.set("workspace", workspace)

    if not os.path.exists(workspace):
        await cl.Message(content=f"❌ Folder not found: `{workspace}`").send()
        return

    try:
        msg = cl.Message(content="⚙️ Initialising review agent...")
        await msg.send()

        checkpointer = await get_checkpointer()
        user_id = cl.user_session.get("user").identifier if cl.user_session.get("user") else "default"
        try:
            cfg = await get_config()
        except Exception:
            cfg = {}

        agent = await create_review_agent(
            workspace, checkpointer, _store, user_id=user_id,
            iteration_limit=cfg.get("iteration_limit"),
        )
        thread_id = str(uuid.uuid4())
        cl.user_session.set("agent", agent)
        cl.user_session.set("thread_id", thread_id)
        cl.user_session.set("workspace", workspace)
        cl.user_session.set("findings", [])
        cl.user_session.set("finding_count", 0)
        cl.user_session.set("review_start_time", None)
        cl.user_session.set("review_scope", "full")

        from chainlit.data import get_data_layer as _dl
        dl = _dl()
        if dl:
            await dl.update_thread(
                thread_id=cl.context.session.thread_id,
                metadata={"thread_id": thread_id, "workspace": workspace}
            )

        task_list = cl.TaskList()
        await task_list.send()
        cl.user_session.set("task_list", task_list)

        # Check for previous review session — offer scope selection
        scope_hint = ""
        try:
            import git as _git
            repo = _git.Repo(workspace, search_parent_directories=False)
            commit_hash = repo.head.commit.hexsha[:8] if repo.head.is_valid() else None
            cl.user_session.set("current_commit", commit_hash)

            last_session = await get_last_review_session(workspace)
            if last_session and last_session.get("commit_hash") and commit_hash:
                last_commit = last_session["commit_hash"]
                last_date = last_session["timestamp"][:10]
                res = await cl.AskActionMessage(
                    content=(
                        f"📋 Found a previous review of **`{project_folder}`** from `{last_date}` at commit `{last_commit}`.\n\n"
                        f"How would you like to proceed?"
                    ),
                    actions=[
                        cl.Action(name="diff", payload={"value": "diff"}, label="🔀 Review only changes since last review"),
                        cl.Action(name="full", payload={"value": "full"}, label="🔍 Full review of entire codebase"),
                    ],
                ).send()
                scope = res.get("payload", {}).get("value", "full") if res else "full"
                cl.user_session.set("review_scope", scope)
                cl.user_session.set("last_commit", last_commit)
                if scope == "diff":
                    # Get the actual diff to inject as context
                    try:
                        diff_output = repo.git.diff(f"{last_commit}..HEAD", "--stat")
                        full_diff = repo.git.diff(f"{last_commit}..HEAD")
                        if len(full_diff) > 8000:
                            full_diff = full_diff[:8000] + "\n... [truncated]"
                        cl.user_session.set("diff_context",
                            f"Changes since commit {last_commit}:\n\n```\n{diff_output}\n```\n\nFull diff:\n```diff\n{full_diff}\n```"
                        )
                    except Exception:
                        cl.user_session.set("diff_context", None)
                    scope_hint = f"\n\n> Reviewing only changes since commit `{last_commit}` — say **`review`** to start."
                else:
                    cl.user_session.set("diff_context", None)
                    scope_hint = ""
        except Exception:
            cl.user_session.set("current_commit", None)

        msg.content = (
            f"✅ Ready to review **`{project_folder}`**{scope_hint}\n\n"
            "Type **`review`** to start a full review, or ask a specific question.\n"
            "After the review, you can say **`fix #1`** or **`fix all critical`** to apply fixes."
        )
        await msg.update()
    except Exception as e:
        await cl.Message(content=f"❌ Error: {e}").send()


@cl.on_chat_resume
async def on_chat_resume(thread):
    global _db_initialized
    if not _db_initialized:
        try:
            await init_db()
            _db_initialized = True
        except Exception as e:
            logger.warning(f"init_db failed: {e}")

    import json as _json
    raw = thread.get("metadata", {}) if isinstance(thread, dict) else {}
    if isinstance(raw, str):
        try:
            meta = _json.loads(raw)
        except Exception:
            meta = {}
    else:
        meta = raw or {}

    thread_id = meta.get("thread_id")
    workspace = meta.get("workspace", "")
    if not thread_id or not workspace:
        await cl.Message(content="⚠️ Could not restore session.").send()
        return

    try:
        checkpointer = await get_checkpointer()
        user_id = cl.user_session.get("user").identifier if cl.user_session.get("user") else "default"
        cfg = await get_config()
        agent = await create_review_agent(
            workspace, checkpointer, _store, user_id=user_id,
            iteration_limit=cfg.get("iteration_limit"),
        )
        cl.user_session.set("agent", agent)
        cl.user_session.set("thread_id", thread_id)
        cl.user_session.set("workspace", workspace)
        cl.user_session.set("findings", [])
        cl.user_session.set("finding_count", 0)
        cl.user_session.set("review_scope", "full")
        cl.user_session.set("current_commit", None)
        task_list = cl.TaskList()
        await task_list.send()
        cl.user_session.set("task_list", task_list)
    except Exception as e:
        await cl.Message(content=f"❌ Error resuming: {e}").send()


@cl.on_message
async def main(message: cl.Message):
    agent = cl.user_session.get("agent")
    thread_id = cl.user_session.get("thread_id")
    if not agent:
        await cl.Message(content="Session expired. Please refresh.").send()
        return

    # Track review start time
    if cl.user_session.get("review_start_time") is None:
        cl.user_session.set("review_start_time", time.monotonic())

    stream_msg = cl.Message(content="")
    await stream_msg.send()

    full_content = ""
    active_steps: dict = {}
    tool_inv_ids: dict = {}
    tool_start_times: dict = {}
    tool_inputs: dict = {}   # store tool inputs by run_id for use at on_tool_end
    all_steps: list = []
    session_findings: list = cl.user_session.get("findings") or []
    finding_count: int = cl.user_session.get("finding_count") or 0
    findings_this_turn: int = 0  # only findings recorded in this message turn
    tools_fired: bool = False
    is_review: bool = False  # only show progress element during a review pass

    # Clear per-turn agent banner flags so each new message can show banners again
    for agent_key in AGENT_DISPLAY:
        cl.user_session.set(f"banner_shown_{agent_key}", False)

    # Progress element — only shown during review, lazily created
    progress_el = None

    async def _update_progress(current_tool: str = ""):
        nonlocal progress_el, is_review
        if not is_review:
            return
        counts = {}
        for fnd in session_findings:
            counts[fnd["criticality"]] = counts.get(fnd["criticality"], 0) + 1
        parts = []
        for crit in ["critical", "high", "medium", "low", "info"]:
            if counts.get(crit):
                parts.append(f"{CRIT_EMOJI[crit]} {counts[crit]}")
        findings_str = "  ".join(parts) if parts else "0 findings"
        tool_str = f"  |  {current_tool}" if current_tool else ""
        content = f"🔍 Reviewing...  {findings_str}{tool_str}"
        try:
            if progress_el is None:
                progress_el = cl.Text(name="review-progress", content=content, display="inline")
            else:
                progress_el.content = content
            await progress_el.send(for_id=stream_msg.id)
        except Exception:
            pass

    async def _clear_progress():
        nonlocal progress_el
        if progress_el is not None:
            try:
                progress_el.content = ""
                await progress_el.remove()
            except Exception:
                pass
            progress_el = None

    try:
        input_data = {"messages": [("user", message.content or "")]}

        # Inject diff context for scoped reviews
        diff_context = cl.user_session.get("diff_context")
        if diff_context and (message.content or "").strip().lower() in ("review", "start review", "go"):
            input_data = {"messages": [("user",
                f"{message.content}\n\n[SCOPE: Review only the following changed files]\n{diff_context}"
            )]}
            cl.user_session.set("diff_context", None)  # consume once

        # Parse which finding IDs the user wants to fix from this message
        # e.g. "fix #3", "fix #2 and #5", "fix all critical", "fix auth.py"
        _fixing_ids: set = _parse_fix_targets(message.content or "", session_findings)

        config = {
            "recursion_limit": getattr(agent, "_iteration_limit", 150) * 2,
            "configurable": {"thread_id": thread_id},
        }

        while True:
            async for event in agent.astream_events(input_data, version="v2", config=config):
                kind = event["event"]
                run_id = event["run_id"]

                # Identify which agent fired this event — only trust lc_agent_name metadata
                agent_name = event.get("metadata", {}).get("lc_agent_name") or None

                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"].content
                    if chunk:
                        full_content += chunk
                        # Only stream to user when no tools have fired yet (pure conversational reply)
                        # Once tools start, the agent is in "working" mode — suppress intermediate tokens
                        if not tools_fired:
                            await stream_msg.stream_token(chunk)

                elif kind == "on_tool_start":
                    tool_name = event["name"]
                    tool_input = event["data"].get("input")
                    tool_inputs[run_id] = tool_input  # store for use at on_tool_end

                    if not tools_fired:
                        tools_fired = True
                        full_content = ""
                        stream_msg.content = ""
                        await stream_msg.update()

                    # Detect review mode — any of these tools means we're reviewing
                    if tool_name in ("f", "record_finding", "write_todos", "task"):
                        is_review = True

                    # Agent transition banner — once per agent per message turn, with context
                    if agent_name and agent_name in AGENT_DISPLAY:
                        banner_key = f"banner_shown_{agent_name}"
                        if not cl.user_session.get(banner_key):
                            cl.user_session.set(banner_key, True)
                            emoji, display_name = AGENT_DISPLAY[agent_name]
                            # Build context string from the first tool being called
                            inp = tool_input if isinstance(tool_input, dict) else {}
                            ctx = (
                                inp.get("file_path") or inp.get("path") or
                                inp.get("pattern") or inp.get("query") or
                                inp.get("subagent_name") or ""
                            )
                            ctx_str = f" → `{ctx}`" if ctx else ""
                            await cl.Message(
                                content=f"{emoji} **{display_name}**{ctx_str}",
                                parent_id=stream_msg.id,
                            ).send()

                    label = _tool_label(tool_name, tool_input)
                    step = cl.Step(name=label, type="tool", parent_id=stream_msg.id)
                    step.input = str(tool_input)[:300] if tool_input else ""
                    await step.send()
                    active_steps[run_id] = step
                    all_steps.append(step)

                    try:
                        inv_id = await record_tool_invocation_start(thread_id, tool_name)
                        tool_inv_ids[run_id] = inv_id
                        tool_start_times[run_id] = asyncio.get_event_loop().time()
                    except Exception:
                        pass

                    # Snapshot file content before edit_file/write_file so we can diff + undo
                    if tool_name in ("edit_file", "write_file") and isinstance(tool_input, dict):
                        fp = tool_input.get("file_path") or tool_input.get("path", "")
                        if fp:
                            workspace_path = cl.user_session.get("workspace", "")
                            # fp from deepagents is always absolute — don't double-join
                            full_fp = fp if os.path.isabs(fp) else os.path.join(workspace_path, fp)
                            undo_store = cl.user_session.get("undo_store") or {}
                            if tool_name == "edit_file":
                                try:
                                    with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                        original = fh.read()
                                    undo_store[fp] = {"type": "edit", "original": original}
                                except Exception:
                                    pass
                            else:  # write_file — new file, undo = delete
                                file_exists = os.path.exists(full_fp)
                                if file_exists:
                                    try:
                                        with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                            original = fh.read()
                                        undo_store[fp] = {"type": "edit", "original": original}
                                    except Exception:
                                        pass
                                else:
                                    undo_store[fp] = {"type": "new_file"}
                            cl.user_session.set("undo_store", undo_store)

                    await _update_progress(label)

                elif kind == "on_tool_end":
                    step = active_steps.pop(run_id, None)
                    tool_output = event["data"].get("output")
                    tool_name = event["name"]
                    out_str = _extract(tool_output)
                    tool_input_data = tool_inputs.pop(run_id, None) or {}
                    if isinstance(tool_input_data, str):
                        tool_input_data = {}

                    if step:
                        step.output = out_str[:400] + "..." if len(out_str) > 400 else out_str
                        await step.update()

                    try:
                        inv_id = tool_inv_ids.pop(run_id, None)
                        start_t = tool_start_times.pop(run_id, None)
                        if inv_id is not None:
                            dur = (asyncio.get_event_loop().time() - start_t) * 1000 if start_t else 0.0
                            status = "failure" if out_str.startswith("Error") else "success"
                            await record_tool_invocation_end(inv_id, dur, status)
                    except Exception:
                        pass

                    # Diff viewer + Undo button after edit_file / write_file
                    if tool_name in ("edit_file", "write_file"):
                        fp = tool_input_data.get("file_path") or tool_input_data.get("path", "")
                        undo_store = cl.user_session.get("undo_store") or {}
                        snap = undo_store.get(fp)
                        if fp and snap is not None:
                            workspace_path = cl.user_session.get("workspace", "")
                            full_fp = fp if os.path.isabs(fp) else os.path.join(workspace_path, fp)
                            # Derive a display-friendly relative path
                            try:
                                display_fp = os.path.relpath(full_fp, workspace_path) if workspace_path else fp
                            except ValueError:
                                display_fp = fp
                            try:
                                with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                    new_content = fh.read()
                                import difflib
                                snap_type = snap["type"] if isinstance(snap, dict) else "edit"
                                original = snap.get("original", "") if isinstance(snap, dict) else snap
                                if snap_type == "new_file":
                                    # Show the new file content as a pure addition diff
                                    added_lines = new_content.splitlines(keepends=True)[:120]
                                    diff_text = f"--- /dev/null\n+++ b/{display_fp}\n"
                                    diff_text += "".join(f"+{line}" for line in added_lines)
                                    if len(new_content.splitlines()) > 120:
                                        diff_text += "\n... [truncated]"
                                    await cl.Message(
                                        content=f"📝 **Created `{display_fp}`**\n```diff\n{diff_text}\n```",
                                        actions=[cl.Action(
                                            name="undo",
                                            payload={"file": fp, "type": "new_file"},
                                            label="↩️ Delete this file"
                                        )],
                                    ).send()
                                else:
                                    diff_lines = list(difflib.unified_diff(
                                        original.splitlines(keepends=True),
                                        new_content.splitlines(keepends=True),
                                        fromfile=f"a/{display_fp}", tofile=f"b/{display_fp}", n=3
                                    ))
                                    diff_text = "".join(diff_lines[:120])
                                    if diff_text:
                                        await cl.Message(
                                            content=f"✏️ **Changed `{display_fp}`**\n```diff\n{diff_text}\n```",
                                            actions=[cl.Action(
                                                name="undo",
                                                payload={"file": fp, "type": "edit"},
                                                label="↩️ Undo this change"
                                            )],
                                        ).send()
                                    else:
                                        await cl.Message(content=f"✏️ **Edited `{display_fp}`** (no diff detected)").send()
                            except Exception as e:
                                await cl.Message(content=f"✏️ **Edited `{display_fp}`**").send()

                        # Mark only the targeted findings for this file as fixed
                        if fp and not out_str.startswith("Error") and _fixing_ids:
                            try:
                                workspace_path = cl.user_session.get("workspace", "")
                                # Normalize fp to a relative path for matching against finding file_path
                                try:
                                    rel_fp = os.path.relpath(fp, workspace_path) if (workspace_path and os.path.isabs(fp)) else fp
                                except ValueError:
                                    rel_fp = fp
                                for fnd in session_findings:
                                    fnd_db_id = fnd.get("db_id") or fnd.get("id")
                                    fnd_seq_id = fnd.get("id")
                                    fnd_file = fnd.get("file_path", "")
                                    file_match = (
                                        fnd_file == rel_fp
                                        or fnd_file == fp
                                        or rel_fp.endswith(fnd_file)
                                        or fnd_file.endswith(rel_fp.lstrip("/\\"))
                                    )
                                    if file_match and fnd_seq_id in _fixing_ids:
                                        if fnd_db_id:
                                            await db_update_finding_status(fnd_db_id, "fixed")
                                            fnd["status"] = "fixed"
                                cl.user_session.set("findings", session_findings)
                            except Exception:
                                pass

                    # Auto-mark finding fixed when git_commit succeeds
                    if tool_name == "git_commit" and not out_str.startswith("Error"):
                        # Mark the most recently fixed finding as fixed in DB
                        try:
                            last_fixed = cl.user_session.get("last_fixing_id")
                            if last_fixed:
                                await db_update_finding_status(last_fixed, "fixed")
                                cl.user_session.set("last_fixing_id", None)
                        except Exception:
                            pass

                    # Parse compact finding: FINDING|file|line|criticality|category|title|desc|fix
                    if tool_name in ("f", "record_finding") and out_str.startswith("FINDING|"):
                        parts = out_str.split("|", 7)
                        if len(parts) == 8:
                            _, fp, ln, crit, cat, title, desc, sug = parts
                            finding_count += 1
                            findings_this_turn += 1
                            cl.user_session.set("finding_count", finding_count)

                            # Estimate fix tokens from file size
                            workspace = cl.user_session.get("workspace", "")
                            est_tokens = None
                            try:
                                fpath = os.path.join(workspace, fp)
                                if os.path.exists(fpath):
                                    size_chars = os.path.getsize(fpath)
                                    est_tokens = int((size_chars / 4) * 1.5 + 300)
                            except Exception:
                                pass

                            finding = {
                                "id": finding_count,
                                "file_path": fp, "line_number": int(ln or 0),
                                "criticality": crit, "category": cat,
                                "title": title, "description": desc, "suggestion": sug,
                                "estimated_fix_tokens": est_tokens,
                            }
                            session_findings.append(finding)
                            cl.user_session.set("findings", session_findings)
                            try:
                                db_id = await db_record_finding(
                                    thread_id, fp, int(ln or 0), crit, cat, title, desc, sug,
                                    finding_id=finding_count, estimated_fix_tokens=est_tokens,
                                    workspace=cl.user_session.get("workspace", ""),
                                )
                                finding["db_id"] = db_id
                            except Exception:
                                pass

                            # Rich finding card
                            emoji = CRIT_EMOJI.get(crit, "⚪")
                            est_str = f"\n\n⚡ Est. fix: ~{est_tokens:,} tokens" if est_tokens else ""
                            card = (
                                f"{emoji} **#{finding_count} [{CRIT_LABEL.get(crit, crit.upper())}]** "
                                f"· `{cat}`\n"
                                f"**{title}**\n"
                                f"📍 `{fp}:{ln}`\n\n"
                                f"{desc}\n\n"
                                f"💡 **Fix:** {sug}"
                                f"{est_str}"
                            )
                            await cl.Message(content=card, parent_id=stream_msg.id).send()
                            await _update_progress()

                    # Todo list updates
                    if tool_name == "write_todos" and tool_output:
                        try:
                            todos = None
                            if hasattr(tool_output, "update") and isinstance(tool_output.update, dict):
                                todos = tool_output.update.get("todos")
                            elif isinstance(tool_output, dict):
                                todos = tool_output.get("todos")
                            if todos:
                                await _update_task_list(todos)
                        except Exception:
                            pass

                elif kind == "on_tool_error":
                    step = active_steps.pop(run_id, None)
                    if step:
                        step.output = f"❌ {event['data'].get('error', 'Unknown error')}"
                        await step.update()

                elif kind == "on_chat_model_end":
                    try:
                        output = event["data"].get("output")
                        usage = getattr(output, "usage_metadata", None) or \
                                getattr(output, "response_metadata", {}).get("token_usage", {})
                        if isinstance(usage, dict):
                            pt = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                            ct = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                            tt = usage.get("total_tokens", pt + ct)
                        else:
                            pt = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0)
                            ct = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0)
                            tt = getattr(usage, "total_tokens", pt + ct)
                        model = getattr(output, "response_metadata", {}).get("model_name", "unknown") if output else "unknown"
                        await record_llm_call(thread_id, model, pt, ct, tt)
                    except Exception:
                        pass

                elif kind == "on_chain_end":
                    try:
                        output = event["data"].get("output")
                        if output and isinstance(output, dict):
                            if "todos" in output:
                                await _update_task_list(output["todos"])
                            if not full_content and "messages" in output:
                                msgs = output["messages"]
                                if hasattr(msgs, "value"):
                                    msgs = msgs.value
                                if isinstance(msgs, list) and msgs:
                                    last = msgs[-1]
                                    is_ai = (hasattr(last, "type") and last.type == "ai") or \
                                            (isinstance(last, dict) and last.get("type") == "ai")
                                    if is_ai:
                                        full_content = getattr(last, "content", "") or last.get("content", "")
                    except Exception:
                        pass

            # Human-in-the-loop approval interrupt
            state = await agent.aget_state(config)
            if state and state.next:
                tasks = getattr(state, "tasks", [])
                if tasks and hasattr(tasks[0], "interrupts") and tasks[0].interrupts:
                    interrupt_info = tasks[0].interrupts[0].value
                    try:
                        if isinstance(interrupt_info, dict) and "action_requests" in interrupt_info:
                            reqs = interrupt_info["action_requests"]
                            cmd_str = "\n".join([
                                f"{r.get('name')}: {r.get('args', {}).get('command', r.get('args', ''))}"
                                for r in reqs
                            ])
                        else:
                            cmd_str = str(interrupt_info)
                    except Exception:
                        cmd_str = str(interrupt_info)

                    res = await cl.AskActionMessage(
                        content=(
                            f"⚠️ **Approval Required**\n\n"
                            f"```\n{cmd_str}\n```\n\n"
                            f"Approve or reject this operation?"
                        ),
                        actions=[
                            cl.Action(name="approve", payload={"value": "approve"}, label="✅ Approve"),
                            cl.Action(name="reject",  payload={"value": "reject"},  label="❌ Reject"),
                        ],
                    ).send()

                    user_response = (res.get("payload", {}).get("value") if res else "reject")
                    if user_response == "reject":
                        await cl.Message(content="🚫 Operation rejected.").send()
                    input_data = Command(resume={"decisions": [{"type": user_response}]})
                    continue
            break

    except Exception as e:
        error_text = f"🚨 {type(e).__name__}: {e}"
        logger.error(error_text, exc_info=True)
        stream_msg.content = error_text
        await stream_msg.update()
        return

    finally:
        if full_content:
            stream_msg.content = full_content
            await stream_msg.update()
        elif not stream_msg.content:
            stream_msg.content = "Done."
            await stream_msg.update()

        # Clear progress indicator
        await _clear_progress()

        for step in all_steps:
            try:
                await step.remove()
            except Exception:
                pass

        # Review summary — only when findings were recorded in THIS turn
        if findings_this_turn > 0:
            counts = {}
            for fnd in session_findings:
                counts[fnd["criticality"]] = counts.get(fnd["criticality"], 0) + 1

            elapsed = ""
            start_t = cl.user_session.get("review_start_time")
            if start_t:
                secs = int(time.monotonic() - start_t)
                elapsed = f"  |  ⏱️ {secs // 60}m {secs % 60}s"
                cl.user_session.set("review_start_time", None)

            crit_line = "  ".join(
                f"{CRIT_EMOJI[c]} **{counts[c]}** {c}"
                for c in ["critical", "high", "medium", "low", "info"]
                if counts.get(c)
            )

            fix_hint = ""
            if counts.get("critical") or counts.get("high"):
                fix_hint = (
                    "\n\n**To fix issues, say:**\n"
                    "- `fix everything` — fix all in priority order\n"
                    "- `fix all critical` — fix only critical issues\n"
                    "- `fix #1` — fix a specific finding by ID\n"
                    "- `fix auth.py` — fix all issues in a file"
                )

            # Build per-finding token estimate list (top 10 by criticality)
            crit_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
            sorted_findings = sorted(session_findings, key=lambda x: crit_order.get(x["criticality"], 5))
            est_lines = []
            for fnd in sorted_findings[:10]:
                est = fnd.get("estimated_fix_tokens")
                est_str = f"~{est:,} tokens" if est else "~500 tokens"
                emoji = CRIT_EMOJI.get(fnd["criticality"], "⚪")
                label = CRIT_LABEL.get(fnd["criticality"], fnd["criticality"].upper())
                est_lines.append(
                    f"  #{fnd['id']} [{label}] `{fnd['file_path']}` — {fnd['title']} — {est_str}"
                )
            est_block = ""
            if est_lines:
                est_block = "\n\n**Est. tokens per fix:**\n" + "\n".join(est_lines)
                if len(sorted_findings) > 10:
                    est_block += f"\n  _(+{len(sorted_findings)-10} more)_"

            # Record review session in DB
            try:
                commit_hash = cl.user_session.get("current_commit")
                scope = cl.user_session.get("review_scope", "full")
                workspace = cl.user_session.get("workspace", "")
                await record_review_session(
                    thread_id=thread_id,
                    workspace=workspace,
                    commit_hash=commit_hash,
                    scope=scope,
                    total_findings=len(session_findings),
                )
            except Exception:
                pass

            summary = (
                f"---\n"
                f"✅ **Review Complete** — {len(session_findings)} findings{elapsed}\n\n"
                f"{crit_line}"
                f"{fix_hint}"
                f"{est_block}\n\n"
                f"📊 [View full dashboard](http://localhost:8001/dashboard)  "
                f"·  [Export HTML](/dashboard/api/reports/html?thread_id={thread_id})"
                f"  ·  [Export XLSX](/dashboard/api/reports/xlsx?thread_id={thread_id})"
            )
            await cl.Message(content=summary).send()


def _parse_fix_targets(msg: str, findings: list) -> set:
    """
    Parse the user's fix request and return a set of finding seq IDs to mark fixed.
    Handles: "fix #3", "fix #2 and #5", "fix all critical", "fix auth.py", "fix everything"
    Returns empty set if we can't determine targets (don't mark anything automatically).
    """
    import re
    msg_lower = msg.lower().strip()
    if not msg_lower.startswith("fix"):
        return set()

    ids = set()

    # "fix everything" / "fix all" — all open findings
    if re.search(r"fix\s+(everything|all\s+findings?|all$)", msg_lower):
        return {f["id"] for f in findings}

    # "fix all critical" / "fix all high" etc.
    crit_match = re.search(r"fix\s+all\s+(critical|high|medium|low|info)", msg_lower)
    if crit_match:
        target_crit = crit_match.group(1)
        return {f["id"] for f in findings if f.get("criticality") == target_crit}

    # "fix #3", "fix #2 and #5", "fix finding 3"
    for m in re.finditer(r"#(\d+)|finding\s+(\d+)", msg_lower):
        n = int(m.group(1) or m.group(2))
        ids.add(n)
    if ids:
        return ids

    # "fix auth.py" — all findings in that file
    file_match = re.search(r"fix\s+([\w./\\-]+\.\w+)", msg_lower)
    if file_match:
        fname = file_match.group(1)
        return {f["id"] for f in findings if fname in f.get("file_path", "")}

    return set()


async def _update_task_list(todos):
    task_list = cl.TaskList()
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        title = todo.get("content", todo.get("title", "Task"))
        raw = todo.get("status", "pending")
        status = (cl.TaskStatus.DONE if raw == "done"
                  else cl.TaskStatus.RUNNING if raw == "in_progress"
                  else cl.TaskStatus.READY)
        await task_list.add_task(cl.Task(title=str(title), status=status))
    try:
        await task_list.send()
    except Exception:
        pass


@cl.action_callback("undo")
async def on_undo_action(action: cl.Action):
    """Handle undo button — restore file from pre-edit snapshot, or delete a newly created file."""
    payload = action.payload or {}
    fp = payload.get("file", "")
    snap_type = payload.get("type", "edit")

    if not fp:
        await cl.Message(content="❌ Could not determine file to undo.").send()
        return

    undo_store = cl.user_session.get("undo_store") or {}
    snap = undo_store.get(fp)
    if snap is None:
        await cl.Message(content=f"❌ No snapshot found for `{fp}`.").send()
        return

    workspace_path = cl.user_session.get("workspace", "")
    full_fp = fp if os.path.isabs(fp) else os.path.join(workspace_path, fp)
    try:
        display_fp = os.path.relpath(full_fp, workspace_path) if workspace_path else fp
    except ValueError:
        display_fp = fp

    try:
        if snap_type == "new_file" or (isinstance(snap, dict) and snap.get("type") == "new_file"):
            # Undo a file creation — delete the file
            if os.path.exists(full_fp):
                os.remove(full_fp)
            undo_store.pop(fp, None)
            cl.user_session.set("undo_store", undo_store)
            await cl.Message(content=f"↩️ Deleted `{display_fp}` (creation undone).").send()
        else:
            # Undo an edit — restore original content
            original = snap.get("original", "") if isinstance(snap, dict) else snap
            with open(full_fp, "w", encoding="utf-8") as fh:
                fh.write(original)
            undo_store.pop(fp, None)
            cl.user_session.set("undo_store", undo_store)
            await cl.Message(content=f"↩️ Restored `{display_fp}` to its pre-fix state.").send()
    except Exception as e:
        await cl.Message(content=f"❌ Undo failed: {e}").send()


def _extract(tool_output) -> str:
    if tool_output is None:
        return ""
    if isinstance(tool_output, str):
        return tool_output
    # ToolMessage / AIMessage — content can be str or list of blocks
    if hasattr(tool_output, "content"):
        content = tool_output.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Extract text from content blocks: [{"type": "text", "text": "..."}]
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    parts.append(block.get("text", block.get("content", str(block))))
            return "".join(parts)
        return str(content)
    if isinstance(tool_output, dict):
        return tool_output.get("content", tool_output.get("output", str(tool_output)))
    return str(tool_output)
