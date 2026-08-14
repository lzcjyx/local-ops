"""M3 project domain tests: models, registry, migration, detection."""

import json
import os
import tempfile
import unittest

from adcc.core.constants import APP_DEFAULT, CONFIG_DEFAULT, CURRENT_SCHEMA_VERSION
from adcc.projects import (
    UNASSIGNED_PROJECT_ID,
    UNASSIGNED_PROJECT_NAME,
    assign_resources_from_apps,
    create_project,
    create_resource,
    delete_project,
    delete_resource,
    ensure_default_workspace,
    ensure_unassigned_project,
    get_project,
    get_resource,
    list_projects,
    list_resources,
    make_project,
    make_resource,
    project_summary,
    update_project,
    update_resource,
)
from adcc.projects.detection import detect_mcp_servers, git_root
from adcc.projects.models import RESOURCE_KINDS
from adcc.storage.config import Config, migrate_config_v1_to_v2


def fresh_config_data():
    return json.loads(json.dumps(CONFIG_DEFAULT))


class ModelValidationTests(unittest.TestCase):
    def test_ids_are_8_hex(self):
        project = make_project("博客", "/tmp/blog")
        self.assertRegex(project["id"], r"^[0-9a-f]{8}$")

    def test_make_resource_validation(self):
        with self.assertRaises(ValueError):
            make_resource("x", "service", "")
        with self.assertRaises(ValueError):
            make_resource("x", "weird-kind", "echo 1")
        with self.assertRaises(ValueError):
            make_resource("x", "task", "echo 1", port=8080)
        resource = make_resource("mcp", "mcp_server", "npx foo", port=None)
        self.assertEqual(resource["kind"], "mcp_server")

    def test_port_validation(self):
        with self.assertRaises(ValueError):
            make_resource("x", "service", "echo 1", port=70000)
        with self.assertRaises(ValueError):
            make_resource("x", "service", "echo 1", port=True)

    def test_project_requires_root(self):
        with self.assertRaises(ValueError):
            make_project("x", None)

    def test_kind_constants(self):
        self.assertEqual(set(RESOURCE_KINDS), {"service", "task", "mcp_server"})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.data = fresh_config_data()

    def test_default_workspace_is_idempotent(self):
        first = ensure_default_workspace(self.data)
        second = ensure_default_workspace(self.data)
        self.assertIs(first, second)
        self.assertEqual(first["name"], "默认工作区")

    def test_unassigned_bucket_is_created_on_demand(self):
        project = ensure_unassigned_project(self.data)
        self.assertEqual(project["id"], UNASSIGNED_PROJECT_ID)
        self.assertEqual(project["name"], UNASSIGNED_PROJECT_NAME)

    def test_project_crud(self):
        project = create_project(self.data, "博客", r"C:\dev\blog")
        self.assertIn(project, self.data["projects"])
        updated = update_project(self.data, project["id"],
                                 {"name": "博客2", "tags": ["web"]})
        self.assertEqual(updated["name"], "博客2")
        self.assertTrue(delete_project(self.data, project["id"]))
        self.assertIsNone(get_project(self.data, project["id"]))

    def test_resource_crud_and_scope(self):
        project = create_project(self.data, "博客", r"C:\dev\blog")
        resource = create_resource(
            self.data, project["id"], "dev", "service",
            "python -m http.server 8080", port=8080)
        self.assertEqual(list_resources(self.data, project["id"]), [resource])
        updated = update_resource(self.data, resource["id"], {"port": 8081})
        self.assertEqual(updated["port"], 8081)
        self.assertTrue(delete_resource(self.data, resource["id"]))
        self.assertIsNone(get_resource(self.data, resource["id"]))

    def test_delete_project_moves_resources_to_unassigned(self):
        project = create_project(self.data, "博客", r"C:\dev\blog")
        resource = create_resource(
            self.data, project["id"], "dev", "service", "echo 1")
        delete_project(self.data, project["id"])
        moved = get_resource(self.data, resource["id"])
        self.assertEqual(moved["project_id"], UNASSIGNED_PROJECT_ID)

    def test_duplicate_port_across_projects_is_allowed(self):
        a = create_project(self.data, "A", r"C:\dev\a")
        b = create_project(self.data, "B", r"C:\dev\b")
        create_resource(self.data, a["id"], "dev", "service",
                        "cmd", port=3000)
        create_resource(self.data, b["id"], "dev", "service",
                        "cmd", port=3000)
        self.assertEqual(
            len(list_resources(self.data, a["id"]))
            + len(list_resources(self.data, b["id"])), 2)

    def test_task_resource_forces_null_port(self):
        project = create_project(self.data, "A", r"C:\dev\a")
        with self.assertRaises(ValueError):
            create_resource(self.data, project["id"], "build", "task",
                            "make", port=8000)


