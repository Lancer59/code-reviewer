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
from config import cfg, cfg_bool, cfg_int
from tools.git_tools import (
    git_status, git_diff, git_log, git_blame,
    git_create_branch, git_commit, git_stash, git_push,
)

logger = logging.getLogger("review_agent")

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "coverage",
}
_MAP_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".cs",
    ".cpp", ".c", ".h", ".rb", ".php", ".swift", ".kt", ".sh",
}
_MAP_CAP = 150


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
# Compact record_finding tool
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

    if ":" in file_line:
        fp, ln = file_line.rsplit(":", 1)
        try:
            ln = int(ln)
        except ValueError:
            fp, ln = file_line, 0
    else:
        fp, ln = file_line, 0

    return f"FINDING|{fp}|{ln}|{crit_map[crit]}|{cat_map[cat]}|{title}|{desc}|{fix}"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """You are an expert code reviewer. Review the target repo and record the most important issues.

## Finding format (call `f` for each issue)
FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
CRIT: C H M L I  |  CAT: sec bug perf maint style doc

## Criticality
C=critical(security/data-loss) H=high(prod bug) M=medium(quality) L=low(style) I=info

## Categories
sec=security  bug=logic-error  perf=performance  maint=maintainability  style=style  doc=documentation

## Finding limit — HARD STOP at {max_findings} findings
You MUST stop recording findings once you reach {max_findings} total.
This is a hard limit — do not exceed it under any circumstances.

Priority order (record these first, stop when limit is reached):
1. Critical (C) — security vulnerabilities, data loss, auth bypass
2. High (H) — production bugs, crashes, data corruption
3. Medium (M) — quality issues that affect reliability
4. Low (L) / Info (I) — only if budget remains after C/H/M

Do NOT record:
- Minor style nits if you already have {max_findings} findings
- Duplicate patterns (one finding per pattern, not one per occurrence)
- Obvious/trivial issues when serious ones exist

## Workflow
1. write_todos — plan passes: security, bugs, performance, maintainability, style
2. Security pass: delegate to `security-scanner` subagent for the full codebase
3. For each source file: delegate to `file-scanner` subagent — it returns findings, call `f` for each
4. STOP immediately if you reach {max_findings} findings — do not scan more files
5. Use grep_search for cross-file patterns (TODO, FIXME, deprecated APIs) — only if under limit
6. Final message: counts by criticality + top 3 issues + health score /10

Rules: be specific (file+line), be actionable (concrete fix).
Delegate file reading to subagents — keep your own context for coordination only.

## Fixing Issues
When the user asks to fix an issue:
1. Use `read_file` to read the target file.
2. Use `edit_file` to apply the code fix.
3. Once all requested fixes are applied, ask the user: "Do you want me to use git tools to verify the changes and create a branch/commit?"
4. If the user agrees, delegate to the `git-agent` to handle git operations.
"""

GIT_AGENT_PROMPT = """You handle git operations for applied code fixes.

Workflow:
1. `git_status` — verify changes exist. If it fails (not a git repo or path error), tell the user and stop.
2. `git_create_branch` — create a new branch named fix/<short-slug>
3. `git_commit` — commit with a conventional-commits message (e.g. "fix(security): move API key to env var")
4. `git_push` — push the fix branch to origin so the user can open a PR.

IMPORTANT: Always use the exact repo_path provided below. Never use relative paths or folder names.

Return a one-line summary: "Committed and pushed branch <name>: <message>"
"""

FILE_SCANNER_PROMPT = """You are a code quality scanner. Read the given file and return the most important issues found.

For each issue call `f` with: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
CRIT: C=critical H=high M=medium L=low I=info
CAT: sec bug perf maint style doc

Priority: critical and high issues first. Skip trivial style nits if serious issues exist.
Record at most 10 findings per file — focus on the worst ones.

Check for: hardcoded secrets, SQL injection, missing error handling,
logic bugs, performance issues, dead code, missing docs on public APIs.
Return compact findings only, no prose."""

