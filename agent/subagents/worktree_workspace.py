"""Git worktree workspace provider for native subagent wrappers."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

from .types import WorkspaceOutcome
from .workspaces import PreparedWorkspace, WorkspaceContext, WorkspaceProvider


class WorktreeError(RuntimeError):
    pass


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def _require_success(cp: subprocess.CompletedProcess, stage: str) -> str:
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "").strip()
        raise WorktreeError(f"{stage} failed: {msg or 'git exited non-zero'}")
    return (cp.stdout or "").strip()


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-.")
    return text[:48] or "child"


class GitPreparedWorktree:
    def __init__(self, parent_cwd: str, cwd: str, child_id: str, branch_prefix: str):
        self.parent_cwd = parent_cwd
        self.cwd = cwd
        self.child_id = child_id
        self.branch_prefix = branch_prefix
        self.isolated = True

    def _branch_exists(self, name: str) -> bool:
        return _run_git(["rev-parse", "--verify", "--quiet", name], self.parent_cwd).returncode == 0

    def _non_conflicting_branch(self) -> str:
        base = f"{self.branch_prefix}{_slug(self.child_id)}"
        candidate = base
        i = 2
        while self._branch_exists(candidate):
            candidate = f"{base}-{i}"
            i += 1
        return candidate

    def _remove(self) -> None:
        cp = _run_git(["worktree", "remove", "--force", self.cwd], self.parent_cwd)
        if cp.returncode != 0:
            shutil.rmtree(self.cwd, ignore_errors=True)

    def dispose(self, status: str) -> WorkspaceOutcome:
        status_cp = _run_git(["status", "--porcelain"], self.cwd)
        if status_cp.returncode != 0:
            err = (status_cp.stderr or status_cp.stdout or "").strip()
            return WorkspaceOutcome(cwd=self.cwd, isolated=True, worktree_path=self.cwd, error=f"git status failed during cleanup: {err}")
        changed = bool((status_cp.stdout or "").strip())
        if status in {"interrupted", "stopped"}:
            self._remove()
            return WorkspaceOutcome(cwd=self.cwd, isolated=True, worktree_path=self.cwd)
        if not changed:
            self._remove()
            return WorkspaceOutcome(cwd=self.cwd, isolated=True, worktree_path=self.cwd)

        _require_success(_run_git(["add", "-A"], self.cwd), "git add")
        branch = self._non_conflicting_branch()
        msg = f"Subagent {self.child_id} changes"
        commit_cp = _run_git(["commit", "-m", msg], self.cwd)
        _require_success(commit_cp, "git commit")
        _require_success(_run_git(["branch", branch, "HEAD"], self.cwd), "git branch")
        self._remove()
        merge = (
            f"Worktree changes were committed to branch '{branch}'.\n"
            f"Review with: git show --stat {branch}\n"
            f"Merge with: git merge {branch}"
        )
        return WorkspaceOutcome(cwd=self.cwd, isolated=True, worktree_path=self.cwd, branch_name=branch, merge_instructions=merge)


class GitWorktreeProvider(WorkspaceProvider):
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dict(config or {})
        self.branch_prefix = str(self.config.get("branch_prefix") or "hermes-subagent-")
        self.base_dir = str(self.config.get("base_dir") or "")

    def _base_dir(self, parent_cwd: str) -> Path:
        if self.base_dir:
            base = Path(self.base_dir).expanduser()
        else:
            base = get_hermes_home() / "subagents" / "worktrees"
        base.mkdir(parents=True, exist_ok=True)
        return base.resolve()

    def prepare(self, context: WorkspaceContext) -> PreparedWorkspace:
        parent = str(Path(context.parent_cwd).resolve())
        _require_success(_run_git(["rev-parse", "--is-inside-work-tree"], parent), "git repository check")
        _require_success(_run_git(["rev-parse", "--verify", "HEAD"], parent), "git HEAD check")
        path = self._base_dir(parent) / f"{_slug(context.child_id)}-{int(time.time()*1000)}"
        cp = _run_git(["worktree", "add", "--detach", str(path), "HEAD"], parent)
        _require_success(cp, "git worktree add")
        return GitPreparedWorktree(parent, str(path), context.child_id, self.branch_prefix)

    def prune_orphans(self, parent_cwd: str) -> list[str]:
        parent = str(Path(parent_cwd).resolve())
        base = self._base_dir(parent)
        removed: list[str] = []
        if not base.exists():
            return removed
        for child in base.iterdir():
            if not child.is_dir():
                continue
            # Bounded ownership: only prune under our configured base dir with our naming convention.
            if not child.name.startswith("sa-") and "hermes" not in child.name:
                continue
            cp = _run_git(["worktree", "remove", "--force", str(child)], parent)
            if cp.returncode == 0:
                removed.append(str(child))
        _run_git(["worktree", "prune"], parent)
        return removed
