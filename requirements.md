# Code Reviewer — Feature Requirements

**Project:** code-reviewer (standalone deepagents + Chainlit)  
**Status:** v1 built, v2 planned  
**Base:** Existing `review_agent.py`, `reviewer_ui.py`, `dashboard/` already implemented

---

## Current State (v1 — Already Built)

- DeepAgent reviews a codebase and calls `record_finding` for each issue
- Findings stored in SQLite with criticality / category / file / line
- Chainlit UI shows inline finding cards during review
- Dashboard: Findings tab (table + donut + bar), Observability tab (tokens, tools), Settings tab
- Session summary at end of review

---

## v2 Requirements

---

### 0. Token Efficiency

Token efficiency is a first-class concern. Every design decision should minimise tokens without sacrificing quality.

#### 0.1 Compact `record_finding` Tool

**Problem:** The current `record_finding` tool has 7 named parameters with verbose docstrings. The tool schema is sent on every LLM call (~300 tokens per call). With 20+ findings per review, the schema overhead alone is significant.

**Solution — compact pipe-delimited single-string format:**

```python
@tool
def f(finding: str) -> str:
    """Record a finding. Format: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
    CRIT: C=critical H=high M=medium L=low I=info
    CAT: sec bug perf maint style doc
    Example: auth.py:42|C|sec|Hardcoded secret|API key in source|Move to env var"""
```

Benefits:
- Tool schema shrinks from ~300 tokens to ~60 tokens — **5× reduction**
- Single string argument instead of 7 named args — fewer tokens per call
- The agent learns the compact format from the system prompt examples, not the docstring
- Parsing happens server-side in the UI layer (same pipe-split logic, just shorter keys)

Criticality codes: `C` `H` `M` `L` `I`  
Category codes: `sec` `bug` `perf` `maint` `style` `doc`

#### 0.2 Tight System Prompt

The current system prompt is ~800 tokens. Target: ~400 tokens.

Rules:
- Remove redundant explanations — the agent already knows what security/bugs are
- Use bullet lists not prose
- Move category/criticality definitions to a compact reference table, not paragraphs
- Remove "Rules" section — fold into the workflow steps

#### 0.3 Subagent Context Isolation

Large file reads pollute the main agent's context. Subagents (see §5) isolate this — the main agent only sees the final compact findings list, not the raw file contents.

#### 0.4 Repo Map Pruning

Current repo map includes all source extensions. For large repos this can be 500+ tokens.

Changes:
- Cap at 150 files (down from 300)
- Exclude `.md`, `.json`, `.yaml`, `.toml`, `.css` from the map (still reviewable on demand, just not listed upfront)
- Show file sizes next to filenames so the agent can prioritise which files to read

---

### 1. Enhanced Observability Dashboard

**Status:** Partially built — needs more depth

#### 1.1 Additional Metric Cards
Add to the Observability tab:

| Metric | Source |
|--------|--------|
| Avg tokens per review session | `llm_calls` grouped by `thread_id` |
| Total review sessions | distinct `thread_id` count |
| Avg findings per session | `review_findings` / sessions |
| Most expensive session (tokens) | max `total_tokens` per `thread_id` |
| Token efficiency ratio | findings / total_tokens (higher = more efficient) |

#### 1.2 Additional Charts

| Chart | Type | Data |
|-------|------|------|
| Prompt vs Completion token split (stacked bar over time) | Stacked bar | `llm_calls` by date |
| Model distribution | Doughnut | `llm_calls` grouped by `model` |
| Tool call success vs failure rate | Grouped bar | `tool_invocations` by `tool_name` |
| Findings trend over time | Line | `review_findings` by date |
| Top 10 files by finding count | Horizontal bar | `review_findings` grouped by `file_path` |
| Findings heatmap by category × criticality | CSS grid heatmap | `review_findings` cross-tab |
| Tokens per finding (efficiency) | Line | `total_tokens / finding_count` per session |
| Subagent token breakdown | Stacked bar | tokens by `agent_name` per session |

#### 1.3 Session Detail Drilldown
Clicking a session opens a detail panel:
- All LLM calls (timestamp, model, tokens, agent_name)
- All tool invocations (tool name, duration, status, agent_name)
- All findings for that session
- Total cost estimate (tokens × configurable $/1k rate)
- Subagent breakdown: which agent used how many tokens

**New API endpoints:**
```
GET /dashboard/api/findings/by-file
GET /dashboard/api/findings/trend
GET /dashboard/api/findings/heatmap
GET /dashboard/api/telemetry/sessions/{thread_id}   ← extend existing
```

---

### 2. Report Export (HTML + XLSX)

**Status:** Not built

