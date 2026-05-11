# Dev Companion — Requirements v3

**Project:** code-reviewer  
**Status:** v2 built, v3 planned  
**Base:** Existing `review_agent.py`, `reviewer_ui.py`, `dashboard/`, `tools/git_tools.py` already implemented

---

## Current State (v2 — Already Built)

- DeepAgent reviews a codebase using compact `f()` tool (pipe-delimited findings)
- Multi-agent architecture: main agent + file-scanner, security-scanner, git-agent subagents
- Findings stored in SQLite (`dashboard.db`) with criticality / category / file / line / status
- Chainlit UI with inline finding cards, agent banners, tool step labels, diff viewer + undo
- Human-in-the-loop approvals for write operations (configurable via `.env`)
- Dashboard: Findings tab, Observability tab, Settings tab, HTML + XLSX report export
- Review scope selection (full vs diff since last commit)
- Post-review fix workflow: "fix #1", "fix all critical", "fix auth.py", "fix everything"
- FastAPI entry point (`app.py`) mounts Chainlit at `/` and dashboard at `/dashboard`
- Git auth via env vars (`GIT_AUTH_TYPE`, `GIT_TOKEN`, etc.)
- Workspace is currently a **local sibling folder** — user types the folder name at chat start

---

## v3 Requirements

---

### 1. Git Clone Onboarding Flow (Cloud-First)

**Problem:** The current onboarding asks for a local folder name. This only works when the repo is already on disk — it breaks in cloud deployments where there is no pre-existing code.

**Solution:** Replace the folder-name prompt with a two-step onboarding that clones the repo from a URL using a PAT.

#### 1.1 Onboarding Sequence

When a new chat session starts, the agent must:

1. Ask for the **Git repository URL** (HTTPS only — e.g. `https://github.com/org/repo`)
2. Ask for the **Personal Access Token (PAT)** — displayed as a password field, never echoed back
3. Validate both inputs before proceeding
4. Clone the repo into a temporary working directory
5. Confirm success and proceed with the review

The agent must **never** proceed to review without both inputs being provided and validated.

#### 1.2 UI Prompts

Use `cl.AskUserMessage` for the URL and a masked input for the PAT:

```
Step 1/2 — Enter the Git repository URL:
  (e.g. https://github.com/your-org/your-repo)

Step 2/2 — Enter your Personal Access Token (PAT):
  (This is used only to clone the repo and is never stored)
```

The PAT must be treated as a secret:
- Never logged to stdout or any file
- Never stored in the database
- Never shown in the UI after submission
- Cleared from memory after the clone completes

#### 1.3 Clone Implementation

Add a `git_clone` function to `tools/git_tools.py`:

```python
async def git_clone(repo_url: str, pat: str, target_dir: str) -> str:
    """
    Clone a repo using HTTPS + PAT auth.
    Injects the PAT into the URL: https://<pat>@github.com/org/repo
    Returns the path to the cloned directory on success.
    Raises ValueError on failure.
    """
```

Clone target: a subdirectory inside a configurable `WORKSPACE_BASE_DIR` (env var, defaults to `./workspaces/`). The folder name is derived from the repo name (last path segment of the URL, without `.git`).

If a folder with that name already exists, append a short UUID suffix to avoid collisions.

#### 1.4 Supported Providers

The clone logic must work with:
- GitHub (`github.com`)
- GitLab (`gitlab.com` and self-hosted)
- Bitbucket (`bitbucket.org`)
- Azure DevOps (`dev.azure.com`)
- Any other HTTPS git host

PAT injection format: `https://<pat>@<host>/...` — this works for all providers above.

For Azure DevOps the format is slightly different: `https://<org>:<pat>@dev.azure.com/...` — handle this as a special case.

#### 1.5 Error Handling

| Error | User-facing message |
|-------|---------------------|
| Invalid URL format | "That doesn't look like a valid HTTPS git URL. Please try again." |
| Auth failure (401/403) | "Authentication failed. Check your PAT has read access to this repo." |
| Repo not found (404) | "Repository not found. Check the URL is correct and the PAT has access." |
| Network error | "Could not reach the git host. Check your network connection." |
| Disk full | "Not enough disk space to clone the repository." |

