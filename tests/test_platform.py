"""M2 PlatformAdapter contract tests (portable + real Windows smoke)."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

import server as server_module

from adcc.platform import (
    PlatformCapabilityError,
    PlatformUnsupportedError,
    get_platform_adapter,
)
from adcc.platform.unsupported import UnsupportedPlatformAdapter
from adcc.runtime.ports import parse_netstat_listeners

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"


class NetstatParserTests(unittest.TestCase):
    def test_parses_listening_rows(self):
        output = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       12345\n"
            "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       23456\n"
            "  TCP    [::]:8080              [::]:0                 LISTENING       34567\n"
            "  TCP    [::1]:8765             [::]:0                 LISTENING       45678\n"
            "  UDP    0.0.0.0:5353           *:*                                    1234\n"
        )
        found = parse_netstat_listeners(output)
        self.assertEqual(found[(12345, 8080)], {"*"})
        self.assertEqual(found[(23456, 8765)], {"127.0.0.1"})
        self.assertEqual(found[(34567, 8080)], {"::"})
        self.assertEqual(found[(45678, 8765)], {"::1"})
        self.assertNotIn((1234, 5353), found)

    def test_rejects_non_listening_rows(self):
        output = (
            "  TCP    127.0.0.1:9999         127.0.0.1:80           ESTABLISHED    7777\n"
            "  TCP    0.0.0.0:2222           0.0.0.0:0              LISTENING       5555\n"
        )
        found = parse_netstat_listeners(output)
        self.assertEqual(found, {(5555, 2222): {"*"}})

    def test_rejects_malformed_rows(self):
        output = (
            "  TCP    not-a-port             0.0.0.0:0              LISTENING       1234\n"
            "  TCP    0.0.0.0:80             0.0.0.0:0              LISTENING       NAN\n"
        )
        self.assertEqual(parse_netstat_listeners(output), {})


class AdapterSelectionTests(unittest.TestCase):
    def test_unsupported_platform_raises_typed_errors(self):
        adapter = UnsupportedPlatformAdapter("linux")
        with self.assertRaises(PlatformUnsupportedError):
            adapter.listeners()
        with self.assertRaises(PlatformUnsupportedError):
            adapter.process_snapshot()

    def test_active_adapter_matches_platform(self):
        adapter = get_platform_adapter()
        if IS_WINDOWS:
            self.assertEqual(adapter.name, "windows")
        elif IS_MACOS:
            self.assertEqual(adapter.name, "macos")

    def test_run_cmd_never_raises(self):
        from adcc.platform import run_cmd
        self.assertEqual(run_cmd(["definitely-not-a-command-xyz"], timeout=2), "")


class WindowsCimParsingTests(unittest.TestCase):
    """Pure parsing of the CIM JSON payload without a live PowerShell."""

    def _make_entry(self, pid, session, created="2026-08-14T08:00:00",
                    name="python.exe", args="python app.py", wss=104857600):
        return {"pid": pid, "ppid": 1, "name": name, "exe": None,
                "args": args, "created": created, "wss": wss,
                "session": session}

    def _adapter_with_session(self, session_id):
        from adcc.platform.windows import WindowsPlatformAdapter
        adapter = WindowsPlatformAdapter()
        adapter._session_id = session_id
        return adapter

    def test_owner_resolution_uses_session(self):
        adapter = self._adapter_with_session(1)
        mine = adapter._owner_for(self._make_entry(100, 1))
        other = adapter._owner_for(self._make_entry(200, 0))
        unknown = adapter._owner_for(self._make_entry(300, None))
        self.assertEqual(mine, adapter.current_user_id())
        self.assertIsNone(other)
        self.assertIsNone(unknown)

    def test_entry_to_snapshot_shapes(self):
        adapter = self._adapter_with_session(1)
        entry = self._make_entry(100, 1, wss=209715200)  # 200 MiB
        snap = adapter._entry_to_snapshot(entry, total_kb=8 * 1024 * 1024)
        self.assertEqual(snap["comm"], "python.exe")
        self.assertEqual(snap["args"], "python app.py")
        self.assertEqual(snap["cpu"], 0.0)
        self.assertGreater(snap["mem"], 0.0)
        self.assertIsInstance(snap["etime"], int)
        self.assertEqual(snap["uid"], adapter.current_user_id())

    def test_snapshot_skips_meta_and_zeros(self):
        adapter = self._adapter_with_session(1)
        payload = {
            "total_mb": 8192,
            "processes": [
                {"pid": 0, "ppid": 0, "name": "System Idle",
                 "exe": None, "args": "", "created": None, "wss": 0,
                 "session": 0},
                {"pid": 100, "ppid": 1, "name": "python.exe", "exe": None,
                 "args": "x", "created": None, "wss": 0, "session": 1},
            ],
        }
        adapter._cim_query = lambda pids=None: payload  # type: ignore[assignment]
        snap = adapter.process_snapshot()
        self.assertEqual(list(snap), [100])


@unittest.skipUnless(IS_WINDOWS, "Windows runtime smoke")
class WindowsRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = get_platform_adapter()

    def test_listeners_shape(self):
        listeners = self.adapter.listeners()
        self.assertIsInstance(listeners, dict)
        for (pid, port), binds in listeners.items():
            self.assertIsInstance(pid, int)
            self.assertIsInstance(port, int)
            self.assertIsInstance(binds, set)

    def test_process_snapshot_current_user(self):
        snap = self.adapter.process_snapshot([os.getpid()])
        self.assertIn(os.getpid(), snap)
        self.assertEqual(snap[os.getpid()]["uid"], self.adapter.current_user_id())

    def test_origin_snapshot(self):
        table = self.adapter.origin_snapshot()
        self.assertGreater(len(table), 0)
        self.assertIn(os.getpid(), table)

    def test_pid_alive_semantics(self):
        self.assertTrue(self.adapter.pid_alive(os.getpid()))
        self.assertFalse(self.adapter.pid_alive(2 ** 31 - 1))

    def test_process_cwds_degrades_to_unknown(self):
        self.assertEqual(self.adapter.process_cwds([os.getpid()]), {})

    def test_process_tree(self):
        tree = self.adapter.process_tree_of(os.getpid())
        self.assertIsInstance(tree, list)
        self.assertNotIn(os.getpid(), tree)

    def test_lifecycle_start_identity_stop(self):
        """M2 exit-gate smoke: start -> discover -> recognize -> stop."""
        td = tempfile.mkdtemp()
        port = 8923
        proc, group_id = self.adapter.start_process(
            td, self.adapter.launch_env("smoketoken"), None,
            "python -m http.server %d" % port, "smoketoken")
        try:
            time.sleep(2.5)
            self.assertTrue(self.adapter.pid_alive(proc.pid))
            listeners = self.adapter.listeners()
            listening = any(
                p == proc.pid or p in self.adapter.process_tree_of(proc.pid)
                for (p, _port) in listeners if _port == port)
            self.assertTrue(listening, "managed service port not discovered")
        finally:
            ok, error = self.adapter.terminate_tree(proc.pid, force=False)
            self.assertTrue(ok, error)
            deadline = time.monotonic() + 8.0
            while self.adapter.pid_alive(proc.pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(self.adapter.pid_alive(proc.pid))


    def test_external_process_on_configured_port_is_never_killed_or_claimed(self):
        """M2 exit-gate: unrelated listener on the configured port must
        not be claimed nor killed by stop."""
        td = tempfile.mkdtemp()
        port = 8945
        external = subprocess.Popen(
            ["python", "-m", "http.server", str(port)],
            creationflags=0x08000000)
        try:
            time.sleep(2.0)
            self.assertIsNone(external.poll())
            s = server_module
            app = {**s.Config.APP_DEFAULT, "id": "eeffffff",
                   "name": "intruder-app",
                   "command": "python -m http.server %d" % port,
                   "cwd": td, "port": port, "kind": "service"}
            cfg = s.Config(os.path.join(td, "config.json"))
            cfg.update(lambda d: d["apps"].append(app))
            state = s.get_state_snapshot(cfg, 9600)
            row = next(a for a in state["apps"] if a["id"] == "eeffffff")
            self.assertFalse(row["running"])
            self.assertTrue(row.get("portOccupied"))
            ok, error = s.stop_app_and_clear(cfg, cfg.snapshot()["apps"][0])
            self.assertFalse(ok)
            time.sleep(0.3)
            self.assertIsNone(external.poll(), "external process was killed")
        finally:
            self.adapter.terminate_tree(external.pid, force=True)


if __name__ == "__main__":
    unittest.main()
