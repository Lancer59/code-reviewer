# Dev Companion

**Chat UI:** http://localhost:8001  
**Dashboard:** http://localhost:8001/dashboard

An AI-powered code review agent built on [deepagents](https://github.com/langchain-ai/deepagents) + Chainlit. Point it at any local repository and it produces structured findings with criticality levels, categories, and actionable fix suggestions — with a full observability dashboard.

---

## Quick Start

```bash
cd code-reviewer
python -m venv env
env\Scripts\activate        # Windows
# source env/bin/activate   # macOS/Linux

pip install -r requirements.txt
cp .env.example .env
# Edit .env — add your API key and set AZURE_OPENAI_DEPLOYMENT_NAME

python init_db.py
uvicorn app:app --host 127.0.0.1 --port 8001
```

Open http://localhost:8001, enter the folder name of the project to review (must be a sibling directory to `code-reviewer/`), then type `review`.

---

## Project Structure

```
code-reviewer/
├── app.py                  # Production entry point — mounts Chainlit + Dashboard on port 8001
├── reviewer_ui.py          # Chainlit UI — streaming, finding cards, approvals, diff/undo
├── review_agent.py         # DeepAgent factory — tools, subagents, prompts, interrupt_on
├── llm_factory.py          # Multi-provider LLM factory (Azure, OpenAI, Gemini, Ollama)
├── init_db.py              # One-time Chainlit SQLite schema setup
├── tools/
│   └── git_tools.py        # GitPython-based tools (status, diff, log, blame, branch, commit, stash)
├── dashboard/
│   ├── api.py              # FastAPI routes — findings, observability, reports, sessions
│   ├── db.py               # SQLite helpers — findings, telemetry, review sessions
│   └── static/
│       └── index.html      # Dashboard SPA — findings, observability, sessions, settings tabs
├── agent_data/             # Runtime databases (gitignored)
├── .env.example            # All config options with documentation
├── requirements.md         # Full v2 feature requirements
└── requirements.txt
```

---

## How It Works

1. Enter a project folder name — the agent maps it to a sibling directory
2. The agent plans the review using `write_todos` (security pass, bug pass, performance, etc.)
3. It delegates file reading to the `file-scanner` subagent and security scanning to `security-scanner`
4. For every issue found, it calls the compact `f()` tool — one pipe-delimited string per finding
5. Findings are persisted to SQLite and shown inline as colour-coded cards with token estimates
6. After the review, say `fix #1`, `fix all critical`, or `fix auth.py` to apply fixes
7. Fixes go through the `git-agent` subagent — branch, edit, commit — with diff viewer and undo

---

## Fix Commands

| Say | Effect |
|-----|--------|
| `fix everything` | Fix all findings, critical → high → medium → low |
| `fix all critical` | Fix only critical findings |
| `fix #3` | Fix finding #3 by ID |
| `fix #2 and #5` | Fix specific findings |
| `fix auth.py` | Fix all findings in that file |

---

## Subagents

| Agent | Role | Tools |
|-------|------|-------|
| `file-scanner` | Reads a single file, returns all quality issues | `read_file`, `grep_search` |
| `security-scanner` | Dedicated OWASP Top 10 + secrets pass | `read_file`, `grep_search` |
| `git-agent` | Creates branch, applies fix, commits | `git_*`, `edit_file` |

---

## Dashboard Tabs

- **Findings** — summary cards, category/criticality charts, trend line, top-10-files bar, heatmap, filterable table with Fix/Dismiss buttons, HTML/XLSX export
- **Observability** — token metrics, model distribution, tool success rates, efficiency charts, session drilldown with cost estimate
- **Sessions** — review history per workspace with commit hash and scope
- **Settings** — system prompt editor, iteration limit

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_OPENAI_API_KEY` | — | Azure OpenAI key |
| `AZURE_OPENAI_ENDPOINT` | — | Azure endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | — | Model deployment name |
| `OPENAI_API_KEY` | — | OpenAI key (alternative) |
| `CHAINLIT_AUTH_SECRET` | — | Chainlit cookie secret |
| `CHAINLIT_USER` / `CHAINLIT_PASSWORD` | `admin` / `admin` | Login credentials |
| `ITERATION_LIMIT` | `150` | Max agent steps — increase for large repos |
| `DEBUG_PRINT_PROMPT` | `false` | Print full prompt before each LLM call |
| `REQUIRE_APPROVAL_GIT_COMMIT` | `true` | Pause for approval before committing |
| `REQUIRE_APPROVAL_GIT_BRANCH` | `true` | Pause for approval before creating branch |
| `REQUIRE_APPROVAL_EDIT` | `true` | Pause for approval before editing files |
| `REQUIRE_APPROVAL_EXECUTE` | `false` | Pause for approval before shell commands |
| `GIT_AUTH_TYPE` | `none` | Git auth: `none` / `https_token` / `https_basic` / `ssh` |
| `GIT_TOKEN` | — | GitHub/GitLab PAT for HTTPS token auth |

See `.env.example` for the full list with documentation.

---

## Token Efficiency Design

**Compact `f()` tool** — single pipe-delimited string instead of 7 named parameters. Schema shrinks from ~300 tokens to ~60 tokens per call (5× reduction).

```
Format:  FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
Example: auth.py:42|C|sec|Hardcoded API key|Key in source|Move to env var
Codes:   CRIT = C H M L I   |   CAT = sec bug perf maint style doc
```

**Subagent context isolation** — file reads and security scans happen inside subagents. The main agent never sees raw file contents — only compact findings. Prevents context bloat on large codebases.

**Tight system prompt** — ~400 tokens. No redundant prose, compact reference tables.

**Repo map pruning** — capped at 150 files, excludes non-code files (`.md`, `.json`, `.yaml`, `.css`), shows file sizes so the agent prioritises what to read.

---

## Running Without Docker

```bash
# Development — Chainlit only (no dashboard)
chainlit run reviewer_ui.py

# Production — Chainlit + Dashboard on same port
uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

---

## Storage

| File | Purpose |
|------|---------|
| `agent_data/chainlit_ui.db` | Chat threads and message history |
| `agent_data/checkpoints_lg.db` | LangGraph agent state per thread |
| `agent_data/dashboard.db` | Findings, LLM telemetry, review sessions |
