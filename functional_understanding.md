# Code Reviewer — Functional Understanding

This document explains how the system behaves from a user's perspective and how each feature works end-to-end. It's the right document to read before making changes to the codebase.

---

## Starting a Session

When you open the chat, Chainlit prompts for a project folder name. This must be a directory that sits **alongside** `code-reviewer/` — not inside it. For example, if your workspace looks like:

```
ALLPROJECTS/
├── code-reviewer/
└── my-project/        ← enter "my-project"
```

The agent resolves the full path and checks it exists. If the folder is a git repo and has been reviewed before, it offers a choice:

- **Full review** — scans the entire codebase
- **Diff review** — only reviews files changed since the last review (`git diff <last_commit>..HEAD`)

The diff option is useful for incremental reviews on active projects — it's faster and cheaper.

---

## The Review Process

Type `review` (or `start review`) to begin. The agent:

1. **Plans** — calls `write_todos` to create a task list: security pass, bug pass, performance, maintainability, style
2. **Security pass** — delegates to the `security-scanner` subagent, which runs an OWASP-focused grep across the codebase
3. **File-by-file scan** — delegates each source file to the `file-scanner` subagent, which reads the file and returns all issues
4. **Cross-file patterns** — uses `grep_search` directly for things like `TODO`, `FIXME`, deprecated APIs
5. **Summary** — reports finding counts by criticality, top 3 issues, and a code health score out of 10

### What you see during review

- **Agent banners** — when a subagent activates: `🔒 Security Scanner → auth.py`
- **Tool steps** — collapsible steps showing exactly what's happening: `🔍 Scanning for 'password' in codebase`
- **Finding cards** — each issue appears inline as it's found, colour-coded by criticality with file, line, description, fix suggestion, and estimated fix cost
- **Progress indicator** — live count of findings so far: `🔍 Reviewing... 🔴 2  🟠 4  🟡 6`
- **Review summary** — at the end, a structured summary with all counts, duration, and per-finding token estimates

### Finding cards

Each card shows:
```
🔴 #1 [CRITICAL] · security
Hardcoded API key in source code
📍 config.py:42

The API key is committed directly to source control...

💡 Fix: Move to environment variable: os.getenv("API_KEY")

⚡ Est. fix: ~800 tokens
```

The `#1` ID is sequential within the session and is what you use to request fixes.

---

## The Fix Workflow

After the review, the agent shows a summary with fix options. You can say:

| Command | What happens |
|---------|-------------|
| `fix everything` | Fixes all findings in priority order (critical first) |
| `fix all critical` | Fixes only critical findings |
| `fix #3` | Fixes finding #3 specifically |
| `fix #2 and #5` | Fixes findings 2 and 5 |
| `fix auth.py` | Fixes all findings in that file |

### What happens during a fix

1. The main agent delegates to the `git-agent` subagent
2. `git-agent` creates a branch (`fix/<short-slug>`) — **pauses for your approval**
3. `git-agent` edits the file — **pauses for your approval**
4. After you approve the edit, a **diff viewer** appears in chat showing exactly what changed
5. An **↩️ Undo** button lets you revert the file to its pre-fix state
6. `git-agent` commits with a conventional-commits message — **pauses for your approval**
7. The finding is automatically marked as **fixed** in the dashboard

### Approval gates

Every write operation pauses and asks before running. This is configurable — set `REQUIRE_APPROVAL_EDIT=false` in `.env` to let edits run autonomously (useful in CI).

### Undo

The undo button restores the file from a snapshot taken just before the edit. It does not revert the git commit — it just writes the original content back. After undoing, the finding status is not automatically reset (use the dashboard Fix/Dismiss buttons if needed).

### Which findings get marked fixed

Only the findings that were explicitly targeted by your fix command AND whose file was actually edited. If you say `fix #3` and the agent edits `auth.py`, only finding #3 gets marked fixed — not the other 2 issues in `auth.py`. This is intentional: the agent may fix one issue per edit, and you should verify each fix.

---

## The Dashboard

Access at http://localhost:8001/dashboard.

### Findings tab

The main view. Shows:
- **Summary cards** — critical / high / medium / low+info counts (all-time, no date filter)
- **Category bar chart** — which types of issues are most common
- **Criticality donut** — proportion by severity
- **Findings trend** — issues found per day over time
- **Top 10 files** — horizontal stacked bar showing which files have the most issues
- **Heatmap** — category × criticality matrix showing where issues cluster
- **Findings table** — filterable by criticality and category, with Fix/Dismiss buttons per row
- **Export buttons** — HTML report (self-contained, offline-capable) or XLSX workbook