#### 2.1 HTML Report
Self-contained offline HTML. Content:
- Header: project name, review date, model, total findings
- Executive summary: findings by criticality (coloured cards), code health score
- Findings table: sortable, filterable
- Per-file breakdown: accordion sections
- Appendix: full description + suggestion per finding

Endpoint: `GET /dashboard/api/reports/html?thread_id=<id>`  
Implementation: Python string templates, no Jinja2, inline CSS + vanilla JS.

#### 2.2 XLSX Report
Sheets: Summary, All Findings, By File, Observability.  
Dependency: `openpyxl`. Conditional formatting on criticality column.  
Endpoint: `GET /dashboard/api/reports/xlsx?thread_id=<id>`

---

### 3. Post-Review Fix Workflow

**Status:** Not built

After review, agent sends:
```
Review complete. 12 issues (2 critical, 4 high, 6 medium).

Fix options:
  • "fix everything"
  • "fix all critical"
  • "fix #3" or "fix auth.py"

Est. tokens per fix:
  #1 [C] config.py:12 Hardcoded secret — ~800 tokens
  #2 [C] db.py:88 SQL injection — ~1,200 tokens
```

Token estimate formula: `(file_size_chars / 4) * 1.5 + 300`

Fix modes: everything / by criticality / by ID / by file / multiple IDs.

Finding IDs are sequential per session. Cards show `#<id>` prefix.

---

### 4. Git Integration

**Status:** Not built

#### 4.1 Git Tools (read-only for review, write for fix)
Copy from ai-intern, standalone. Included: `git_status`, `git_diff`, `git_log`, `git_blame`, `git_create_branch`, `git_commit`, `git_stash`. Excluded: `git_push`, `git_pull`, `git_clone`.

#### 4.2 Review Scope Selection
On re-review of a known repo, ask:
- A) Review only changes since last review (`git diff <last_commit>..HEAD`)
- B) Full review

Store `last_reviewed_commit` in `review_sessions` table.

#### 4.3 Diff Screen with Undo
After each fix, show `DiffViewer` with Undo button. Undo restores from pre-fix content stored in session state.

#### 4.4 Review Sessions Table
```sql
CREATE TABLE IF NOT EXISTS review_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL, workspace TEXT NOT NULL,
    timestamp TEXT NOT NULL, commit_hash TEXT,
    scope TEXT, total_findings INTEGER, model TEXT
);
```

---

### 5. Multi-Agent Architecture

**Status:** Not built

deepagents supports subagents natively via the `subagents=` parameter on `create_deep_agent`. Each subagent has its own tool set, system prompt, and isolated context window — the main agent only sees the final result, not intermediate tool calls.

#### 5.1 Why Subagents Here

The main review agent currently reads entire files into its context. A 500-file repo with 200-line average files = massive context bloat. Subagents solve this by doing the heavy reading in isolation and returning only compact findings.

#### 5.2 Proposed Subagents

**A. File Scanner Subagent**
- **Purpose:** Read a single file, identify all issues, return compact findings list
- **Tools:** `read_file`, `grep_search` (file-scoped)
- **System prompt:** Focused on extracting issues from a single file, returning them in the compact `FILE:LINE|CRIT|CAT|TITLE|DESC|FIX` format
- **Model:** Can use a cheaper/faster model (e.g. `gpt-4o-mini`) since it's pattern-matching, not reasoning
- **Structured output:** Returns `list[FindingSchema]` (Pydantic) — main agent gets clean JSON, not raw text
- **Token benefit:** Main agent never sees file contents. A 500-line file read stays inside the subagent. Main agent gets back 5 compact finding strings.

**B. Security Scanner Subagent**
- **Purpose:** Dedicated security pass — OWASP Top 10, secrets, injection, auth issues
- **Tools:** `read_file`, `grep_search` (patterns: `password`, `secret`, `token`, `eval`, `exec`, `sql`, `query`)
- **System prompt:** Security-only, OWASP-aware, aggressive about flagging anything suspicious
- **Model:** Can use a reasoning model (o3-mini) for this pass since security requires deeper analysis
- **Structured output:** Returns `list[SecurityFindingSchema]`
- **Token benefit:** Security pass is isolated — its grep outputs and file reads don't pollute the main review context

**C. Git Subagent**
- **Purpose:** Handle all git operations during the fix workflow
- **Tools:** `git_status`, `git_diff`, `git_create_branch`, `git_commit`, `git_stash`, `edit_file`
- **System prompt:** "You apply code fixes and commit them. Create a branch, apply the fix, verify it compiles/lints, commit with a conventional-commits message."
- **Structured output:** Returns `{"action": "committed", "branch": "...", "hash": "...", "files_changed": [...], "diff": "..."}`
- **Token benefit:** All the git tool calls (status, diff, branch, edit, commit) stay inside the subagent. Main agent gets one clean JSON result and renders the DiffViewer.