SECURITY_SCANNER_PROMPT = """You are a security-focused code auditor. Perform a dedicated OWASP Top 10 pass.

Focus on: injection (SQL/cmd/LDAP), broken auth, sensitive data exposure, XXE,
broken access control, security misconfiguration, XSS, insecure deserialization,
known vulnerable components, insufficient logging.

Use grep_search for: password, secret, token, api_key, eval, exec, query, cursor,
subprocess, pickle, yaml.load, assert, TODO, FIXME, hardcoded.

For each issue call `f` with: FILE:LINE|CRIT|CAT|TITLE|DESC|FIX
CRIT: C=critical H=high M=medium L=low I=info  CAT must be: sec

Record at most 20 security findings total — prioritise critical and high severity.
One finding per vulnerability pattern, not one per occurrence."""


# ---------------------------------------------------------------------------
# interrupt_on config
# ---------------------------------------------------------------------------

def _build_interrupt_on() -> dict:
    """Build interrupt_on from config. All write tools require approval by default."""
    gates = {
        "git_commit":        cfg_bool("REQUIRE_APPROVAL_GIT_COMMIT",  True),
        "git_create_branch": cfg_bool("REQUIRE_APPROVAL_GIT_BRANCH",  True),
        "git_stash":         cfg_bool("REQUIRE_APPROVAL_GIT_STASH",   True),
        "edit_file":         cfg_bool("REQUIRE_APPROVAL_EDIT",        True),
        "execute":           cfg_bool("REQUIRE_APPROVAL_EXECUTE",     True),
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

    max_findings = cfg_int("MAX_FINDINGS", 50)

    resolved_prompt = (
        REVIEW_PROMPT.format(max_findings=max_findings)
        + f"\n\nTarget folder: `{repo_folder}/` — use relative paths for findings."
        + f"\n\nFull workspace path (use this exact value as `repo_path` for ALL git tool calls): `{workspace_path}`"
        + f"\n\n**HARD LIMIT: Stop after {max_findings} findings. Do not record finding #{max_findings + 1} or beyond.**"
        + repo_map_section
    )

    git_subagent = {
        "name": "git-agent",
        "description": (
            "Handles git operations (branching, committing, pushing). Use this ONLY AFTER you have "
            "already applied code fixes and the user has explicitly agreed to branch and commit them."
        ),
        "system_prompt": GIT_AGENT_PROMPT + f"\n\nRepo path (use this exact string for repo_path in every tool call): `{workspace_path}`",
        "tools": [git_create_branch, git_commit, git_stash, git_status, git_diff, git_push],
    }

    file_scanner_subagent = {
        "name": "file-scanner",
        "description": (
            "Reads a single source file and returns all code quality issues found in it. "
            "Use this to scan any file for bugs, performance issues, maintainability problems, "
            "style issues, and missing documentation. Provide the file path."
        ),
        "system_prompt": FILE_SCANNER_PROMPT,
    }

    security_scanner_subagent = {
        "name": "security-scanner",
        "description": (
            "Performs a dedicated security review pass on the codebase. "
            "Checks for OWASP Top 10 vulnerabilities, hardcoded secrets, injection flaws, "
            "broken auth, and insecure patterns. Use once per review for the security pass."
        ),
        "system_prompt": SECURITY_SCANNER_PROMPT,
    }

    core_tools = [f, git_status, git_diff, git_log, git_blame]

    # Build backend as instance (callable factory deprecated in deepagents 0.7.0)
    backend = CompositeBackend(
        default=shell_backend,
        routes={"/memories/": StoreBackend(namespace=lambda ctx, uid=user_id: (uid,))}
    )

    interrupt_on = _build_interrupt_on()

    agent = create_deep_agent(
        model=llm,
        system_prompt=resolved_prompt,
        backend=backend,
        checkpointer=checkpointer,
        store=store,
        tools=core_tools,
        subagents=[git_subagent, file_scanner_subagent, security_scanner_subagent],
        interrupt_on=interrupt_on,
    )
    agent._iteration_limit = iteration_limit or cfg_int("ITERATION_LIMIT", 150)
    return agent
