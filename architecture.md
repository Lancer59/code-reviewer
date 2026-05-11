# Dev Companion — Architecture

## Overview

A single-process Python application combining a chat UI, a FastAPI dashboard, and a LangGraph-based multi-agent review engine. Everything runs on one port via FastAPI's `mount_chainlit` integration.

```
Browser
  │
  ├── /              → Chat UI        (reviewer_ui.py)
  └── /dashboard     → Dashboard API  (dashboard/api.py)
         └── /dashboard/api/*  → REST endpoints
```

---

## Process Architecture

```
uvicorn app:app  (or  uvicorn run:standalone)
│
├── FastAPI app
│   ├── Chat UI mounted at /          ← reviewer_ui.py
│   ├── Dashboard routes at /dashboard ← dashboard/api.py
│   └── /health                        ← liveness probe
│
├── SQLite databases  (agent_data/ — mount Azure Files here)
│   ├── chainlit_ui.db      ← Chat threads, messages, users
│   ├── checkpoints_lg.db   ← LangGraph state checkpoints
│   └── dashboard.db        ← Findings, LLM calls, tool invocations,
│                              review sessions, encrypted PATs
│
└── In-memory LangGraph store (InMemoryStore)
    └── Per-user long-term memory namespace
```

---

## Onboarding Flow

```
User opens chat
  │
  ├── Step 1: Enter repo URL (+ optional branch)
  │     e.g. "https://github.com/org/repo develop"
  │
  ├── Step 2: Enter PAT (or "skip" for public repos)
  │
  ├── git_clone(url, pat, branch)
  │     └── Clones into workspaces/<repo>-<uuid>/
  │
  ├── save_pat(thread_id, encrypted_pat)  → dashboard.db
  │     └── Fernet encryption, key derived from CHAINLIT_AUTH_SECRET
  │
  └── _init_agent_session(workspace, git_pat, git_repo_url)
        ├── create_review_agent(workspace_path)
        ├── os.environ["_SESSION_GIT_PAT"] = git_pat
        ├── os.environ["_SESSION_GIT_THREAD_ID"] = thread_id
        └── Ready ✅
```

---

## Agent Architecture

```
User message
     │
     ▼
Main Review Agent  (LangGraph ReAct, Azure OpenAI)
│
├── Core tools
│   ├── f()              — record a finding (compact pipe format)
│   ├── git_status       — read-only git info
│   ├── git_diff         — read-only diff
│   ├── git_log          — read-only history
│   └── git_blame        — read-only authorship
│
├── Backend tools  (LocalShellBackend — root_dir = parent of workspace)
│   ├── read_file        — read any file in the workspace
│   ├── grep_search      — regex search across files
│   ├── write_todos      — manage the review task list
│   └── edit_file        — write/patch a file (requires approval)
│
└── Subagents  (isolated context windows)
    │
    ├── file-scanner
    │   ├── Purpose: read one file, return quality issues (max 10/file)
    │   ├── Tools: read_file, grep_search
    │   └── Returns: compact findings list
    │
    ├── security-scanner
    │   ├── Purpose: OWASP Top 10 + secrets + injection pass (max 20 total)
    │   ├── Tools: read_file, grep_search (security patterns)
    │   └── Returns: security findings list
    │
    └── git-agent
        ├── Purpose: apply fix, branch, commit, push
        ├── Tools: git_create_branch, git_commit, git_stash,
        │         git_status, git_diff, git_push
        └── Returns: "Committed and pushed branch <name>: <message>"
```

### Finding Limit

The agent is instructed to stop at `MAX_FINDINGS` (default 50). Priority order: Critical → High → Medium → Low → Info. The UI also enforces this as a hard cap — findings beyond the limit are silently dropped.

---

## Data Flow — Review

```
User: "review"
  │
  ▼
reviewer_ui.py: on_message()
  │
  ▼
agent.astream_events()  ← LangGraph streams events
  │
  ├── on_chat_model_stream  → stream tokens to UI (pre-tool only)
  │
  ├── on_tool_start
  │   ├── snapshot file content if edit_file (for undo)
  │   ├── show agent banner (once per agent per turn)
  │   └── show tool step with descriptive label
  │
  ├── on_tool_end
  │   ├── update step output (steps stay visible — not removed)
  │   ├── record tool invocation duration + status to DB
  │   ├── if edit_file → show colour-coded diff + undo button
  │   └── if f() → parse FINDING|... → save to DB, show finding card
  │
  └── on_chat_model_end → record LLM token usage to DB
  │
  ▼
finally block:
  ├── set final message content
  ├── record review_session to DB
  └── show review summary with per-finding token estimates
```

---

## Data Flow — Fix + Push

```
User: "fix #3"
  │
  ▼
_parse_fix_targets() → {3}
  │
  ▼
Main agent → delegates to git-agent subagent
  │
  ▼
git-agent:
  1. git_create_branch  → "fix/<slug>"       [requires approval]
  2. edit_file          → apply the fix       [requires approval]
  3. git_status         → verify change
  4. git_commit         → conventional commit [requires approval]
  5. git_push           → push to origin      [requires approval]
     │
     └── PAT lookup:
           1. os.environ["_SESSION_GIT_PAT"]   (fast path)
           2. load_pat(thread_id) from DB       (fallback)
           → inject into HTTPS URL for this push only
           → GIT_TERMINAL_PROMPT=0 (no credential manager fallback)
  │
  ▼
on_tool_end(edit_file):
  ├── show unified diff (+N -N) in chat
  ├── show ↩️ Undo button
  └── mark finding #3 as "fixed" in dashboard.db
```