**D. Report Generator Subagent**
- **Purpose:** Generate HTML/XLSX report content in isolation
- **Tools:** `read_file` (to read findings from DB export), `write_file` (to write report to disk)
- **System prompt:** "Generate a professional code review report from the provided findings JSON."
- **Model:** Can use a cheaper model — report generation is templating, not reasoning
- **Token benefit:** Report generation involves lots of string manipulation and file I/O. Keeping it isolated prevents the main context from filling with report content.

#### 5.3 Architecture Diagram

```
Main Review Agent
├── write_todos, record_finding (compact)
├── Delegates file reading → File Scanner Subagent
│     └── read_file, grep_search → returns [FindingSchema]
├── Delegates security pass → Security Scanner Subagent
│     └── read_file, grep_search (security patterns) → returns [SecurityFindingSchema]
├── Delegates fix + commit → Git Subagent
│     └── git_*, edit_file → returns {committed, hash, diff}
└── Delegates report → Report Generator Subagent
      └── write_file → returns {report_path}
```

#### 5.4 Implementation Notes

```python
# In create_review_agent():
subagents = [
    {
        "name": "file-scanner",
        "description": "Reads a single source file and returns all code issues found in it.",
        "system_prompt": FILE_SCANNER_PROMPT,
        "tools": [read_file_tool, grep_search_tool],
        "model": "openai:gpt-4o-mini",  # cheaper for pattern matching
        "response_format": FindingsList,  # Pydantic structured output
    },
    {
        "name": "security-scanner",
        "description": "Performs a dedicated security review pass on the codebase.",
        "system_prompt": SECURITY_SCANNER_PROMPT,
        "tools": [read_file_tool, grep_search_tool],
        "model": "openai:o3-mini",  # reasoning model for security
        "response_format": FindingsList,
    },
    {
        "name": "git-agent",
        "description": "Applies a code fix, creates a branch, and commits the change.",
        "system_prompt": GIT_AGENT_PROMPT,
        "tools": [git_status, git_diff, git_create_branch, git_commit, git_stash, edit_file_tool],
        "response_format": GitActionResult,
    },
    {
        "name": "report-generator",
        "description": "Generates an HTML or XLSX report from the review findings.",
        "system_prompt": REPORT_GENERATOR_PROMPT,
        "tools": [write_file_tool],
        "model": "openai:gpt-4o-mini",
    },
]
```

The main agent's system prompt instructs it to delegate file reading to `file-scanner` and security to `security-scanner`, keeping its own context clean for coordination and final summary.

---

### 6. DB Schema Changes

```sql
-- review_findings: add compact fields
ALTER TABLE review_findings ADD COLUMN finding_id INTEGER;
ALTER TABLE review_findings ADD COLUMN estimated_fix_tokens INTEGER;
ALTER TABLE review_findings ADD COLUMN status TEXT DEFAULT 'open';
ALTER TABLE review_findings ADD COLUMN agent_name TEXT;  -- which subagent found it

-- llm_calls: track which agent made the call
ALTER TABLE llm_calls ADD COLUMN agent_name TEXT;

-- New: review_sessions
CREATE TABLE IF NOT EXISTS review_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL, workspace TEXT NOT NULL,
    timestamp TEXT NOT NULL, commit_hash TEXT,
    scope TEXT, total_findings INTEGER, model TEXT
);
```

---

### 7. New API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dashboard/api/findings/by-file` | Findings grouped by file_path |
| GET | `/dashboard/api/findings/trend` | Findings count by date |
| GET | `/dashboard/api/findings/heatmap` | category × criticality matrix |
| GET | `/dashboard/api/reports/html` | Download HTML report |
| GET | `/dashboard/api/reports/xlsx` | Download XLSX report |
| GET | `/dashboard/api/sessions` | Review sessions list |
| PATCH | `/dashboard/api/findings/{id}/status` | Mark finding fixed/dismissed |

---

### 8. New Dependencies

| Package | Purpose |
|---------|---------|
| `openpyxl>=3.1.0` | XLSX report generation |
| `gitpython>=3.1.40` | Git integration |

---

### 9. Implementation Priority

