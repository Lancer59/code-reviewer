# 🔍 Dev Companion

An AI-powered code review agent built on [deepagents](https://github.com/langchain-ai/deepagents) + Chainlit. Point it at any local repository and it produces structured findings with criticality levels, categories, and actionable fix suggestions.

---

## 📊 [Open Dashboard →](/dashboard)

View findings by criticality and category, token observability, session history, and export reports (HTML / XLSX).

> The dashboard is only available when running via `uvicorn app:app --port 8001`. If running via `chainlit run reviewer_ui.py`, this link won't work.

---

## Getting Started

1. Enter the **folder name** of the project you want to review (must be a sibling directory to `code-reviewer/`)
2. Type **`review`** to start a full review
3. After the review, say **`fix #1`**, **`fix all critical`**, or **`fix auth.py`** to apply fixes

---

## What it does

- **Plans the review** — creates a task list (security pass, bug pass, performance, etc.)
- **Delegates to subagents** — file reading and security scanning happen in isolated subagents, keeping the main context clean
- **Records every finding** — criticality, category, file, line, description, and fix suggestion
- **Shows live progress** — finding count updates in real time as the review runs
- **Offers to fix** — after review, estimates fix cost per finding and applies fixes via the git-agent subagent
- **Diff + Undo** — every file edit shows a diff with an Undo button

---

## Fix commands

| Say | Effect |
|-----|--------|
| `fix everything` | Fix all findings, critical → high → medium → low |
| `fix all critical` | Fix only critical findings |
| `fix #3` | Fix finding #3 by ID |
| `fix auth.py` | Fix all findings in that file |
| `fix #2 and #5` | Fix specific findings |

---

## Subagents

| Agent | Role |
|-------|------|
| `file-scanner` | Reads a single file, returns all quality issues |
| `security-scanner` | Dedicated OWASP Top 10 + secrets pass |
| `git-agent` | Creates branch, applies fix, commits |

---

## Storage

| File | Purpose |
|------|---------|
| `agent_data/chainlit_ui.db` | Chat threads & message history |
| `agent_data/checkpoints_lg.db` | LangGraph agent state per thread |
| `agent_data/dashboard.db` | Findings, telemetry, review sessions |
