"""
Code Review Agent — DeepAgent that reviews a codebase and produces
structured findings, then optionally fixes them via a git subagent.
"""

import os
import pathlib
import logging
from langchain_core.tools import tool
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend, CompositeBackend, StoreBackend
from llm_factory import get_llm
from tools.git_tools import (
    git_status, git_diff, git_log, git_blame,
    git_create_branch, git_commit, git_stash,
)

logger = logging.getLogger("review_agent")

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "coverage",
}
# Only code files in the map — no json/yaml/md/css to keep it lean
_MAP_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".cs",
    ".cpp", ".c", ".h", ".rb", ".php", ".swift", ".kt", ".sh",
}
_MAP_CAP = 150  # max files in repo map


def _build_repo_map(workspace_path: str, repo_folder: str) -> str:
    lines = [f"## {repo_folder}/"]
    count = 0
    try:
        for root, dirs, files in os.walk(workspace_path):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
            rel_root = os.path.relpath(root, workspace_path)
            depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
            folder_name = os.path.basename(root) if rel_root != "." else repo_folder
            if rel_root != ".":
                lines.append(f"{'  ' * depth}📁 {folder_name}/")
            for fname in sorted(files):
                ext = pathlib.Path(fname).suffix.lower()
                if ext in _MAP_EXTENSIONS:
                    fpath = os.path.join(root, fname)
                    try:
                        size_kb = os.path.getsize(fpath) // 1024
                        size_str = f" ({size_kb}KB)" if size_kb > 0 else ""
                    except OSError:
                        size_str = ""
                    lines.append(f"{'  ' * (depth + 1)}📄 {fname}{size_str}")
                    count += 1
                    if count >= _MAP_CAP:
                        lines.append(f"  ... (capped at {_MAP_CAP} files)")
                        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Repo map failed: {e}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compact record_finding tool — 5× smaller schema than 7-param version
# ---------------------------------------------------------------------------

@tool
def f(finding: str) -> str:
    """Record a code review finding.

    Format: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
    CRIT: C=critical H=high M=medium L=low I=info
    CAT:  sec bug perf maint style doc
    Example: auth.py:42|C|sec|Hardcoded secret|API key in source|Move to env var
    """
    parts = finding.split("|", 5)
    if len(parts) != 6:
        return "Error: expected FILE:LINE|CRIT|CAT|TITLE|DESC|FIX"

    file_line, crit, cat, title, desc, fix = parts
    crit_map = {"C": "critical", "H": "high", "M": "medium", "L": "low", "I": "info"}
    cat_map = {"sec": "security", "bug": "bug", "perf": "performance",
               "maint": "maintainability", "style": "style", "doc": "documentation"}

    if crit not in crit_map:
        return f"Error: CRIT must be one of {list(crit_map)}"
    if cat not in cat_map:
        return f"Error: CAT must be one of {list(cat_map)}"

    # Parse file:line
    if ":" in file_line:
        fp, ln = file_line.rsplit(":", 1)
        try:
            ln = int(ln)
        except ValueError:
            fp, ln = file_line, 0
    else:
        fp, ln = file_line, 0

    # Return structured pipe string for UI layer to parse
    return f"FINDING|{fp}|{ln}|{crit_map[crit]}|{cat_map[cat]}|{title}|{desc}|{fix}"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """You are an expert code reviewer. Review the target repo and record every issue.

## Finding format (call `f` for each issue)
FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
CRIT: C H M L I  |  CAT: sec bug perf maint style doc

## Criticality
C=critical(security/data-loss) H=high(prod bug) M=medium(quality) L=low(style) I=info

## Categories
sec=security  bug=logic-error  perf=performance  maint=maintainability  style=style  doc=documentation

## Workflow
1. write_todos — plan passes: security, bugs, performance, maintainability, style
2. Security pass: delegate to `security-scanner` subagent for the full codebase
3. For each source file: delegate to `file-scanner` subagent — it returns findings, call `f` for each
4. Use grep_search for cross-file patterns (TODO, FIXME, deprecated APIs)
5. Final message: counts by criticality + top 3 issues + health score /10

Rules: be specific (file+line), be actionable (concrete fix), cover ALL files.
Delegate file reading to subagents — keep your own context for coordination only."""

GIT_AGENT_PROMPT = """You apply code fixes and commit them cleanly.

Workflow for each fix:
1. git_create_branch — create branch named fix/<short-slug>
2. edit_file — apply the fix
3. git_status — verify the change
4. git_commit — commit with conventional-commits message (e.g. "fix(security): move API key to env var")

Return a one-line summary: "Committed <hash> on branch <name>: <message>"
Keep changes minimal — fix only what was asked, nothing else."""

