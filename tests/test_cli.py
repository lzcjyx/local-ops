"""M5 CLI contract tests: daemon discovery, exit codes, and real requests.

The CLI is exercised through the real HTTP daemon (V1Harness) plus an
unreachable-endpoint scenario; it never duplicates runtime logic.
"""

import http.client
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
import adcc.cli.main as cli

V1 = "/api/v1"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CliHarness:
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
        self.cfg = server.Config(os.path.join(self.data_dir, "config.json"))
        server.ensure_project_domain(self.cfg)
        self.httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, self.cfg, 0)
        self.port = self.httpd.server_address[1]
        server.invalidate_state_cache()
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        with open(os.path.join(self.data_dir, cli.ENDPOINT_FILENAME),
                  "w", encoding="utf-8") as handle:
            json.dump({"port": self.port, "pid": os.getpid(),
                       "token": self.httpd.control_token}, handle)

    def close(self):
        db = server.get_runs_db()
        if db is not None:
            db.close()
            server.RUNS_DB = None
        self._patch_db.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        time.sleep(0.8)  # 等待 watch 线程完成日志轮转，避免与清理竞态
        self.tmp.cleanup()

    def run(self, *argv):
        return cli.main(["--data-dir", self.data_dir, *argv])


class CliDiscoveryTests(unittest.TestCase):
    def test_missing_endpoint_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            code = cli.main(["--data-dir", td, "status"])
            self.assertEqual(code, cli.EXIT_UNAVAILABLE)

    def test_discover_endpoint_parses_managed_ports(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, cli.ENDPOINT_FILENAME)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"port": 9603, "pid": 1, "token": "t"}, handle)
            endpoint = cli.discover_endpoint(td)
            self.assertEqual(endpoint["port"], 9603)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"port": 99999}, handle)
            with self.assertRaises(cli.DaemonUnavailable):
                cli.discover_endpoint(td)

    def test_unreachable_daemon_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, cli.ENDPOINT_FILENAME), "w",
                      encoding="utf-8") as handle:
                json.dump({"port": free_port(), "pid": 1}, handle)
            code = cli.main(["--data-dir", td, "status"])
            self.assertEqual(code, cli.EXIT_UNAVAILABLE)


class CliLiveDaemonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = CliHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_status_json_and_plain(self):
        self.assertEqual(self.h.run("status", "--json"), cli.EXIT_OK)
        self.assertEqual(self.h.run("status"), cli.EXIT_OK)

    def test_doctor(self):
        self.assertEqual(self.h.run("doctor", "--json"), cli.EXIT_OK)
        self.assertEqual(self.h.run("doctor"), cli.EXIT_OK)

    def test_projects_lifecycle(self):
        code = self.h.run("projects", "list", "--json")
        self.assertEqual(code, cli.EXIT_OK)
        code = self.h.run("projects", "list")
        self.assertEqual(code, cli.EXIT_OK)
        # 创建项目（经 HTTP POST，CLI 只读命令不创建；此处直接调 API）
        conn = http.client.HTTPConnection(server.HOST, self.h.port, timeout=10)
        conn.request("POST", V1 + "/projects",
                     json.dumps({"name": "博客", "rootPath": r"C:\dev\blog"}),
                     {"Content-Type": "application/json",
                      "X-ADCC-Token": self.h.httpd.control_token})
        response = conn.getresponse()
        project = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 201)
        code = self.h.run("projects", "show", project["id"])
        self.assertEqual(code, cli.EXIT_OK)
        self.h.run("projects", "list", "--json")

    def test_resources_list(self):
        self.assertEqual(self.h.run("resources", "list", "--json"), cli.EXIT_OK)
        self.assertEqual(self.h.run("resources", "list"), cli.EXIT_OK)

    def test_ports_and_owner(self):
        self.assertEqual(self.h.run("ports", "--json"), cli.EXIT_OK)
        # 空端口查询返回 1（无监听者）
        self.assertEqual(self.h.run("port", "owner", "59999"), cli.EXIT_ERROR)
        self.assertEqual(
            self.h.run("port", "owner", "59999", "--json"), cli.EXIT_ERROR)

    def test_runs_list_and_logs(self):
        # 注册并运行一个任务，产生 run 记录
        port = free_port()
        created, err = server.validate_app_fields(
            {"name": "cli-task",
             "command": 'python -c "import time; time.sleep(0.6)"',
             "cwd": self.h.tmp.name, "port": None, "kind": "task"},
            partial=False)
        assert err is None, err
        app = created
        app["id"] = "abcd0001"
        self.h.cfg.update(lambda d: d["apps"].append(app))
        with mock.patch.object(server, "scan_listeners", return_value=set()):
            conn = http.client.HTTPConnection(server.HOST, self.h.port, timeout=10)
            conn.request("POST", "/api/apps/abcd0001/start", "{}",
                         {"Content-Type": "application/json",
                          "X-ADCC-Token": self.h.httpd.control_token})
            response = conn.getresponse()
            conn.close()
        self.assertEqual(response.status, 200)
        db = server.get_runs_db()
        deadline = time.time() + 10
        run = None
        while time.time() < deadline:
            runs = db.list_runs(app_id="abcd0001")
            if runs and runs[0]["status"] != "running":
                run = runs[0]
                break
            time.sleep(0.2)
        self.assertIsNotNone(run)
        self.assertEqual(self.h.run("runs", "list", "--json"), cli.EXIT_OK)
        self.assertEqual(self.h.run("runs", "list"), cli.EXIT_OK)
        self.assertEqual(self.h.run("logs", run["id"]), cli.EXIT_OK)
        self.assertEqual(self.h.run("logs", "abcd0001"), cli.EXIT_OK)
        self.assertEqual(self.h.run("logs", "doesnotexist"), cli.EXIT_ERROR)

    def test_start_stop_restart(self):
        port = free_port()
        # 经 HTTP 创建（同步注册项目资源）
        conn = http.client.HTTPConnection(server.HOST, self.h.port, timeout=10)
        conn.request("POST", "/api/apps",
                     json.dumps({"name": "cli-svc",
                                 "command": "python -m http.server %d" % port,
                                 "cwd": self.h.tmp.name, "port": port,
                                 "kind": "service"}),
                     {"Content-Type": "application/json",
                      "X-ADCC-Token": self.h.httpd.control_token})
        response = conn.getresponse()
        app = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 200, app)
        snapshot = self.h.cfg.snapshot()
        resource = next(
            r for r in snapshot["resources"] if r.get("app_id") == app["id"])
        self.assertEqual(self.h.run("start", resource["id"]), cli.EXIT_OK)
        deadline = time.time() + 8
        current = None
        while time.time() < deadline:
            snapshot = self.h.cfg.snapshot()
            current = next(a for a in snapshot["apps"] if a["id"] == app["id"])
            if server.app_running(current):
                break
            time.sleep(0.2)
        self.assertTrue(server.app_running(current))
        self.assertEqual(self.h.run("restart", resource["id"]), cli.EXIT_OK)
        time.sleep(1.0)
        self.assertEqual(self.h.run("stop", resource["id"]), cli.EXIT_OK)
        deadline = time.time() + 8
        while time.time() < deadline:
            snapshot = self.h.cfg.snapshot()
            current = next(a for a in snapshot["apps"] if a["id"] == app["id"])
            if not server.app_running(current):
                break
            time.sleep(0.2)
        self.assertFalse(server.app_running(current))
        # 不存在的资源 → 1
        self.assertEqual(self.h.run("stop", "ffffffff"), cli.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()
