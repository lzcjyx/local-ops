"""M4 /api/v1 HTTP contract tests (health/state/projects/resources/runs/events)."""

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

V1 = "/api/v1"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class V1Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch_db = mock.patch.object(
            server, "RUNS_DB_PATH",
            os.path.join(self.tmp.name, "console.sqlite3"))
        self._patch_db.start()
        server.RUNS_DB = None
        self.cfg = server.Config(os.path.join(self.tmp.name, "config.json"))
        server.ensure_project_domain(self.cfg)
        self.httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, self.cfg, 0)
        self.port = self.httpd.server_address[1]
        server.invalidate_state_cache()
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

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

    def request(self, method, path, body=None, headers=None, timeout=15):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=timeout)
        request_headers = dict(headers or {})
        if body is not None and not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body)
            request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("X-ADCC-Token", self.httpd.control_token)
        request_headers.setdefault("X-ADCC-Token", self.httpd.control_token)
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        conn.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = raw
        return status, payload, result_headers


class V1HealthStateTests(unittest.TestCase):
    def setUp(self):
        self.h = V1Harness()

    def tearDown(self):
        self.h.close()

    def test_v1_health_shape(self):
        status, body, _ = self.h.request("GET", V1 + "/health")
        self.assertEqual(status, 200)
        self.assertIn("status", body)
        self.assertIn("version", body)
        self.assertEqual(body["schemaVersion"], 2)
        self.assertIsInstance(body["issues"], list)

    def test_v1_state_shape(self):
        status, body, _ = self.h.request("GET", V1 + "/state")
        self.assertEqual(status, 200)
        for key in ("services", "apps", "projects", "watched",
                    "consolePort", "version", "schemaVersion"):
            self.assertIn(key, body)
        self.assertEqual(body["schemaVersion"], 2)


class V1ProjectResourceTests(unittest.TestCase):
    def setUp(self):
        self.h = V1Harness()

    def tearDown(self):
        self.h.close()

    def test_create_and_list_projects(self):
        status, body, _ = self.h.request(
            "POST", V1 + "/projects",
            {"name": "博客", "rootPath": r"C:\dev\blog"})
        self.assertEqual(status, 201, body)
        project_id = body["id"]
        self.assertRegex(project_id, r"^[0-9a-f]{8}$")
        status, body, _ = self.h.request("GET", V1 + "/projects")
        self.assertEqual(status, 200)
        ids = [p["id"] for p in body]
        self.assertIn(project_id, ids)
        created = next(p for p in body if p["id"] == project_id)
        self.assertEqual(created["name"], "博客")
        self.assertEqual(created["resources"], [])
        self.assertIn("root_path", created)

    def test_create_project_rejects_invalid_input(self):
        status, body, _ = self.h.request(
            "POST", V1 + "/projects", {"name": "", "rootPath": ""})
        self.assertEqual(status, 400)

    def test_get_single_project(self):
        status, body, _ = self.h.request(
            "POST", V1 + "/projects", {"name": "x", "rootPath": r"C:\dev\x"})
        project_id = body["id"]
        status, body, _ = self.h.request(
            "GET", V1 + "/projects/" + project_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], project_id)
        status, body, _ = self.h.request(
            "GET", V1 + "/projects/ffffffff")
        self.assertEqual(status, 404)

    def test_resources_listed_from_legacy_migration(self):
        legacy_apps = [
            {"id": "aaaaaaaa", "name": "dev", "command": "vite",
             "cwd": r"C:\dev\blog", "port": 5173, "kind": "service",
             "createdAt": 0},
            {"id": "bbbbbbbb", "name": "build", "command": "make",
             "cwd": None, "kind": "task", "createdAt": 0},
        ]
        self.h.cfg.update(lambda d: d["apps"].extend(legacy_apps))
        server.ensure_project_domain(self.h.cfg)
        status, body, _ = self.h.request("GET", V1 + "/resources")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["kind"], "service")
        self.assertIn("app_id", body[0])


