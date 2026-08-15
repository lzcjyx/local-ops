"""M4 run model / status policy / SQLite storage contract tests."""

import os
import tempfile
import unittest
from unittest import mock

from adcc.runtime.runs import (
    RUN_KINDS,
    RUN_STATUSES,
    finalize_run_status,
    make_run,
    public_run,
    validate_run_kind,
    validate_run_status,
)
from adcc.storage.database import RunDatabase


class RunStatusContractTests(unittest.TestCase):
    def test_canonical_status_enum_matches_spec(self):
        self.assertEqual(tuple(RUN_STATUSES), (
            "queued", "starting", "running", "succeeded", "failed",
            "canceled", "stopped", "timed_out", "lost"))

    def test_kinds_match_spec(self):
        self.assertEqual(tuple(RUN_KINDS),
                         ("service", "task", "agent", "workflow_step"))

    def test_finalize_mapping(self):
        self.assertEqual(finalize_run_status({}, 0), "succeeded")
        self.assertEqual(finalize_run_status({}, 130), "canceled")
        self.assertEqual(finalize_run_status({}, 1), "failed")
        self.assertEqual(finalize_run_status({}, 130, manual_stop=True),
                         "stopped")
        self.assertEqual(finalize_run_status({}, 0, manual_stop=True),
                         "stopped")

    def test_make_run_defaults(self):
        run = make_run(app_id="aaaaaaaa", project_id=None, kind="task")
        self.assertRegex(run["id"], r"^[0-9a-f]{8}$")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["kind"], "task")
        self.assertIsNone(run["ended_at"])
        self.assertIsNone(run["exit_code"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            validate_run_status("exploded")
        with self.assertRaises(ValueError):
            validate_run_kind("sidecar")

    def test_public_projection_shape_and_duration(self):
        run = make_run(app_id="aaaaaaaa", project_id="bbbbbbbb",
                       kind="service", pid=42, log_path="/tmp/x.log")
        projected = public_run(run)
        self.assertEqual(projected["appId"], "aaaaaaaa")
        self.assertEqual(projected["projectId"], "bbbbbbbb")
        self.assertEqual(projected["status"], "running")
        self.assertIsNone(projected["durationSec"])
        run["ended_at"] = run["started_at"] + 1250
        self.assertEqual(public_run(run)["durationSec"], 1250.0)


class RunDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "ops.sqlite3")
        self.db = RunDatabase(self.path)

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_crud_and_listing(self):
        run = make_run(app_id="aaaaaaaa", kind="task")
        self.db.insert_run(run)
        loaded = self.db.get_run(run["id"])
        self.assertEqual(loaded["id"], run["id"])
        self.assertEqual(loaded["status"], "running")
        rows = self.db.list_runs()
        self.assertEqual([row["id"] for row in rows], [run["id"]])
        rows = self.db.list_runs(status="running")
        self.assertEqual(len(rows), 1)
        rows = self.db.list_runs(status="succeeded")
        self.assertEqual(rows, [])

    def test_update_is_limited_to_allowed_fields(self):
        run = make_run(app_id="aaaaaaaa", kind="task")
        self.db.insert_run(run)
        self.db.update_run(run["id"], {
            "status": "succeeded", "exit_code": 0,
            "ended_at": 1700000000, "bogus": "ignored"})
        loaded = self.db.get_run(run["id"])
        self.assertEqual(loaded["status"], "succeeded")
        self.assertEqual(loaded["exit_code"], 0)
        self.assertNotIn("bogus", loaded)

    def test_running_runs_and_latest_per_app(self):
        a = make_run(app_id="aaaaaaaa", kind="service")
        b = make_run(app_id="bbbbbbbb", kind="task")
        self.db.insert_run(a)
        self.db.insert_run(b)
        self.assertEqual(len(self.db.running_runs()), 2)
        self.db.update_run(a["id"], {"status": "failed", "exit_code": 1,
                                     "ended_at": 1700000001})
        self.assertEqual(len(self.db.running_runs()), 1)
        latest = self.db.latest_run_for_app("aaaaaaaa")
        self.assertEqual(latest["id"], a["id"])
        self.assertEqual(latest["status"], "failed")

    def test_reopen_preserves_data_and_migration_is_idempotent(self):
        run = make_run(app_id="aaaaaaaa", kind="service")
        self.db.insert_run(run)
        self.db.close()
        reopened = RunDatabase(self.path)
        try:
            self.assertEqual(reopened.get_run(run["id"])["id"], run["id"])
        finally:
            reopened.close()

    def test_corrupt_database_does_not_break_server_lifecycle(self):
        """M11：损坏的 SQLite 文件导致打开失败时，daemon 必须降级而非崩溃。"""
        import server as server_module
        corrupt = os.path.join(self.directory.name, "corrupt.sqlite3")
        with open(corrupt, "wb") as handle:
            handle.write(b"this is not a sqlite database at all" * 8)
        with mock.patch.object(server_module, "RUNS_DB_PATH", corrupt), \
                mock.patch.object(server_module, "RUNS_DB", None):
            db = server_module.get_runs_db()
            self.assertIsNone(db)  # 降级：返回 None，不影响其他功能


if __name__ == "__main__":
    unittest.main()
