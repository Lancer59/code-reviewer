# Code Reviewer — Architecture

## Overview

The system is a single-process Python application that combines a Chainlit chat UI, a FastAPI dashboard, and a LangGraph-based multi-agent review engine. Everything runs on one port (`8001`) via FastAPI's `mount_chainlit` integration.

```
Browser
  │
  ├── /              → Chainlit chat UI  (reviewer_ui.py)
  └── /dashboard     → FastAPI dashboard (dashboard/api.py)
         │
         └── /dashboard/api/*  → REST endpoints
```

---

## Process Architecture

```
uvicorn app:app (port 8001)
│
├── FastAPI app
│   ├── Chainlit mounted at /          ← reviewer_ui.py
│   └── Dashboard routes at /dashboard ← dashboard/api.py
│
├── SQLite databases (agent_data/)
│   ├── chainlit_ui.db     ← Chainlit threads, messages, users
│   ├── checkpoints_lg.db  ← LangGraph state checkpoints
│   └── dashboard.db       ← Findings, LLM calls, tool invocations, sessions
│
└── In-memory LangGraph store (InMemoryStore)
    └── Per-user long-term memory namespace
```

---

## Agent Architecture

```
User message
     │
     ▼
Main Review Agent  (LangGraph ReAct, gpt-4o / gpt-5.3)
│
├── Tools (always available to main agent)
│   ├── f()              — record a finding (compact pipe format)
│   ├── git_status       — read-only git info
│   ├── git_diff         — read-only diff
│   ├── git_log          — read-only history
│   └── git_blame        — read-only authorship
│
├── Backend tools (provided by LocalShellBackend)
│   ├── read_file        — read any file in the workspace
│   ├── grep_search      — regex search across files
│   ├── write_todos      — manage the review task list
│   └── edit_file        — write/patch a file (requires approval)
│
└── Subagents (isolated context windows)
    │
    ├── file-scanner
    │   ├── Purpose: read one file, return all quality issues
    │   ├── Tools: read_file, grep_search
    │   └── Returns: compact findings list
    │
    ├── security-scanner
    │   ├── Purpose: OWASP Top 10 + secrets + injection pass
    │   ├── Tools: read_file, grep_search (security patterns)
    │   └── Returns: security findings list
    │
    └── git-agent
        ├── Purpose: apply fix, branch, commit
        ├── Tools: git_create_branch, git_commit, git_stash,
        │         git_status, git_diff, edit_file
        └── Returns: commit hash + branch name
```

### Why Subagents

Each subagent runs in its own isolated context window. The main agent delegates file reading to `file-scanner` and security scanning to `security-scanner` — it never sees raw file contents. This prevents context bloat: a 500-line file read stays inside the subagent; the main agent gets back 5 compact finding strings.

---

## Data Flow — Review

```
User: "review"
  │
  ▼
reviewer_ui.py: on_message()
  │  builds input_data, injects diff context if scope=diff
  │
  ▼
agent.astream_events()  ← LangGraph streams events
  │
  ├── on_chat_model_stream  → stream tokens to UI (pre-tool only)
  │
  ├── on_tool_start
  │   ├── store tool_input in tool_inputs[run_id]
  │   ├── snapshot file content if edit_file (for undo)
  │   ├── show agent banner (once per agent per turn)
  │   ├── show tool step with descriptive label
  │   └── update progress indicator
  │
  ├── on_tool_end
  │   ├── update step output
  │   ├── record tool invocation duration + status to DB
  │   ├── if edit_file → show diff + undo button, mark findings fixed
  │   └── if f() → parse FINDING|... → save to DB, show finding card
  │
  └── on_chat_model_end → record LLM token usage to DB
  │
  ▼
finally block:
  ├── clear progress indicator
  ├── set final message content
  ├── record review_session to DB
  └── show review summary with per-finding token estimates
```

---

## Data Flow — Fix

```
User: "fix #3"
  │
  ▼
_parse_fix_targets() → {3}  (set of finding IDs to mark fixed)
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
  │
  ▼
on_tool_end(edit_file):
  ├── show unified diff in chat
  ├── show ↩️ Undo button
  └── mark finding #3 as "fixed" in dashboard.db
       (only if file_path matches AND finding ID is in _fixing_ids)
```

---

## Human-in-the-Loop

Write operations pause for user approval before executing. Configured via `.env`:

```
REQUIRE_APPROVAL_GIT_COMMIT=true
REQUIRE_APPROVAL_GIT_BRANCH=true
REQUIRE_APPROVAL_EDIT=true
REQUIRE_APPROVAL_EXECUTE=true
```

When a tool requiring approval is about to run, LangGraph raises an interrupt. The UI detects `state.next` with `tasks[0].interrupts`, shows an `AskActionMessage` with Approve/Reject buttons, then resumes with `Command(resume={"decisions": [{"type": "approve"}]})`.

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
    finding_id,           -- sequential ID within session (1, 2, 3...)
    estimated_fix_tokens, -- (file_size / 4) * 1.5 + 300
    status,               -- open | fixed | dismissed
    agent_name            -- which subagent found it
)

llm_calls (
    id, thread_id, timestamp, model,
    prompt_tokens, completion_tokens, total_tokens,
    agent_name
)

tool_invocations (
    id, thread_id, tool_name, timestamp,
    duration_ms, status     -- success | failure | pending
)

review_sessions (
    id, thread_id, workspace, timestamp,
    commit_hash,            -- git HEAD at review time
    scope,                  -- full | diff
    total_findings, model
)

agent_config (
    id, system_prompt, iteration_limit,
    enabled_tools, llm_provider, model_name
)
```

---

## Finding Format

The `f()` tool uses a compact pipe-delimited format to minimise token overhead:

```
FILE:LINE|CRIT|CAT|TITLE|DESC|FIX

CRIT codes:  C=critical  H=high  M=medium  L=low  I=info
CAT codes:   sec  bug  perf  maint  style  doc

Example:
auth.py:42|C|sec|Hardcoded API key|Key committed to source|Move to os.getenv("API_KEY")
```

The tool schema is ~60 tokens vs ~300 for a 7-parameter version — a 5× reduction that compounds across 20+ findings per review.

The return value is a structured pipe string that the UI layer parses:
```
FINDING|auth.py|42|critical|security|Hardcoded API key|Key committed...|Move to env var
```

---

## Review Scope Selection

On re-review of a known repo, the UI checks `review_sessions` for a previous commit hash and offers:
- **Full review** — entire codebase
- **Diff review** — `git diff <last_commit>..HEAD` injected as context, agent focuses on changed files only

---

## Token Efficiency Summary

| Technique | Saving |
|-----------|--------|
| Compact `f()` tool schema | ~240 tokens per call (5× reduction) |
| Subagent context isolation | File contents never in main context |
| Repo map pruning (150 files, no md/json/yaml/css) | ~200–500 tokens on large repos |
| Tight system prompt (~400 tokens) | ~400 tokens vs ~800 in v1 |
| Suppress intermediate LLM streaming | No wasted render cycles |

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