---

## Human-in-the-Loop

Write operations pause for user approval. Configured via env/config:

```
REQUIRE_APPROVAL_GIT_COMMIT=true
REQUIRE_APPROVAL_GIT_BRANCH=true
REQUIRE_APPROVAL_EDIT=true
REQUIRE_APPROVAL_EXECUTE=true
```

The approval UI shows a colour-coded unified diff for `edit_file`, formatted JSON for git operations, and bash for `execute`. The user sees exactly what will change before approving.

---

## PAT Security

```
Onboarding
  └── save_pat(thread_id, pat, repo_url)
        └── Fernet.encrypt(pat)  ← key = SHA-256(CHAINLIT_AUTH_SECRET)
              └── stored in dashboard.db / session_pats table

git_push
  ├── os.environ["_SESSION_GIT_PAT"]  (set at session start)
  └── load_pat(thread_id)             (DB fallback if env is empty)
        └── Fernet.decrypt(token)

on_chat_end
  ├── delete_pat(thread_id)           (removed from DB)
  └── os.environ.pop("_SESSION_GIT_PAT")
```

The PAT is never written to git config, never logged, and never stored in plaintext.

---

## Configuration System

```
config.py  (cfg(), cfg_bool(), cfg_int())
  │
  ├── 1. config.json          ← highest priority
  │         (path: CONFIG_FILE env var, default: ./config.json)
  ├── 2. os.environ / .env
  └── 3. built-in defaults    ← lowest priority
```

When mounting inside a host app, pass `config_file=` to `create_app()` to isolate Dev Companion's config from the host's environment.

---

## Database Schema

### dashboard.db

```sql
review_findings (
    id, thread_id, timestamp,
    file_path, line_number,
    criticality,          -- critical | high | medium | low | info
    category,             -- security | bug | performance | maintainability | style | documentation
    title, description, suggestion,
    finding_id,           -- sequential ID within session
    estimated_fix_tokens,
    status,               -- open | fixed | dismissed
    agent_name,           -- which subagent found it
    workspace             -- project name (basename of workspace path)
)

llm_calls (
    id, thread_id, timestamp, model,
    prompt_tokens, completion_tokens, total_tokens, agent_name
)

tool_invocations (
    id, thread_id, tool_name, timestamp, duration_ms,
    status                -- success | failure | pending
)

review_sessions (
    id, thread_id, workspace, timestamp,
    commit_hash, scope,   -- full | diff
    total_findings, model
)

agent_config (
    id, system_prompt, iteration_limit,
    enabled_tools, llm_provider, model_name
)

session_pats (
    thread_id PRIMARY KEY,
    token,                -- Fernet-encrypted PAT
    repo_url,
    created_at
)
```

---

## Finding Format

The `f()` tool uses a compact pipe-delimited format:

```
FILE:LINE|CRIT|CAT|TITLE|DESC|FIX

CRIT codes:  C=critical  H=high  M=medium  L=low  I=info
CAT codes:   sec  bug  perf  maint  style  doc

Example:
auth.py:42|C|sec|Hardcoded API key|Key committed to source|Move to os.getenv("API_KEY")
```

The tool schema is ~60 tokens vs ~300 for a 7-parameter version — 5× reduction that compounds across many findings per review.

Return value parsed by the UI:
```
FINDING|auth.py|42|critical|security|Hardcoded API key|Key committed...|Move to env var
```

---

## Workspace Safety

All git write tools (`git_create_branch`, `git_commit`, `git_push`, `git_stash`) call `_safe_repo()` which validates that `repo_path` is inside `WORKSPACE_BASE_DIR` before opening the repository. Any path outside the workspaces directory raises a `Security violation` error — preventing the agent from accidentally operating on the code-reviewer repo itself or any other path on the system.

---

## Dashboard API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dashboard/api/findings` | All findings (filterable) |
| GET | `/dashboard/api/findings/summary` | Counts by criticality + category |
| GET | `/dashboard/api/findings/by-file` | Top 10 files by finding count |
| GET | `/dashboard/api/findings/trend` | Findings count by date |
| GET | `/dashboard/api/findings/heatmap` | Category × criticality matrix |
| PATCH | `/dashboard/api/findings/{id}/status` | Mark fixed / dismissed |
| GET | `/dashboard/api/sessions` | Review sessions list |
| GET | `/dashboard/api/telemetry/summary` | Token totals |
| GET | `/dashboard/api/telemetry/tokens-over-time` | Tokens by date |
| GET | `/dashboard/api/telemetry/tools` | Tool invocation stats |
| GET | `/dashboard/api/telemetry/models` | Model distribution |
| GET | `/dashboard/api/telemetry/efficiency` | Tokens per finding per session |
| GET | `/dashboard/api/telemetry/sessions/{thread_id}` | Session drilldown |
| GET | `/dashboard/api/reports/html` | Download HTML report |
| GET | `/dashboard/api/reports/xlsx` | Download XLSX report |
| GET | `/dashboard/api/config` | Get agent config |
| PUT | `/dashboard/api/config` | Update agent config |
| GET | `/health` | Liveness probe |