### Observability tab

Token and performance metrics:
- Total tokens, prompt vs completion split, LLM call count
- Session count, average tokens per session, average findings per session
- Most expensive session, token efficiency ratio (findings per 1k tokens)
- Stacked bar of prompt/completion tokens over time
- Model distribution donut (which models were used)
- Tool success/failure rate grouped bar
- Tokens per finding efficiency line (lower = more efficient reviews over time)

### Sessions tab

Every completed review is listed with:
- Thread ID, workspace path, date, scope (full/diff), finding count, git commit hash, model

Click any session to open a drilldown panel showing all LLM calls, tool invocations, findings, and a cost estimate for that session.

### Settings tab

- Edit the system prompt (with reset to default)
- Set the iteration limit (max agent steps before stopping)

---

## Finding Status Lifecycle

```
open  →  fixed      (auto: when edit_file succeeds for a targeted finding)
open  →  dismissed  (manual: via dashboard Fix/Dismiss buttons or chat)
fixed →  open       (manual: via dashboard if a fix was wrong)
```

The status is stored in `dashboard.db` and shown in the findings table. The summary cards count all findings regardless of status — this is intentional so you can see the full scope of issues found.

---

## Review Scope: Full vs Diff

**Full review** — the agent reads every source file in the repo (up to 150 in the repo map). Good for first-time reviews or after major changes.

**Diff review** — the agent receives the output of `git diff <last_commit>..HEAD` as context and focuses only on changed files. Good for incremental reviews on active development. The last reviewed commit hash is stored in `review_sessions` and retrieved on the next session start.

---

## Token Efficiency

The system is designed to minimise token usage without sacrificing quality:

**Compact finding format** — the `f()` tool takes one pipe-delimited string instead of 7 named parameters. The tool schema is ~60 tokens vs ~300 for a verbose version. With 20+ findings per review, this saves thousands of tokens.

**Subagent isolation** — file contents never appear in the main agent's context. A 500-line file read stays inside the `file-scanner` subagent. The main agent only sees the compact findings list returned by the subagent.

**Repo map pruning** — the initial repo map shown to the agent is capped at 150 files and excludes non-code files (`.md`, `.json`, `.yaml`, `.toml`, `.css`). File sizes are shown so the agent can prioritise which files to read first.

**Streaming suppression** — once the agent starts calling tools, intermediate LLM tokens are not streamed to the user. This avoids showing raw reasoning text and keeps the UI clean.

---

## Git Authentication

All git auth is configured via `.env` — no runtime prompts.

| Type | When to use | Key env vars |
|------|-------------|-------------|
| `none` (default) | Local repos, no remote operations | — |
| `https_token` | GitHub/GitLab with a PAT | `GIT_TOKEN` |
| `https_basic` | HTTPS with username + password | `GIT_USERNAME`, `GIT_PASSWORD` |
| `ssh` | SSH key auth | `GIT_SSH_KEY_PATH` |

Auth is only applied before push/pull operations (which are out of scope for v2 — the reviewer only commits locally).

---

## Multi-Provider LLM Support

The `llm_factory.py` supports four providers:

| Provider | Key env var | Default model |
|----------|-------------|---------------|
| `azure` (default) | `AZURE_OPENAI_API_KEY` | Set via `AZURE_OPENAI_DEPLOYMENT_NAME` |
| `openai` | `OPENAI_API_KEY` | `gpt-4o` |
| `google` | `GOOGLE_API_KEY` | `gemini-1.5-pro` |
| `ollama` | — (local) | `llama3` |

Set `DEBUG_PRINT_PROMPT=true` in `.env` to print the full prompt (with token count estimate) before every LLM call — useful for debugging context bloat.

---

## Known Limitations

- **No git push/pull** — the reviewer commits locally only. Remote operations are out of scope for v2.
- **Single user** — no multi-tenant support. All sessions share the same SQLite databases.
- **Undo doesn't reset git** — undo restores the file content but doesn't revert the git commit if one was already made.
- **Subagent model is the same as main** — the requirements spec cheaper models for subagents (e.g. `gpt-4o-mini` for `file-scanner`) but the current implementation uses the same model for all agents. This is a future optimisation.
- **Health score is heuristic** — the code health score (penalty-based, max 10) is not LLM-generated. It's a simple weighted sum of finding criticalities.