After 3 failed attempts, end the session with a clear error message.

#### 1.6 Post-Clone State

After a successful clone:
- Set `workspace` in `cl.user_session` to the cloned directory path
- Set `repo_url` in `cl.user_session` (for display purposes only)
- Clear the PAT from all variables immediately
- Proceed with the existing agent initialisation flow (same as current `start()` after workspace is set)

#### 1.7 Workspace Cleanup

Cloned repos accumulate on disk. Add a cleanup strategy:
- On session end (`@cl.on_chat_end` or `lifespan` shutdown), delete the cloned workspace directory
- Configurable via `CLEANUP_WORKSPACE_ON_EXIT=true` (default: `true` in cloud, `false` locally)
- Log the cleanup action but do not fail if the directory is already gone

---

### 2. Cloud Readiness

**Goal:** The application must run correctly in a containerised cloud environment (Docker, Kubernetes, cloud run services) with no dependency on the local filesystem beyond ephemeral storage.

#### 2.1 Dockerfile

Create a production-ready `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# System deps: git (for cloning), build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create directories for runtime data
RUN mkdir -p agent_data workspaces

EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
```

Key points:
- `git` must be installed in the image (needed for `gitpython` and `git clone`)
- `agent_data/` holds SQLite databases — should be a mounted volume in production
- `workspaces/` holds cloned repos — ephemeral, can be in-container storage
- Run as non-root user in production (add `RUN useradd -m appuser && USER appuser`)

#### 2.2 docker-compose.yml

Provide a `docker-compose.yml` for local development and simple cloud deployments:

```yaml
version: "3.9"
services:
  code-reviewer:
    build: .
    ports:
      - "8001:8001"
    volumes:
      - ${AGENT_DATA_MOUNT:-./agent_data}:/app/agent_data   # Azure Files share or local dir
    env_file:
      - .env
    environment:
      - AGENT_DATA_DIR=/app/agent_data
      - WORKSPACE_BASE_DIR=/app/workspaces
      - CLEANUP_WORKSPACE_ON_EXIT=true
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

For Azure Container Apps, the Azure Files share is mounted directly at `/app/agent_data` via the container app's volume configuration — the `docker-compose.yml` volume entry is only used for local dev.

#### 2.3 Environment Variables — Cloud Additions

Add to `.env.example`:

```bash
# Cloud / Docker settings
AGENT_DATA_DIR=./agent_data            # path to SQLite DB directory (mount Azure Files share here)
WORKSPACE_BASE_DIR=./workspaces        # where cloned repos are stored (ephemeral, not mounted)
CLEANUP_WORKSPACE_ON_EXIT=true         # delete workspace after session ends

# Base URL — used for dashboard links in review summary
# Set to your public URL in cloud deployments (e.g. https://myapp.azurecontainerapps.io)
APP_BASE_URL=http://localhost:8001
```

The existing `APP_BASE_URL` is currently hardcoded as `http://localhost:8001` in the review summary message. Replace all hardcoded localhost URLs with `os.getenv("APP_BASE_URL", "http://localhost:8001")`.

#### 2.4 Azure Blob Storage Mount

The `agent_data/` directory (SQLite databases) is persisted via an Azure Blob Storage container mounted as a volume. This means:

- `dashboard.db`, `checkpoints_lg.db`, and `chainlit_ui.db` survive container restarts and redeployments
- The mount point is configured via the `AGENT_DATA_DIR` env var (defaults to `./agent_data`)
- In the `docker-compose.yml`, `agent_data/` maps to the Azure-mounted path on the host
- No code changes needed — `aiosqlite` writes to whatever path `DB_PATH` resolves to

**Azure Container Apps / ACI setup:**
- Mount the Azure Files share at `/app/agent_data` inside the container
- Set `AGENT_DATA_DIR=/app/agent_data` in the container environment
- The share must be pre-created; the app creates the SQLite files on first run

**Important:** SQLite is not safe for concurrent writes from multiple container instances. Keep the deployment to a single replica. If horizontal scaling is needed in the future, migrate to PostgreSQL (the `dashboard/db.py` abstraction makes this straightforward).

