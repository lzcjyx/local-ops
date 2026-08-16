"""P1 tests: Linux adapter wiring, project templates/manifests, discovery,
secrets resolution, remote read-only mode."""

import json
import os
import subprocess
import time
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
        os.makedirs(os.path.join(data_dir, "icons"))
        os.makedirs(os.path.join(data_dir, "logs"))
        mock.patch.object(server, "RUNS_DB_PATH",
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


def shutil_which_available():
    import shutil
    return shutil.which("git") is not None


class WorktreeAssignmentTests(unittest.TestCase):
    """P1：适配器模板含 {worktree_path} 时自动创建 ADCC worktree。"""

    def _harness_with_repo(self):
        import http.client
        tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(tmp.name, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True,
                       capture_output=True)
        with open(os.path.join(repo, "a.txt"), "w") as handle:
            handle.write("x")
        subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "init"],
                       check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})
        data_dir = os.path.join(tmp.name, "data")
        os.makedirs(os.path.join(data_dir, "icons"))
        os.makedirs(os.path.join(data_dir, "logs"))
        mock.patch.object(server, "RUNS_DB_PATH",
                          os.path.join(data_dir, "db.sqlite3")).start()
        for name, value in (("DATA_DIR", data_dir),
                            ("ICONS_DIR", os.path.join(data_dir, "icons")),
                            ("LOGS_DIR", os.path.join(data_dir, "logs")),
                            ("CONFIG_PATH",
                             os.path.join(data_dir, "config.json"))):
            mock.patch.object(server, name, value).start()
        server.RUNS_DB = None
        server.AGENT_RUNNER = None
        cfg = server.Config(os.path.join(data_dir, "config.json"))
        server.ensure_project_domain(cfg)
        from adcc.projects import create_project
        cfg.update(lambda d: create_project(d, "Git 项目", repo))
        httpd = server.ConsoleServer(
            (server.HOST, 0), server.Handler, cfg, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        runner = server.get_agent_runner(cfg)
        project_id = cfg.snapshot()["projects"][0]["id"]
        return tmp, httpd, runner, project_id, repo

    @unittest.skipUnless(shutil_which_available(), "需要 git")
    def test_worktree_is_created_and_injected(self):
        tmp, httpd, runner, project_id, repo = self._harness_with_repo()
        try:
            from adcc.agents.models import make_adapter
            from adcc.git.repository import list_worktrees
            adapter = make_adapter(
                name="wt-agent", executable=sys.executable,
                args_template=["-c", "import sys; print('x')"],
                cwd_template="{worktree_path}")
            runner.add_adapter(adapter)
            session, error = runner.start(
                adapter["id"], project_id, prompt="带 worktree")
            self.assertIsNone(error, error)
            branches = [w["branch"] for w in list_worktrees(repo)]
            self.assertTrue(any(b and b.startswith("adcc/")
                                for b in branches), branches)
            deadline = time.time() + 8
            current = None
            while time.time() < deadline:
                current = runner.get_session(session["id"])
                if current["status"] != "running":
                    break
                time.sleep(0.3)
            self.assertIn(current["status"],
                          ("succeeded", "failed", "lost"))
        finally:
            db = server.get_runs_db()
            if db is not None:
                db.close()
                server.RUNS_DB = None
            server.AGENT_RUNNER = None
            httpd.shutdown()
            httpd.server_close()
            time.sleep(0.8)
            tmp.cleanup()

    @unittest.skipUnless(shutil_which_available(), "需要 git")
    def test_worktree_required_but_not_a_repo_fails_clearly(self):
        tmp = tempfile.TemporaryDirectory()
        data_dir = os.path.join(tmp.name, "data")
        os.makedirs(data_dir)
        mock.patch.object(server, "RUNS_DB_PATH",
                          os.path.join(data_dir, "db.sqlite3")).start()
        for name, value in (("DATA_DIR", data_dir),
                            ("ICONS_DIR", os.path.join(data_dir, "icons")),
                            ("LOGS_DIR", os.path.join(data_dir, "logs")),
                            ("CONFIG_PATH",
                             os.path.join(data_dir, "config.json"))):
            mock.patch.object(server, name, value).start()
        server.RUNS_DB = None
        server.AGENT_RUNNER = None
        cfg = server.Config(os.path.join(data_dir, "config.json"))
        plain_dir = os.path.join(tmp.name, "plain")
        os.makedirs(plain_dir)
        from adcc.projects import create_project
        cfg.update(lambda d: create_project(d, "普通目录", plain_dir))
        runner = server.get_agent_runner(cfg)
        try:
            from adcc.agents.models import make_adapter
            adapter = make_adapter(
                name="wt-need", executable=sys.executable,
                args_template=["-c", "print(1)"],
                cwd_template="{worktree_path}")
            runner.add_adapter(adapter)
            session, error = runner.start(
                adapter["id"], cfg.snapshot()["projects"][0]["id"],
                prompt="x")
            self.assertIsNotNone(error)
            self.assertIn("worktree", error)
            self.assertEqual(session["status"], "failed")
        finally:
            db = server.get_runs_db()
            if db is not None:
                db.close()
                server.RUNS_DB = None
            server.AGENT_RUNNER = None
            tmp.cleanup()


def shutil_which_available():
    import shutil
    return shutil.which("git") is not None


if __name__ == "__main__":
    unittest.main()
