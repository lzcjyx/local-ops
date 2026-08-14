import unittest

from adcc.runtime import lifecycle, tasks


class TaskExitCompatibilityTests(unittest.TestCase):
    def test_exit_codes_preserve_existing_task_semantics(self):
        self.assertEqual(tasks.classify_task_exit(0), "succeeded")
        self.assertEqual(tasks.classify_task_exit(130), "canceled")
        self.assertEqual(tasks.classify_task_exit(1), "failed")
        self.assertEqual(tasks.classify_task_exit(-15), "failed")

    def test_public_last_exit_normalizes_a_copy_only_for_tasks(self):
        stored = {"code": 0, "at": 123}
        public = tasks.public_last_exit({"kind": "task", "lastExit": stored})

        self.assertEqual(public["status"], "succeeded")
        self.assertNotIn("status", stored)
        self.assertEqual(
            tasks.public_last_exit({
                "kind": "task",
                "lastExit": {"status": "canceled", "code": None},
            })["status"],
            "stopped",
        )


class ManagedIdentityPolicyTests(unittest.TestCase):
    def test_token_pgid_and_current_uid_are_all_required(self):
        app = {"id": "app", "lastPid": 42, "lastPgid": 42,
               "runToken": "right"}
        groups = {42: [42, 43, 44]}
        facts = {
            42: {"uid": 501, "args": "bash console-run:right"},
            43: {"uid": 501, "args": "python service.py"},
            44: {"uid": 502, "args": "python foreign.py"},
        }

        self.assertEqual(
            lifecycle.managed_process_index(
                [app], groups, facts, current_uid=501),
            {"app": [42, 43]},
        )
        self.assertEqual(
            lifecycle.managed_process_index(
                [dict(app, runToken="wrong")], groups, facts, current_uid=501),
            {"app": []},
        )
        self.assertEqual(
            lifecycle.managed_process_index(
                [app], groups,
                {42: {"uid": 502, "args": "bash console-run:right"}},
                current_uid=501),
            {"app": []},
        )

    def test_legacy_identity_requires_pid_port_uid_and_cwd(self):
        app = {"id": "legacy", "lastPid": 99, "runToken": None,
               "port": 3000, "cwd": "/project"}
        listeners = {(99, 3000)}
        facts = {99: {"uid": 501, "args": "python app.py"}}

        self.assertEqual(
            lifecycle.legacy_managed_pid(
                app, listeners, facts, {99: "/project"}, current_uid=501),
            99,
        )
        self.assertIsNone(lifecycle.legacy_managed_pid(
            app, {(99, 3001)}, facts, {99: "/project"}, current_uid=501))
        self.assertIsNone(lifecycle.legacy_managed_pid(
            app, listeners, {99: {"uid": 502}}, {99: "/project"},
            current_uid=501))
        self.assertIsNone(lifecycle.legacy_managed_pid(
            app, listeners, facts, {99: "/other"}, current_uid=501))

    def test_attached_card_can_follow_one_unique_same_cwd_listener(self):
        app = {"id": "attached", "lastPid": 4242, "runToken": None,
               "port": 3000, "cwd": "/project", "attached": True}

        pid = lifecycle.legacy_managed_pid(
            app,
            {(5252, 3000), (6262, 3000)},
            {5252: {"uid": 501}, 6262: {"uid": 501}},
            {5252: "/project", 6262: "/other"},
            current_uid=501,
        )

        self.assertEqual(pid, 5252)
        self.assertIsNone(lifecycle.legacy_managed_pid(
            app,
            {(5252, 3000), (6262, 3000)},
            {5252: {"uid": 501}, 6262: {"uid": 501}},
            {5252: "/project", 6262: "/project"},
            current_uid=501,
        ))

    def test_listener_owner_omits_pid_when_multiple_cards_verify_it(self):
        apps = [
            {"id": "a", "lastPid": 42, "lastPgid": 42,
             "runToken": "shared"},
            {"id": "b", "lastPid": 42, "lastPgid": 42,
             "runToken": "shared"},
        ]
        owners = lifecycle.listener_app_owners(
            apps,
            {(42, 3000)},
            {42: {"uid": 501, "args": "bash console-run:shared"}},
            {42: "/project"},
            {42: [42]},
            current_uid=501,
        )

        self.assertEqual(owners, {})

    def test_listener_owner_uses_supplied_managed_index_without_recomputing(self):
        app = {"id": "a", "lastPid": 1, "lastPgid": 1,
               "runToken": "token"}
        # The listener-only facts omit the token-bearing group controller.
        # A caller that already inspected the full group can still safely
        # project PID 2 as owned by this card.
        owners = lifecycle.listener_app_owners(
            [app],
            {(2, 3000)},
            {2: {"uid": 501, "args": "node app.js"}},
            {2: "/project"},
            current_uid=501,
            managed_by_app={"a": [2]},
        )

        self.assertEqual(owners, {2: app})


if __name__ == "__main__":
    unittest.main()