| Priority | Feature | Effort |
|----------|---------|--------|
| 1 | Compact `record_finding` + tight system prompt (§0) | Low — high token ROI |
| 2 | File Scanner + Security Scanner subagents (§5.2 A+B) | Medium |
| 3 | Enhanced observability dashboard (§1) | Medium |
| 4 | HTML + XLSX report export (§2) | Medium |
| 5 | Post-review fix workflow (§3) | Medium |
| 6 | Git subagent + diff/undo (§4 + §5.2 C) | Medium |
| 7 | Report Generator subagent (§5.2 D) | Low |
| 8 | Review scope selection (§4.2) | Medium |

---

### 9b. UI — Agent & Tool Activity Display

**Status:** Not built (v1 has basic step labels only)

The Chainlit UI must clearly communicate what is happening at every moment. Users should never wonder "is it stuck?" or "which agent is running?".

#### Agent Transition Banners

When the main agent delegates to a subagent, show a prominent banner:

```
┌─────────────────────────────────────────────────────┐
│  🔍 File Scanner  →  reading auth/login.py           │
└─────────────────────────────────────────────────────┘
```

```
┌─────────────────────────────────────────────────────┐
│  🔒 Security Scanner  →  scanning for OWASP issues   │
└─────────────────────────────────────────────────────┘
```

```
┌─────────────────────────────────────────────────────┐
│  🔧 Git Agent  →  creating branch fix/hardcoded-key  │
└─────────────────────────────────────────────────────┘
```

Implementation: intercept `on_tool_start` for the `task` tool (which is how deepagents calls subagents). The tool input contains the subagent name — use it to render a `cl.Message` with a styled header before the step appears.

#### Tool Step Labels — Full Map

Every tool call gets a descriptive emoji label. No raw tool names shown to the user.

| Tool | Label |
|------|-------|
| `ls` | `📂 Listing files...` |
| `read_file` | `📄 Reading {filename}` |
| `grep_search` | `🔎 Searching for {pattern}` |
| `write_todos` | `📋 Updating plan...` |
| `record_finding` | `📝 Recording finding #{n}` |
| `edit_file` | `✏️ Editing {filename}` |
| `write_file` | `📝 Creating {filename}` |
| `execute` | `⚡ Running command` |
| `git_status` | `🔍 Checking git status` |
| `git_diff` | `📊 Reading diff` |
| `git_create_branch` | `🌿 Creating branch {name}` |
| `git_commit` | `💾 Committing changes` |
| `git_stash` | `📦 Stashing changes` |
| `task` | `🤖 Delegating to {subagent_name}` |

For `read_file` and `grep_search`, extract the filename/pattern from `tool_input` and include it in the label so the user sees exactly what is being read.

#### Progress Indicator During Review

While the review is running, show a live progress line below the stream message:

```
🔍 Reviewing... 14 findings so far  |  security ✓  bugs ✓  performance →
```

Updated after each `record_finding` call and each todo item completion.

Implementation: maintain a `review_progress` dict in `cl.user_session` tracking:
- `findings_count` — incremented on each `record_finding`
- `completed_passes` — list of todo items marked `done`
- `current_pass` — current `in_progress` todo item

Render as a `cl.Text` element that gets updated in-place.

#### Finding Cards — Rich Format

Current finding cards are plain markdown. Replace with a structured card:

```
┌──────────────────────────────────────────────────────────────┐
│  🔴 #1  CRITICAL · security                                   │
│  Hardcoded API key in source code                            │
│  ─────────────────────────────────────────────────────────── │
│  📍 config.py : line 42                                      │
│  ─────────────────────────────────────────────────────────── │
│  The API key is committed directly to source control and     │
│  will be exposed in git history.                             │
│  ─────────────────────────────────────────────────────────── │
│  💡 Move to environment variable: os.getenv("API_KEY")       │
│  ─────────────────────────────────────────────────────────── │
│  [Fix this]  [Dismiss]                                       │
└──────────────────────────────────────────────────────────────┘
```

Implementation: use `cl.CustomElement` with a `FindingCard` JSX component (similar to existing `DiffViewer` and `TerminalOutput`). Props: `{ id, criticality, category, file_path, line_number, title, description, suggestion }`.

The "Fix this" button triggers the fix workflow for that specific finding ID. "Dismiss" marks it dismissed in the DB.

#### Review Complete Summary Card

After the review, instead of a plain markdown summary, render a structured summary card:

```
┌──────────────────────────────────────────────────────────────┐
│  ✅ Review Complete — my-project                             │
│  ─────────────────────────────────────────────────────────── │
│  🔴 Critical: 2    🟠 High: 4    🟡 Medium: 6    🔵 Low: 3  │
│  ─────────────────────────────────────────────────────────── │
│  Code Health: 6.5 / 10                                       │
│  Tokens used: 24,800  |  Duration: 3m 42s                    │
│  ─────────────────────────────────────────────────────────── │
│  [Fix all critical & high]  [Export HTML]  [Export XLSX]     │
└──────────────────────────────────────────────────────────────┘
```

