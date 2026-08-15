"""Workflow executor (M8): scheduling, step execution, cancellation, recovery.

The executor consumes injected hooks for resource/agent operations so it
stays testable without a live daemon.  State transitions are persisted to
SQLite before/after each significant change; restart recovery never
invents success (SPEC §12.3).
"""

import json
import subprocess
import threading
import time

from adcc.orchestrator.models import (
    STEP_STATUSES,
    validate_dag,
)

GLOBAL_PARALLELISM = 4


class ExecutorHooks:
    """Dependency-injection seam; server.py provides the real callbacks."""

    def resolve_resource(self, resource_id):
        raise NotImplementedError

    def get_workflow_definition(self, workflow_id):
        raise NotImplementedError

    def start_resource(self, resource_id):
        raise NotImplementedError

    def stop_resource(self, resource_id):
        raise NotImplementedError

    def resource_alive(self, resource_id):
        raise NotImplementedError

    def resource_run_status(self, resource_id):
        raise NotImplementedError

    def start_agent_session(self, adapter_id, project_id, prompt):
        raise NotImplementedError

    def stop_agent_session(self, session_id):
        raise NotImplementedError

    def get_agent_session(self, session_id):
        raise NotImplementedError

    def agent_session_alive(self, session_id):
        raise NotImplementedError

    def project_root(self, project_id):
        raise NotImplementedError


