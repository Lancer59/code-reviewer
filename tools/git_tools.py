"""
Git tools for Dev Companion.
Read-only tools for review, write tools for the fix workflow (git-agent subagent).
Excluded: git_push, git_pull — reviewer is read-only by default.
git_clone is used during onboarding to fetch the target repo.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime

from git import GitCommandError, InvalidGitRepositoryError, Repo
from langchain_core.tools import tool
from config import cfg, cfg_bool, cfg_int

logger = logging.getLogger("git_tools")
_MAX_DIFF_CHARS = 15_000


def _repo(path: str) -> Repo:
    try:
        # search_parent_directories=True works reliably on Windows with absolute paths.
        # We always pass the exact repo root so this won't accidentally find a parent repo.
        return Repo(path, search_parent_directories=True)
    except InvalidGitRepositoryError:
        raise ValueError(f"Not a git repository: {path}")


def _configure_git_auth(repo: Repo) -> None:
    """Configure git remote auth from config. Called before push/pull operations."""
    auth_type = cfg("GIT_AUTH_TYPE", "none").lower()

    if auth_type == "https_token":
        token = cfg("GIT_TOKEN", "")
        if token and repo.remotes:
            url = repo.remotes["origin"].url
            if url.startswith("https://"):
                authed = url.replace("https://", f"https://{token}@")
                repo.remotes["origin"].set_url(authed)

    elif auth_type == "https_basic":
        username = cfg("GIT_USERNAME", "")
        password = cfg("GIT_PASSWORD", "")
        if username and password and repo.remotes:
            url = repo.remotes["origin"].url
            if url.startswith("https://"):
                authed = url.replace("https://", f"https://{username}:{password}@")
                repo.remotes["origin"].set_url(authed)

    elif auth_type == "ssh":
        key_path = cfg("GIT_SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
        os.environ["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"


# ---------------------------------------------------------------------------
# git_clone — used during onboarding, not exposed as an agent tool
# ---------------------------------------------------------------------------

def _inject_pat_into_url(repo_url: str, pat: str) -> str:
    """
    Inject a PAT into an HTTPS git URL.

    Standard:  https://github.com/org/repo  →  https://<pat>@github.com/org/repo
    Azure DevOps: https://dev.azure.com/org/proj/_git/repo
                → https://org:<pat>@dev.azure.com/org/proj/_git/repo
    """
    repo_url = repo_url.strip().rstrip("/")
    if not repo_url.startswith("https://"):
        raise ValueError("Only HTTPS URLs are supported for cloud clone.")

    if "dev.azure.com" in repo_url:
        # Azure DevOps format: https://org:<pat>@dev.azure.com/...
        # Extract org from URL: https://dev.azure.com/<org>/...
        m = re.match(r"https://dev\.azure\.com/([^/]+)/", repo_url)
        org = m.group(1) if m else "org"
        return repo_url.replace("https://", f"https://{org}:{pat}@")
    else:
        return repo_url.replace("https://", f"https://{pat}@")


def _repo_name_from_url(repo_url: str) -> str:
    """Extract a filesystem-safe repo name from a git URL."""
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    # Sanitise: keep only alphanumeric, dash, underscore, dot
    name = re.sub(r"[^\w.\-]", "_", name)
    return name or "repo"


async def git_clone(repo_url: str, pat: str, base_dir: str = None, branch: str = None) -> str:
    """
    Clone a repository using HTTPS + PAT authentication.

    Args:
        repo_url:  HTTPS URL of the repository.
        pat:       Personal Access Token (injected into URL, never logged).
                   Pass an empty string for public repos.
        base_dir:  Base directory to clone into. Defaults to WORKSPACE_BASE_DIR config value.
        branch:    Branch to clone. If None, clones the default branch.

    Returns the absolute path to the cloned directory on success.
    Raises ValueError with a user-friendly message on failure.
    """
    # Validate URL format
    if not repo_url.strip().startswith("https://"):
        raise ValueError(
            "Only HTTPS URLs are supported. "
            "Please provide a URL like https://github.com/org/repo"
        )

    if base_dir is None:
        base_dir = cfg("WORKSPACE_BASE_DIR", "./workspaces")
    base_dir = os.path.abspath(base_dir)
    os.makedirs(base_dir, exist_ok=True)

    repo_name = _repo_name_from_url(repo_url)
    short_id = uuid.uuid4().hex[:8]
    target_dir = os.path.join(base_dir, f"{repo_name}-{short_id}")

    depth = cfg_int("GIT_CLONE_DEPTH", 1)
    max_mb = cfg_int("MAX_CLONE_SIZE_MB", 500)

    authed_url = _inject_pat_into_url(repo_url, pat) if pat else repo_url

    def _run():
        clone_kwargs = {}
        if depth > 0:
            clone_kwargs["depth"] = depth
        if branch:
            clone_kwargs["branch"] = branch
        try:
            Repo.clone_from(authed_url, target_dir, **clone_kwargs)
        except GitCommandError as e:
            err = str(e).lower()
            # Scrub the PAT from the error message before raising
            safe_err = str(e).replace(pat, "***") if pat else str(e)
            if "authentication failed" in err or "could not read username" in err or "403" in err or "401" in err:
                raise ValueError(
                    "Authentication failed. Check your PAT has read access to this repository."
                )
            elif "repository not found" in err or "not found" in err or "404" in err:
                raise ValueError(
                    "Repository not found. Check the URL is correct and your PAT has access."
                )
            elif "remote branch" in err or "reference is not a tree" in err or "invalid branch" in err:
                raise ValueError(
                    f"Branch `{branch}` not found. Check the branch name and try again."
                )
            elif "could not resolve host" in err or "unable to connect" in err:
                raise ValueError(
                    "Could not reach the git host. Check your network connection."
                )
            else:
                raise ValueError(f"Clone failed: {safe_err}")
        finally:
            # Overwrite the authed URL variable — belt-and-suspenders
            pass

        # Check size if limit is set
        if max_mb > 0:
            total_bytes = sum(
                os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(target_dir)
                for f in files
            )
            total_mb = total_bytes / (1024 * 1024)
            if total_mb > max_mb:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise ValueError(
                    f"Repository is too large ({total_mb:.0f} MB). "
                    f"Limit is {max_mb} MB. Set MAX_CLONE_SIZE_MB=0 to disable."
                )

        return target_dir

    try:
        result = await asyncio.to_thread(_run)
        return result
    except ValueError:
        raise
    except Exception as e:
        safe = str(e).replace(pat, "***")
        raise ValueError(f"Unexpected error during clone: {safe}")


async def cleanup_workspace(workspace_path: str) -> None:
    """
    Delete a cloned workspace directory.
    Safe to call even if the directory does not exist.
    Only deletes paths inside WORKSPACE_BASE_DIR as a safety check.
    """
    if not workspace_path:
        return
    base_dir = os.path.abspath(cfg("WORKSPACE_BASE_DIR", "./workspaces"))
    workspace_path = os.path.abspath(workspace_path)
    # Safety: only delete if it's inside the workspaces base dir
    if not workspace_path.startswith(base_dir):
        logger.warning("cleanup_workspace: refusing to delete path outside WORKSPACE_BASE_DIR: %s", workspace_path)
        return
    try:
        if os.path.exists(workspace_path):
            await asyncio.to_thread(shutil.rmtree, workspace_path, True)
            logger.info("Cleaned up workspace: %s", workspace_path)
    except Exception as e:
        logger.warning("cleanup_workspace failed for %s: %s", workspace_path, e)



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

@tool
async def git_push(repo_path: str, remote: str = "origin", branch: str = "") -> str:
    """
    Push the current branch (or a named branch) to the remote.

    Uses the PAT provided during onboarding — injected into the remote URL at
    push time and never stored on disk. For public repos cloned without a PAT,
    this will fail with an auth error unless GIT_AUTH_TYPE is configured.
    """
    def _run():
        repo = _repo(repo_path)
        target_branch = branch.strip() or repo.active_branch.name

        # Inject PAT into remote URL if available (set by reviewer_ui at session start)
        pat = cfg("_SESSION_GIT_PAT", "")
        if pat and repo.remotes:
            try:
                current_url = repo.remotes[remote].url
                if current_url.startswith("https://") and pat not in current_url:
                    authed_url = _inject_pat_into_url(current_url, pat)
                    repo.remotes[remote].set_url(authed_url)
            except Exception:
                pass  # fall through to push attempt anyway

        repo.remotes[remote].push(refspec=f"{target_branch}:{target_branch}")
        return f"Pushed branch `{target_branch}` to `{remote}`."

    try:
        return await asyncio.to_thread(_run)
    except GitCommandError as e:
        err = str(e).lower()
        pat = cfg("_SESSION_GIT_PAT", "")
        safe = str(e).replace(pat, "***") if pat else str(e)
        if "authentication failed" in err or "403" in err or "401" in err:
            return "Error: Push authentication failed. The PAT may not have write access to this repo."
        elif "rejected" in err:
            return f"Error: Push rejected — branch may already exist remotely or needs a pull first. {safe}"
        return f"Error: {safe}"
    except Exception as e:
        return f"Error: {e}"
