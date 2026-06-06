"""Typed structures for Hermes native subagent wrappers."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class SubagentTypeConfig:
    name: str
    description: str = ""
    toolsets: Optional[List[str]] = None
    prompt_mode: str = "append"  # append | replace
    system_prompt: str = ""
    model: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_mode: Optional[str] = None
    max_iterations: Optional[int] = None
    default_background: bool = False
    readonly: bool = False
    worktree: bool = False
    role: str = "leaf"
    inherit_context: bool = False


@dataclass
class SubagentsSettings:
    max_concurrent_background: int = 4
    default_type: str = "general-purpose"
    default_max_iterations: int = 50
    unknown_type_policy: str = "error"  # error | fallback
    types: Dict[str, SubagentTypeConfig] = field(default_factory=dict)
    worktrees: Dict[str, Any] = field(default_factory=dict)
    project_config_path: Optional[str] = None


@dataclass
class SubagentSpawnRequest:
    prompt: str
    type_name: str = "general-purpose"
    context: Optional[str] = None
    background: Optional[bool] = None
    inherit_context: Optional[bool] = None


@dataclass
class WorkspaceOutcome:
    cwd: str
    isolated: bool = False
    worktree_path: Optional[str] = None
    branch_name: Optional[str] = None
    merge_instructions: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["WorkspaceOutcome"]:
        if not isinstance(data, dict):
            return None
        return cls(
            cwd=str(data.get("cwd") or ""),
            isolated=bool(data.get("isolated", False)),
            worktree_path=data.get("worktree_path") or None,
            branch_name=data.get("branch_name") or None,
            merge_instructions=data.get("merge_instructions") or None,
            error=data.get("error") or None,
        )


@dataclass
class SubagentResultRecord:
    child_id: str
    type_name: str
    description: str
    status: str
    prompt: str
    started_at: float
    completed_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    stage: Optional[str] = None
    transcript_path: Optional[str] = None
    workspace: Optional[WorkspaceOutcome] = None
    progress: Dict[str, Any] = field(default_factory=dict)
    steering_messages: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["child_identifier"] = self.child_id
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubagentResultRecord":
        workspace = WorkspaceOutcome.from_dict(data.get("workspace"))
        return cls(
            child_id=str(data.get("child_id") or data.get("child_identifier") or ""),
            type_name=str(data.get("type_name") or ""),
            description=str(data.get("description") or ""),
            status=str(data.get("status") or ""),
            prompt=str(data.get("prompt") or ""),
            started_at=float(data.get("started_at") or 0.0),
            completed_at=(float(data["completed_at"]) if data.get("completed_at") is not None else None),
            duration_seconds=(float(data["duration_seconds"]) if data.get("duration_seconds") is not None else None),
            result=data.get("result") or None,
            error=data.get("error") or None,
            stage=data.get("stage") or None,
            transcript_path=data.get("transcript_path") or None,
            workspace=workspace,
            progress=dict(data.get("progress") or {}),
            steering_messages=list(data.get("steering_messages") or []),
        )


@dataclass
class SteeringRequest:
    child_id: str
    message: str
    created_at: float