class WorkflowExecutor:
    def __init__(self, db, hooks, locks, events=None):
        self._db = db
        self._hooks = hooks
        self._locks = locks
        self._events = events
        self._guard = threading.RLock()
        self._timers = {}  # step_run_id -> deadline monotonic
        self._cancel_requested = set()
        self._cancel_lock = threading.Lock()

    # ------------------------------------------------------------ run CRUD

    def create_run(self, workflow, project_id):
        run = {
            "id": __import__("adcc.orchestrator.models", fromlist=["new_id"]).new_id(),
            "workflow_id": workflow.get("id"),
            "workflow_version": workflow.get("version", 1),
            "project_id": project_id,
            "name": workflow.get("name"),
            "status": "running",
            "started_at": int(time.time()),
            "ended_at": None,
            "locks_held": "{}",
            "created_at": int(time.time()),
        }
        self._db.insert_workflow_run(run)
        for step in workflow.get("steps", []):
            self._db.insert_step_run({
                "id": __import__("adcc.orchestrator.models",
                                 fromlist=["new_id"]).new_id(),
                "run_id": run["id"],
                "step_id": step["id"],
                "kind": step["kind"],
                "status": "pending",
                "retries": 0,
                "run_ref": None,
                "started_at": None,
                "ended_at": None,
                "error": None,
                "created_at": int(time.time()),
            })
        return run

    # ------------------------------------------------------------ lifecycle

    def start(self, workflow, project_id):
        """Create a run and drive scheduling; returns (run, error)."""
        try:
            validate_dag(workflow)
        except ValueError as exc:
            return None, str(exc)
        run = self.create_run(workflow, project_id)
        self._publish("workflow.started", run["id"])
        self._schedule(run["id"], workflow)
        return run, None

    def _publish(self, event_type, data=None):
        if self._events is None:
            return
        try:
            self._events.publish(event_type, data)
        except Exception:
            pass

    def _schedule(self, run_id, workflow=None):
        """Advance ready steps subject to parallelism and locks."""
        with self._guard:
            run = self._db.get_workflow_run(run_id)
            if run is None or run.get("status") != "running":
                return
            if workflow is None:
                workflow = self._hooks.get_workflow_definition(
                    run.get("workflow_id"))
                if workflow is None:
                    self._finalize_run(run_id, "failed", "工作流定义已删除")
                    return
            steps = {step["id"]: step for step in workflow.get("steps", [])}
            step_runs = {
                sr["step_id"]: sr for sr in
                self._db.list_step_runs(run_id)}
            running = [sr for sr in step_runs.values()
                       if sr["status"] == "running"]
            if len(running) >= GLOBAL_PARALLELISM:
                return
            ready = []
            for step in workflow.get("steps", []):
                sr = step_runs[step["id"]]
                if sr["status"] != "pending":
                    continue
                needs_ok = True
                for need in step.get("needs") or []:
                    need_sr = step_runs.get(need)
                    if need_sr is None:
                        needs_ok = False
                        break
                    if need_sr["status"] != "succeeded":
                        needs_ok = False
                        break
                if needs_ok:
                    ready.append(step)
            for step in ready:
                if len([sr for sr in step_runs.values()
                        if sr["status"] == "running"]) >= GLOBAL_PARALLELISM:
                    break
                sr = step_runs[step["id"]]
                lock_keys = [lock_key_of(item) for item in
                             (step.get("locks") or [])]
                if not self._locks.try_acquire(
                        lock_keys, run_id, step["id"]):
                    continue
                self._persist_locks(run_id)
                self._launch_step(run_id, workflow, step, sr, lock_keys)

    def _persist_locks(self, run_id):
        """Persist held locks of this run for restart reconciliation."""
        held = {
            key: entry for key, entry in self._locks.snapshot().items()
            if entry.get("workflow_run_id") == run_id}
        self._db.update_workflow_run(
            run_id, {"locks_held": json.dumps(held, ensure_ascii=False)})

    def _launch_step(self, run_id, workflow, step, step_run, lock_keys):
        step_run_id = step_run["id"]
        self._db.update_step_run(step_run_id, {
            "status": "running",
            "started_at": int(time.time()),
        })
        self._publish("workflow.step_started", {"runId": run_id,
                                                "stepId": step["id"]})
        deadline = None
        timeout = step.get("timeout_sec")
        if timeout:
            deadline = time.monotonic() + float(timeout)
            self._timers[step_run_id] = deadline

        def _done():
            self._locks.release(lock_keys, run_id)
            self._persist_locks(run_id)
            self._timers.pop(step_run_id, None)
            self._schedule(run_id, workflow)

        def _run():
            with self._cancel_lock:
                canceled = run_id in self._cancel_requested
            if canceled:
                self._complete_step(
                    step_run_id, "canceled", workflow, run_id, _done)
                return
            error = None
            run_ref = None
            if step["kind"] in ("service", "task"):
                run_ref, error = self._run_resource_step(
                    step, run_id, step_run_id)
            elif step["kind"] == "agent":
                run_ref, error = self._run_agent_step(
                    step, run_id, step_run_id)
            elif step["kind"] == "gate":
                error = self._run_gate_step(step, run_id)
            else:
                error = "未知步骤类型: %s" % step["kind"]
            if error:
                with self._cancel_lock:
                    canceled = run_id in self._cancel_requested
                if canceled:
                    self._complete_step(
                        step_run_id, "canceled", workflow, run_id, _done)
                    return
                latest = self._db.get_step_run(step_run_id)
                retries = latest.get("retries", 0) if latest else 0
                policy = step.get("retry_policy") or {}
                max_retries = int(policy.get("max_retries") or 0)
                if retries < max_retries:
                    self._db.update_step_run(step_run_id, {
                        "retries": retries + 1})
                    delay = float(policy.get("delay_sec") or 0)
                    def _retry():
                        self._db.update_step_run(step_run_id, {
                            "status": "running",
                            "started_at": int(time.time()),
                        })
                        self._launch_step(
                            run_id, workflow, step, step_run, lock_keys)
                    threading.Timer(delay, _retry).start()
                    return
                self._complete_step(
                    step_run_id, "failed", workflow, run_id, _done,
                    error=error)
                return
            self._complete_step(step_run_id, "succeeded", workflow, run_id,
                                _done, run_ref=run_ref)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        if deadline is not None:
            def _watch():
                time.sleep(step.get("timeout_sec"))
                with self._guard:
                    current = self._db.get_step_run(step_run_id)
                    if current and current.get("status") == "running":
                        self._db.update_step_run(step_run_id, {
                            "status": "timed_out",
                            "ended_at": int(time.time()),
                            "error": "步骤超时（%ss）" % step.get("timeout_sec"),
                        })
                        self._timers.pop(step_run_id, None)
                        self._locks.release(lock_keys, run_id)
                        self._schedule(run_id, workflow)
            threading.Thread(target=_watch, daemon=True).start()

    def _complete_step(self, step_run_id, status, workflow, run_id, _done,
                       error=None, run_ref=None):
        with self._guard:
            current = self._db.get_step_run(step_run_id)
            if current is None or current.get("status") != "running":
                return
            self._db.update_step_run(step_run_id, {
                "status": status,
                "ended_at": int(time.time()),
                "error": error,
                "run_ref": run_ref,
            })
            self._publish("workflow.step_finished", {
                "runId": run_id, "stepId": current.get("step_id"),
                "status": status})
        _done()
        self._check_run_end(run_id, workflow)

    def _check_run_end(self, run_id, workflow):
        with self._guard:
            run = self._db.get_workflow_run(run_id)
            if run is None or run.get("status") != "running":
                return
            step_runs = {sr["step_id"]: sr for sr in
                         self._db.list_step_runs(run_id)}
            steps = {step["id"]: step for step in workflow.get("steps", [])}
            any_running = any(sr["status"] == "running"
                              for sr in step_runs.values())
            if any_running:
                return
            failed = [sr for sr in step_runs.values()
                      if sr["status"] in ("failed", "timed_out", "lost")]
            canceled = [sr for sr in step_runs.values()
                        if sr["status"] == "canceled"]
            if canceled and not any_running:
                self._finalize_run(run_id, "canceled", None)
                return
            if failed:
                # continue_on_error 的失败不阻断整体
                blocking = [
                    sr for sr in failed
                    if not steps.get(sr["step_id"], {}).get(
                        "continue_on_error")]
                status = "failed" if blocking else "succeeded"
                self._finalize_run(run_id, status,
                                   "失败步骤: %s" % ", ".join(
                                       sr["step_id"] for sr in failed)
                                   if failed else None)
                return
            pending = [sr for sr in step_runs.values()
                       if sr["status"] == "pending"]
            if not pending and not any_running:
                self._finalize_run(run_id, "succeeded", None)

    def _finalize_run(self, run_id, status, error):
        self._db.update_workflow_run(run_id, {
            "status": status,
            "ended_at": int(time.time()),
        })
        self._locks.release_run(run_id)
        self._publish("workflow.finished", {"runId": run_id,
                                            "status": status})

    # ------------------------------------------------------------ step exec

    def _run_resource_step(self, step, run_id, step_run_id):
        """Service: started means success.  Task: wait for its run to end."""
        resource_id = step["config"].get("resource_id")
        resource = self._hooks.resolve_resource(resource_id)
        if resource is None:
            return None, "资源不存在: %s" % resource_id
        ok, error, info = self._hooks.start_resource(resource_id)
        if not ok:
            return None, error or "资源启动失败"
        self._db.update_step_run(step_run_id, {"run_ref": resource_id})
        with self._cancel_lock:
            if run_id in self._cancel_requested:
                self._hooks.stop_resource(resource_id)
                return resource_id, "已取消"
        if step["kind"] == "service":
            return resource_id, None
        deadline = time.monotonic() + (
            float(step["timeout_sec"]) if step.get("timeout_sec") else 600)
        while time.monotonic() < deadline:
            with self._cancel_lock:
                if run_id in self._cancel_requested:
                    return resource_id, "已取消"
            status = self._hooks.resource_run_status(resource_id)
            if status == "succeeded":
                return resource_id, None
            if status in ("failed", "canceled", "stopped", "lost"):
                return resource_id, "任务 %s" % status
            time.sleep(0.5)
        return resource_id, "任务超时"

    def _run_agent_step(self, step, run_id, step_run_id):
        config = step["config"]
        adapter_id = config.get("adapter_id")
        run = self._db.get_workflow_run(run_id)
        project_id = run.get("project_id") if run else None
        prompt = config.get("prompt")
        session, error = self._hooks.start_agent_session(
            adapter_id, project_id, prompt)
        if error or session is None:
            return None, error or "agent 会话启动失败"
        session_id = session.get("id")
        # 立即持久化 run_ref，使 cancel 能终止底层会话
        self._db.update_step_run(step_run_id, {"run_ref": session_id})
        with self._cancel_lock:
            if run_id in self._cancel_requested:
                # cancel 发生在本步骤启动窗口内：主动终止刚创建的会话
                self._hooks.stop_agent_session(session_id)
                return session_id, "已取消"
        deadline = time.monotonic() + (
            float(step["timeout_sec"]) if step.get("timeout_sec") else 86400)
        while time.monotonic() < deadline:
            with self._cancel_lock:
                if run_id in self._cancel_requested:
                    return session_id, "已取消"
            current = self._hooks.get_agent_session(session_id)
            if current is None:
                return session_id, "会话记录消失"
            status = current.get("status")
            if status == "succeeded":
                return session_id, None
            if status in ("failed", "lost", "timed_out"):
                return session_id, "agent 会话 %s" % status
            if status in ("canceled", "stopped"):
                return session_id, "agent 会话已停止"
            time.sleep(1.0)
        return session_id, "agent 会话超时"

    def _run_gate_step(self, step, run_id):
        command = step["config"].get("command")
        project_id = self._db.get_workflow_run(run_id).get("project_id")
        cwd = self._hooks.project_root(project_id) or "."
        try:
            result = subprocess.run(
                ["/bin/bash", "-c", command] if not _IS_WINDOWS else
                ["cmd.exe", "/d", "/s", "/c", command],
                cwd=cwd, capture_output=True, text=True, errors="replace",
                timeout=float(step.get("timeout_sec") or 300))
        except subprocess.TimeoutExpired:
            return "验证命令超时"
        except OSError as exc:
            return "验证命令执行失败: %s" % exc
        if result.returncode != 0:
            tail = (result.stdout or result.stderr or "").strip()[-300:]
            return "验证失败（exit %d）%s" % (
                result.returncode, (": " + tail) if tail else "")
        return None

    # ------------------------------------------------------------ cancel

    def cancel(self, run_id):
        """Cancel pending work and terminate running managed steps."""
        with self._cancel_lock:
            self._cancel_requested.add(run_id)
        run = self._db.get_workflow_run(run_id)
        if run is None:
            return False, "工作流运行不存在"
        if run.get("status") != "running":
            return False, "工作流已结束（%s）" % run.get("status")
        for sr in self._db.list_step_runs(run_id):
            if sr["status"] == "pending":
                self._db.update_step_run(sr["id"], {
                    "status": "canceled",
                    "ended_at": int(time.time()),
                })
            elif sr["status"] == "running":
                self._cancel_running_step(sr)
        self._finalize_run(run_id, "canceled", None)
        return True, None

    def _cancel_running_step(self, step_run):
        if step_run.get("kind") in ("service", "task"):
            run_ref = step_run.get("run_ref")
            if run_ref:
                self._hooks.stop_resource(run_ref)
        elif step_run.get("kind") == "agent":
            run_ref = step_run.get("run_ref")
            if run_ref:
                self._hooks.stop_agent_session(run_ref)

    # ------------------------------------------------------------ recovery

    def recover(self):
        """Restart reconciliation: verify running runs/steps, resume queue."""
        for run in self._db.get_running_workflow_runs():
            workflow = self._hooks.get_workflow_definition(run.get("workflow_id"))
            if workflow is None:
                self._finalize_run(run["id"], "failed", "工作流定义已删除")
                continue
            # 重新持有持久化锁（同一 run 的语义不变）
            try:
                held = json.loads(run.get("locks_held") or "{}")
            except ValueError:
                held = {}
            self._locks.restore(held)
            for sr in self._db.list_step_runs(run["id"]):
                if sr["status"] != "running":
                    continue
                # 运行中的步骤：底层 run/agent 记录存在则视为仍运行
                # （daemon 重启不发明成功），否则 lost。
                ref = sr.get("run_ref")
                if ref and self._ref_alive(sr.get("kind"), ref):
                    continue
                self._db.update_step_run(sr["id"], {
                    "status": "lost",
                    "ended_at": int(time.time()),
                    "error": "daemon 重启后无法验证",
                })
            self._schedule(run["id"], workflow)

    def _ref_alive(self, kind, ref):
        if kind in ("service", "task"):
            return self._hooks.resource_alive(ref)
        if kind == "agent":
            return self._hooks.agent_session_alive(ref)
        return False


from adcc.orchestrator.locks import lock_key_of


import os as _os
_IS_WINDOWS = _os.name == "nt"