FILE_SCANNER_PROMPT = """You are a code quality scanner. Read the given file and return ALL issues found.

For each issue call `f` with: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
CRIT: C=critical H=high M=medium L=low I=info
CAT: sec bug perf maint style doc

Be thorough — check for: hardcoded secrets, SQL injection, missing error handling,
logic bugs, performance issues, dead code, missing docs on public APIs.
Cover every line. Return compact findings only, no prose."""

SECURITY_SCANNER_PROMPT = """You are a security-focused code auditor. Perform a dedicated OWASP Top 10 pass.

Focus on: injection (SQL/cmd/LDAP), broken auth, sensitive data exposure, XXE,
broken access control, security misconfiguration, XSS, insecure deserialization,
known vulnerable components, insufficient logging.

Use grep_search for: password, secret, token, api_key, eval, exec, query, cursor,
subprocess, pickle, yaml.load, assert, TODO, FIXME, hardcoded.

For each issue call `f` with: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
CRIT: C=critical H=high M=medium L=low I=info  CAT must be: sec

Be aggressive — flag anything suspicious. False positives are acceptable."""


# ---------------------------------------------------------------------------
# interrupt_on config from env vars
# ---------------------------------------------------------------------------

def _build_interrupt_on() -> dict:
    """Build interrupt_on from env vars. All write tools require approval by default."""
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


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

async def create_review_agent(
    workspace_path: str,
    checkpointer,
    store,
    user_id: str = "default",
    iteration_limit: int = None,
    enabled_tools: list = None,
):
    if not os.path.exists(workspace_path):
        raise ValueError(f"Workspace path does not exist: {workspace_path}")

    llm = get_llm(provider="azure")

    parent_dir = os.path.abspath(os.path.join(workspace_path, ".."))
    shell_backend = LocalShellBackend(root_dir=parent_dir, virtual_mode=True, inherit_env=True)

    repo_folder = os.path.basename(workspace_path)
    repo_map = _build_repo_map(workspace_path, repo_folder)
    repo_map_section = f"\n\n## Repository Structure\n{repo_map}" if repo_map else ""

    resolved_prompt = (
        REVIEW_PROMPT
        + f"\n\nTarget folder: `{repo_folder}/` — use relative paths."
        + repo_map_section
    )

    # Git subagent — handles all fix+commit work in isolated context
    git_subagent = {
        "name": "git-agent",
        "description": (
            "Applies a code fix to a specific file, creates a git branch, and commits the change. "
            "Use when the user asks to fix a finding. Provide the file path, the fix description, "
            "and the original + new code."
        ),
        "system_prompt": GIT_AGENT_PROMPT,
        "tools": [git_create_branch, git_commit, git_stash, git_status, git_diff],
        # interrupt_on inherited from main agent — git_commit and git_create_branch will pause for approval
    }

    # File Scanner subagent — reads individual files in isolation, keeps main context clean
    file_scanner_subagent = {
        "name": "file-scanner",
        "description": (
            "Reads a single source file and returns all code quality issues found in it. "
            "Use this to scan any file for bugs, performance issues, maintainability problems, "
            "style issues, and missing documentation. Provide the file path."
        ),
        "system_prompt": FILE_SCANNER_PROMPT,
        # tools provided by the backend (read_file, grep_search)
    }

    # Security Scanner subagent — dedicated OWASP pass in isolated context
    security_scanner_subagent = {
        "name": "security-scanner",
        "description": (
            "Performs a dedicated security review pass on the codebase. "
            "Checks for OWASP Top 10 vulnerabilities, hardcoded secrets, injection flaws, "
            "broken auth, and insecure patterns. Use once per review for the security pass."
        ),
        "system_prompt": SECURITY_SCANNER_PROMPT,
        # tools provided by the backend (read_file, grep_search)
    }

    core_tools = [f, git_status, git_diff, git_log, git_blame]

    def make_backend(runtime):
        return CompositeBackend(
            default=shell_backend,
            routes={"/memories/": StoreBackend(runtime, namespace=lambda ctx, uid=user_id: (uid,))}
        )

    interrupt_on = _build_interrupt_on()

    agent = create_deep_agent(
        model=llm,
        system_prompt=resolved_prompt,
        backend=make_backend,
        checkpointer=checkpointer,
        store=store,
        tools=core_tools,
        subagents=[git_subagent, file_scanner_subagent, security_scanner_subagent],
        interrupt_on=interrupt_on,
    )
    agent._iteration_limit = iteration_limit or int(os.getenv("ITERATION_LIMIT", "150"))
    return agent
