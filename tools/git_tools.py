"""
Git tools for Dev Companion.
Read-only tools for review, write tools for the fix workflow (git-agent subagent).
Excluded: git_push, git_pull, git_clone — reviewer is read-only by default.
"""

import asyncio
import json
import logging
import os
from datetime import datetime

from git import GitCommandError, InvalidGitRepositoryError, Repo
from langchain_core.tools import tool

logger = logging.getLogger("git_tools")
_MAX_DIFF_CHARS = 15_000


def _repo(path: str) -> Repo:
    try:
        return Repo(path, search_parent_directories=False)
    except InvalidGitRepositoryError:
        raise ValueError(f"Not a git repository: {path}")


def _configure_git_auth(repo: Repo) -> None:
    """Configure git remote auth from env vars. Called before push/pull operations."""
    auth_type = os.getenv("GIT_AUTH_TYPE", "none").lower()

    if auth_type == "https_token":
        token = os.getenv("GIT_TOKEN", "")
        if token and repo.remotes:
            url = repo.remotes["origin"].url
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
        os.environ["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"


@tool
async def git_status(repo_path: str) -> str:
    """Show staged, modified, and untracked files in the repository."""
    def _run():
        repo = _repo(repo_path)
        staged = [i.a_path for i in repo.index.diff("HEAD")] if repo.head.is_valid() else []
        modified = [i.a_path for i in repo.index.diff(None)]
        untracked = repo.untracked_files
        branch = repo.active_branch.name if not repo.head.is_detached else "HEAD (detached)"
        return (
            f"Branch: {branch}\n"
            f"Staged ({len(staged)}): {', '.join(staged) or 'none'}\n"
            f"Modified ({len(modified)}): {', '.join(modified) or 'none'}\n"
            f"Untracked ({len(untracked)}): {', '.join(untracked) or 'none'}"
        )
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"


@tool
async def git_diff(repo_path: str, file_path: str = "", staged: bool = False) -> str:
    """Return unified diff of working tree or staged changes. Truncated at 15,000 chars."""
    def _run():
        repo = _repo(repo_path)
        kwargs = {"create_patch": True}
        if file_path:
            kwargs["paths"] = [file_path]
        diffs = (repo.index.diff("HEAD", **kwargs)
                 if staged and repo.head.is_valid()
                 else repo.index.diff(None, **kwargs))
        parts = []
        for d in diffs:
            try:
                parts.append(d.diff.decode("utf-8", errors="replace"))
            except Exception:
                parts.append(str(d))
        result = "\n".join(parts) or "No changes."
        if len(result) > _MAX_DIFF_CHARS:
            result = result[:_MAX_DIFF_CHARS] + f"\n... [truncated at {_MAX_DIFF_CHARS} chars]"
        return result
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"


@tool
async def git_log(repo_path: str, max_count: int = 10) -> str:
    """Return recent commit history as JSON array of {hash, author, date, message}."""
    def _run():
        repo = _repo(repo_path)
        commits = []
        for c in repo.iter_commits(max_count=max_count):
            commits.append({
                "hash": c.hexsha[:8],
                "author": f"{c.author.name} <{c.author.email}>",
                "date": datetime.fromtimestamp(c.committed_date).strftime("%Y-%m-%d %H:%M"),
                "message": c.message.strip(),
            })
        return json.dumps(commits, indent=2)
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"


@tool
async def git_blame(repo_path: str, file_path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Return line-by-line authorship for a file."""
    def _run():
        repo = _repo(repo_path)
        blame = repo.blame("HEAD", file_path)
        lines_out = []
        line_num = 1
        for commit, lines in blame:
            for line in lines:
                if (start_line == 0 or line_num >= start_line) and (end_line == 0 or line_num <= end_line):
                    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                    lines_out.append(
                        f"{commit.hexsha[:8]} {commit.author.name:<20} "
                        f"{datetime.fromtimestamp(commit.committed_date).strftime('%Y-%m-%d')} "
                        f"L{line_num:>4}: {text.rstrip()}"
                    )
                line_num += 1
        return "\n".join(lines_out) or "No blame data."
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"


@tool
async def git_create_branch(repo_path: str, branch_name: str) -> str:
    """Create and check out a new branch. Use before applying any fixes."""
    def _run():
        repo = _repo(repo_path)
        new_branch = repo.create_head(branch_name)
        new_branch.checkout()
        return f"Created and checked out branch: {branch_name}"
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"


@tool
async def git_commit(repo_path: str, message: str, files: list = None) -> str:
    """Stage files and create a commit. Never amends or force-pushes."""
    def _run():
        if not message.strip():
            return "Error: Commit message cannot be empty."
        repo = _repo(repo_path)
        if files:
            repo.index.add(files)
        else:
            repo.git.add(A=True)
        commit = repo.index.commit(message.strip())
        return f"Committed {commit.hexsha[:8]}: {commit.message.strip()}"
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"


@tool
async def git_stash(repo_path: str, action: str = "push", message: str = "") -> str:
    """Stash (push) or restore (pop) uncommitted changes."""
    def _run():
        repo = _repo(repo_path)
        if action == "push":
            args = ["push"] + (["-m", message.strip()] if message.strip() else [])
            repo.git.stash(*args)
            return f"Stashed changes{f': {message}' if message.strip() else ''}."
        elif action == "pop":
            repo.git.stash("pop")
            return "Restored latest stash."
        else:
            return "Error: action must be 'push' or 'pop'."
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        return f"Error: {e}"
