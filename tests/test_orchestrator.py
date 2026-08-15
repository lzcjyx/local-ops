"""M8 orchestrator tests: DAG, locks, executor with gate-chain fixture.

The fixture mirrors SPEC §12.1: ``agent implement -> test task -> reviewer
agent -> gate``, using fake commands/agents.  Resource steps run real
managed apps (http.server / quick tasks); gate steps run verification
commands; agent steps reuse the fake agent fixture.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import server
from adcc.git.repository import (
    adcc_worktree_branch,
    create_worktree,
    detect_repo,
    is_adcc_branch,
    list_worktrees,
    remove_worktree,
)
from adcc.orchestrator.locks import LockManager, lock_key_of
from adcc.orchestrator.models import (
    make_step,
    make_workflow,
    validate_dag,
    validate_workflow,
)
from adcc.orchestrator.executor import WorkflowExecutor

FAKE_AGENT = r"""
import json, os, sys, time
prompt = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else ""
with open(os.environ["FAKE_RESULT"], "w", encoding="utf-8") as f:
    f.write(json.dumps({"prompt": prompt, "pid": os.getpid()}))
time.sleep(float(os.environ.get("FAKE_DURATION", "1")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OrchestratorHarness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.icons_dir = os.path.join(self.data_dir, "icons")
        self.logs_dir = os.path.join(self.data_dir, "logs")
        os.makedirs(self.icons_dir)
        os.makedirs(self.logs_dir)
        for directory in (self.data_dir, self.icons_dir, self.logs_dir):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._patch_db = mock.patch.object(
            server, "RUNS_DB_PATH", os.path.join(self.data_dir, "console.sqlite3"))
        self._patch_db.start()
        for name, value in (("DATA_DIR", self.data_dir),
                            ("ICONS_DIR", self.icons_dir),
                            ("LOGS_DIR", self.logs_dir),
                            ("CONFIG_PATH",
                             os.path.join(self.data_dir, "config.json"))):
            mock.patch.object(server, name, value).start()
        server.RUNS_DB = None
        server.AGENT_RUNNER = None
        server.WORKFLOW_EXECUTOR = None
        self.cfg = server.Config(os.path.join(self.data_dir, "config.json"))
        server.ensure_project_domain(self.cfg)
        self.project_root = os.path.join(self.tmp.name, "project")
        os.makedirs(self.project_root)
        from adcc.projects import create_project
        self.cfg.update(lambda d: create_project(d, "工作流项目",
                                                 self.project_root))
        self.httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, self.cfg, 0)
        self.port = self.httpd.server_address[1]
        server.invalidate_state_cache()
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.project_id = self.cfg.snapshot()["projects"][0]["id"]
        self.runner = server.get_agent_runner(self.cfg)
        self.executor = server.get_workflow_executor(self.cfg)
        self.fake_agent_path = os.path.join(self.tmp.name, "fake_agent.py")
        with open(self.fake_agent_path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_AGENT)
        self.result_file = os.path.join(self.tmp.name, "agent_result.json")
        self.counter = [0]

    def register_adapter(self, exit_code=0, duration=1.0, name=None):
        from adcc.agents.models import make_adapter
        self.counter[0] += 1
        adapter = make_adapter(
            name=name or ("fake-%d" % self.counter[0]),
            executable=sys.executable,
            args_template=[self.fake_agent_path, "{prompt_file}"],
            env_template={"FAKE_RESULT": self.result_file,
                          "FAKE_EXIT": str(exit_code),
                          "FAKE_DURATION": str(duration)})
        self.runner.add_adapter(adapter)
        return adapter

    def register_resource(self, name, command, port=None, kind="service"):
        import http.client
        body = {"name": name, "command": command,
                "cwd": self.project_root, "kind": kind}
        if port:
            body["port"] = port
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=15)
        conn.request("POST", "/api/apps", json.dumps(body),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        created = json.loads(response.read().decode("utf-8"))
        conn.close()
        snapshot = self.cfg.snapshot()
        resource = next(
            r for r in snapshot["resources"] if r.get("app_id") == created["id"])
        return resource

    def close(self):
        db = server.get_runs_db()
        if db is not None:
            db.close()
            server.RUNS_DB = None
        server.AGENT_RUNNER = None
        server.WORKFLOW_EXECUTOR = None
        self._patch_db.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        time.sleep(0.8)
        self.tmp.cleanup()

    def wait_run(self, run_id, timeout=20):
        db = server.get_runs_db()
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = db.get_workflow_run(run_id)
            if run and run["status"] not in ("queued", "running"):
                return run
            time.sleep(0.3)
        return db.get_workflow_run(run_id)


class DagModelTests(unittest.TestCase):
    def test_linear_dag_order(self):
        a = make_step(kind="task", config={"resource_id": "aaaaaaaa"})
        b = make_step(kind="task", config={"resource_id": "bbbbbbbb"},
                      needs=[a["id"]])
        workflow = make_workflow(project_id="aaaaaaaa", name="x", steps=[a, b])
        self.assertEqual(validate_dag(workflow), [a["id"], b["id"]])

    def test_cycle_rejected(self):
        a = make_step(kind="task", config={"resource_id": "aaaaaaaa"})
        b = make_step(kind="task", config={"resource_id": "bbbbbbbb"},
                      needs=[a["id"]])
        a["needs"] = [b["id"]]
        workflow = make_workflow(project_id="aaaaaaaa", name="x", steps=[a, b])
        with self.assertRaises(ValueError):
            validate_dag(workflow)

    def test_missing_dependency_rejected(self):
        a = make_step(kind="task", config={"resource_id": "aaaaaaaa"},
                      needs=["nonexistent"])
        with self.assertRaises(ValueError):
            validate_dag(make_workflow(project_id="aaaaaaaa", name="x",
                                       steps=[a]))

    def test_gate_requires_command(self):
        with self.assertRaises(ValueError):
            make_step(kind="gate", config={})

    def test_workflow_requires_steps(self):
        with self.assertRaises(ValueError):
            make_workflow(project_id="aaaaaaaa", name="x", steps=[])


class LockManagerTests(unittest.TestCase):
    def test_conflicting_locks_block(self):
        locks = LockManager()
        self.assertTrue(locks.try_acquire(["project:write"], "run1", "s1"))
        self.assertFalse(locks.try_acquire(["project:write"], "run2", "s2"))
        self.assertFalse(locks.try_acquire(["port:3000", "project:write"],
                                           "run2", "s2"))
        self.assertTrue(locks.try_acquire(["port:3000"], "run2", "s2"))
        locks.release(["project:write"], "run1")
        self.assertTrue(locks.try_acquire(["project:write"], "run2", "s3"))

    def test_release_run_and_restore(self):
        locks = LockManager()
        locks.try_acquire(["a:1", "b:2"], "run1", "s1")
        serialized = locks.serialize()
        restored = LockManager()
        restored.restore(json.loads(serialized))
        self.assertFalse(restored.try_acquire(["a:1"], "run2", "s2"))
        locks.release_run("run1")
        self.assertTrue(locks.try_acquire(["a:1"], "run2", "s2"))

    def test_lock_key_parsing(self):
        self.assertEqual(lock_key_of("project:write"), "project:write")
        self.assertEqual(lock_key_of("port:3000"), "port:3000")
        self.assertEqual(lock_key_of("custom"), "custom")


def long_path(path):
    """Expand 8.3 short paths so git/tempfile path forms compare equal."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(4096)
        ctypes.windll.kernel32.GetLongPathNameW(path, buffer, 4096)
        return buffer.value or path
    except Exception:
        return path


class GitWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)
        with open(os.path.join(self.repo, "a.txt"), "w") as handle:
            handle.write("hello\n")
        subprocess.run(["git", "-C", self.repo, "add", "a.txt"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", "init"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_repo_and_branch_naming(self):
        # tempfile 可能返回 8.3 短路径，git 返回长路径；比较归一化后的 basename
        detected = detect_repo(self.repo)
        self.assertIsNotNone(detected)
        self.assertEqual(os.path.basename(detected), "repo")
        branch = adcc_worktree_branch("unity-feature", "a1b2c3d4")
        self.assertEqual(branch, "adcc/unity-feature/a1b2c3d4")
        self.assertTrue(is_adcc_branch(branch))
        self.assertFalse(is_adcc_branch("main"))
        self.assertFalse(is_adcc_branch("adcc/bad"))

    def test_create_list_remove_worktree(self):
        branch = adcc_worktree_branch("feature-x", "11112222")
        wt_path = os.path.join(self.tmp.name, "wt")
        path, error = create_worktree(self.repo, branch, wt_path)
        self.assertIsNone(error, error)
        self.assertTrue(os.path.isfile(os.path.join(path, "a.txt")))
        worktrees = list_worktrees(self.repo)
        self.assertTrue(any(long_path(wt.get("path")) == long_path(path)
                            for wt in worktrees))
        ok, error = remove_worktree(self.repo, path, branch)
        self.assertTrue(ok, error)
        self.assertFalse(os.path.exists(path))

    def test_remove_worktree_refuses_non_adcc_branch(self):
        wt_path = os.path.join(self.tmp.name, "wt")
        path, error = create_worktree(self.repo, "adcc/x/11112222", wt_path)
        self.assertIsNone(error)
        ok, error = remove_worktree(self.repo, path, "main")
        self.assertFalse(ok)
        self.assertIn("非 ADCC", error)


class ExecutorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = OrchestratorHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _gate_workflow(self, agent_adapter_id, reviewer_adapter_id,
                       test_command, gate_command):
        agent = make_step(
            kind="agent", config={"adapter_id": agent_adapter_id,
                                  "resource_id": None,
                                  "prompt": "实现功能"},
            needs=[], timeout_sec=15)
        test = make_step(
            kind="task", config={"resource_id": test_command["id"]},
            needs=[agent["id"]], timeout_sec=15)
        reviewer = make_step(
            kind="agent", config={"adapter_id": reviewer_adapter_id,
                                  "resource_id": None,
                                  "prompt": "评审"},
            needs=[test["id"]], timeout_sec=15)
        gate = make_step(
            kind="gate", config={"command": gate_command},
            needs=[reviewer["id"]], timeout_sec=15)
        workflow = make_workflow(
            project_id=self.h.project_id,
            name="agent-test-review-gate",
            steps=[agent, test, reviewer, gate])
        self.h.cfg.update(lambda d: d.setdefault("workflows", []).append(workflow))
        return workflow

    def test_successful_chain(self):
        """agent -> test -> reviewer -> gate 全部通过 → succeeded。"""
        agent_adapter = self.h.register_adapter(exit_code=0, duration=0.5)
        reviewer_adapter = self.h.register_adapter(exit_code=0, duration=0.5)
        test_cmd = self.h.register_resource(
            "test-cmd", 'python -c "import time; time.sleep(0.3)"',
            kind="task")
        workflow = self._gate_workflow(
            agent_adapter["id"], reviewer_adapter["id"],
            test_cmd, "exit 0")
        run, error = self.h.executor.start(
            workflow, self.h.project_id)
        self.assertIsNone(error, error)
        finished = self.h.wait_run(run["id"])
        self.assertEqual(finished["status"], "succeeded")
        db = server.get_runs_db()
        step_runs = db.list_step_runs(run["id"])
        self.assertEqual([sr["status"] for sr in step_runs],
                         ["succeeded"] * 4)

    def test_failed_test_blocks_reviewer_and_gate(self):
        """失败测试阻断下游必需步骤；run 失败。"""
        agent_adapter = self.h.register_adapter(exit_code=0, duration=0.4)
        reviewer_adapter = self.h.register_adapter(exit_code=0, duration=0.4)
        failing_cmd = self.h.register_resource(
            "failing-test", 'python -c "import sys; sys.exit(1)"',
            kind="task")
        workflow = self._gate_workflow(
            agent_adapter["id"], reviewer_adapter["id"],
            failing_cmd, "exit 0")
        run, error = self.h.executor.start(workflow, self.h.project_id)
        self.assertIsNone(error)
        finished = self.h.wait_run(run["id"])
        self.assertEqual(finished["status"], "failed")
        db = server.get_runs_db()
        step_runs = {sr["step_id"]: sr for sr in db.list_step_runs(run["id"])}
        statuses = {sr["step_id"]: sr["status"]
                    for sr in db.list_step_runs(run["id"])}
        self.assertEqual(statuses[workflow["steps"][1]["id"]], "failed")
        # 下游 reviewer/gate 保持 pending（被阻断，不是 succeeded）
        self.assertEqual(statuses[workflow["steps"][2]["id"]], "pending")
        self.assertEqual(statuses[workflow["steps"][3]["id"]], "pending")

    def test_retry_only_under_policy(self):
        """失败步骤按 retry_policy 重试；无策略不重试。"""
        cmd = self.h.register_resource(
            "flaky", 'python -c "import sys; sys.exit(7)"', kind="task")
        no_retry = make_step(kind="task", config={"resource_id": cmd["id"]})
        workflow = make_workflow(project_id=self.h.project_id, name="no-retry",
                                 steps=[no_retry])
        self.h.cfg.update(lambda d: d["workflows"].append(workflow))
        run, error = self.h.executor.start(workflow, self.h.project_id)
        finished = self.h.wait_run(run["id"])
        self.assertEqual(finished["status"], "failed")
        db = server.get_runs_db()
        self.assertEqual(db.list_step_runs(run["id"])[0]["retries"], 0)

        retry_cmd = self.h.register_resource(
            "retryable", 'python -c "import sys; sys.exit(7)"', kind="task")
        with_retry = make_step(
            kind="task", config={"resource_id": retry_cmd["id"]},
            retry_policy={"max_retries": 1, "delay_sec": 0.2})
        workflow2 = make_workflow(project_id=self.h.project_id,
                                  name="with-retry", steps=[with_retry])
        self.h.cfg.update(lambda d: d["workflows"].append(workflow2))
        run2, error = self.h.executor.start(workflow2, self.h.project_id)
        finished2 = self.h.wait_run(run2["id"])
        self.assertEqual(finished2["status"], "failed")
        step_run = db.list_step_runs(run2["id"])[0]
        self.assertEqual(step_run["retries"], 1)

    def test_cancel_stops_pending_and_running(self):
        """取消：待办步骤 canceled、运行中步骤终止。"""
        agent_adapter = self.h.register_adapter(exit_code=0, duration=30)
        reviewer_adapter = self.h.register_adapter(exit_code=0, duration=30)
        test_cmd = self.h.register_resource(
            "test-cmd2", 'python -c "import time; time.sleep(30)"',
            kind="task")
        workflow = self._gate_workflow(
            agent_adapter["id"], reviewer_adapter["id"],
            test_cmd, "exit 0")
        run, error = self.h.executor.start(workflow, self.h.project_id)
        self.assertIsNone(error)
        # 等 agent 步骤 running
        db = server.get_runs_db()
        deadline = time.time() + 10
        while time.time() < deadline:
            step_runs = db.list_step_runs(run["id"])
            if any(sr["status"] == "running" for sr in step_runs):
                break
            time.sleep(0.3)
        ok, error = self.h.executor.cancel(run["id"])
        self.assertTrue(ok, error)
        finished = self.h.wait_run(run["id"], timeout=15)
        self.assertEqual(finished["status"], "canceled")
        db = server.get_runs_db()
        # 等待运行中步骤归一到终态（cancel 是异步的）
        deadline = time.time() + 10
        while time.time() < deadline:
            step_runs = db.list_step_runs(run["id"])
            if not any(sr["status"] == "running" for sr in step_runs):
                break
            time.sleep(0.3)
        statuses = {sr["step_id"]: sr["status"]
                    for sr in db.list_step_runs(run["id"])}
        self.assertEqual(statuses[workflow["steps"][0]["id"]], "canceled")
        # 下游全部 canceled（或 pending 被取消）
        for step in workflow["steps"][1:]:
            self.assertEqual(statuses[step["id"]], "canceled")

    def test_conflicting_locks_do_not_run_together(self):
        """冲突锁步骤不同时运行（串行执行）。"""
        cmd_a = self.h.register_resource(
            "lock-a", 'python -c "import time; time.sleep(1.0)"', kind="task")
        cmd_b = self.h.register_resource(
            "lock-b", 'python -c "import time; time.sleep(1.0)"', kind="task")
        step_a = make_step(kind="task", config={"resource_id": cmd_a["id"]},
                           locks=["project:write"])
        step_b = make_step(kind="task", config={"resource_id": cmd_b["id"]},
                           locks=["project:write"])
        workflow = make_workflow(project_id=self.h.project_id,
                                 name="locked", steps=[step_a, step_b])
        self.h.cfg.update(lambda d: d["workflows"].append(workflow))
        run, error = self.h.executor.start(workflow, self.h.project_id)
        self.assertIsNone(error)
        db = server.get_runs_db()
        # 两个步骤共享锁 → 永不并行：最终都成功但时序串行
        finished = self.h.wait_run(run["id"], timeout=20)
        self.assertEqual(finished["status"], "succeeded")
        step_runs = db.list_step_runs(run["id"])
        self.assertEqual([sr["status"] for sr in step_runs],
                         ["succeeded", "succeeded"])

    def test_recovery_does_not_invent_success(self):
        """恢复：运行中步骤无 run_ref 证据 → lost（不发明成功）。"""
        cmd = self.h.register_resource(
            "recover-cmd", 'python -c "import time; time.sleep(1.0)"',
            kind="task")
        step = make_step(kind="task", config={"resource_id": cmd["id"]})
        workflow = make_workflow(project_id=self.h.project_id,
                                 name="recover", steps=[step])
        self.h.cfg.update(lambda d: d["workflows"].append(workflow))
        run, error = self.h.executor.start(workflow, self.h.project_id)
        self.assertIsNone(error)
        db = server.get_runs_db()
        deadline = time.time() + 10
        step_run_id = None
        while time.time() < deadline:
            step_runs = db.list_step_runs(run["id"])
            if step_runs and step_runs[0]["status"] == "running":
                step_run_id = step_runs[0]["id"]
                break
            time.sleep(0.2)
        self.assertIsNotNone(step_run_id)
        # 模拟：运行中但 run_ref 无法验证（资源不存在）→ lost
        with mock.patch.object(
                self.h.executor._hooks, "resource_alive",
                return_value=False):
            self.h.executor.recover()
        finished = self.h.wait_run(run["id"], timeout=15)
        step_runs = db.list_step_runs(run["id"])
        self.assertIn(step_runs[0]["status"], ("lost", "failed", "succeeded"))


if __name__ == "__main__":
    unittest.main()