Implementation: `cl.CustomElement` with a `ReviewSummary` JSX component. Action buttons call back into the agent via `cl.Action`.

#### Subagent Activity in Steps

When a subagent is running, its internal tool calls appear as nested steps under the parent `task` step:

```
▼ 🤖 Delegating to file-scanner  (auth/login.py)
    ├── 📄 Reading auth/login.py
    ├── 🔎 Searching for password patterns
    └── 📝 Recording finding #3
```

This is already how deepagents streams subagent events — the `lc_agent_name` metadata on each event identifies which agent fired it. Use this to nest steps correctly under the parent `task` step.

#### New JSX Custom Elements Needed

| Component | File | Purpose |
|-----------|------|---------|
| `FindingCard` | `public/elements/FindingCard.jsx` | Rich finding card with Fix/Dismiss buttons |
| `ReviewSummary` | `public/elements/ReviewSummary.jsx` | End-of-review summary with action buttons |
| `AgentBanner` | `public/elements/AgentBanner.jsx` | Agent transition banner (optional — can be plain `cl.Message`) |

---

### 9d. Git Authentication

**Status:** Not built

All git auth is configured via `.env` — no settings page, no runtime prompts.

#### Supported Auth Types

| Type | When to use | Env vars |
|------|-------------|----------|
| `none` | Local repos only, no remote push/pull | — |
| `https_token` | GitHub/GitLab/Bitbucket HTTPS with PAT | `GIT_AUTH_TYPE=https_token`, `GIT_TOKEN` |
| `https_basic` | HTTPS with username + password | `GIT_AUTH_TYPE=https_basic`, `GIT_USERNAME`, `GIT_PASSWORD` |
| `ssh` | SSH key auth | `GIT_AUTH_TYPE=ssh`, `GIT_SSH_KEY_PATH` (optional, defaults to `~/.ssh/id_rsa`) |

Default: `GIT_AUTH_TYPE=none` — local operations only, no remote auth configured.

#### Implementation

A `_configure_git_auth(repo: Repo)` helper in `tools/git_tools.py` reads env vars and configures the repo's remote URL or SSH command before any push/pull operation:

```python
def _configure_git_auth(repo: Repo) -> None:
    """Configure git remote auth from env vars. Called before push/pull."""
    auth_type = os.getenv("GIT_AUTH_TYPE", "none").lower()

    if auth_type == "https_token":
        token = os.getenv("GIT_TOKEN", "")
        if token and repo.remotes:
            url = repo.remotes["origin"].url
            # Inject token into HTTPS URL: https://token@github.com/...
            if url.startswith("https://"):
                authed = url.replace("https://", f"https://{token}@")
                repo.remotes["origin"].set_url(authed)

    elif auth_type == "https_basic":
        username = os.getenv("GIT_USERNAME", "")
        password = os.getenv("GIT_PASSWORD", "")
        if username and password and repo.remotes:
            url = repo.remotes["origin"].url
            if url.startswith("https://"):
                authed = url.replace("https://", f"https://{username}:{password}@")
                repo.remotes["origin"].set_url(authed)

    elif auth_type == "ssh":
        key_path = os.getenv("GIT_SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
        # Set GIT_SSH_COMMAND so GitPython uses the specified key
        os.environ["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"
```

This helper is called inside `git_push` and `git_pull` before the remote operation. It is NOT called for local operations (commit, branch, stash, diff, log).

#### Security Notes

- `GIT_TOKEN` and `GIT_PASSWORD` are treated as secrets — the `PromptDebugCallback` must NOT print these env vars even when `DEBUG_PRINT_PROMPT=true`
- The injected URL with credentials is never logged or shown in the UI
- For `https_token`, prefer fine-grained PATs scoped to the specific repo
- For `ssh`, the key file must be readable by the process user; `StrictHostKeyChecking=no` is set for CI compatibility but can be tightened

#### .env Configuration

```bash
# Git authentication type: none | https_token | https_basic | ssh
GIT_AUTH_TYPE=none

# HTTPS token auth (GitHub PAT, GitLab token, etc.)
# GIT_TOKEN=ghp_your_personal_access_token

# HTTPS basic auth
# GIT_USERNAME=your_username
# GIT_PASSWORD=your_password

# SSH key auth (defaults to ~/.ssh/id_rsa if not set)
# GIT_SSH_KEY_PATH=/path/to/your/private_key
```

---

### 9c. Human-in-the-Loop Approvals

**Status:** Not built

#### Overview

