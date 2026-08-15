"""M6 MCP contract tests: JSON-RPC over stdio against a live daemon.

The MCP server runs as a child process (real stdio transport); the daemon
runs in-process behind a harness.  A tiny JSON-RPC client talks to the
child and verifies initialize/tools/list/tools/call contracts.
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
from adcc.cli.main import DaemonUnavailable
from adcc.mcp.server import (
    DEFAULT_LOG_TAIL,
    MAX_LOG_TAIL,
    MAX_RUNS,
    PROTOCOL_VERSION,
    McpServer,
    TOOLS,
    TOOL_BY_NAME,
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class McpDaemonHarness:
    """Daemon harness + MCP child process connected over stdio."""

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
        with open(os.path.join(self.data_dir, "daemon.json"),
                  "w", encoding="utf-8") as handle:
            json.dump({"port": self.port, "pid": os.getpid(),
                       "token": self.httpd.control_token}, handle)
        root = os.path.dirname(os.path.abspath(server.__file__))
        self.mcp = subprocess.Popen(
            [sys.executable, "-m", "adcc.mcp.server",
             "--data-dir", self.data_dir],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace")
        time.sleep(1.0)  # 等待子进程完成导入/启动
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id,
                              "method": method,
                              "params": params or {}}, ensure_ascii=False)
        self.mcp.stdin.write(payload + "\n")
        self.mcp.stdin.flush()
        line = self.mcp.stdout.readline()
        if not line:
            raise AssertionError("MCP 子进程无响应")
        return json.loads(line)

    def call_tool(self, name, arguments=None):
        response = self.request("tools/call", {
            "name": name, "arguments": arguments or {}})
        if response.get("error"):
            raise AssertionError("工具调用失败: %s" % response["error"])
        return response["result"]

    def close(self):
        try:
            self.mcp.stdin.close()
            self.mcp.wait(timeout=5)
        except Exception:
            self.mcp.kill()
        db = server.get_runs_db()
        if db is not None:
            db.close()
            server.RUNS_DB = None
        self._patch_db.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        time.sleep(0.8)
        self.tmp.cleanup()


class McpProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = McpDaemonHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_initialize_handshake(self):
        response = self.h.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientInfo": {"name": "test-client"}})
        self.assertIsNone(response.get("error"), response)
        result = response["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], "adcc-mcp")
        self.assertIn("tools", result["capabilities"])

    def test_ping(self):
        response = self.h.request("ping")
        self.assertIsNone(response.get("error"))
        self.assertEqual(response["result"], {})

    def test_unknown_method_returns_typed_error(self):
        response = self.h.request("bogus/method")
        self.assertEqual(response["error"]["code"], -32601)

    def test_malformed_request_returns_parse_error(self):
        self.h.mcp.stdin.write("{not json}\n")
        self.h.mcp.stdin.flush()
        line = self.h.mcp.stdout.readline()
        response = json.loads(line)
        self.assertEqual(response["error"]["code"], -32700)


class McpToolCatalogTests(unittest.TestCase):
    def test_catalog_contains_required_safe_tools(self):
        names = {tool["name"] for tool in TOOLS}
        for required in (
                "list_projects", "get_project", "list_resources",
                "get_resource_status", "start_resource", "stop_resource",
                "restart_resource", "list_runs", "get_run", "get_run_logs",
                "get_port_owner", "run_task"):
            self.assertIn(required, names)

    def test_no_dangerous_tools_exposed(self):
        names = {tool["name"] for tool in TOOLS}
        self.assertNotIn("kill", names)
        self.assertNotIn("kill_process", names)
        self.assertNotIn("shell", names)
        self.assertNotIn("run_shell", names)
        self.assertFalse(any("kill" in name for name in names))

    def test_catalog_has_schemas(self):
        for tool in TOOLS:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")


class McpToolCallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = McpDaemonHarness()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _register_resource(self, name, command, port=None, kind="service"):
        body = {"name": name, "command": command,
                "cwd": self.h.tmp.name, "kind": kind}
        if port:
            body["port"] = port
        import http.client
        conn = http.client.HTTPConnection(server.HOST, self.h.port, timeout=10)
        conn.request("POST", "/api/apps", json.dumps(body),
                     {"Content-Type": "application/json",
                      "X-ADCC-Token": self.h.httpd.control_token})
        response = conn.getresponse()
        created = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 200, created)
        snapshot = self.h.cfg.snapshot()
        resource = next(
            r for r in snapshot["resources"] if r.get("app_id") == created["id"])
        return resource

    def test_list_projects(self):
        result = self.h.call_tool("list_projects")
        self.assertEqual(result["isError"], False)
        content = json.loads(result["content"][0]["text"])
        self.assertIsInstance(content, list)

    def test_resource_lifecycle_start_stop(self):
        port = free_port()
        resource = self._register_resource(
            "mcp-svc", "python -m http.server %d" % port, port=port)
        result = self.h.call_tool("start_resource", {"id": resource["id"]})
        self.assertEqual(result["isError"], False)
        deadline = time.time() + 8
        while time.time() < deadline:
            status = self.h.call_tool(
                "get_resource_status", {"id": resource["id"]})
            payload = json.loads(status["content"][0]["text"])
            if payload.get("running"):
                break
            time.sleep(0.3)
        self.assertTrue(payload["running"])
        result = self.h.call_tool("stop_resource", {"id": resource["id"]})
        self.assertEqual(result["isError"], False)
        deadline = time.time() + 8
        while time.time() < deadline:
            status = self.h.call_tool(
                "get_resource_status", {"id": resource["id"]})
            payload = json.loads(status["content"][0]["text"])
            if not payload.get("running"):
                break
            time.sleep(0.3)
        self.assertFalse(payload["running"])

    def test_task_run_and_bounded_logs(self):
        resource = self._register_resource(
            "mcp-task", 'python -c "import time; time.sleep(0.5)"',
            kind="task")
        result = self.h.call_tool("run_task", {"id": resource["id"]})
        self.assertEqual(result["isError"], False)
        deadline = time.time() + 10
        run = None
        while time.time() < deadline:
            runs = self.h.call_tool("list_runs", {"limit": 5})
            payload = json.loads(runs["content"][0]["text"])
            candidates = [r for r in payload.get("runs", [])
                          if r.get("appId") == resource.get("app_id")]
            if candidates and candidates[0]["status"] != "running":
                run = candidates[0]
                break
            time.sleep(0.3)
        self.assertIsNotNone(run, "任务 run 未终结")
        result = self.h.call_tool("get_run", {"id": run["id"]})
        self.assertEqual(result["isError"], False)
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["status"], "succeeded")
        result = self.h.call_tool("get_run_logs", {"id": run["id"]})
        self.assertEqual(result["isError"], False)
        logs = json.loads(result["content"][0]["text"])
        self.assertIn("text", logs)

    def test_get_port_owner(self):
        result = self.h.call_tool("get_port_owner", {"port": 59999})
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(payload["found"])

    def test_invalid_arguments_return_typed_errors(self):
        response = self.h.request("tools/call", {
            "name": "get_project", "arguments": {}})
        self.assertIsNotNone(response["error"])
        self.assertIn("必填", response["error"]["message"])
        response = self.h.request("tools/call", {
            "name": "get_port_owner", "arguments": {"port": "abc"}})
        self.assertIsNotNone(response["error"])
        response = self.h.request("tools/call", {
            "name": "nonexistent_tool", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_missing_resource_returns_typed_error(self):
        response = self.h.request("tools/call", {
            "name": "get_resource_status", "arguments": {"id": "ffffffff"}})
        self.assertIsNotNone(response["error"])
        self.assertIn("资源不存在", response["error"]["message"])

    def test_stop_unmanaged_resource_is_rejected(self):
        """exit gate: stop/cancel only managed items."""
        response = self.h.request("tools/call", {
            "name": "stop_resource", "arguments": {"id": "ffffffff"}})
        self.assertIsNotNone(response["error"])


class McpUnitTests(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(MAX_LOG_TAIL, 2000)
        self.assertEqual(MAX_RUNS, 100)

    def test_server_handles_non_dict_request(self):
        server = McpServer(client_factory=lambda: None)
        response = server.handle_request([])
        self.assertEqual(response["error"]["code"], -32600)

    def test_server_unavailable_daemon_typed_error(self):
        def factory():
            raise DaemonUnavailable("测试不可达")
        server = McpServer(client_factory=factory)
        response = server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32001)


if __name__ == "__main__":
    unittest.main()
