"""Chainlit UI for Dev Companion — rich activity display, finding cards, approvals."""

import chainlit as cl
import json
import os
import asyncio
import logging
import uuid
import time
import difflib

import engineio
engineio.payload.Payload.max_decode_packets = 100000

from dotenv import load_dotenv
load_dotenv()  # load .env first so env vars are available as fallback for cfg()

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from langgraph.types import Command

from config import cfg, cfg_bool, cfg_int
from review_agent import create_review_agent
from dashboard.db import (
    init_db, get_config,
    record_llm_call, record_tool_invocation_start,
    record_tool_invocation_end, record_finding as db_record_finding,
    record_review_session, get_last_review_session,
    update_finding_status as db_update_finding_status,
    save_pat, load_pat, delete_pat,
)
from tools.git_tools import git_clone, cleanup_workspace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("ReviewerUI")

_db_initialized = False
_checkpointer = None
_checkpointer_conn = None
_store = InMemoryStore()

# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------

_APP_BASE_URL = cfg("APP_BASE_URL", "http://localhost:8001").rstrip("/")

_WORKSPACE_BASE_DIR = cfg(
    "WORKSPACE_BASE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces"),
)

if cfg("CHAINLIT_USER", "admin") == "admin" and cfg("CHAINLIT_PASSWORD", "admin") == "admin":
    logging.getLogger("ReviewerUI").warning(
        "SECURITY: Default Chainlit credentials in use. "
        "Set CHAINLIT_USER and CHAINLIT_PASSWORD in your .env or config.json before deploying."
    )

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

CRIT_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
CRIT_LABEL = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW", "info": "INFO"}

AGENT_DISPLAY = {
    "git-agent":          ("🔧", "Git Agent"),
    "file-scanner":       ("🔍", "File Scanner"),
    "security-scanner":   ("🔒", "Security Scanner"),
    "report-generator":   ("📊", "Report Generator"),
    "general-purpose":    ("🤖", "General Purpose"),
}


def _finding_label(raw: str) -> str:
    n = (cl.user_session.get("finding_count") or 0) + 1
    if not raw:
        return f"📝 Recording finding #{n}"
    parts = raw.split("|", 5)
    if len(parts) < 4:
        return f"📝 Recording finding #{n}"
    file_line = parts[0]
    crit_code = parts[1].strip()
    title = parts[3].strip() if len(parts) > 3 else ""
    fname = file_line.split(":")[0] if ":" in file_line else file_line
    crit_emoji = {"C": "🔴", "H": "🟠", "M": "🟡", "L": "🔵", "I": "⚪"}.get(crit_code, "📝")
    label = f"{crit_emoji} Finding #{n}"
    if fname:
        label += f" · `{fname}`"
    if title:
        label += f" — {title[:50]}"
    return label


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
        "git_push":            lambda: f"🚀 Pushing {_get('branch') or 'branch'} to {_get('remote') or 'origin'}",
        "task":                lambda: f"🤖 Delegating to {_get('subagent_name', 'name') or 'subagent'}",
    }
    fn = labels.get(tool_name)
    try:
        return fn() if fn else f"🔧 {tool_name}"
    except Exception:
        return f"🔧 {tool_name}"


def _detect_provider(repo_url: str) -> tuple[str, str]:
    url = repo_url.lower()
    if "github.com" in url:
        return "GitHub", "https://github.com/settings/tokens"
    elif "gitlab.com" in url:
        return "GitLab", "https://gitlab.com/-/profile/personal_access_tokens"
    elif "bitbucket.org" in url:
        return "Bitbucket", "https://bitbucket.org/account/settings/app-passwords"
    elif "dev.azure.com" in url or "visualstudio.com" in url:
        return "Azure DevOps", "https://dev.azure.com → User Settings → Personal Access Tokens"
    else:
        return "your git provider", ""


# ---------------------------------------------------------------------------
# Chainlit setup
# ---------------------------------------------------------------------------

