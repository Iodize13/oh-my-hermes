"""Regression tests for the native Hermes subagent wrapper."""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent.subagents.types import SubagentsSettings


class _FakeTool:
    def __init__(self, fn):
        self.fn = fn


class _FakeToolManager:
    def __init__(self):
        self._tools = {}

    def register(self, fn):
        self._tools[fn.__name__] = _FakeTool(fn)
        return fn

    def list_tools(self):
        return list(self._tools.values())


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        self._tool_manager = _FakeToolManager()

    def tool(self):
        def decorator(fn):
            self._tool_manager.register(fn)
            return fn

        return decorator


def _make_type_config(name="general-purpose", worktree=False):
    from agent.subagents.types import SubagentTypeConfig

    return SubagentTypeConfig(
        name=name,
        description=f"{name} test type",
        toolsets=["terminal"],
        prompt_mode="append",
        default_background=False,
        readonly=False,
        worktree=worktree,
        role="leaf",
    )


@pytest.fixture
def fresh_subagents_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    import agent.subagents.config as subconfig

    importlib.reload(subconfig)
    monkeypatch.setattr(subconfig, "_runtime_config_section", lambda: {})
    return subconfig


@pytest.fixture
def fresh_subagents_service(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    import agent.subagents.service as subservice

    importlib.reload(subservice)
    subservice.reset_subagent_service_for_tests()
    return subservice


def test_load_subagents_settings_uses_project_override_and_defaults(fresh_subagents_config, tmp_path):
    project_cfg = tmp_path / ".hermes" / "subagents.yaml"
    project_cfg.parent.mkdir(parents=True)
    project_cfg.write_text(
        yaml.safe_dump(
            {
                "subagents": {
                    "max_concurrent_background": 7,
                    "default_type": "writer",
                    "types": {
                        "writer": {
                            "description": "Writer subagent",
                            "toolsets": ["terminal", "file"],
                            "worktree": True,
                            "readonly": False,
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    settings = fresh_subagents_config.load_subagents_settings(cwd=str(tmp_path))

    assert settings.project_config_path == str(project_cfg)
    assert settings.default_type == "writer"
    assert settings.max_concurrent_background == 7
    assert settings.worktrees["enabled"] is False
    assert "general-purpose" in settings.types
    assert settings.types["writer"].worktree is True
    assert settings.types["writer"].toolsets == ["terminal", "file"]


def test_resolve_subagent_type_fallback_policy(fresh_subagents_config):
    from agent.subagents.types import SubagentsSettings

    settings = SubagentsSettings(
        default_type="general-purpose",
        unknown_type_policy="fallback",
        types={"general-purpose": _make_type_config("general-purpose")},
    )

    resolved, note = fresh_subagents_config.resolve_subagent_type("missing", settings)

    assert resolved is not None
    assert resolved.name == "general-purpose"
    assert "falling back" in note


def test_subagent_service_foreground_runs_and_persists_record(fresh_subagents_service, monkeypatch, tmp_path):
    from agent.subagents.types import SubagentSpawnRequest

    settings = SubagentsSettings(
        default_type="general-purpose",
        default_max_iterations=3,
        types={"general-purpose": _make_type_config("general-purpose")},
        worktrees={"enabled": False},
    )
    monkeypatch.setattr(fresh_subagents_service, "load_subagents_settings", lambda cwd=None: settings)

    class FakeChild:
        session_id = "session-123"

    def fake_build_child_agent(**kwargs):
        return FakeChild()

    def fake_run_single_child(task_index, goal, child=None, parent_agent=None):
        return {
            "status": "completed",
            "summary": f"done: {goal}",
            "api_calls": 2,
            "model": "test-model",
            "exit_reason": "stop",
        }

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", fake_build_child_agent)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", fake_run_single_child)

    parent = SimpleNamespace(terminal_cwd=str(tmp_path), cwd=str(tmp_path))
    service = fresh_subagents_service.SubagentService(max_workers=1)
    result = service.spawn(SubagentSpawnRequest(prompt="do the thing"), parent)

    assert result["status"] == "completed"
    assert result["child_identifier"] == result["child_id"]
    assert result["result"] and "done: do the thing" in result["result"]

    state_file = Path(tmp_path / ".hermes" / "subagents" / "records.json")
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["records"][0]["child_id"] == result["child_id"]
    assert payload["records"][0]["transcript_path"] == "session-123"


def test_background_subagent_list_result_and_steer_are_persistent(fresh_subagents_service, monkeypatch, tmp_path):
    from agent.subagents.types import SubagentSpawnRequest
    import threading

    settings = SubagentsSettings(
        default_type="general-purpose",
        default_max_iterations=3,
        types={"general-purpose": _make_type_config("general-purpose")},
        worktrees={"enabled": False},
    )
    monkeypatch.setattr(fresh_subagents_service, "load_subagents_settings", lambda cwd=None: settings)

    gate = threading.Event()

    def fake_run_child(self, child_id, request, type_cfg, parent_agent, settings, parent_thread_id=None):
        with self._lock:
            rec = self._records[child_id]
            rec.status = "running"
            rec.progress["stage"] = "waiting"
            self._persist_locked()
        gate.wait(timeout=5)
        with self._lock:
            rec = self._records[child_id]
            rec.status = "completed"
            rec.result = "background complete"
            rec.duration_seconds = 1.0
            rec.completed_at = time.time()
            self._persist_locked()

    monkeypatch.setattr(fresh_subagents_service.SubagentService, "_run_child", fake_run_child)

    parent = SimpleNamespace(terminal_cwd=str(tmp_path), cwd=str(tmp_path))
    service = fresh_subagents_service.SubagentService(max_workers=1)
    spawn_result = service.spawn(SubagentSpawnRequest(prompt="background job", background=True), parent)
    child_id = spawn_result["child_id"]

    running = None
    for _ in range(100):
        running = service.result(child_id)
        if running["status"] != "pending":
            break
        time.sleep(0.02)
    assert running is not None
    assert running["status"] == "running"
    assert service.list()["subagents"][0]["child_id"] == child_id
    assert service.steer(child_id, "please continue")["status"] == "accepted"

    gate.set()
    for _ in range(50):
        final = service.result(child_id)
        if final["status"] == "completed":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("background subagent did not complete in time")

    assert final["result"] == "background complete"
    assert service.steer(child_id, "too late")["status"] == "rejected"

    state_file = Path(tmp_path / ".hermes" / "subagents" / "records.json")
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["records"][0]["status"] == "completed"
    assert payload["records"][0]["steering_messages"]


def test_subagent_service_foreground_surfaces_child_errors(fresh_subagents_service, monkeypatch, tmp_path):
    from agent.subagents.types import SubagentSpawnRequest

    settings = SubagentsSettings(
        default_type="general-purpose",
        default_max_iterations=3,
        types={"general-purpose": _make_type_config("general-purpose")},
        worktrees={"enabled": False},
    )
    monkeypatch.setattr(fresh_subagents_service, "load_subagents_settings", lambda cwd=None: settings)

    class FakeChild:
        session_id = "session-error"

    def fake_build_child_agent(**kwargs):
        return FakeChild()

    def fake_run_single_child(task_index, goal, child=None, parent_agent=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", fake_build_child_agent)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", fake_run_single_child)

    parent = SimpleNamespace(terminal_cwd=str(tmp_path), cwd=str(tmp_path))
    service = fresh_subagents_service.SubagentService(max_workers=1)
    result = service.spawn(SubagentSpawnRequest(prompt="fail please"), parent)

    assert result["status"] == "error"
    assert "boom" in result["error"]
    assert result["stage"] == "setup" or result["stage"] == "run_child"
    assert service.result(result["child_id"])["status"] == "error"


def test_background_subagent_interrupts_and_cleans_workspace_on_parent_cancel(fresh_subagents_service, monkeypatch, tmp_path):
    from agent.subagents.types import SubagentSpawnRequest, WorkspaceOutcome
    from tools.interrupt import is_interrupted, set_interrupt
    import threading

    settings = SubagentsSettings(
        default_type="general-purpose",
        default_max_iterations=3,
        types={"general-purpose": _make_type_config("general-purpose")},
        worktrees={"enabled": False},
    )
    monkeypatch.setattr(fresh_subagents_service, "load_subagents_settings", lambda cwd=None: settings)

    cleanup_statuses: list[str] = []
    started = threading.Event()

    class FakePrepared:
        cwd = str(tmp_path / "workspace")

        def dispose(self, status):
            cleanup_statuses.append(status)
            return WorkspaceOutcome(cwd=self.cwd, isolated=False, worktree_path=self.cwd)

    class FakeProvider:
        def prepare(self, context):
            return FakePrepared()

    def fake_workspace_provider(self, type_cfg, settings, parent_cwd):
        return FakeProvider()

    def fake_build_child_agent(**kwargs):
        class FakeChild:
            session_id = "session-interrupt"
        return FakeChild()

    def fake_run_single_child(task_index, goal, child=None, parent_agent=None):
        started.set()
        for _ in range(100):
            if is_interrupted():
                raise InterruptedError("child interrupted")
            time.sleep(0.02)
        return {
            "status": "completed",
            "summary": "should not reach",
            "api_calls": 1,
            "model": "test-model",
            "exit_reason": "stop",
        }

    monkeypatch.setattr(fresh_subagents_service.SubagentService, "_workspace_provider", fake_workspace_provider)
    monkeypatch.setattr("tools.delegate_tool._build_child_agent", fake_build_child_agent)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", fake_run_single_child)

    parent_tid = threading.current_thread().ident
    parent = SimpleNamespace(terminal_cwd=str(tmp_path), cwd=str(tmp_path), _execution_thread_id=parent_tid)
    service = fresh_subagents_service.SubagentService(max_workers=1)
    spawn_result = service.spawn(SubagentSpawnRequest(prompt="interrupt me", background=True), parent)
    child_id = spawn_result["child_id"]

    assert started.wait(timeout=2), "background child did not start in time"
    set_interrupt(True, thread_id=parent_tid)
    try:
        for _ in range(100):
            final = service.result(child_id)
            if final["status"] == "interrupted":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("background subagent did not interrupt in time")
    finally:
        set_interrupt(False, thread_id=parent_tid)

    assert final["status"] == "interrupted"
    assert cleanup_statuses == ["interrupted"]
    assert final["workspace"]["isolated"] is False
    assert final["stage"] in {"run_child", "interrupted"}
    persisted = json.loads((Path(tmp_path) / ".hermes" / "subagents" / "records.json").read_text(encoding="utf-8"))
    assert persisted["records"][0]["status"] == "interrupted"
    assert persisted["records"][0]["progress"]["stage"] == "interrupted"


def test_registry_discovers_wrapper_tools():
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    entry = registry.get_entry("subagent_spawn")

    assert entry is not None
    assert entry.toolset == "subagents"


def test_mcp_server_registers_subagent_bridge(monkeypatch):
    import mcp_serve

    monkeypatch.setattr(mcp_serve, "FastMCP", _FakeFastMCP)
    monkeypatch.setattr(mcp_serve, "_MCP_SERVER_AVAILABLE", True)

    server = mcp_serve.create_mcp_server(event_bridge=None)
    tool_names = {tool.fn.__name__ for tool in server._tool_manager.list_tools()}

    assert {"subagent_spawn", "subagent_result", "subagent_list", "subagent_steer"}.issubset(tool_names)