`workspaces/` (cloned repos) is intentionally **not** mounted to Azure storage — it is ephemeral per-container storage. Cloned repos are deleted on session end and do not need to persist.

#### 2.5 Health Check Endpoint

Add a `/health` endpoint to `app.py` for container orchestration:

```python
@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0"}
```

This is used by Docker healthchecks, Kubernetes liveness probes, and load balancers.

#### 2.6 Stateless Session Design

In cloud deployments, the process may restart between user sessions. Ensure:
- All session state is stored in SQLite (already done via LangGraph checkpointer + Chainlit data layer)
- `InMemoryStore` (used for long-term agent memory) is acceptable for v3 — document that it resets on restart
- The `_checkpointer` global is initialised lazily on first use (already done)
- No hardcoded absolute paths — all paths derived from `__file__` or env vars (audit and fix any violations)

---

### 3. FastAPI Mounting — Integration Guide

**Goal:** Provide a clear, tested guide for mounting Dev Companion inside another FastAPI application.

#### 3.1 How It Works Today

`app.py` already demonstrates the mounting pattern:

```python
from fastapi import FastAPI
from chainlit.utils import mount_chainlit
from dashboard.api import dashboard_app

app = FastAPI()

# Mount dashboard routes
for route in dashboard_app.routes:
    app.routes.append(route)

# Mount Chainlit UI at root
mount_chainlit(app=app, target="reviewer_ui.py", path="/")
```

This works because `mount_chainlit` uses Starlette's sub-application mounting. The Chainlit app handles WebSocket connections and HTTP at the specified path prefix.

#### 3.2 Mounting at a Sub-Path

To mount Dev Companion inside a host FastAPI app at `/reviewer` instead of `/`:

```python
# host_app.py (the other FastAPI application)
from fastapi import FastAPI
from chainlit.utils import mount_chainlit
from code_reviewer.dashboard.api import dashboard_app

host_app = FastAPI(title="My Platform")

# Your existing routes
@host_app.get("/api/v1/status")
async def status():
    return {"ok": True}

# Mount Dev Companion dashboard at /reviewer/dashboard
for route in dashboard_app.routes:
    # Prefix all dashboard routes
    route.path = "/reviewer" + route.path
    host_app.routes.append(route)

# Mount Chainlit UI at /reviewer
mount_chainlit(app=host_app, target="path/to/reviewer_ui.py", path="/reviewer")
```

**Important:** The `path` argument to `mount_chainlit` must match the prefix used for dashboard routes, otherwise the dashboard's API calls (which are relative to `/dashboard/api/...`) will 404.

#### 3.3 Document: `MOUNTING.md`

Create a `MOUNTING.md` file at the project root with:

1. **Prerequisites** — what the host app needs (FastAPI ≥ 0.100, same Python process)
2. **Quick start** — minimal working example (mount at `/reviewer`)
3. **Path configuration** — how to set `APP_BASE_URL` so dashboard links work
4. **Database isolation** — how to point Dev Companion at a different `DB_PATH` when running alongside other apps
5. **Authentication** — how Chainlit's password auth interacts with the host app's auth
6. **Static files** — the dashboard's static files are served from `dashboard/static/` — ensure the path is correct relative to the host app's working directory
7. **Lifespan events** — how to merge Dev Companion's lifespan with the host app's lifespan
8. **Known limitations** — Chainlit WebSocket path conflicts, single-process requirement

#### 3.4 Lifespan Merging

The current `app.py` lifespan closes the SQLite checkpointer connection on shutdown. When mounting inside a host app, this lifespan must be merged:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def merged_lifespan(app: FastAPI):
    # Host app startup
    yield
    # Host app shutdown
    # Dev Companion cleanup
    try:
        import reviewer_ui
        if reviewer_ui._checkpointer_conn is not None:
            await reviewer_ui._checkpointer_conn.close()
    except Exception:
        pass

host_app = FastAPI(lifespan=merged_lifespan)
```

Document this pattern in `MOUNTING.md`.

---

### 4. Onboarding UX Improvements

These changes improve the first-run experience, especially in cloud where users arrive with no local context.

#### 4.1 Welcome Message

Replace the bare `AskUserMessage` with a rich welcome card shown before the URL prompt:

```
🔍 Dev Companion — AI Code Reviewer

