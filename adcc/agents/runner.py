"""Agent session runner (M7): launch, watch, stop, concurrency queueing.

The runner drives the same PlatformAdapter process primitives used by
managed apps; sessions get their own run token so identity checks never
rely on ports.  Prompt files live under the data directory; logs are
files under the logs directory; state lives in the SQLite database.
"""

import os
import threading
import time

from adcc.agents.models import render_command, render_cwd, render_env
from adcc.core.constants import RUN_TOKEN_ARG_PREFIX

AGENT_MARKER = "console-run-"
DEFAULT_GLOBAL_MAX = 3
DEFAULT_PROJECT_MAX = 1


class AgentPolicyError(RuntimeError):
    pass


class AgentRunner:
    def __init__(self, cfg, db, platform, logs_dir, prompts_dir,
                 current_user, events=None):
        self._cfg = cfg
        self._db = db
        self._platform = platform
        self._logs_dir = logs_dir
        self._prompts_dir = prompts_dir
        self._current_user = current_user
        self._events = events
        self._wake_lock = threading.Lock()
        self._manual_stops = set()
        self._manual_stops_lock = threading.Lock()

    # ------------------------------------------------------------ config

    def _policy(self):
        policy = self._cfg.snapshot().get("agent_policy") or {}
        return {
            "global_max": int(policy.get("global_max") or DEFAULT_GLOBAL_MAX),
            "per_project_max": int(policy.get("per_project_max")
                                   or DEFAULT_PROJECT_MAX),
        }

    def list_adapters(self):
        return list(self._cfg.snapshot().get("agent_adapters") or [])

    def get_adapter(self, adapter_id):
        for adapter in self.list_adapters():
            if adapter.get("id") == adapter_id:
                return adapter
        return None

    def add_adapter(self, adapter):
        def op(c):
            c.setdefault("agent_adapters", []).append(adapter)
        self._cfg.update(op)
        return adapter

    # ------------------------------------------------------------ sessions

    def list_sessions(self, limit=50, status=None, project_id=None):
        return self._db.list_sessions(
            limit, status=status, project_id=project_id)

    def get_session(self, session_id):
        return self._db.get_session(session_id)

    def start(self, adapter_id, project_id, prompt=None, prompt_file=None,
              workflow_run_id=None, workflow_step_id=None):
        """Start (or queue) an agent session.  Returns (session, error)."""
        adapter = self.get_adapter(adapter_id)
        if adapter is None:
            return None, "适配器不存在: %s" % adapter_id
        snapshot = self._cfg.snapshot()
        project = next(
            (p for p in snapshot.get("projects") or []
             if p.get("id") == project_id), None)
        if project is None:
            return None, "项目不存在: %s" % project_id

        from adcc.agents.models import make_session
        prompt_text = prompt
        if prompt_text is None and prompt_file:
            try:
                with open(prompt_file, "r", encoding="utf-8") as handle:
                    prompt_text = handle.read()
            except OSError as exc:
                return None, "无法读取 prompt 文件: %s" % exc
        session = make_session(
            project_id=project_id, adapter_id=adapter_id,
            prompt_ref=prompt_text,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id)
        self._db.insert_session(session)
        self._publish(session)

        policy = self._policy()
        running = self._db.running_sessions()
        if len(running) >= policy["global_max"]:
            self._db.update_session(session["id"], {"status": "queued"})
            self._publish(self._db.get_session(session["id"]))
            return self._db.get_session(session["id"]), None
        project_running = [
            s for s in running if s.get("project_id") == project_id]
        if len(project_running) >= policy["per_project_max"]:
            self._db.update_session(session["id"], {"status": "queued"})
            self._publish(self._db.get_session(session["id"]))
            return self._db.get_session(session["id"]), None

        error = self._launch(session, prompt_text)
        if error:
            self._db.update_session(session["id"], {
                "status": "failed", "ended_at": int(time.time())})
            self._publish(self._db.get_session(session["id"]))
            return self._db.get_session(session["id"]), error
        return self._db.get_session(session["id"]), None

    def _launch(self, session, prompt_text):
        """Launch the rendered agent command; returns error string or None."""
        adapter = self.get_adapter(session.get("adapter_id"))
        snapshot = self._cfg.snapshot()
        project = next(
            (p for p in snapshot.get("projects") or []
             if p.get("id") == session.get("project_id")), None)
        if project is None:
            return "项目不存在"
        import secrets
        token = secrets.token_urlsafe(24)
        os.makedirs(self._prompts_dir, exist_ok=True)
        resolved_prompt_file = None
        if adapter.get("stdin_mode") == "file" or prompt_text is not None:
            resolved_prompt_file = os.path.join(
                self._prompts_dir, "%s.txt" % session["id"])
            try:
                with open(resolved_prompt_file, "w", encoding="utf-8") as f:
                    f.write(prompt_text or "")
            except OSError as exc:
                return "无法写入 prompt 文件: %s" % exc
        variables = {
            "project_id": session.get("project_id"),
            "session_id": session.get("id"),
            "project_root": project.get("root_path"),
            "prompt_file": resolved_prompt_file,
            "worktree_path": None,
            "run_id": session.get("id"),
        }
        wants_worktree = any(
            "{worktree_path}" in str(item)
            for item in (adapter.get("args_template") or [])
        ) or "{worktree_path}" in str(adapter.get("cwd_template") or "")
        if wants_worktree:
            worktree_path = self._create_worktree(session, project)
            if worktree_path is None:
                return "需要 worktree 但创建失败（项目不是 Git 仓库？）"
            variables["worktree_path"] = worktree_path
        argv = render_command(adapter, variables)
        env = dict(os.environ)
        env.update(render_env(adapter, variables))
        from adcc.core.constants import RUN_TOKEN_ENV
        env[RUN_TOKEN_ENV] = token
        cwd = render_cwd(adapter, variables) or project.get("root_path") or None
        if cwd and not os.path.isdir(cwd):
            return "cwd 不存在: %s" % cwd
        log_path = os.path.join(self._logs_dir, "agent-%s.log" % session["id"])
        try:
            fd = self._platform.open_private(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            logf = os.fdopen(fd, "ab", buffering=0)
        except OSError as exc:
            return "无法打开日志: %s" % exc
        try:
            header = "\n===== agent 启动于 %s =====\n" % time.strftime(
                "%Y-%m-%d %H:%M:%S")
            logf.write(header.encode("utf-8"))
            proc, _group = self._platform.start_process(
                cwd or ".", env, logf, " ".join(argv), token)
        except Exception as exc:
            logf.close()
            return "启动失败: %s" % exc
        logf.close()
        if hasattr(self._platform, "invalidate_cache"):
            try:
                self._platform.invalidate_cache()
            except Exception:
                pass
        self._db.update_session(session["id"], {
            "status": "running",
            "pid": proc.pid,
            "run_token": token,
            "started_at": int(time.time()),
            "log_path": log_path,
        })
        started_at = time.time()
        self._publish(self._db.get_session(session["id"]))
        self._watch(session["id"], proc, token, started_at)
        return None

    def _watch(self, session_id, proc, token, started_at):
        def _wait():
            try:
                code = proc.wait()
                ended_at = time.time()
                self._finish(session_id, code, manual_stop=False,
                             ended_at=ended_at)
                try:
                    if os.name == "nt":
                        os.remove(os.path.join(
                            os.environ.get("TEMP", os.path.expanduser("~")),
                            "console-run-%s.cmd" % token))
                except OSError:
                    pass
                self._wake_queued()
            except Exception:
                # 进程已退出但 DB 可能已关闭（daemon 停止场景）：静默收尾
                pass
        thread = threading.Thread(target=_wait, daemon=True)
        thread.start()

    def _finish(self, session_id, code, manual_stop, ended_at=None):
        from adcc.runtime.runs import finalize_run_status
        with self._manual_stops_lock:
            if session_id in self._manual_stops:
                manual_stop = True
        session = self._db.get_session(session_id)
        if session is None or session.get("status") not in (
                "running", "starting"):
            return
        status = finalize_run_status(
            session, code, manual_stop=manual_stop)
        self._db.update_session(session_id, {
            "status": status,
            "ended_at": int(ended_at or time.time()),
            "exit_code": code if code is not None else None,
        })
        with self._manual_stops_lock:
            self._manual_stops.discard(session_id)
        self._publish(self._db.get_session(session_id))

    def stop(self, session_id):
        """Stop a session: cancel queued, or terminate the verified tree."""
        session = self._db.get_session(session_id)
        if session is None:
            return False, "会话不存在"
        if session.get("status") == "queued":
            self._db.update_session(session_id, {
                "status": "canceled", "ended_at": int(time.time())})
            self._publish(self._db.get_session(session_id))
            return True, None
        if session.get("status") != "running":
            return False, "会话未在运行"
        pid = session.get("pid")
        token = session.get("run_token")
        if not isinstance(pid, int) or not isinstance(token, str):
            return False, "会话运行身份无效"
        if not self._identity_ok(session):
            self._db.update_session(session_id, {
                "status": "lost", "ended_at": int(time.time())})
            self._publish(self._db.get_session(session_id))
            return False, "会话进程已消失（标记为 lost）"
        with self._manual_stops_lock:
            self._manual_stops.add(session_id)
        ok, error = self._platform.terminate_tree(pid, force=False)
        if not ok:
            return False, error
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and self._platform.pid_alive(pid):
            time.sleep(0.05)
        self._finish(session_id, None, manual_stop=True)
        self._wake_queued()
        return True, None

    def _identity_ok(self, session):
        """Current-user + run-marker check for the session controller.

        The marker appears as ``console-run:<token>`` in macOS bash argv
        and as ``console-run-<token>`` in the Windows batch filename;
        accept either spelling.
        """
        pid = session.get("pid")
        token = session.get("run_token")
        if not isinstance(pid, int) or not isinstance(token, str):
            return False
        if not self._platform.pid_alive(pid):
            return False
        snapshot = {}
        try:
            snapshot = self._platform.process_snapshot([pid], with_uid=True)
        except Exception:
            return False
        entry = snapshot.get(pid, {})
        if entry.get("uid") != self._current_user:
            return False
        args = entry.get("args", "")
        return ("console-run:" + token in args
                or "console-run-" + token in args)

    def reconcile(self):
        """Daemon restart: verify running sessions; vanish → lost."""
        for session in self._db.running_sessions():
            if not self._identity_ok(session):
                self._db.update_session(session["id"], {
                    "status": "lost", "ended_at": int(time.time())})
                self._publish(self._db.get_session(session["id"]))
        self._wake_queued()

    def _wake_queued(self):
        """Start queued sessions while concurrency allows (oldest first)."""
        with self._wake_lock:
            policy = self._policy()
            queued = self._db.list_sessions(status="queued", limit=100)
            for session in sorted(queued, key=lambda s: s.get("created_at", 0)):
                running = self._db.running_sessions()
                if len(running) >= policy["global_max"]:
                    break
                project_running = [
                    s for s in running
                    if s.get("project_id") == session.get("project_id")]
                if len(project_running) >= policy["per_project_max"]:
                    continue
                error = self._launch(
                    session,
                    prompt_text=session.get("prompt_ref"))
                if error:
                    self._db.update_session(session["id"], {
                        "status": "failed", "ended_at": int(time.time())})

    def _create_worktree(self, session, project):
        """Create an ADCC-owned worktree for an agent session (P1).

        Branch: ``adcc/<session-id>/<run8>``; path under the prompts
        sibling ``worktrees/`` data directory.  Returns the path or None.
        """
        from adcc.git.repository import create_worktree, detect_repo
        repo = detect_repo(project.get("root_path"))
        if repo is None:
            return None
        from adcc.git.repository import adcc_worktree_branch
        branch = adcc_worktree_branch(session.get("id"), session.get("id"))
        path = os.path.join(os.path.dirname(self._prompts_dir),
                            "worktrees", session["id"])
        try:
            created, error = create_worktree(repo, branch, path)
        except Exception:
            return None
        return created if error is None else None

    def _publish(self, session):
        if self._events is None or session is None:
            return
        self._events.publish("agent.updated", {
            "id": session.get("id"),
            "status": session.get("status"),
        })
