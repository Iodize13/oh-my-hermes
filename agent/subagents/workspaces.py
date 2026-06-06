"""Workspace provider seam for native subagent wrappers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from .types import SubagentTypeConfig, WorkspaceOutcome


@dataclass
class WorkspaceContext:
    child_id: str
    type_config: SubagentTypeConfig
    parent_cwd: str


class PreparedWorkspace(Protocol):
    cwd: str
    isolated: bool

    def dispose(self, status: str) -> WorkspaceOutcome:
        ...


class WorkspaceProvider(Protocol):
    def prepare(self, context: WorkspaceContext) -> PreparedWorkspace:
        ...


class NoopPreparedWorkspace:
    def __init__(self, cwd: str):
        self.cwd = str(Path(cwd).resolve())
        self.isolated = False

    def dispose(self, status: str) -> WorkspaceOutcome:
        return WorkspaceOutcome(cwd=self.cwd, isolated=False)


class NoopWorkspaceProvider:
    def prepare(self, context: WorkspaceContext) -> PreparedWorkspace:
        return NoopPreparedWorkspace(context.parent_cwd)