I'll review your codebase and find bugs, security issues, and code quality problems.

To get started, I need:
  1. Your repository URL (HTTPS)
  2. A Personal Access Token with read access

Your PAT is used only to clone the repo and is never stored.
```

#### 4.2 PAT Guidance

After asking for the PAT, show provider-specific guidance on how to create one:

| Detected provider | Link shown |
|-------------------|------------|
| github.com | https://github.com/settings/tokens |
| gitlab.com | https://gitlab.com/-/profile/personal_access_tokens |
| bitbucket.org | https://bitbucket.org/account/settings/app-passwords |
| dev.azure.com | https://dev.azure.com → User Settings → Personal Access Tokens |

Detect the provider from the URL entered in step 1.

#### 4.3 Clone Progress

Show a progress message while cloning:

```
⏳ Cloning https://github.com/org/repo...
✅ Cloned successfully (247 files, 3.2 MB)
⚙️ Initialising review agent...
✅ Ready to review repo
```

Show file count and size after clone using `os.walk`.

---

### 5. Security Hardening for Cloud

Running in a shared cloud environment requires additional security measures.

#### 5.1 PAT Handling

- PAT must be passed directly to `git clone` via the URL — never written to disk, never logged
- After clone, immediately overwrite the PAT variable: `pat = ""`
- The `PromptDebugCallback` in `llm_factory.py` must not print any message containing `GIT_TOKEN`, `GIT_PASSWORD`, or the PAT value
- Add a check: if `DEBUG_PRINT_PROMPT=true` in production (detected by `APP_BASE_URL` not being localhost), log a warning

#### 5.2 Workspace Isolation

Each session clones into its own directory (`workspaces/<repo-name>-<uuid>/`). This ensures:
- Sessions cannot read each other's code
- A compromised session cannot affect other sessions' workspaces
- Cleanup is per-session, not global

The `LocalShellBackend` in `review_agent.py` is already scoped to `parent_dir` of the workspace — verify this still holds with the new clone-based path structure.

#### 5.3 Chainlit Auth in Cloud

The current auth uses a single hardcoded username/password from `.env`. For cloud:
- Keep the existing `CHAINLIT_USER` / `CHAINLIT_PASSWORD` env vars
- Document that these should be changed from the defaults (`admin`/`admin`) before deployment
- Add a startup warning if the defaults are detected: `logger.warning("SECURITY: Default Chainlit credentials in use. Change CHAINLIT_USER and CHAINLIT_PASSWORD.")`

#### 5.4 Rate Limiting (Optional, v3.1)

In a public cloud deployment, the clone endpoint could be abused. Consider:
- Limit to N active sessions per IP (configurable via `MAX_SESSIONS_PER_IP`)
- Limit total concurrent clones (configurable via `MAX_CONCURRENT_CLONES`)
- This is optional for v3 but document the approach

---

### 6. Configuration Changes

#### 6.1 New Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_DATA_DIR` | `./agent_data` | Path to SQLite DB directory — mount Azure Files share here |
| `WORKSPACE_BASE_DIR` | `./workspaces` | Base directory for cloned repos (ephemeral, not mounted) |
| `CLEANUP_WORKSPACE_ON_EXIT` | `true` | Delete workspace when session ends |
| `APP_BASE_URL` | `http://localhost:8001` | Public URL — used in dashboard links (set to Azure Container Apps URL) |
| `MAX_CLONE_SIZE_MB` | `500` | Reject repos larger than this (0 = no limit) |
| `GIT_CLONE_DEPTH` | `1` | Shallow clone depth (1 = latest commit only, faster) |

#### 6.2 Updated `.env.example`

The `.env.example` must be updated to include all new variables with comments explaining cloud vs local usage.

#### 6.3 Remove `GIT_AUTH_TYPE` Complexity

The existing `GIT_AUTH_TYPE` env var supports `none`, `https_token`, `https_basic`, `ssh`. In the new flow, auth is always `https_token` (PAT provided at runtime by the user). The static env-var auth is still useful for local development where the workspace is pre-existing.

