import unittest

from adcc.runtime import ports, processes


class PortNormalizationTests(unittest.TestCase):
    def test_validate_port_preserves_legacy_coercion_and_errors(self):
        self.assertEqual(ports.validate_port(None), (None, None))
        self.assertEqual(ports.validate_port(" 8791 "), (8791, None))
        self.assertEqual(ports.validate_port(65535), (65535, None))
        self.assertEqual(
            ports.validate_port(True), (None, "port 必须是 1-65535 的整数"))
        self.assertEqual(
            ports.validate_port(65536), (None, "port 必须在 1-65535 之间"))

    def test_lsof_listener_parser_preserves_bind_hosts_and_open_host_rules(self):
        output = """COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
node 101 user 1u IPv6 0x0 0t0 TCP [::1]:5173 (LISTEN)
node 101 user 2u IPv4 0x0 0t0 TCP 127.0.0.1:5173 (LISTEN)
node 202 user 3u IPv4 0x0 0t0 TCP *:3000 (LISTEN)
"""
        listeners = ports.parse_lsof_listeners(output)

        self.assertEqual(listeners[(101, 5173)], {"::1", "127.0.0.1"})
        self.assertEqual(listeners[(202, 3000)], {"*"})
        self.assertEqual(
            ports.listener_open_host({(101, 5173): {"::1"}}, 5173, {101}),
            "localhost")
        self.assertEqual(
            ports.listener_open_host(listeners, 5173, {101}), "127.0.0.1")
        self.assertEqual(
            ports.listener_open_host({(101, 5173)}, 5173, {101}), "127.0.0.1")


class ProcessNormalizationTests(unittest.TestCase):
    def test_etime_and_ps_snapshot_parsing_preserve_bsd_shape(self):
        self.assertEqual(processes.parse_etime("02:03"), 123)
        self.assertEqual(processes.parse_etime("01:02:03"), 3723)
        self.assertEqual(processes.parse_etime("2-01:02:03"), 176523)
        self.assertEqual(processes.parse_etime("bad"), 0)

        fixed = """  PID   UID ELAPSED  %CPU %MEM COMM
  101   501   02:03   0.2  1.5 /usr/local/bin/node
  202  nope 01:02:03  nope  2.0 /usr/bin/python3
"""
        args = """  PID ARGS
  101 node app.js --dev
  202 python3 worker.py
  999 not-present
"""
        snapshot = processes.parse_ps_snapshot(fixed, args)

        self.assertEqual(snapshot[101], {
            "args": "node app.js --dev", "uid": 501, "etime": 123,
            "cpu": 0.2, "mem": 1.5, "comm": "/usr/local/bin/node",
        })
        self.assertEqual(snapshot[202]["uid"], -1)
        self.assertEqual(snapshot[202]["etime"], 3723)
        self.assertEqual(snapshot[202]["cpu"], 0.0)
        self.assertEqual(snapshot[202]["args"], "python3 worker.py")

    def test_cwd_pgid_and_origin_text_parsers_ignore_malformed_rows(self):
        self.assertEqual(processes.parse_lsof_cwds(
            "p101\nn/tmp/project\npbad\nn/ignored\np202\nn/tmp/other\n"),
            {101: "/tmp/project", 202: "/tmp/other"})
        self.assertEqual(processes.parse_pgid_members(
            "101 100\n102 100\ninvalid\n103 nope\n"), {100: [101, 102]})
        self.assertEqual(processes.parse_origin_snapshot(
            "101 90 node server.js\n90 1 /usr/local/bin/codex\nbad 1 x\n"),
            {101: (90, "node server.js"), 90: (1, "/usr/local/bin/codex")})

    def test_process_presentation_policy_preserves_priority_and_origin_rules(self):
        self.assertEqual(
            processes.classify_group(
                "node:3000", "node", "/Applications/App.app/Contents/MacOS/x",
                "node dev", None, set()),
            "mine")
        self.assertEqual(
            processes.classify_group(
                "x:1", "x", "/System/Library/x", "", None, set()),
            "background")
        self.assertEqual(
            processes.classify_group("x:1", "x", "", "", None, {"x:1"}),
            "mine")
        self.assertEqual(processes.project_name("/tmp/demo/"), "demo")
        self.assertIsNone(processes.project_name("/"))
        self.assertIsNone(processes.project_name(processes.HOME_DIR))

        codex = processes.parse_origin_snapshot(
            "100 90 node server.mjs\n90 80 pnpm dev\n80 1 /usr/local/bin/codex\n")
        self.assertEqual(
            processes.attribute_origin(100, codex),
            {"label": "Codex", "icon": "bot"})
        console = {100: (90, "node app.js"),
                   90: (1, "/bin/bash console-run:token")}
        self.assertEqual(
            processes.attribute_origin(100, console),
            {"label": "总控台", "icon": "rocket"})
        cycle = {100: (90, "a"), 90: (100, "b")}
        self.assertEqual(
            processes.attribute_origin(100, cycle),
            {"label": "b", "icon": "package"})


if __name__ == "__main__":
    unittest.main()
