"""M7 agent session tests: models, template rendering, runner integration.

A fake command-based agent (a Python script) stands in for a real coding
harness; it echoes its prompt file and exits with a configurable code.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import server
from adcc.agents.models import (
    make_adapter,
    render_command,
    render_cwd,
    render_env,
    session_variables,
    validate_adapter,
    validate_session,
)

FAKE_AGENT = r"""
import json, os, sys, time
prompt = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else ""
with open(os.environ["FAKE_RESULT"], "w", encoding="utf-8") as f:
    f.write(json.dumps({
        "prompt": prompt,
        "session": os.environ.get("ADCC_SESSION_ID"),
        "project": os.environ.get("ADCC_PROJECT_ID"),
        "pid": os.getpid(),
    }))
time.sleep(float(os.environ.get("FAKE_DURATION", "2")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AgentHarness:
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
        self.cfg = server.Config(os.path.join(self.data_dir, "config.json"))
        server.ensure_project_domain(self.cfg)
        from adcc.projects import create_project
        self.project_root = os.path.join(self.tmp.name, "agentproj")
        os.makedirs(self.project_root)
        self.cfg.update(lambda d: create_project(d, "Agent 项目",
                                                 self.project_root))
        self.httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, self.cfg, 0)
        self.port = self.httpd.server_address[1]
        server.invalidate_state_cache()
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.runner = server.get_agent_runner(self.cfg)
        self.fake_agent_path = os.path.join(self.tmp.name, "fake_agent.py")
        with open(self.fake_agent_path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_AGENT)
        self.project_id = self.cfg.snapshot()["projects"][0]["id"]

    def register_adapter(self, **kwargs):
        from adcc.agents.models import make_adapter
        adapter = make_adapter(**kwargs)
        self.runner.add_adapter(adapter)
        return adapter

    def request(self, method, path, body=None):
        import http.client
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=20)
        headers = {"Content-Type": "application/json"}
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else "{}",
                     headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return response.status, raw

    def close(self):
        runner = server.get_agent_runner(self.cfg)
        if runner is not None:
            for session in runner.list_sessions(limit=100):
                if session.get("status") == "running":
                    try:
                        runner.stop(session["id"])
                    except Exception:
                        pass
        db = server.get_runs_db()
        if db is not None:
            db.close()
            server.RUNS_DB = None
        server.AGENT_RUNNER = None
        self._patch_db.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        time.sleep(1.2)  # 等待 watch 线程完成日志轮转，避免与清理竞态
        self.tmp.cleanup()


class ModelTests(unittest.TestCase):
    def test_adapter_validation(self):
        with self.assertRaises(ValueError):
            make_adapter(name="x", executable="")
        adapter = make_adapter(
            name="测试", executable="opencode",
            args_template=["run", "--prompt-file", "{prompt_file}"],
            env_template={"ADCC_SESSION_ID": "{session_id}"})
        self.assertEqual(adapter["type"], "command")
        validate_adapter(adapter)

    def test_template_rendering(self):
        adapter = make_adapter(
            name="x", executable="{project_root}/bin/agent",
            args_template=["--file", "{prompt_file}", "--s", "{session_id}"],
            env_template={"ADCC_PROJECT_ID": "{project_id}"},
            cwd_template="{project_root}")
        variables = {
            "prompt_file": "/tmp/p.txt",
            "session_id": "abcd1234", "project_id": "efgh5678",
            "project_root": "/repo"}
        self.assertEqual(render_command(adapter, variables),
                         ["/repo/bin/agent", "--file", "/tmp/p.txt",
                          "--s", "abcd1234"])
        self.assertEqual(render_env(adapter, variables),
                         {"ADCC_PROJECT_ID": "efgh5678"})
        self.assertEqual(render_cwd(adapter, variables), "/repo")

    def test_unknown_placeholders_left_verbatim(self):
        adapter = make_adapter(name="x", executable="e",
                               args_template=["{unknown_var}"])
        self.assertEqual(render_command(adapter, {"session_id": "a"}),
                         ["e", "{unknown_var}"])

    def test_session_validation(self):
        from adcc.agents.models import make_session
        session = make_session(project_id="aaaaaaaa", adapter_id="bbbbbbbb")
        validate_session(session)
        session["status"] = "exploded"
        with self.assertRaises(ValueError):
            validate_session(session)


class RunnerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = AgentHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _adapter(self, exit_code=0, duration=1.0, extra_args=None):
        result_file = os.path.join(self.h.tmp.name, "result.json")
        if os.path.exists(result_file):
            os.remove(result_file)
        env = {"FAKE_RESULT": result_file, "FAKE_EXIT": str(exit_code),
               "FAKE_DURATION": str(duration),
               "ADCC_SESSION_ID": "{session_id}",
               "ADCC_PROJECT_ID": "{project_id}"}
        return self.h.register_adapter(
            name="fake-%d" % exit_code, executable=sys.executable,
            args_template=[self.h.fake_agent_path, "{prompt_file}",
                           *(extra_args or [])],
            env_template=env)

    def test_fake_agent_runs_and_succeeds(self):
        adapter = self._adapter(exit_code=0, duration=1.0)
        session, error = self.h.runner.start(
            adapter["id"], self.h.project_id, prompt="你好 agent")
        self.assertIsNone(error, error)
        self.assertEqual(session["status"], "running")
        deadline = time.time() + 12
        while time.time() < deadline:
            current = self.h.runner.get_session(session["id"])
            if current["status"] != "running":
                break
            time.sleep(0.2)
        self.assertEqual(current["status"], "succeeded")
        self.assertEqual(current["exit_code"], 0)
        self.assertIsNotNone(current["started_at"])
        self.assertIsNotNone(current["ended_at"])
        result = json.loads(open(os.path.join(
            self.h.tmp.name, "result.json"), encoding="utf-8").read())
        self.assertEqual(result["prompt"], "你好 agent")
        self.assertEqual(result["session"], session["id"])
        self.assertEqual(result["project"], self.h.project_id)

    def test_fake_agent_failure_is_recorded(self):
        adapter = self._adapter(exit_code=3, duration=0.5)
        session, error = self.h.runner.start(
            adapter["id"], self.h.project_id, prompt="会失败")
        self.assertIsNone(error)
        deadline = time.time() + 12
        while time.time() < deadline:
            current = self.h.runner.get_session(session["id"])
            if current["status"] != "running":
                break
            time.sleep(0.2)
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["exit_code"], 3)

    def test_stop_running_agent(self):
        adapter = self._adapter(exit_code=0, duration=30)
        session, error = self.h.runner.start(
            adapter["id"], self.h.project_id, prompt="长任务")
        self.assertIsNone(error)
        ok, stop_error = self.h.runner.stop(session["id"])
        self.assertTrue(ok, stop_error)
        current = self.h.runner.get_session(session["id"])
        self.assertEqual(current["status"], "stopped")

    def test_concurrency_queues_sessions(self):
        policy = {"global_max": 2, "per_project_max": 1}
        self.h.cfg.update(lambda d: d.__setitem__("agent_policy", policy))
        try:
            # 长 duration：避免 CI 慢机器上 agent 在 stop 前自然退出
            adapter = self._adapter(exit_code=0, duration=30)
            first, error = self.h.runner.start(
                adapter["id"], self.h.project_id, prompt="1")
            self.assertIsNone(error)
            second, error = self.h.runner.start(
                adapter["id"], self.h.project_id, prompt="2")
            self.assertIsNone(error)
            # per_project_max=1 → 第二个排队
            self.assertEqual(second["status"], "queued")
            ok, stop_error = self.h.runner.stop(first["id"])
            self.assertTrue(ok, stop_error)
            # 第一个结束后唤醒排队会话
            deadline = time.time() + 10
            while time.time() < deadline:
                current = self.h.runner.get_session(second["id"])
                if current["status"] in ("running", "succeeded", "failed"):
                    break
                time.sleep(0.3)
            self.assertIn(current["status"], ("running", "succeeded", "failed"))
            if current["status"] == "running":
                ok, stop_error = self.h.runner.stop(second["id"])
                self.assertTrue(ok, stop_error)
        finally:
            self.h.cfg.update(lambda d: d.__setitem__("agent_policy", {}))

    def test_global_limit_queues(self):
        policy = {"global_max": 1, "per_project_max": 5}
        self.h.cfg.update(lambda d: d.__setitem__("agent_policy", policy))
        try:
            adapter = self._adapter(exit_code=0, duration=30)
            first, error = self.h.runner.start(
                adapter["id"], self.h.project_id, prompt="1")
            self.assertIsNone(error)
            second, error = self.h.runner.start(
                adapter["id"], self.h.project_id, prompt="2")
            self.assertIsNone(error)
            self.assertEqual(second["status"], "queued")
            ok, stop_error = self.h.runner.stop(first["id"])
            self.assertTrue(ok, stop_error)
            # 第一个结束后唤醒排队会话；测试结束前清理可能被唤醒的进程
            deadline = time.time() + 10
            while time.time() < deadline:
                current = self.h.runner.get_session(second["id"])
                if current["status"] in ("running", "succeeded", "failed",
                                         "lost"):
                    break
                time.sleep(0.3)
            if current["status"] == "running":
                ok, stop_error = self.h.runner.stop(second["id"])
                self.assertTrue(ok, stop_error)
        finally:
            self.h.cfg.update(lambda d: d.__setitem__("agent_policy", {}))

    def test_unknown_adapter_returns_error(self):
        session, error = self.h.runner.start("ffffffff", self.h.project_id)
        self.assertIsNone(session)
        self.assertIn("适配器不存在", error)

    def test_queued_session_can_be_canceled(self):
        policy = {"global_max": 1, "per_project_max": 5}
        self.h.cfg.update(lambda d: d.__setitem__("agent_policy", policy))
        try:
            adapter = self._adapter(exit_code=0, duration=30)
            first, error = self.h.runner.start(
                adapter["id"], self.h.project_id, prompt="1")
            self.assertIsNone(error)
            second, error = self.h.runner.start(
                adapter["id"], self.h.project_id, prompt="2")
            self.assertEqual(second["status"], "queued")
            ok, stop_error = self.h.runner.stop(second["id"])
            self.assertTrue(ok, stop_error)
            self.assertEqual(
                self.h.runner.get_session(second["id"])["status"], "canceled")
            ok, stop_error = self.h.runner.stop(first["id"])
            self.assertTrue(ok, stop_error)
        finally:
            self.h.cfg.update(lambda d: d.__setitem__("agent_policy", {}))


class AgentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = AgentHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_adapter_crud_via_api(self):
        status, body = self.h.request(
            "POST", "/api/v1/agents/adapters", {
                "name": "opencode", "executable": "opencode",
                "argsTemplate": ["run", "--prompt-file", "{prompt_file}"]})
        self.assertEqual(status, 201, body)
        adapter_id = body["id"]
        status, body = self.h.request("GET", "/api/v1/agents/adapters")
        self.assertEqual(status, 200)
        self.assertTrue(any(a["id"] == adapter_id for a in body))
        status, body = self.h.request(
            "POST", "/api/v1/agents/adapters", {"name": "", "executable": ""})
        self.assertEqual(status, 400)

    def test_session_lifecycle_via_api(self):
        adapter = self.h.register_adapter(
            name="api-fake", executable=sys.executable,
            args_template=[self.h.fake_agent_path, "{prompt_file}"],
            env_template={"FAKE_DURATION": "30", "FAKE_EXIT": "0",
                          "FAKE_RESULT": os.path.join(self.h.tmp.name, "r.json")})
        status, body = self.h.request(
            "POST", "/api/v1/agents/sessions", {
                "adapterId": adapter["id"],
                "projectId": self.h.project_id,
                "prompt": "来自 API"})
        self.assertEqual(status, 201, body)
        session_id = body["id"]
        status, body = self.h.request("GET", "/api/v1/agents/sessions")
        self.assertEqual(status, 200)
        self.assertTrue(any(s["id"] == session_id for s in body["sessions"]))
        status, body = self.h.request(
            "GET", "/api/v1/agents/sessions/" + session_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], session_id)
        status, body = self.h.request(
            "POST", "/api/v1/agents/sessions/%s/stop" % session_id)
        self.assertEqual(status, 200, body)
        deadline = time.time() + 8
        while time.time() < deadline:
            status, body = self.h.request(
                "GET", "/api/v1/agents/sessions/" + session_id)
            if body.get("status") != "running":
                break
            time.sleep(0.3)
        self.assertNotEqual(body.get("status"), "running")

    def test_session_validation_via_api(self):
        status, body = self.h.request(
            "POST", "/api/v1/agents/sessions", {"adapterId": "aaaaaaaa"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