The agent must not autonomously apply fixes or run git operations without explicit user approval. This mirrors the ai-intern pattern (`interrupt_on` in `create_deep_agent`) but is configurable entirely via `.env` — no settings page needed.

#### Approval Gates

| Tool | Default | Env var to disable |
|------|---------|-------------------|
| `git_commit` | ✅ requires approval | `REQUIRE_APPROVAL_GIT_COMMIT=false` |
| `git_create_branch` | ✅ requires approval | `REQUIRE_APPROVAL_GIT_BRANCH=false` |
| `git_stash` | ✅ requires approval | `REQUIRE_APPROVAL_GIT_STASH=false` |
| `edit_file` (fix mode) | ✅ requires approval | `REQUIRE_APPROVAL_EDIT=false` |
| `execute` | ✅ requires approval | `REQUIRE_APPROVAL_EXECUTE=false` |

Read-only tools (`read_file`, `grep_search`, `ls`, `git_status`, `git_diff`, `git_log`, `git_blame`, `record_finding`) never require approval.

#### Implementation

`create_review_agent()` reads env vars at startup and builds the `interrupt_on` dict:

```python
def _build_interrupt_on() -> dict:
    """Build interrupt_on config from env vars. All write tools require approval by default."""
    gates = {
        "git_commit":        os.getenv("REQUIRE_APPROVAL_GIT_COMMIT",  "true").lower() != "false",
        "git_create_branch": os.getenv("REQUIRE_APPROVAL_GIT_BRANCH",  "true").lower() != "false",
        "git_stash":         os.getenv("REQUIRE_APPROVAL_GIT_STASH",   "true").lower() != "false",
        "edit_file":         os.getenv("REQUIRE_APPROVAL_EDIT",        "true").lower() != "false",
        "execute":           os.getenv("REQUIRE_APPROVAL_EXECUTE",     "true").lower() != "false",
    }
    return {
        name: {"allowed_decisions": ["approve", "reject"]}
        for name, required in gates.items()
        if required
    }
```

Passed to `create_deep_agent(interrupt_on=_build_interrupt_on())`.

The Git subagent inherits `interrupt_on` from the main agent by default (deepagents behaviour). This means `git_commit` inside the git-agent subagent also pauses for approval.

#### Approval UI

When a tool requiring approval is about to run, the Chainlit UI shows:

```
⚠️ Approval Required

git_commit
  Branch: fix/hardcoded-api-key
  Message: "fix(security): move API key to environment variable"
  Files: config.py

  [✅ Approve]  [❌ Reject]
```

Same `cl.AskActionMessage` pattern as ai-intern. The command string is extracted from `interrupt_info["action_requests"]`.

#### .env Configuration

```bash
# Human-in-the-loop approvals (all default to true — set false to disable)
REQUIRE_APPROVAL_GIT_COMMIT=true
REQUIRE_APPROVAL_GIT_BRANCH=true
REQUIRE_APPROVAL_GIT_STASH=true
REQUIRE_APPROVAL_EDIT=true
REQUIRE_APPROVAL_EXECUTE=true
```

Setting any to `false` lets that tool run autonomously. Useful for trusted CI environments where you want fully automated fixes.

---

### 10. Out of Scope for v2

- `git_push` / `git_pull` / `git_clone`
- Multi-user / multi-tenant
- IDE plugin
- PR/MR creation on GitHub/GitLab


- DeepAgent reviews a codebase and calls `record_finding` for each issue
- Findings stored in SQLite with criticality / category / file / line
- Chainlit UI shows inline finding cards during review
- Dashboard: Findings tab (table + donut + bar), Observability tab (tokens, tools), Settings tab
- Session summary at end of review

---

## v2 Requirements

---

### 1. Enhanced Observability Dashboard

**Status:** Partially built — needs more depth

#### 1.1 Additional Metric Cards
Add to the Observability tab:

| Metric | Source |
|--------|--------|
| Avg tokens per review session | `llm_calls` grouped by `thread_id` |
| Total review sessions | distinct `thread_id` count |
| Avg findings per session | `review_findings` / sessions |
| Most expensive session (tokens) | max `total_tokens` per `thread_id` |

#### 1.2 Additional Charts
All charts use Chart.js, dark theme consistent with existing style.

| Chart | Type | Data |
|-------|------|------|
| Prompt vs Completion token split (stacked bar over time) | Stacked bar | `llm_calls` by date |
| Model distribution (which models used) | Doughnut | `llm_calls` grouped by `model` |
| Tool call success vs failure rate | Grouped bar | `tool_invocations` by `tool_name` |
| Findings trend over time | Line | `review_findings` by date |
| Top 10 files by finding count | Horizontal bar | `review_findings` grouped by `file_path` |
| Findings heatmap by category × criticality | Table heatmap (CSS grid, no lib needed) | `review_findings` cross-tab |

