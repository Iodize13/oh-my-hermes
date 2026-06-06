"""Native Hermes subagent wrapper service."""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.interrupt import _interrupted_threads, _lock as _interrupt_lock, set_interrupt as _set_interrupt

from .config import load_subagents_settings, resolve_subagent_type
from .types import SubagentResultRecord, SubagentSpawnRequest, SubagentTypeConfig
from .workspaces import NoopWorkspaceProvider, WorkspaceContext
from .worktree_workspace import GitWorktreeProvider, WorktreeError

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "failed", "error", "timeout", "interrupted", "stopped"}


def _parent_cwd(parent_agent: Any) -> str:
    for attr in ("terminal_cwd", "cwd"):
        val = getattr(parent_agent, attr, None)
        if val:
            return str(Path(str(val)).expanduser().resolve())
    return str(Path.cwd().resolve())


def _clone_parent_with_cwd(parent_agent: Any, cwd: str) -> Any:
    try:
        clone = copy.copy(parent_agent)
    except Exception:
        return parent_agent
    try:
        clone.terminal_cwd = cwd
        clone.cwd = cwd
    except Exception:
        pass
    return clone


def _thread_interrupted(thread_id: Optional[int]) -> bool:
    if thread_id is None:
        return False
    with _interrupt_lock:
        return thread_id in _interrupted_threads


def _mirror_interrupt(source_thread_id: Optional[int], target_thread_id: Optional[int], stop_event: threading.Event) -> None:
    if source_thread_id is None or target_thread_id is None or source_thread_id == target_thread_id:
        return
    while not stop_event.wait(0.05):
        if _thread_interrupted(source_thread_id):
            try:
                _set_interrupt(True, target_thread_id)
            except Exception:
                logger.debug("Failed to mirror interrupt from %s to %s", source_thread_id, target_thread_id, exc_info=True)
            return


def _compose_goal(prompt: str, context: Optional[str], type_cfg: SubagentTypeConfig, workspace_cwd: str) -> tuple[str, Optional[str]]:
    policy_lines = []
    if type_cfg.description:
        policy_lines.append(f"Subagent type: {type_cfg.name} — {type_cfg.description}")
    if type_cfg.readonly:
        policy_lines.append("Policy: read-only. Inspect and report; do not intentionally modify files.")
    if type_cfg.system_prompt:
        policy_lines.append(type_cfg.system_prompt)
    policy_lines.append(f"Workspace: {workspace_cwd}")
    policy = "\n".join(policy_lines)
    if type_cfg.prompt_mode == "replace" and policy:
        return f"{policy}\n\nTask:\n{prompt}", context
    merged_context = "\n\n".join([p for p in [policy, context] if p]) or None
    return prompt, merged_context


