"""P1 tests: Linux adapter wiring, project templates/manifests, discovery,
secrets resolution, remote read-only mode."""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

import server
from adcc.agents.discovery import discover_agents, suggest_adapter
from adcc.core.secrets import (
    SecretUnavailable,
    resolve_environment,
    secret_get,
    secret_set,
)
from adcc.platform.linux import LinuxPlatformAdapter
from adcc.projects.templates import (
    TEMPLATES,
    apply_template,
    export_manifest,
    import_manifest,
    list_templates,
)
from adcc.core.constants import CONFIG_DEFAULT


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProjectTemplatesTests(unittest.TestCase):
    def test_templates_exist_and_are_pure_presets(self):
        ids = {t["id"] for t in TEMPLATES}
        self.assertIn("web-frontend", ids)
        self.assertIn("python-api", ids)
        self.assertIn("static-site", ids)
        self.assertIn("mcp-server", ids)

    def test_apply_template_creates_resources_idempotently(self):
        data = json.loads(json.dumps(CONFIG_DEFAULT))
        from adcc.projects import create_project
        project = create_project(data, "博客", "/tmp/blog")
        created = apply_template(data, project["id"], "web-frontend")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["kind"], "service")
        self.assertEqual(created[0]["port"], 5173)
        # 幂等：同名不重复
        again = apply_template(data, project["id"], "web-frontend")
        self.assertEqual(len(again), 0)

    def test_unknown_template_and_project_errors(self):
        data = json.loads(json.dumps(CONFIG_DEFAULT))
        from adcc.projects import create_project
        project = create_project(data, "x", "/tmp/x")
        with self.assertRaises(ValueError):
            apply_template(data, project["id"], "nope")
        with self.assertRaises(ValueError):
            apply_template(data, "ffffffff", "web-frontend")


class ManifestTests(unittest.TestCase):
    def test_export_import_roundtrip(self):
        data = json.loads(json.dumps(CONFIG_DEFAULT))
        from adcc.projects import create_project, create_resource
        project = create_project(data, "博客", "/tmp/blog")
        create_resource(data, project["id"], "dev", "service",
                        "npm run dev", port=3000)
        manifest = export_manifest(data)
        self.assertEqual(manifest["manifestVersion"], 1)
        self.assertEqual(len(manifest["projects"]), 1)
        self.assertEqual(len(manifest["resources"]), 1)

        target = json.loads(json.dumps(CONFIG_DEFAULT))
        result = import_manifest(target, manifest)
        self.assertEqual(result["projects"], 1)
        self.assertEqual(result["resources"], 1)
        self.assertEqual(target["projects"][0]["id"], project["id"])
        # 幂等：重复导入跳过
        again = import_manifest(target, manifest)
        self.assertEqual(again["projects"], 0)
        self.assertEqual(again["resources"], 0)

    def test_import_rejects_unknown_version(self):
        with self.assertRaises(ValueError):
            import_manifest({"projects": []}, {"manifestVersion": 99})


class DiscoveryTests(unittest.TestCase):
    def test_suggest_adapter_for_known_agents(self):
        adapter = suggest_adapter("opencode")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter["executable"], "opencode")
        self.assertIn("{prompt_file}", " ".join(adapter["args_template"]))
        self.assertIsNone(suggest_adapter("not-an-agent-xyz"))

    def test_discover_agents_finds_python_path_entries(self):
        found = discover_agents()
        self.assertIsInstance(found, list)
        for item in found:
            self.assertIn("executable", item)
            self.assertIn("path", item)

    def test_adapter_cost_metadata(self):
        from adcc.agents.models import make_adapter
        adapter = make_adapter(
            name="paid", executable="opencode",
            cost={"model": "claude-sonnet", "inputPer1k": 0.003,
                  "outputPer1k": 0.015},
            token_budget=200000)
        self.assertEqual(adapter["cost"]["model"], "claude-sonnet")
        self.assertEqual(adapter["token_budget"], 200000)
        with self.assertRaises(ValueError):
            make_adapter(name="bad", executable="x", token_budget=-1)


class SecretsTests(unittest.TestCase):
    def test_resolve_environment_leaves_unknown_verbatim(self):
        resolved, unresolved = resolve_environment({
            "K": "${secret:missing-secret-xyz}", "N": "plain"})
        self.assertEqual(resolved["K"], "${secret:missing-secret-xyz}")
        self.assertEqual(unresolved, ["missing-secret-xyz"])

    @unittest.skipUnless(sys.platform.startswith("win"),
                         "Windows Credential Manager")
    def test_windows_credential_roundtrip(self):
        name = "adcc-test-%d" % os.getpid()
        self.assertTrue(secret_set(name, "s3cret-value"))
        self.assertEqual(secret_get(name), "s3cret-value")
        resolved, unresolved = resolve_environment(
            {"TOKEN": "${secret:%s}" % name})
        self.assertEqual(resolved["TOKEN"], "s3cret-value")
        self.assertEqual(unresolved, [])
        # 清理
        import ctypes
        ctypes.windll.advapi32.CredDeleteW("adcc:" + name, 1, 0)

    def test_linux_adapter_degrades_secrets(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux only")
        with self.assertRaises(SecretUnavailable):
            secret_get("anything")


class LinuxAdapterTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_linux_adapter_is_selected(self):
        from adcc.platform import get_platform_adapter
        self.assertIsInstance(get_platform_adapter(), LinuxPlatformAdapter)


class RemoteReadonlyTests(unittest.TestCase):
    """CONSOLE_REMOTE_READONLY=1：写拒绝、读放行、Host 放宽。"""

    def _harness(self):
        from unittest import mock as m
        tmp = tempfile.TemporaryDirectory()
        data_dir = os.path.join(tmp.name, "data")
        os.makedirs(data_dir)
        m.patch.object(server, "RUNS_DB_PATH",
                       os.path.join(data_dir, "db.sqlite3")).start()
        for name, value in (("DATA_DIR", data_dir),
                            ("CONFIG_PATH",
                             os.path.join(data_dir, "config.json"))):
            m.patch.object(server, name, value).start()
        server.RUNS_DB = None
        cfg = server.Config(os.path.join(data_dir, "config.json"))
        httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, cfg, 0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return tmp, httpd, thread, port

    def test_readonly_rejects_writes_and_allows_reads(self):
        import http.client
        with mock.patch.object(server, "REMOTE_READONLY", True):
            tmp, httpd, thread, port = self._harness()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                conn.request("GET", "/api/health")
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                conn.request("POST", "/api/ui/theme", "{}",
                             {"Content-Type": "application/json",
                              "X-ADCC-Token": httpd.control_token})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertIn("只读", payload["error"])
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