class MigrationTests(unittest.TestCase):
    def test_v1_to_v2_adds_skeleton(self):
        v1 = {key: value for key, value in CONFIG_DEFAULT.items() if key != "schemaVersion"}
        v1["schemaVersion"] = 1
        migrated = migrate_config_v1_to_v2(v1)
        self.assertEqual(migrated["schemaVersion"], 2)
        self.assertEqual(migrated["projects"], [])
        self.assertEqual(migrated["resources"], [])
        self.assertIn("apps", migrated)

    def test_assign_resources_groups_by_cwd_and_keeps_apps(self):
        data = json.loads(json.dumps(CONFIG_DEFAULT))
        data["apps"] = [
            {**APP_DEFAULT, "id": "aaaaaaaa", "name": "blog-dev",
             "command": "vite", "cwd": r"C:\dev\blog", "port": 5173},
            {**APP_DEFAULT, "id": "bbbbbbbb", "name": "blog-api",
             "command": "uvicorn app:app", "cwd": r"C:\dev\blog",
             "port": 8000, "kind": "service"},
            {**APP_DEFAULT, "id": "cccccccc", "name": "orphan-task",
             "command": "make build", "cwd": None, "kind": "task"},
        ]
        self.assertTrue(assign_resources_from_apps(data))
        self.assertFalse(assign_resources_from_apps(data))  # 幂等
        resources = data["resources"]
        self.assertEqual(len(resources), 3)
        blog = [r for r in resources if r["project_id"] != UNASSIGNED_PROJECT_ID]
        unassigned = [r for r in resources if r["project_id"] == UNASSIGNED_PROJECT_ID]
        self.assertEqual(len(blog), 2)
        self.assertEqual(len(unassigned), 1)
        self.assertEqual(unassigned[0]["name"], "orphan-task")
        self.assertEqual(unassigned[0]["kind"], "task")
        self.assertEqual({r["port"] for r in blog}, {5173, 8000})
        self.assertEqual(len(data["apps"]), 3)  # legacy 保留

    def test_migration_via_config_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            v1 = {"schemaVersion": 1, "apps": [], "hidden": [],
                  "pinned": [], "promoted": [], "watchedKeywords": [],
                  "uiTheme": "ops"}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(v1, handle)
            config = Config(path)
            snapshot = config.snapshot()
            self.assertEqual(snapshot["schemaVersion"], 2)
            self.assertIn("projects", snapshot)


class SummaryTests(unittest.TestCase):
    def test_project_summary_counts_resources_and_running(self):
        project = make_project("博客", r"C:\dev\blog")
        resources = [
            make_resource("a", "service", "cmd1", project_id=project["id"]),
            make_resource("b", "task", "cmd2", project_id=project["id"]),
            make_resource("c", "service", "cmd3", project_id="eeeeeeee"),
        ]
        summary = project_summary(project, resources, {"b_id"})
        self.assertEqual(summary["resourceCount"], 2)
        self.assertEqual(summary["runningCount"], 0)
        summary2 = project_summary(project, resources, {resources[1]["id"]})
        self.assertEqual(summary2["runningCount"], 1)


class DetectionTests(unittest.TestCase):
    def test_git_root_detects_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = os.path.join(directory, "repo")
            os.makedirs(os.path.join(repo, "sub"))
            result = subprocess_run(["git", "init", "-q", repo])
            self.assertEqual(result, 0)
            self.assertEqual(git_root(repo), os.path.realpath(repo))
            self.assertEqual(
                git_root(os.path.join(repo, "sub")), os.path.realpath(repo))

    def test_git_root_degrades_gracefully(self):
        self.assertIsNone(git_root(os.path.join(tempfile.gettempdir(), "no-such-dir-xyz")))
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(git_root(directory))

    def test_detect_mcp_servers_from_mcp_json(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, ".mcp.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"mcpServers": {
                    "unity": {"command": "npx", "args": ["-y", "unity-mcp"]},
                    "bad": {"command": ""},
                }}, handle)
            candidates = detect_mcp_servers(directory)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["kind"], "mcp_server")
            self.assertIn("unity-mcp", candidates[0]["command"])
            self.assertEqual(candidates[0]["source"], ".mcp.json")

    def test_detect_mcp_servers_from_package_json(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "package.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"mcp": {"servers": {
                    "blender": {"command": "blender-mcp", "args": []},
                }}}, handle)
            candidates = detect_mcp_servers(directory)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["label"], "MCP 服务器：blender")


def subprocess_run(args):
    import subprocess
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=30).returncode


if __name__ == "__main__":
    unittest.main()