class SubagentService:
    def __init__(self, max_workers: int = 8):
        self._records: Dict[str, SubagentResultRecord] = {}
        self._futures: Dict[str, Future] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hermes-subagent-wrapper")
        self._state_root = get_hermes_home() / "subagents"
        self._state_root.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_root / "records.json"
        self._load_persisted_records()

    def _load_persisted_records(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Could not load persisted subagent records from %s", self._state_path)
            return
        payload = raw.get("records") if isinstance(raw, dict) else raw
        if not isinstance(payload, list):
            return
        with self._lock:
            for item in payload:
                if not isinstance(item, dict):
                    continue
                try:
                    rec = SubagentResultRecord.from_dict(item)
                except Exception:
                    continue
                if rec.child_id:
                    self._records[rec.child_id] = rec

    def _persist_locked(self) -> None:
        payload = {
            "records": [r.to_dict() for r in sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)],
        }
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def spawn(self, request: SubagentSpawnRequest, parent_agent: Any) -> Dict[str, Any]:
        settings = load_subagents_settings(_parent_cwd(parent_agent))
        type_cfg, note = resolve_subagent_type(request.type_name, settings)
        if type_cfg is None:
            return {"status": "error", "stage": "resolve_type", "error": note}
        child_id = f"sa-{uuid.uuid4().hex[:10]}"
        background = type_cfg.default_background if request.background is None else bool(request.background)
        parent_thread_id = getattr(parent_agent, "_execution_thread_id", None) or threading.current_thread().ident
        record = SubagentResultRecord(
            child_id=child_id,
            type_name=type_cfg.name,
            description=type_cfg.description,
            status="pending",
            prompt=request.prompt,
            started_at=time.time(),
        )
        if note:
            record.progress["note"] = note
        record.progress["parent_thread_id"] = parent_thread_id
        with self._lock:
            self._records[child_id] = record
            self._persist_locked()
        if background:
            fut = self._executor.submit(self._run_child, child_id, request, type_cfg, parent_agent, settings, parent_thread_id)
            with self._lock:
                self._futures[child_id] = fut
            return {"status": "running", "child_id": child_id, "child_identifier": child_id, "type_name": type_cfg.name, "description": type_cfg.description}
        self._run_child(child_id, request, type_cfg, parent_agent, settings, parent_thread_id)
        return self.result(child_id)

    def _workspace_provider(self, type_cfg: SubagentTypeConfig, settings, parent_cwd: str):
        if type_cfg.worktree:
            if settings.worktrees and settings.worktrees.get("enabled") is False:
                raise WorktreeError("Subagent type requires worktree isolation but subagents.worktrees.enabled is false")
            provider = GitWorktreeProvider(settings.worktrees)
            if settings.worktrees.get("cleanup_orphans_on_start"):
                try:
                    provider.prune_orphans(parent_cwd)
                except Exception:
                    logger.debug("Worktree orphan pruning failed", exc_info=True)
            return provider
        return NoopWorkspaceProvider()

    def _run_child(self, child_id: str, request: SubagentSpawnRequest, type_cfg: SubagentTypeConfig, parent_agent: Any, settings, parent_thread_id: Optional[int]) -> None:
        start = time.monotonic()
        parent_cwd = _parent_cwd(parent_agent)
        prepared = None
        workspace_outcome = None
        worker_tid = threading.current_thread().ident
        monitor_stop = threading.Event()
        monitor_thread = None

        def _stop_monitor() -> None:
            monitor_stop.set()
            if monitor_thread is not None and monitor_thread.is_alive():
                monitor_thread.join(timeout=0.2)
            if worker_tid is not None:
                try:
                    _set_interrupt(False, worker_tid)
                except Exception:
                    logger.debug("Failed to clear mirrored interrupt for %s", worker_tid, exc_info=True)

        try:
            with self._lock:
                self._records[child_id].status = "running"
                self._records[child_id].progress["stage"] = "prepare_workspace"
                self._persist_locked()
            provider = self._workspace_provider(type_cfg, settings, parent_cwd)
            prepared = provider.prepare(WorkspaceContext(child_id=child_id, type_config=type_cfg, parent_cwd=parent_cwd))
            run_parent = _clone_parent_with_cwd(parent_agent, prepared.cwd)
            goal, ctx = _compose_goal(request.prompt, request.context, type_cfg, prepared.cwd)
            with self._lock:
                self._records[child_id].progress["stage"] = "run_child"
                self._persist_locked()
            from tools.delegate_tool import _build_child_agent, _run_single_child
            child = _build_child_agent(
                task_index=0,
                goal=goal,
                context=ctx,
                toolsets=type_cfg.toolsets,
                model=type_cfg.model,
                max_iterations=type_cfg.max_iterations or settings.default_max_iterations,
                task_count=1,
                parent_agent=run_parent,
                override_provider=type_cfg.provider,
                override_base_url=type_cfg.base_url,
                override_api_key=type_cfg.api_key,
                override_api_mode=type_cfg.api_mode,
                role=type_cfg.role,
            )
            setattr(child, "_subagent_id", child_id)
            if parent_thread_id is not None and parent_thread_id != worker_tid:
                monitor_thread = threading.Thread(target=_mirror_interrupt, args=(parent_thread_id, worker_tid, monitor_stop), daemon=True)
                monitor_thread.start()
            result = _run_single_child(0, goal, child=child, parent_agent=run_parent)
            status = result.get("status") or "failed"
            summary = result.get("summary")
            error = result.get("error")
            if _thread_interrupted(parent_thread_id):
                status = "interrupted"
                summary = None
                error = "parent interrupted"
            try:
                workspace_outcome = prepared.dispose(status)
                if workspace_outcome.merge_instructions and status not in {"interrupted", "stopped"}:
                    summary = (summary or "") + "\n\n" + workspace_outcome.merge_instructions
            except Exception as exc:
                status = "failed" if status not in {"interrupted", "stopped"} else status
                error = f"workspace cleanup failed: {exc}"
            _stop_monitor()
            with self._lock:
                rec = self._records[child_id]
                rec.status = status
                rec.result = summary
                rec.error = error
                rec.stage = result.get("exit_reason")
                rec.duration_seconds = round(time.monotonic() - start, 2)
                rec.completed_at = time.time()
                rec.workspace = workspace_outcome
                rec.progress.update({"api_calls": result.get("api_calls", 0), "model": result.get("model")})
                rec.transcript_path = getattr(child, "session_id", None)
                if status == "interrupted":
                    rec.progress["stage"] = "interrupted"
                self._persist_locked()
        except InterruptedError as exc:
            logger.info("subagent wrapper child %s interrupted", child_id)
            if prepared is not None:
                try:
                    workspace_outcome = prepared.dispose("interrupted")
                except Exception as cleanup_exc:
                    logger.debug("Interrupted subagent cleanup failed for %s", child_id, exc_info=True)
                    exc = RuntimeError(f"{exc}; cleanup also failed: {cleanup_exc}")
            _stop_monitor()
            with self._lock:
                rec = self._records[child_id]
                rec.status = "interrupted"
                rec.error = str(exc)
                rec.stage = rec.progress.get("stage") or "setup"
                rec.duration_seconds = round(time.monotonic() - start, 2)
                rec.completed_at = time.time()
                rec.workspace = workspace_outcome
                rec.progress["stage"] = "interrupted"
                self._persist_locked()
        except Exception as exc:
            logger.exception("subagent wrapper child %s failed", child_id)
            if prepared is not None:
                try:
                    workspace_outcome = prepared.dispose("failed")
                except Exception as cleanup_exc:
                    workspace_outcome = None
                    exc = RuntimeError(f"{exc}; cleanup also failed: {cleanup_exc}")
            status = "interrupted" if _thread_interrupted(parent_thread_id) else "error"
            _stop_monitor()
            with self._lock:
                rec = self._records[child_id]
                rec.status = status
                rec.error = str(exc)
                rec.stage = rec.progress.get("stage") or "setup"
                rec.duration_seconds = round(time.monotonic() - start, 2)
                rec.completed_at = time.time()
                rec.workspace = workspace_outcome
                if status == "interrupted":
                    rec.progress["stage"] = "interrupted"
                self._persist_locked()

    def result(self, child_id: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._records.get(child_id)
            if not rec:
                return {"status": "error", "error": f"Unknown subagent '{child_id}'"}
            return rec.to_dict()

    def list(self) -> Dict[str, Any]:
        with self._lock:
            items = [r.to_dict() for r in self._records.values()]
        items.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return {"status": "ok", "subagents": items[:50]}

    def steer(self, child_id: str, message: str) -> Dict[str, Any]:
        if not message or not str(message).strip():
            return {"status": "rejected", "reason": "message is required"}
        with self._lock:
            rec = self._records.get(child_id)
            if not rec:
                return {"status": "rejected", "reason": f"Unknown subagent '{child_id}'"}
            if rec.status in _TERMINAL_STATUSES:
                return {"status": "rejected", "reason": f"Subagent '{child_id}' is terminal ({rec.status})"}
            rec.steering_messages.append({"message": message, "created_at": time.time()})
            self._persist_locked()
            return {"status": "accepted", "child_id": child_id, "queued": len(rec.steering_messages)}

_SERVICE: Optional[SubagentService] = None
_SERVICE_LOCK = threading.Lock()


def get_subagent_service() -> SubagentService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = SubagentService()
        return _SERVICE


def reset_subagent_service_for_tests() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = SubagentService()