Keep both flows:
- **Cloud flow:** PAT provided at chat start → used for clone → cleared after clone
- **Local flow:** Workspace folder name provided → no clone → existing `GIT_AUTH_TYPE` env vars used for push/pull if needed

The UI detects which flow to use: if `WORKSPACE_BASE_DIR` is set and writable, offer the clone flow. Otherwise fall back to the folder-name flow.

---

### 7. Implementation Plan

#### Phase 1 — Cloud Infrastructure (do first)
1. Add `git_clone()` to `tools/git_tools.py`
2. Update `reviewer_ui.py` `on_chat_start` to run the two-step onboarding (URL + PAT)
3. Add workspace cleanup on session end
4. Add `WORKSPACE_BASE_DIR`, `APP_BASE_URL`, `CLEANUP_WORKSPACE_ON_EXIT` env vars
5. Replace hardcoded `localhost:8001` URLs with `APP_BASE_URL`
6. Add `/health` endpoint to `app.py`
7. Add startup warning for default credentials

#### Phase 2 — Docker + Deployment
1. Write `Dockerfile`
2. Write `docker-compose.yml`
3. Update `.env.example` with all new variables
4. Test full flow: `docker build` → `docker run` → clone → review

#### Phase 3 — Mounting Guide
1. Write `MOUNTING.md` covering all scenarios in §3
2. Test mounting inside a minimal host FastAPI app
3. Verify dashboard routes work at sub-path
4. Verify Chainlit WebSocket works at sub-path

#### Phase 4 — Security Hardening
1. PAT clearing after clone
2. Startup warning for default credentials
3. Audit all log statements for accidental secret leakage
4. Verify workspace isolation (each session in its own directory)

---

### 8. Out of Scope for v3

- Multi-user / multi-tenant auth (beyond single shared password)
- PostgreSQL migration (SQLite is sufficient for single-instance cloud)
- PR/MR creation on GitHub/GitLab
- SSH key auth for cloud clone (HTTPS PAT is the standard cloud pattern)
- Rate limiting (v3.1)
- Kubernetes Helm chart (v3.1)

---

### 9. Files to Create / Modify

| File | Action | Description |
|------|--------|-------------|
| `tools/git_tools.py` | Modify | Add `git_clone()` function |
| `reviewer_ui.py` | Modify | Replace folder-name prompt with URL + PAT onboarding; add workspace cleanup |
| `app.py` | Modify | Add `/health` endpoint; replace hardcoded URLs |
| `dashboard/db.py` | Modify | Derive `DB_PATH` from `AGENT_DATA_DIR` env var instead of `__file__` |
| `.env.example` | Modify | Add new cloud env vars |
| `Dockerfile` | Create | Production container image |
| `docker-compose.yml` | Create | Local dev + simple cloud deployment |
| `MOUNTING.md` | Create | Guide for mounting inside another FastAPI app |
| `llm_factory.py` | Modify | Guard `PromptDebugCallback` against logging secrets |

---

### 10. Key Design Decisions

**Why HTTPS PAT only (not SSH) for cloud clone?**  
SSH requires key management (mounting secrets, known_hosts setup). HTTPS PAT is simpler, works everywhere, and is the standard for CI/CD systems. Users already have PATs for GitHub Actions, GitLab CI, etc.

**Why shallow clone (`--depth 1`) by default?**  
Full history is not needed for code review. Shallow clone is 10–100× faster for large repos and uses far less disk space. The `git_log` tool still works (shows the single commit). Set `GIT_CLONE_DEPTH=0` to disable if full history is needed.

**Why delete the workspace on session end?**  
Cloud storage is ephemeral and expensive. Keeping cloned repos around serves no purpose after the session ends — the user can re-clone. This also prevents sensitive code from accumulating on the server.

**Why keep SQLite instead of moving to a cloud database?**  
SQLite with a mounted volume works perfectly for single-instance deployments (which covers 95% of use cases). The abstraction in `dashboard/db.py` makes migration straightforward if needed. Premature optimisation.

**Why not store the PAT in the database for re-use?**  
Security. A stored PAT is a liability — it can be leaked via DB export, logs, or a compromised server. The user provides it fresh each session. This is the correct pattern for a code review tool that handles potentially sensitive repositories.