@cl.data_layer
def get_data_layer():
    agent_data_dir = cfg(
        "AGENT_DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_data"),
    )
    os.makedirs(agent_data_dir, exist_ok=True)
    db_path = os.path.join(agent_data_dir, "chainlit_ui.db")
    return SQLAlchemyDataLayer(conninfo=f"sqlite+aiosqlite:///{db_path}")


@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    if username == cfg("CHAINLIT_USER", "admin") and password == cfg("CHAINLIT_PASSWORD", "admin"):
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
        agent_data_dir = cfg(
            "AGENT_DATA_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_data"),
        )
        os.makedirs(agent_data_dir, exist_ok=True)
        cp_path = os.path.join(agent_data_dir, "checkpoints_lg.db")
        _checkpointer_conn = await aiosqlite.connect(cp_path)
        _checkpointer = AsyncSqliteSaver(_checkpointer_conn)
        await _checkpointer.setup()
    return _checkpointer


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

@cl.on_chat_start
async def start():
    global _db_initialized
    if not _db_initialized:
        try:
            await init_db()
            _db_initialized = True
        except Exception as e:
            logger.warning(f"init_db failed: {e}")

    workspace = await _onboard_cloud()
    if not workspace:
        return
    # Read PAT from session immediately — before any awaits that might lose context
    git_pat = cl.user_session.get("git_pat", "")
    git_repo_url = cl.user_session.get("git_repo_url", "")
    await _init_agent_session(workspace, git_pat=git_pat, git_repo_url=git_repo_url)


async def _onboard_cloud() -> str | None:
    """Two-step onboarding: URL (+optional branch) → PAT → clone."""
    await cl.Message(
        content=(
            "🔍 **Dev Companion — AI Code Reviewer**\n\n"
            "I'll review your codebase and find bugs, security issues, and code quality problems.\n\n"
            "To get started I need:\n"
            "1. Your repository URL (HTTPS) and optionally a branch\n"
            "2. A Personal Access Token (PAT) with read access\n\n"
            "_Your PAT is used only to clone the repo and is never stored._"
        )
    ).send()

    # Step 1: URL + optional branch
    repo_url = None
    repo_branch = None
    for attempt in range(3):
        res = await cl.AskUserMessage(
            content=(
                "**Step 1 / 2** — Enter the Git repository URL and (optionally) a branch, separated by a space:\n"
                "_(e.g. `https://github.com/your-org/your-repo` or `https://github.com/your-org/your-repo develop`)_"
            ),
            timeout=300,
        ).send()
        if not res:
            await cl.Message(content="⏱️ Timed out waiting for input. Please refresh and try again.").send()
            return None

        parts = res["output"].strip().split(None, 1)
        repo_url = parts[0]
        repo_branch = parts[1].strip() if len(parts) > 1 else None

        if repo_url.startswith("https://"):
            break
        await cl.Message(
            content="❌ That doesn't look like a valid HTTPS git URL. Please try again.\n_(Must start with `https://`)_"
        ).send()
        if attempt == 2:
            await cl.Message(content="Too many invalid attempts. Please refresh and try again.").send()
            return None

    # Step 2: PAT
    provider, guide_url = _detect_provider(repo_url)
    guide_hint = f"\n\n📖 Create a {provider} PAT: {guide_url}" if guide_url else ""

    res = await cl.AskUserMessage(
        content=(
            f"**Step 2 / 2** — Enter your Personal Access Token (PAT) for **{provider}**:{guide_hint}\n\n"
            "_For public repositories, type_ `skip` _to proceed without a token._"
        ),
        timeout=300,
    ).send()
    if not res:
        await cl.Message(content="⏱️ Timed out. Please refresh and try again.").send()
        return None

    pat_input = res["output"].strip()
    pat = "" if pat_input.lower() in ("skip", "none", "") else pat_input

    # Clone
    branch_label = f" @ `{repo_branch}`" if repo_branch else ""
    progress_msg = cl.Message(content=f"⏳ Cloning `{repo_url}`{branch_label}...")
    await progress_msg.send()

    workspace = None
    try:
        workspace = await git_clone(repo_url, pat, base_dir=_WORKSPACE_BASE_DIR, branch=repo_branch)
        # Store PAT in session (in-memory only) for git push later
        if pat:
            cl.user_session.set("git_pat", pat)
            cl.user_session.set("git_repo_url", repo_url)
    except ValueError as e:
        await cl.Message(content=f"❌ {e}").send()
        return None
    finally:
        pat = ""  # clear local variable
    try:
        file_count = sum(len(files) for _, _, files in os.walk(workspace))
        total_bytes = sum(
            os.path.getsize(os.path.join(root, f))
            for root, _, files in os.walk(workspace)
            for f in files
        )
        size_str = f"{total_bytes / (1024*1024):.1f} MB"
        progress_msg.content = f"✅ Cloned successfully ({file_count:,} files, {size_str})"
    except Exception:
        progress_msg.content = "✅ Cloned successfully"
    await progress_msg.update()

    cl.user_session.set("repo_url", repo_url)
    cl.user_session.set("cloned_workspace", workspace)

    return workspace


