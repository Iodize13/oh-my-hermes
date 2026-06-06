#!/usr/bin/env python3
"""Native Hermes subagent wrapper tools."""
from __future__ import annotations

import json
from typing import Optional

from agent.subagents.service import get_subagent_service
from agent.subagents.types import SubagentSpawnRequest
from tools.registry import registry, tool_error


def check_subagents_requirements() -> bool:
    return True


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def subagent_spawn(
    prompt: str,
    type_name: Optional[str] = None,
    context: Optional[str] = None,
    background: Optional[bool] = None,
    inherit_context: Optional[bool] = None,
    parent_agent=None,
) -> str:
    if parent_agent is None:
        return tool_error("subagent_spawn requires a parent agent context.")
    if not prompt or not str(prompt).strip():
        return tool_error("prompt is required.")
    req = SubagentSpawnRequest(
        prompt=str(prompt),
        type_name=type_name or "general-purpose",
        context=context,
        background=background,
        inherit_context=inherit_context,
    )
    return _json(get_subagent_service().spawn(req, parent_agent))


def subagent_result(child_id: str) -> str:
    if not child_id:
        return tool_error("child_id is required.")
    return _json(get_subagent_service().result(str(child_id)))


def subagent_list() -> str:
    return _json(get_subagent_service().list())


def subagent_steer(child_id: str, message: str) -> str:
    if not child_id:
        return tool_error("child_id is required.")
    return _json(get_subagent_service().steer(str(child_id), str(message or "")))


SUBAGENT_SPAWN_SCHEMA = {
    "name": "subagent_spawn",
    "description": (
        "Spawn a named Hermes subagent through the native wrapper. Supports "
        "foreground mode (returns final structured result) and background mode "
        "(returns child_id immediately for subagent_result polling). Use this "
        "when named type policy, background lifecycle, steering, or optional "
        "per-child worktree isolation is needed. Use delegate_task for simple "
        "synchronous delegation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type_name": {"type": "string", "description": "Configured subagent type name. Defaults to general-purpose."},
            "prompt": {"type": "string", "description": "Task prompt for the child subagent."},
            "context": {"type": "string", "description": "Optional context passed to the child."},
            "background": {"type": "boolean", "description": "If true, return immediately with child_id."},
            "inherit_context": {"type": "boolean", "description": "Reserved policy flag for type configs; false by default."},
        },
        "required": ["prompt"],
    },
}

SUBAGENT_RESULT_SCHEMA = {
    "name": "subagent_result",
    "description": "Poll or retrieve a wrapper-managed background subagent's running or completed status/result.",
    "parameters": {
        "type": "object",
        "properties": {"child_id": {"type": "string", "description": "Child identifier returned by subagent_spawn."}},
        "required": ["child_id"],
    },
}

SUBAGENT_LIST_SCHEMA = {
    "name": "subagent_list",
    "description": "List active and recently completed native wrapper-managed subagents.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SUBAGENT_STEER_SCHEMA = {
    "name": "subagent_steer",
    "description": "Queue a steering message for a running wrapper-managed subagent; rejects unknown or terminal children clearly.",
    "parameters": {
        "type": "object",
        "properties": {
            "child_id": {"type": "string", "description": "Child identifier returned by subagent_spawn."},
            "message": {"type": "string", "description": "Steering message to queue for the running child."},
        },
        "required": ["child_id", "message"],
    },
}

registry.register(
    name="subagent_spawn",
    toolset="subagents",
    schema=SUBAGENT_SPAWN_SCHEMA,
    handler=lambda args, **kw: subagent_spawn(
        prompt=args.get("prompt"),
        type_name=args.get("type_name"),
        context=args.get("context"),
        background=args.get("background"),
        inherit_context=args.get("inherit_context"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_subagents_requirements,
    emoji="🧩",
)
registry.register(
    name="subagent_result",
    toolset="subagents",
    schema=SUBAGENT_RESULT_SCHEMA,
    handler=lambda args, **kw: subagent_result(args.get("child_id")),
    check_fn=check_subagents_requirements,
    emoji="🧾",
)
registry.register(
    name="subagent_list",
    toolset="subagents",
    schema=SUBAGENT_LIST_SCHEMA,
    handler=lambda args, **kw: subagent_list(),
    check_fn=check_subagents_requirements,
    emoji="📋",
)
registry.register(
    name="subagent_steer",
    toolset="subagents",
    schema=SUBAGENT_STEER_SCHEMA,
    handler=lambda args, **kw: subagent_steer(args.get("child_id"), args.get("message")),
    check_fn=check_subagents_requirements,
    emoji="🕹️",
)