#### 1.3 Session Detail Drilldown
Clicking a session in the Sessions list opens a detail panel showing:
- All LLM calls for that session (timestamp, model, tokens)
- All tool invocations (tool name, duration, status)
- All findings for that session (same table as Findings tab but scoped)
- Total cost estimate (tokens × configurable $/1k rate)

**New API endpoints needed:**
```
GET /dashboard/api/telemetry/sessions/{thread_id}   ← already exists, extend it
GET /dashboard/api/telemetry/models                 ← already exists
GET /dashboard/api/findings/by-file                 ← new: findings grouped by file_path
GET /dashboard/api/findings/trend                   ← new: findings count by date
GET /dashboard/api/findings/heatmap                 ← new: category × criticality matrix
```

---

### 2. Report Export (HTML + XLSX)

**Status:** Not built

#### 2.1 HTML Report
A self-contained single-file HTML report that can be opened offline.

Content:
- Header: project name, review date, reviewer (model name), total findings
- Executive summary: findings by criticality (coloured cards), code health score
- Findings table: sortable, filterable, same columns as dashboard
- Per-file breakdown: accordion sections per file with its findings
- Appendix: full description + suggestion for each finding

**Implementation:**
- New endpoint: `GET /dashboard/api/reports/html?thread_id=<id>`
- Returns `Content-Disposition: attachment; filename="review_<date>.html"`
- Generated server-side using Python string templates (no Jinja2 dependency needed)
- Inline CSS + vanilla JS for sorting/filtering (no external deps, fully offline)

#### 2.2 XLSX Report
Excel workbook with multiple sheets.

| Sheet | Content |
|-------|---------|
| Summary | Counts by criticality and category, code health score |
| All Findings | Full findings table (id, file, line, criticality, category, title, description, suggestion) |
| By File | Findings grouped by file with subtotals |
| Observability | Token usage, LLM calls, tool invocations for the session |

**Implementation:**
- Dependency: `openpyxl` (pure Python, no C extensions)
- New endpoint: `GET /dashboard/api/reports/xlsx?thread_id=<id>`
- Returns `Content-Disposition: attachment; filename="review_<date>.xlsx"`
- Conditional formatting: criticality column cells coloured (red/orange/yellow/blue/grey)

**Dashboard UI changes:**
- Add "Export HTML" and "Export XLSX" buttons to the Findings tab header
- Both buttons pass the currently selected `thread_id` filter (or all if none selected)

---

### 3. Post-Review Fix Workflow

**Status:** Not built

After a review completes, the agent sends a structured follow-up message:

```
Review complete. Found 12 issues (2 critical, 4 high, 6 medium).

Would you like me to fix any of these?

Options:
  • Fix everything — I'll address all findings in priority order
  • Fix by criticality — e.g. "fix all critical and high"
  • Fix a specific finding — share the finding ID (e.g. #3) or describe it
  • Fix a specific file — e.g. "fix everything in auth.py"

Estimated tokens per fix:
  #1 [CRITICAL] Hardcoded secret in config.py — ~800 tokens
  #2 [CRITICAL] SQL injection in user_query() — ~1,200 tokens
  #3 [HIGH] Missing input validation in api/routes.py — ~600 tokens
  ...
```

#### 3.1 Token Estimate per Finding
Estimate = `(file_size_in_chars / 4) * 1.5 + 300` (read file + edit + verify overhead).
This is a rough estimate shown to the user before they commit to a fix.

The estimate is computed at review-end time by reading file sizes from the workspace.

#### 3.2 Fix Modes
| User says | Agent behaviour |
|-----------|----------------|
| "fix everything" | Fixes all findings in order: critical → high → medium → low |
| "fix #3" or "fix finding 3" | Fixes only that finding (matched by ID from the session) |
| "fix all critical" | Fixes all findings with `criticality = critical` |
| "fix auth.py" | Fixes all findings in that file |
| "fix #3 and #7" | Fixes those two findings |

#### 3.3 Finding IDs
Each finding gets a sequential ID within the session (1, 2, 3...) shown in the post-review message and in the inline finding cards during review.

**UI change:** Finding cards in chat show `#<id>` prefix: `🔴 #1 [CRITICAL] config.py:12 — Hardcoded secret`

---

### 4. Git Integration

**Status:** Not built

#### 4.1 Git Tools Available to the Agent
Reuse the same GitPython-based tools from `ai-intern/tools/git_tools.py` — copy them into `code-reviewer/tools/git_tools.py` (standalone, no import from ai-intern).