async def _init_agent_session(workspace: str, git_pat: str = "", git_repo_url: str = "") -> None:
    """Shared agent initialisation after onboarding."""
    project_folder = os.path.basename(workspace)

    # Log PAT availability upfront
    if git_pat:
        logger.info("_init_agent_session: PAT available (length=%d)", len(git_pat))
    else:
        logger.warning("_init_agent_session: No PAT provided")

    try:
        msg = cl.Message(content="⚙️ Initialising review agent...")
        await msg.send()

        checkpointer = await get_checkpointer()
        user_id = cl.user_session.get("user").identifier if cl.user_session.get("user") else "default"
        try:
            db_cfg = await get_config()
        except Exception:
            db_cfg = {}

        agent = await create_review_agent(
            workspace, checkpointer, _store, user_id=user_id,
            iteration_limit=db_cfg.get("iteration_limit"),
        )
        thread_id = str(uuid.uuid4())
        cl.user_session.set("agent", agent)
        cl.user_session.set("thread_id", thread_id)
        cl.user_session.set("workspace", workspace)
        cl.user_session.set("findings", [])
        cl.user_session.set("finding_count", 0)
        cl.user_session.set("review_start_time", None)
        cl.user_session.set("review_scope", "full")

        # Save PAT encrypted in DB keyed by thread_id so git_push can retrieve it
        # regardless of os.environ state (survives subagent context switches)
        if git_pat:
            await save_pat(thread_id, git_pat, git_repo_url or cl.user_session.get("repo_url", ""))
            logger.info("PAT saved to DB for thread %s", thread_id[:8])
            os.environ["_SESSION_GIT_PAT"] = git_pat
            os.environ["_SESSION_GIT_THREAD_ID"] = thread_id
        else:
            logger.warning("No PAT — git_push will require re-clone with a PAT")

        from chainlit.data import get_data_layer as _dl
        dl = _dl()
        if dl:
            # Use a human-readable thread name: "repo-name @ branch-or-date"
            repo_url = cl.user_session.get("repo_url", "")
            repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "") if repo_url else project_folder
            import datetime as _dt
            thread_name = f"{repo_name} — {_dt.datetime.now().strftime('%b %d %H:%M')}"
            await dl.update_thread(
                thread_id=cl.context.session.thread_id,
                name=thread_name,
                metadata={"thread_id": thread_id, "workspace": workspace, "repo_url": repo_url}
            )

        task_list = cl.TaskList()
        await task_list.send()
        cl.user_session.set("task_list", task_list)

        scope_hint = ""
        try:
            import git as _git
            repo = _git.Repo(workspace, search_parent_directories=True)
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

    if not thread_id:
        await cl.Message(content="⚠️ Could not restore session — missing thread ID.").send()
        return

    # If the workspace no longer exists (cloned repo was cleaned up), offer to re-clone
    if not workspace or not os.path.exists(workspace):
        repo_url = meta.get("repo_url", "")
        repo_hint = f" (`{repo_url}`)" if repo_url else ""
        await cl.Message(
            content=(
                f"⚠️ The workspace for this session no longer exists{repo_hint}.\n\n"
                "Cloned repositories are removed when a session ends. "
                "To continue working on this repo, start a **new chat** and clone it again."
            )
        ).send()
        return

    try:
        checkpointer = await get_checkpointer()
        user_id = cl.user_session.get("user").identifier if cl.user_session.get("user") else "default"
        db_cfg = await get_config()
        agent = await create_review_agent(
            workspace, checkpointer, _store, user_id=user_id,
            iteration_limit=db_cfg.get("iteration_limit"),
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

        project_folder = os.path.basename(workspace)
        await cl.Message(
            content=(
                f"✅ Session restored — **`{project_folder}`**\n\n"
                "You can continue asking questions or say **`review`** to run a fresh review."
            )
        ).send()
    except Exception as e:
        await cl.Message(content=f"❌ Error resuming: {e}").send()


@cl.on_chat_end
async def on_chat_end():
    """Clean up cloned workspace and clear PAT from environment and DB."""
    os.environ.pop("_SESSION_GIT_PAT", None)
    os.environ.pop("_SESSION_GIT_REPO_URL", None)
    os.environ.pop("_SESSION_GIT_THREAD_ID", None)
    # Delete encrypted PAT from DB
    thread_id = cl.user_session.get("thread_id")
    if thread_id:
        await delete_pat(thread_id)
    if not cfg_bool("CLEANUP_WORKSPACE_ON_EXIT", True):
        return
    cloned = cl.user_session.get("cloned_workspace")
    if cloned:
        await cleanup_workspace(cloned)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

@cl.on_message
async def main(message: cl.Message):
    agent = cl.user_session.get("agent")
    thread_id = cl.user_session.get("thread_id")
    if not agent:
        await cl.Message(content="Session expired. Please refresh.").send()
        return

    if cl.user_session.get("review_start_time") is None:
        cl.user_session.set("review_start_time", time.monotonic())

    stream_msg = cl.Message(content="🤔 Thinking...")
    await stream_msg.send()

    full_content = ""
    active_steps: dict = {}
    tool_inv_ids: dict = {}
    tool_start_times: dict = {}
    tool_inputs: dict = {}
    all_steps: list = []
    session_findings: list = cl.user_session.get("findings") or []
    finding_count: int = cl.user_session.get("finding_count") or 0
    findings_this_turn: int = 0
    tools_fired: bool = False
    was_cancelled: bool = False

    for agent_key in AGENT_DISPLAY:
        cl.user_session.set(f"banner_shown_{agent_key}", False)

    try:
        input_data = {"messages": [("user", message.content or "")]}

        diff_context = cl.user_session.get("diff_context")
        if diff_context and (message.content or "").strip().lower() in ("review", "start review", "go"):
            input_data = {"messages": [("user",
                f"{message.content}\n\n[SCOPE: Review only the following changed files]\n{diff_context}"
            )]}
            cl.user_session.set("diff_context", None)

        _fixing_ids: set = _parse_fix_targets(message.content or "", session_findings)

        config = {
            "recursion_limit": getattr(agent, "_iteration_limit", 150) * 2,
            "configurable": {"thread_id": thread_id},
        }

        while True:
            async for event in agent.astream_events(input_data, version="v2", config=config):
                kind = event["event"]
                run_id = event["run_id"]
                agent_name = event.get("metadata", {}).get("lc_agent_name") or None

                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"].content
                    if chunk:
                        full_content += chunk
                        if not tools_fired:
                            if stream_msg.content == "🤔 Thinking...":
                                stream_msg.content = ""
                                await stream_msg.update()
                            await stream_msg.stream_token(chunk)

                elif kind == "on_tool_start":
                    tool_name = event["name"]
                    tool_input = event["data"].get("input")
                    tool_inputs[run_id] = tool_input

                    if not tools_fired:
                        tools_fired = True
                        full_content = ""
                        stream_msg.content = ""
                        await stream_msg.update()

                    if agent_name and agent_name in AGENT_DISPLAY:
                        banner_key = f"banner_shown_{agent_name}"
                        if not cl.user_session.get(banner_key):
                            cl.user_session.set(banner_key, True)
                            emoji, display_name = AGENT_DISPLAY[agent_name]
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

                    if tool_name in ("edit_file", "write_file"):
                        inp_dict = tool_input if isinstance(tool_input, dict) else {}
                        fp = (
                            inp_dict.get("file_path") or inp_dict.get("path")
                            or inp_dict.get("target_file") or inp_dict.get("filename", "")
                        )
                        if inp_dict:
                            cl.user_session.set("_last_edit_input", inp_dict)
                        if fp:
                            workspace_path = cl.user_session.get("workspace", "")
                            full_fp = fp if os.path.isabs(fp) else os.path.join(workspace_path, fp)
                            undo_store = cl.user_session.get("undo_store") or {}
                            if tool_name == "edit_file":
                                try:
                                    with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                        original = fh.read()
                                    undo_store[fp] = {"type": "edit", "original": original}
                                except Exception:
                                    pass
                            else:
                                if os.path.exists(full_fp):
                                    try:
                                        with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                            original = fh.read()
                                        undo_store[fp] = {"type": "edit", "original": original}
                                    except Exception:
                                        pass
                                else:
                                    undo_store[fp] = {"type": "new_file"}
                            cl.user_session.set("undo_store", undo_store)

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
                        await step.update()  # update but do NOT remove — steps stay visible

                    try:
                        inv_id = tool_inv_ids.pop(run_id, None)
                        start_t = tool_start_times.pop(run_id, None)
                        if inv_id is not None:
                            dur = (asyncio.get_event_loop().time() - start_t) * 1000 if start_t else 0.0
                            status = "failure" if out_str.startswith("Error") else "success"
                            await record_tool_invocation_end(inv_id, dur, status)
                    except Exception:
                        pass

                    if tool_name in ("edit_file", "write_file"):
                        if not tool_input_data:
                            tool_input_data = cl.user_session.get("_last_edit_input") or {}

                        fp = (
                            tool_input_data.get("file_path") or tool_input_data.get("path")
                            or tool_input_data.get("target_file") or tool_input_data.get("filename", "")
                        )

                        undo_store = cl.user_session.get("undo_store") or {}
                        snap = undo_store.get(fp)

                        if snap is None and fp:
                            fp_base = os.path.basename(fp)
                            for stored_fp, stored_snap in undo_store.items():
                                if os.path.basename(stored_fp) == fp_base:
                                    snap = stored_snap
                                    break

                        if fp and snap is not None:
                            workspace_path = cl.user_session.get("workspace", "")
                            full_fp = fp if os.path.isabs(fp) else os.path.join(workspace_path, fp)
                            try:
                                display_fp = os.path.relpath(full_fp, workspace_path) if workspace_path else fp
                            except ValueError:
                                display_fp = fp
                            try:
                                with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                    new_content = fh.read()
                                snap_type = snap["type"] if isinstance(snap, dict) else "edit"
                                original = snap.get("original", "") if isinstance(snap, dict) else snap
                                if snap_type == "new_file":
                                    added_lines = new_content.splitlines(keepends=True)[:120]
                                    diff_text = f"--- /dev/null\n+++ b/{display_fp}\n"
                                    diff_text += "".join(f"+{line}" for line in added_lines)
                                    if len(new_content.splitlines()) > 120:
                                        diff_text += "\n... [truncated]"
                                    await cl.Message(
                                        content=f"📝 **Created `{display_fp}`**\n```diff\n{diff_text}\n```",
                                        actions=[cl.Action(name="undo", payload={"file": fp, "type": "new_file"}, label="↩️ Delete this file")],
                                    ).send()
                                else:
                                    diff_lines = list(difflib.unified_diff(
                                        original.splitlines(keepends=True),
                                        new_content.splitlines(keepends=True),
                                        fromfile=f"a/{display_fp}", tofile=f"b/{display_fp}", n=3
                                    ))
                                    # Ensure no missing-newline artifacts in the diff
                                    diff_text = "".join(diff_lines[:120])
                                    if diff_text:
                                        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
                                        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
                                        summary = f"  `+{added}` `−{removed}`"
                                        await cl.Message(
                                            content=f"✏️ **Changed `{display_fp}`**{summary}\n```diff\n{diff_text}```",
                                            actions=[cl.Action(name="undo", payload={"file": fp, "type": "edit"}, label="↩️ Undo this change")],
                                        ).send()
                                    else:
                                        await cl.Message(content=f"✏️ **Edited `{display_fp}`** (no diff detected)").send()
                            except Exception as e:
                                logger.warning("diff display failed: %s", e, exc_info=True)
                                await cl.Message(content=f"✏️ **Edited `{display_fp}`**").send()

                        if fp and not out_str.startswith("Error") and _fixing_ids:
                            try:
                                workspace_path = cl.user_session.get("workspace", "")
                                try:
                                    rel_fp = os.path.relpath(fp, workspace_path) if (workspace_path and os.path.isabs(fp)) else fp
                                except ValueError:
                                    rel_fp = fp
                                for fnd in session_findings:
                                    fnd_db_id = fnd.get("db_id") or fnd.get("id")
                                    fnd_seq_id = fnd.get("id")
                                    fnd_file = fnd.get("file_path", "")
                                    file_match = (
                                        fnd_file == rel_fp or fnd_file == fp
                                        or rel_fp.endswith(fnd_file) or fnd_file.endswith(rel_fp.lstrip("/\\"))
                                    )
                                    if file_match and fnd_seq_id in _fixing_ids:
                                        if fnd_db_id:
                                            await db_update_finding_status(fnd_db_id, "fixed")
                                            fnd["status"] = "fixed"
                                cl.user_session.set("findings", session_findings)
                            except Exception:
                                pass

                    if tool_name == "git_commit" and not out_str.startswith("Error"):
                        try:
                            last_fixed = cl.user_session.get("last_fixing_id")
                            if last_fixed:
                                await db_update_finding_status(last_fixed, "fixed")
                                cl.user_session.set("last_fixing_id", None)
                        except Exception:
                            pass

                    if tool_name in ("f", "record_finding") and out_str.startswith("FINDING|"):
                        parts = out_str.split("|", 7)
                        if len(parts) == 8:
                            _, fp, ln, crit, cat, title, desc, sug = parts
                            finding_count += 1
                            findings_this_turn += 1
                            cl.user_session.set("finding_count", finding_count)

                            # Hard limit — stop recording once cap is reached
                            max_findings = cfg_int("MAX_FINDINGS", 50)
                            if finding_count > max_findings:
                                # Don't record to DB or show card — just silently drop
                                logger.info("Finding cap (%d) reached — dropping finding #%d", max_findings, finding_count)
                                finding_count -= 1  # don't count it
                                cl.user_session.set("finding_count", finding_count)
                                continue

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

                            emoji = CRIT_EMOJI.get(crit, "⚪")
                            est_str = f"\n\n⚡ Est. fix: ~{est_tokens:,} tokens" if est_tokens else ""
                            card = (
                                f"{emoji} **#{finding_count} [{CRIT_LABEL.get(crit, crit.upper())}]** · `{cat}`\n"
                                f"**{title}**\n"
                                f"📍 `{fp}:{ln}`\n\n"
                                f"{desc}\n\n"
                                f"💡 **Fix:** {sug}"
                                f"{est_str}"
                            )
                            await cl.Message(content=card, parent_id=stream_msg.id).send()

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
                            approval_content_parts = []

                            for r in reqs:
                                req_name = r.get("name", "")
                                req_args = r.get("args", {}) if isinstance(r.get("args"), dict) else {}

                                if req_name == "edit_file":
                                    fp = (req_args.get("file_path") or req_args.get("path")
                                          or req_args.get("target_file") or req_args.get("filename", ""))
                                    old_str = req_args.get("old_string", req_args.get("old_str", ""))
                                    new_str = req_args.get("new_string", req_args.get("new_str", ""))
                                    if old_str and new_str:
                                        # Ensure both strings end with newline so unified_diff
                                        # produces proper line-by-line output (not a single merged line)
                                        old_norm = old_str if old_str.endswith("\n") else old_str + "\n"
                                        new_norm = new_str if new_str.endswith("\n") else new_str + "\n"
                                        diff_lines = list(difflib.unified_diff(
                                            old_norm.splitlines(keepends=True),
                                            new_norm.splitlines(keepends=True),
                                            fromfile=f"a/{fp}",
                                            tofile=f"b/{fp}",
                                            n=2,
                                        ))
                                        diff_text = "".join(diff_lines[:100])
                                        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
                                        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
                                        summary = f"`+{added}` `−{removed}`"
                                        approval_content_parts.append(
                                            f"✏️ **Edit `{fp}`** — {summary}\n```diff\n{diff_text}```"
                                        )
                                    else:
                                        approval_content_parts.append(f"✏️ **Edit `{fp}`**")

                                elif req_name == "write_file":
                                    fp = (req_args.get("file_path") or req_args.get("path")
                                          or req_args.get("target_file") or req_args.get("filename", ""))
                                    content_preview = str(req_args.get("content", ""))[:400]
                                    added_lines = content_preview.splitlines(keepends=True)[:40]
                                    diff_text = f"--- /dev/null\n+++ b/{fp}\n"
                                    diff_text += "".join(f"+{line}" for line in added_lines)
                                    approval_content_parts.append(
                                        f"📝 **Create `{fp}`**\n```diff\n{diff_text}\n```"
                                    )

                                elif req_name in ("git_commit", "git_create_branch", "git_stash", "git_push"):
                                    approval_content_parts.append(
                                        f"🔧 **`{req_name}`**\n```json\n{json.dumps(req_args, indent=2)}\n```"
                                    )

                                elif req_name == "execute":
                                    cmd = req_args.get("command", str(req_args))
                                    approval_content_parts.append(f"⚡ **Execute**\n```bash\n{cmd}\n```")

                                else:
                                    approval_content_parts.append(
                                        f"🔧 **`{req_name}`**\n```\n{str(req_args)[:300]}\n```"
                                    )

                                # Capture pre-edit snapshot for undo
                                if req_name in ("edit_file", "write_file") and isinstance(req_args, dict):
                                    fp_snap = (req_args.get("file_path") or req_args.get("path")
                                               or req_args.get("target_file") or req_args.get("filename", ""))
                                    cl.user_session.set("_last_edit_input", req_args)
                                    if fp_snap:
                                        workspace_path = cl.user_session.get("workspace", "")
                                        full_fp = fp_snap if os.path.isabs(fp_snap) else os.path.join(workspace_path, fp_snap)
                                        undo_store = cl.user_session.get("undo_store") or {}
                                        if req_name == "edit_file":
                                            try:
                                                with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                                    original = fh.read()
                                                undo_store[fp_snap] = {"type": "edit", "original": original}
                                            except Exception:
                                                pass
                                        else:
                                            if os.path.exists(full_fp):
                                                try:
                                                    with open(full_fp, "r", encoding="utf-8", errors="replace") as fh:
                                                        original = fh.read()
                                                    undo_store[fp_snap] = {"type": "edit", "original": original}
                                                except Exception:
                                                    pass
                                            else:
                                                undo_store[fp_snap] = {"type": "new_file"}
                                        cl.user_session.set("undo_store", undo_store)

                            cmd_str = "\n\n".join(approval_content_parts) if approval_content_parts else str(interrupt_info)
                        else:
                            cmd_str = str(interrupt_info)
                    except Exception:
                        cmd_str = str(interrupt_info)

                    res = await cl.AskActionMessage(
                        content=(
                            f"⚠️ **Approval Required**\n\n"
                            f"{cmd_str}\n\n"
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

    except (asyncio.CancelledError, KeyboardInterrupt):
        was_cancelled = True
        logger.info("Task manually stopped by user (findings so far: %d)", len(session_findings))
        raise

    except Exception as e:
        error_text = f"🚨 {type(e).__name__}: {e}"
        logger.error(error_text, exc_info=True)
        stream_msg.content = error_text
        await stream_msg.update()
        return

    finally:
        try:
            if was_cancelled:
                stream_msg.content = "⏹️ Task manually stopped."
                await asyncio.shield(stream_msg.update())
            elif full_content:
                stream_msg.content = full_content
                await asyncio.shield(stream_msg.update())
            elif not stream_msg.content:
                stream_msg.content = "Done."
                await asyncio.shield(stream_msg.update())
        except Exception:
            pass

        if session_findings:
            try:
                findings_lines = []
                for fnd in session_findings:
                    fid = fnd.get("id", "?")
                    crit = fnd.get("criticality", "info")
                    cat = fnd.get("category", "")
                    title = fnd.get("title", "")
                    fp = fnd.get("file_path", "")
                    ln = fnd.get("line_number", 0)
                    status = fnd.get("status", "open")
                    findings_lines.append(f"  #{fid} [{crit}] {cat} — {title} — {fp}:{ln} ({status})")
                stop_note = "\n\n⚠️ Task stopped manually by the user. The above findings were already shown." if was_cancelled else ""
                memory_summary = (
                    f"[SYSTEM: Review state — {len(session_findings)} findings recorded so far]\n"
                    + "\n".join(findings_lines) + stop_note
                )
                await asyncio.shield(agent.aupdate_state(
                    config, {"messages": [("assistant", memory_summary)]},
                ))
            except Exception as e:
                logger.warning("Failed to inject findings into agent state: %s", e)

        # Steps stay visible — just flush final state, do NOT remove
        for step in all_steps:
            try:
                await step.update()
            except Exception:
                pass

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

            crit_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
            sorted_findings = sorted(session_findings, key=lambda x: crit_order.get(x["criticality"], 5))
            est_lines = []
            for fnd in sorted_findings[:10]:
                est = fnd.get("estimated_fix_tokens")
                est_str = f"~{est:,} tokens" if est else "~500 tokens"
                label = CRIT_LABEL.get(fnd["criticality"], fnd["criticality"].upper())
                est_lines.append(f"  #{fnd['id']} [{label}] `{fnd['file_path']}` — {fnd['title']} — {est_str}")
            est_block = ""
            if est_lines:
                est_block = "\n\n**Est. tokens per fix:**\n" + "\n".join(est_lines)
                if len(sorted_findings) > 10:
                    est_block += f"\n  _(+{len(sorted_findings)-10} more)_"

            try:
                commit_hash = cl.user_session.get("current_commit")
                scope = cl.user_session.get("review_scope", "full")
                workspace = cl.user_session.get("workspace", "")
                await record_review_session(
                    thread_id=thread_id, workspace=workspace,
                    commit_hash=commit_hash, scope=scope, total_findings=len(session_findings),
                )
            except Exception:
                pass

            summary = (
                f"---\n"
                f"✅ **Review Complete** — {len(session_findings)} findings{elapsed}\n\n"
                f"{crit_line}"
                f"{fix_hint}"
                f"{est_block}\n\n"
                f"📊 [View full dashboard]({_APP_BASE_URL}/dashboard)  "
                f"·  [Export HTML](/dashboard/api/reports/html?thread_id={thread_id})"
                f"  ·  [Export XLSX](/dashboard/api/reports/xlsx?thread_id={thread_id})"
            )
            await cl.Message(content=summary).send()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_fix_targets(msg: str, findings: list) -> set:
    import re
    msg_lower = msg.lower().strip()
    if not msg_lower.startswith("fix"):
        return set()
    ids = set()
    if re.search(r"fix\s+(everything|all\s+findings?|all$)", msg_lower):
        return {f["id"] for f in findings}
    crit_match = re.search(r"fix\s+all\s+(critical|high|medium|low|info)", msg_lower)
    if crit_match:
        target_crit = crit_match.group(1)
        return {f["id"] for f in findings if f.get("criticality") == target_crit}
    for m in re.finditer(r"#(\d+)|finding\s+(\d+)", msg_lower):
        n = int(m.group(1) or m.group(2))
        ids.add(n)
    if ids:
        return ids
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
            if os.path.exists(full_fp):
                os.remove(full_fp)
            undo_store.pop(fp, None)
            cl.user_session.set("undo_store", undo_store)
            await cl.Message(content=f"↩️ Deleted `{display_fp}` (creation undone).").send()
        else:
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
    if hasattr(tool_output, "content"):
        content = tool_output.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
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