class V1RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = V1Harness()

    def tearDown(self):
        self.h.close()

    def _register_service(self, port):
        created, err = server.validate_app_fields(
            {"name": "srv",
             "command": ("python -m http.server %d" % port if port
                         else 'python -c "import time; time.sleep(30)"'),
             "cwd": self.h.tmp.name,
             "port": port or None}, partial=False)
        assert err is None, err
        app = created
        app["id"] = "abcdefab"
        self.h.cfg.update(lambda d: d["apps"].append(app))
        return app

    def test_task_run_has_durable_history(self):
        """Exit gate: a task run has a durable run id/history record."""
        app = self._register_service(0)
        app["port"] = None
        app["kind"] = "task"
        app["command"] = 'python -c "import time; time.sleep(0.8)"'
        self.h.cfg.update(lambda d: d["apps"][-1].update(app))
        db = server.get_runs_db()
        self.assertIsNotNone(db)
        with mock.patch.object(server, "scan_listeners", return_value=set()):
            status, body, _ = self.h.request(
                "POST", "/api/apps/abcdefab/start", {})
        self.assertEqual(status, 200, body)
        deadline = time.time() + 10
        run = None
        while time.time() < deadline:
            runs = db.list_runs(app_id="abcdefab")
            if runs and runs[0]["status"] in (
                    "succeeded", "failed", "canceled", "stopped"):
                run = runs[0]
                break
            time.sleep(0.2)
        self.assertIsNotNone(run, "run never finalized")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["kind"], "task")
        status, body, _ = self.h.request("GET", V1 + "/runs")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["runs"][0]["id"], run["id"])
        self.assertEqual(body["runs"][0]["appId"], "abcdefab")
        status, body, _ = self.h.request(
            "GET", V1 + "/runs/" + run["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["exitCode"], 0)

    def test_run_logs_endpoint(self):
        app = self._register_service(0)
        app["kind"] = "task"
        app["command"] = "python -c 'import time; time.sleep(0.5)'"
        app["port"] = None
        self.h.cfg.update(lambda d: d["apps"][-1].update(app))
        with mock.patch.object(server, "scan_listeners", return_value=set()):
            self.h.request("POST", "/api/apps/abcdefab/start", {})
        db = server.get_runs_db()
        deadline = time.time() + 10
        while time.time() < deadline:
            runs = db.list_runs(app_id="abcdefab")
            if runs and runs[0]["status"] != "running":
                run_id = runs[0]["id"]
                break
            time.sleep(0.2)
        else:
            self.fail("run never finalized")
        status, body, _ = self.h.request(
            "GET", V1 + "/runs/" + run_id + "/logs")
        self.assertEqual(status, 200)
        self.assertEqual(body["runId"], run_id)
        self.assertIsInstance(body["text"], str)

    def test_service_run_stop_records_stopped(self):
        app = self._register_service(free_port())
        db = server.get_runs_db()
        status, body, _ = self.h.request(
            "POST", "/api/apps/abcdefab/start", {})
        self.assertEqual(status, 200, body)
        deadline = time.time() + 10
        while time.time() < deadline:
            if db.list_runs(app_id="abcdefab"):
                break
            time.sleep(0.2)
        status, body, _ = self.h.request(
            "POST", "/api/apps/abcdefab/stop", {})
        self.assertEqual(status, 200, body)
        deadline = time.time() + 10
        while time.time() < deadline:
            runs = db.list_runs(app_id="abcdefab")
            if runs and runs[0]["status"] == "stopped":
                break
            time.sleep(0.2)
        runs = db.list_runs(app_id="abcdefab")
        self.assertEqual(runs[0]["status"], "stopped")
        self.assertEqual(runs[0]["kind"], "service")

    def test_restart_reconciliation_marks_vanished_as_lost(self):
        """Exit gate: restart must not mark vanished work as success."""
        app = self._register_service(0)
        app["kind"] = "task"
        # 双引号：单引号在 Windows cmd 批处理里不是引号，命令会立即失败
        app["command"] = 'python -c "import time; time.sleep(30)"'
        app["port"] = None
        self.h.cfg.update(lambda d: d["apps"][-1].update(app))
        db = server.get_runs_db()
        with mock.patch.object(server, "scan_listeners", return_value=set()):
            status, body, _ = self.h.request(
                "POST", "/api/apps/abcdefab/start", {})
        self.assertEqual(status, 200, body)
        deadline = time.time() + 10
        while time.time() < deadline:
            runs = db.list_runs(app_id="abcdefab")
            if runs and runs[0]["status"] == "running":
                break
            time.sleep(0.2)
        self.assertEqual(runs[0]["status"], "running",
                         "任务应保持运行中（进程秒退说明命令有误）")
        # 模拟进程消失但身份仍在配置里（例如外部杀掉了进程）
        with mock.patch.object(server, "app_running", return_value=False):
            server.reconcile_runs(self.h.cfg)
        runs = db.list_runs(app_id="abcdefab")
        self.assertEqual(runs[0]["status"], "lost")
        self.assertNotEqual(runs[0]["status"], "succeeded")
        # 清理真实进程，避免残留持有日志文件（reconcile 只是状态对账）
        current = next((a for a in self.h.cfg.snapshot()["apps"]
                        if a["id"] == "abcdefab"), None)
        pid = current.get("lastPid") if current else None
        if isinstance(pid, int) and pid > 0:
            server.PLATFORM.terminate_tree(pid, force=True)
            deadline = time.time() + 5
            while time.time() < deadline and server.PLATFORM.pid_alive(pid):
                time.sleep(0.1)


class V1EventStreamTests(unittest.TestCase):
    def setUp(self):
        self.h = V1Harness()

    def tearDown(self):
        self.h.close()

    def test_events_stream_delivers_published_events(self):
        import queue
        import socket

        received = queue.Queue()
        stop = threading.Event()

        def consume():
            conn = http.client.HTTPConnection(server.HOST, self.h.port, timeout=15)
            conn.request("GET", V1 + "/events")
            response = conn.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type") == "text/event-stream"
            deadline = time.time() + 8
            while time.time() < deadline:
                line = response.fp.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    received.put(line.decode("utf-8", "replace").strip())
                    return

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        time.sleep(0.8)
        server.EVENTS.publish("run.updated", {"id": "xxxxxxxx", "status": "succeeded"})
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                block = received.get(timeout=1)
                if "run.updated" in block:
                    self.assertIn("succeeded", block)
                    break
            except Exception:
                continue
        else:
            self.fail("no SSE event delivered")
        stop.set()
        conn_closed = threading.Event()

        def close_conn():
            conn_closed.set()
        thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
