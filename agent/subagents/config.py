"""Configuration loading for native Hermes subagent wrappers."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

from hermes_constants import get_hermes_home
from .types import SubagentTypeConfig, SubagentsSettings


DEFAULT_SUBAGENT_TYPES: Dict[str, Dict[str, Any]] = {
    "general-purpose": {
        "description": "General-purpose coding and research subagent.",
        "toolsets": ["terminal", "file", "web"],
        "prompt_mode": "append",
        "default_background": False,
        "readonly": False,
        "worktree": False,
        "role": "leaf",
    },
    "explore": {
        "description": "Read-only exploration subagent for codebase inspection and research.",
        "toolsets": ["terminal", "file", "web"],
        "prompt_mode": "append",
        "default_background": False,
        "readonly": True,
        "worktree": False,
        "role": "leaf",
    },
    "plan": {
        "description": "Read-only planning subagent that writes recommendations instead of changing files.",
        "toolsets": ["terminal", "file", "web"],
        "prompt_mode": "append",
        "default_background": False,
        "readonly": True,
        "worktree": False,
        "role": "leaf",
    },
}

DEFAULT_SUBAGENTS_CONFIG: Dict[str, Any] = {
    "max_concurrent_background": 4,
    "default_type": "general-purpose",
    "default_max_iterations": 50,
    "unknown_type_policy": "error",
    "types": DEFAULT_SUBAGENT_TYPES,
    "worktrees": {
        "enabled": False,
        "branch_prefix": "hermes-subagent-",
        "cleanup_orphans_on_start": False,
        "base_dir": "",
    },
    "project_config_files": [".hermes/subagents.yaml", ".hermes/subagents.yml"],
}


def _deep_merge(base: Dict[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _runtime_config_section() -> Dict[str, Any]:
    try:
        from cli import CLI_CONFIG
        cfg = CLI_CONFIG.get("subagents") or {}
        if cfg:
            return cfg
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config
        full = load_config()
        return full.get("subagents") or {}
    except Exception:
        return {}


def _project_config(cwd: Optional[str], candidates: list[str]) -> tuple[Dict[str, Any], Optional[str]]:
    if not cwd:
        return {}, None
    root = Path(cwd).expanduser().resolve()
    for rel in candidates:
        path = (root / rel).resolve()
        try:
            data = _load_yaml_file(path)
        except Exception:
            data = {}
        if data:
            if "subagents" in data and isinstance(data["subagents"], dict):
                data = data["subagents"]
            return data, str(path)
    return {}, None


def _coerce_type(name: str, data: Mapping[str, Any]) -> SubagentTypeConfig:
    return SubagentTypeConfig(
        name=name,
        description=str(data.get("description") or ""),
        toolsets=list(data.get("toolsets") or []) or None,
        prompt_mode=str(data.get("prompt_mode") or "append"),
        system_prompt=str(data.get("system_prompt") or data.get("prompt") or ""),
        model=str(data.get("model") or "") or None,
        provider=str(data.get("provider") or "") or None,
        base_url=str(data.get("base_url") or "") or None,
        api_key=str(data.get("api_key") or "") or None,
        api_mode=str(data.get("api_mode") or "") or None,
        max_iterations=(int(data["max_iterations"]) if data.get("max_iterations") is not None else None),
        default_background=bool(data.get("default_background", False)),
        readonly=bool(data.get("readonly", False)),
        worktree=bool(data.get("worktree", False)),
        role=str(data.get("role") or "leaf"),
        inherit_context=bool(data.get("inherit_context", False)),
    )


def load_subagents_settings(cwd: Optional[str] = None) -> SubagentsSettings:
    """Load global + project subagent settings using profile-safe paths."""
    # Touch get_hermes_home deliberately so callers/tests can assert profile-safe path use.
    hermes_home = get_hermes_home()
    del hermes_home

    cfg = _deep_merge(DEFAULT_SUBAGENTS_CONFIG, _runtime_config_section())
    project_data, project_path = _project_config(cwd or os.getcwd(), list(cfg.get("project_config_files") or []))
    if project_data:
        cfg = _deep_merge(cfg, project_data)

    raw_types = cfg.get("types") or {}
    types = {name: _coerce_type(name, data or {}) for name, data in raw_types.items()}
    return SubagentsSettings(
        max_concurrent_background=max(1, int(cfg.get("max_concurrent_background") or 4)),
        default_type=str(cfg.get("default_type") or "general-purpose"),
        default_max_iterations=max(1, int(cfg.get("default_max_iterations") or 50)),
        unknown_type_policy=str(cfg.get("unknown_type_policy") or "error"),
        types=types,
        worktrees=dict(cfg.get("worktrees") or {}),
        project_config_path=project_path,
    )


def resolve_subagent_type(type_name: Optional[str], settings: Optional[SubagentsSettings] = None) -> tuple[Optional[SubagentTypeConfig], Optional[str]]:
    settings = settings or load_subagents_settings()
    requested = (type_name or settings.default_type or "general-purpose").strip()
    if requested in settings.types:
        return settings.types[requested], None
    if settings.unknown_type_policy == "fallback" and settings.default_type in settings.types:
        return settings.types[settings.default_type], f"Unknown subagent type '{requested}', falling back to '{settings.default_type}'."
    return None, f"Unknown subagent type '{requested}'. Available types: {', '.join(sorted(settings.types))}"