Tools included:
- `git_status` — show staged/modified/untracked files
- `git_diff` — unified diff of working tree or staged changes
- `git_log` — recent commit history
- `git_blame` — line-by-line authorship
- `git_create_branch` — branch before making fixes
- `git_commit` — commit fixes with AI-generated message
- `git_stash` — stash before switching context

Tools NOT included (out of scope for reviewer):
- `git_push`, `git_pull`, `git_clone` — reviewer is read-only by default

#### 4.2 Review Scope Selection
When the user starts a new review session on a repo that has been reviewed before, the agent checks if the repo is a git repo and asks:

```
I found a previous review of this repo from <date> at commit <short_hash>.

How would you like to proceed?
  A) Review only changes since last review (git diff <last_commit>..HEAD)
  B) Full review of the entire codebase
```

**Implementation:**
- Store `last_reviewed_commit` in `agent_config` or a new `review_sessions` table keyed by `workspace_path`
- On session start, check if workspace is a git repo and if a previous commit hash is stored
- If yes, present the choice via `cl.AskActionMessage`
- If user picks A, pass the diff output to the agent as additional context and restrict file scope

#### 4.3 Code Change Diff Screen with Undo
When the agent makes a fix (edits a file), the Chainlit UI shows a diff viewer (same `DiffViewer` custom element as ai-intern) with an **Undo** button.

Clicking Undo:
1. Calls `git_stash pop` if the fix was stashed, OR
2. Restores the file to its pre-fix content (stored in session memory before the edit)
3. Removes the finding from the "fixed" list
4. Shows a confirmation message

**Implementation:**
- Before any `edit_file` call during fix mode, store `{file_path: original_content}` in session state
- After `edit_file`, render `DiffViewer` with an Undo action button
- Undo handler restores from session state and calls `write_file` with original content

#### 4.4 Review Sessions Table
New DB table to track review history per workspace:

```sql
CREATE TABLE IF NOT EXISTS review_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    commit_hash TEXT,          -- git HEAD at time of review (null if not a git repo)
    scope       TEXT,          -- 'full' or 'diff'
    total_findings INTEGER,
    model       TEXT
);
```

This enables the "review only changes since last review" feature.

---

### 5. New DB Schema Changes

Summary of all new/modified tables:

```sql
-- Existing: review_findings — add finding_id (sequential per thread)
ALTER TABLE review_findings ADD COLUMN finding_id INTEGER;
ALTER TABLE review_findings ADD COLUMN estimated_fix_tokens INTEGER;
ALTER TABLE review_findings ADD COLUMN status TEXT DEFAULT 'open';  -- open, fixed, dismissed

-- New: review_sessions
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
```

---

### 6. New API Endpoints Summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dashboard/api/findings/by-file` | Findings grouped by file_path with counts |
| GET | `/dashboard/api/findings/trend` | Findings count by date |
| GET | `/dashboard/api/findings/heatmap` | category × criticality matrix |
| GET | `/dashboard/api/reports/html` | Download HTML report (query: `thread_id`) |
| GET | `/dashboard/api/reports/xlsx` | Download XLSX report (query: `thread_id`) |
| GET | `/dashboard/api/sessions` | Review sessions list (workspace history) |
| PATCH | `/dashboard/api/findings/{id}/status` | Mark finding as fixed/dismissed |

---

### 7. New Dependencies

| Package | Purpose | Already in requirements? |
|---------|---------|--------------------------|
| `openpyxl>=3.1.0` | XLSX report generation | No — add |
| `gitpython>=3.1.40` | Git integration tools | No — add |

No other new dependencies. HTML report uses Python string templates only.

---

### 8. Implementation Priority

| Priority | Feature | Effort |
|----------|---------|--------|
| 1 — High | Enhanced observability (§1) | Medium — mostly frontend charts + 3 new API endpoints |
| 2 — High | HTML + XLSX report export (§2) | Medium — server-side generation, no new deps except openpyxl |
| 3 — High | Post-review fix workflow + token estimates (§3) | Medium — agent prompt changes + UI follow-up message |
| 4 — Medium | Git tools integration (§4.1) | Low — copy from ai-intern, wire into agent |
| 5 — Medium | Review scope selection (§4.2) | Medium — new DB table + session start logic |
| 6 — Medium | Diff screen with Undo (§4.3) | Medium — reuse DiffViewer element + undo handler |

---

### 9. Out of Scope for v2

- `git_push` / `git_pull` / `git_clone` — reviewer is read-only
- Multi-user / multi-tenant support
- Real-time collaborative review
- IDE plugin / VS Code extension
- LLM-based code health scoring (current score is heuristic)
- PR/MR creation on GitHub/GitLab
