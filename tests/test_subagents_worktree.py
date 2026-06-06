"""Tests for the native subagent git worktree provider."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.subagents.types import SubagentTypeConfig
from agent.subagents.worktree_workspace import GitWorktreeProvider, WorktreeError
from agent.subagents.workspaces import WorkspaceContext


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Hermes Test"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-m", "init"], repo)


def _type_cfg(worktree: bool = True) -> SubagentTypeConfig:
    return SubagentTypeConfig(
        name="isolated",
        description="isolated worktree test type",
        toolsets=["terminal"],
        prompt_mode="append",
        worktree=worktree,
    )


def test_prepare_fails_when_not_a_git_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    provider = GitWorktreeProvider({"enabled": True})

    with pytest.raises(WorktreeError):
        provider.prepare(WorkspaceContext(child_id="sa-1", type_config=_type_cfg(), parent_cwd=str(tmp_path)))


def test_prepare_fails_when_head_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Hermes Test"], repo)
    head = repo / ".git" / "HEAD"
    head.unlink()

    provider = GitWorktreeProvider({"enabled": True})
    with pytest.raises(WorktreeError):
        provider.prepare(WorkspaceContext(child_id="sa-1", type_config=_type_cfg(), parent_cwd=str(repo)))


def test_no_change_cleanup_removes_temp_worktree(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    provider = GitWorktreeProvider({"enabled": True})
    prepared = provider.prepare(WorkspaceContext(child_id="sa-1", type_config=_type_cfg(), parent_cwd=str(repo)))
    worktree_path = Path(prepared.cwd)
    assert worktree_path.exists()
    assert str(tmp_path / ".hermes" / "subagents" / "worktrees") in prepared.cwd

    outcome = prepared.dispose("completed")

    assert not worktree_path.exists()
    assert outcome.branch_name is None
    assert outcome.merge_instructions is None


def test_changed_worktree_creates_branch_and_merge_instructions(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    provider = GitWorktreeProvider({"enabled": True})
    prepared = provider.prepare(WorkspaceContext(child_id="sa-branch", type_config=_type_cfg(), parent_cwd=str(repo)))
    worktree_path = Path(prepared.cwd)
    (worktree_path / "README.md").write_text("changed\n", encoding="utf-8")
    (worktree_path / "extra.txt").write_text("new file\n", encoding="utf-8")

    outcome = prepared.dispose("completed")

    assert outcome.branch_name
    assert outcome.merge_instructions and outcome.branch_name in outcome.merge_instructions
    assert not worktree_path.exists()
    _run(["git", "rev-parse", "--verify", outcome.branch_name], repo)


def test_branch_conflict_gets_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    provider = GitWorktreeProvider({"enabled": True})
    prepared = provider.prepare(WorkspaceContext(child_id="sa-conflict", type_config=_type_cfg(), parent_cwd=str(repo)))
    worktree_path = Path(prepared.cwd)
    (worktree_path / "README.md").write_text("changed\n", encoding="utf-8")

    base_branch = "hermes-subagent-sa-conflict"
    _run(["git", "branch", base_branch], repo)
    outcome = prepared.dispose("completed")

    assert outcome.branch_name and outcome.branch_name.startswith(base_branch)
    assert outcome.branch_name != base_branch
    assert outcome.branch_name.endswith("-2")


def test_prune_orphans_does_not_touch_unrelated_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    owned = tmp_path / "owned"
    owned.mkdir()
    unrelated = owned / "keep-me"
    unrelated.mkdir()
    maybe_owned = owned / "sa-orphan"
    maybe_owned.mkdir()

    provider = GitWorktreeProvider({"base_dir": str(owned), "enabled": True})
    removed = provider.prune_orphans(str(repo))

    assert unrelated.exists()
    assert maybe_owned.exists() or str(maybe_owned) in removed or removed == []
