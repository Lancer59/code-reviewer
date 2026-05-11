# Dev Companion — AI Code Reviewer

An AI-powered code review agent. Point it at any Git repository and it produces structured findings with criticality levels, categories, and actionable fix suggestions — then applies fixes and pushes a PR-ready branch.

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure (copy and fill in your keys)
cp .env.example .env

# Run
uvicorn app:app --host 0.0.0.0 --port 8001
```

Open `http://localhost:8001` — you'll be prompted for a repo URL and PAT.

---

## How It Works

### 1. Onboarding
Enter a Git repository URL (and optionally a branch) plus a Personal Access Token. The app clones the repo into a temporary workspace. For public repos, type `skip` instead of a PAT.

```
https://github.com/org/repo          ← default branch
https://github.com/org/repo develop  ← specific branch
```

### 2. Review
Type `review` to start. The agent:
- Plans passes (security, bugs, performance, maintainability, style)
- Delegates file reading to isolated subagents (keeps main context clean)
- Records findings with criticality, category, file, line, description, and fix suggestion
- Stops at a configurable finding limit (default: 50) — prioritises critical/high first

### 3. Fix
After the review, say what to fix:

| Command | Effect |
|---------|--------|
| `fix everything` | All findings, critical → low |
| `fix all critical` | Only critical findings |
| `fix #3` | Finding #3 by ID |
| `fix auth.py` | All findings in that file |

Each fix shows a colour-coded diff with an Undo button. You approve before anything is written.

### 4. Push
After fixes are applied, the git-agent creates a branch (`fix/<slug>`), commits, and pushes to origin. Your PAT is used for the push — it's stored encrypted in the local database and deleted when the session ends.

---

## Subagents

| Agent | Role |
|-------|------|
| `file-scanner` | Reads one file, returns all quality issues |
| `security-scanner` | OWASP Top 10 + secrets + injection pass |
| `git-agent` | Creates branch, commits, pushes fix branch |

---

## Dashboard

Available at `/dashboard` when running via `uvicorn app:app`.

- **Findings** — table with criticality/category filters, per-project view, mark fixed/dismissed
- **Observability** — token usage, tool invocations, session history, model distribution
- **Reports** — export HTML or XLSX report for any session
- **Settings** — configure system prompt, iteration limit, LLM provider

---

## Configuration

All configuration is read from (in priority order):
1. `config.json` — rename `config.json.example` to `config.json` and fill in values
2. Environment variables / `.env` file
3. Built-in defaults

Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `AZURE_OPENAI_API_KEY` | — | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | — | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | `gpt-4o` | Deployment name |
| `ITERATION_LIMIT` | `150` | Max agent steps per turn |
| `MAX_FINDINGS` | `50` | Hard cap on findings per review |
| `AGENT_DATA_DIR` | `./agent_data` | SQLite database directory (mount Azure Files here) |
| `WORKSPACE_BASE_DIR` | `./workspaces` | Cloned repo directory (ephemeral) |
| `APP_BASE_URL` | `http://localhost:8001` | Public URL for dashboard links |
| `CLEANUP_WORKSPACE_ON_EXIT` | `true` | Delete cloned repo when session ends |
| `GIT_CLONE_DEPTH` | `1` | Shallow clone depth |
| `MAX_CLONE_SIZE_MB` | `500` | Reject repos larger than this |
| `CHAINLIT_USER` | `admin` | Login username — **change before deploying** |
| `CHAINLIT_PASSWORD` | `admin` | Login password — **change before deploying** |

---

## Mounting in another FastAPI app

```python
from run import create_app

reviewer = create_app(
    mount_path="/reviewer",
    config_file="/etc/reviewer/config.json",
)
your_app.mount("/reviewer", reviewer)
```

See [MOUNTING.md](MOUNTING.md) for the full guide.

---

## Docker

```bash
docker build -t dev-companion .
docker run -p 8001:8001 \
  -v /your/azure/files/mount:/app/agent_data \
  --env-file .env \
  dev-companion
```

Or with docker-compose:
```bash
docker-compose up
```

For Azure Container Apps, mount an Azure Files share at `/app/agent_data` to persist the SQLite databases across restarts.

---

## Storage

| File | Purpose |
|------|---------|
| `agent_data/chainlit_ui.db` | Chat threads and message history |
| `agent_data/checkpoints_lg.db` | LangGraph agent state per thread |
| `agent_data/dashboard.db` | Findings, telemetry, review sessions, encrypted PATs |

---

## Security notes

- PATs are encrypted at rest (Fernet/AES-128) using a key derived from `CHAINLIT_AUTH_SECRET`
- PATs are deleted from the database when the session ends
- Git push uses the PAT injected directly into the HTTPS URL — never written to git config
- The system credential manager is disabled for push operations to prevent accidental pushes to the wrong repo
- Cloned workspaces are sandboxed — git tools refuse to operate outside `WORKSPACE_BASE_DIR`
- Change `CHAINLIT_USER` and `CHAINLIT_PASSWORD` from their defaults before any deployment
