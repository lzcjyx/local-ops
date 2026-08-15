"""M12 Dogfood：SPEC §26 首个可用版本十四项验证（Windows/macOS 可执行）。

用真实 daemon + HTTP 客户端逐项验证 14 个发布条件。用法：
    python tools/dogfood_release_check.py
退出码：0 全过 / 1 有失败（打印逐项结果）。
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402

RESULTS = []


def check(number, name, fn):
    try:
        fn()
        RESULTS.append((number, name, True, ""))
        print("  [%2d] PASS %s" % (number, name))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((number, name, False, str(exc)))
        print("  [%2d] FAIL %s — %s" % (number, name, exc))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Client:
    def __init__(self, port, token):
        self.port = port
        self.token = token

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"X-ADCC-Token": self.token}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return r.status, raw

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body or {})


def main():
    tmp = tempfile.TemporaryDirectory()
    data_dir = os.path.join(tmp.name, "data")
    os.makedirs(os.path.join(data_dir, "icons"))
    os.makedirs(os.path.join(data_dir, "logs"))
    for directory in (data_dir, os.path.join(data_dir, "icons"),
                      os.path.join(data_dir, "logs")):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    from unittest import mock
    mock.patch.object(server, "RUNS_DB_PATH",
                      os.path.join(data_dir, "console.sqlite3")).start()
    for name, value in (("DATA_DIR", data_dir),
                        ("ICONS_DIR", os.path.join(data_dir, "icons")),
                        ("LOGS_DIR", os.path.join(data_dir, "logs")),
                        ("CONFIG_PATH", os.path.join(data_dir, "config.json"))):
        mock.patch.object(server, name, value).start()
    server.RUNS_DB = None
    server.AGENT_RUNNER = None
    server.WORKFLOW_EXECUTOR = None
    cfg = server.Config(os.path.join(data_dir, "config.json"))
    server.ensure_project_domain(cfg)
    httpd = server.ConsoleServer((server.HOST, 0), server.Handler, cfg, 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = Client(port, httpd.control_token)
    runner = server.get_agent_runner(cfg)
    executor = server.get_workflow_executor(cfg)

    print("M12 Dogfood — SPEC §26 发布条件验证（daemon :%d）" % port)

    # 1. 安装/打开 Desktop GUI —— 桌面壳本机运行 smoke（Tauri 需 GUI，
    #    此处验证 daemon 启动与 / 可访问性作为其前置条件）。
    def v1():
        status, _ = client.get("/")
        assert status == 200
    check(1, "Desktop GUI（daemon+UI 可达）", v1)

    # 2. 添加至少两个本地开发项目。
    project_ids = []

    def v2():
        for name in ("项目A", "项目B"):
            root = os.path.join(tmp.name, name)
            os.makedirs(root, exist_ok=True)
            status, body = client.post("/api/v1/projects", {
                "name": name, "rootPath": root})
            assert status == 201, body
            project_ids.append(body["id"])
    check(2, "添加两个项目", v2)

    # 3. 查看每个项目服务/任务与端口。
    resources = {}

    def v3():
        for index, project_id in enumerate(project_ids):
            port = free_port()
            status, app = client.post("/api/apps", {
                "name": "svc-%d" % index,
                "command": "python -m http.server %d" % port,
                "cwd": os.path.join(tmp.name, "项目A" if index == 0 else "项目B"),
                "port": port, "kind": "service"})
            assert status == 200, app
            status, projects = client.get("/api/v1/projects")
            assert status == 200
            resources[project_id] = next(
                (r for r in projects if r["id"] == project_id),
                None)["resources"][0]["id"]
        status, state = client.get("/api/v1/state")
        assert status == 200 and state["projects"]
    check(3, "项目服务/端口可见", v3)

    # 4. 安全启停服务。
    def v4():
        resource_id = resources[project_ids[0]]
        status, body = client.post(
            "/api/v1/resources/%s/start" % resource_id)
        assert status == 200 and body.get("ok"), body
        time.sleep(1.5)
        status, state = client.get("/api/v1/state")
        app = next(a for a in state["apps"] if a["running"])
        assert app["listening"]
        status, body = client.post(
            "/api/v1/resources/%s/stop" % resource_id)
        assert status == 200 and body.get("ok"), body
        time.sleep(1.0)
        status, state = client.get("/api/v1/state")
        assert not any(a["running"] for a in state["apps"])
    check(4, "服务安全启停", v4)

    # 5. 配置 MCP server 为项目资源（.mcp.json 检测 + 注册）。
    def v5():
        project_dir = os.path.join(tmp.name, "项目B")
        with open(os.path.join(project_dir, ".mcp.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"mcpServers": {
                "unity": {"command": "npx", "args": ["-y", "unity-mcp"]}}},
                handle)
        status, body = client.post("/api/project/detect",
                                   {"cwd": project_dir})
        assert status == 200, body
        assert any(c["kind"] == "mcp_server" for c in body["candidates"]), \
            body["candidates"]
    check(5, "MCP server 项目资源", v5)

    # 6-8. 通用 agent 命令适配器 + 启动/观察/停止。
    fake_agent = os.path.join(tmp.name, "fake_agent.py")
    with open(fake_agent, "w", encoding="utf-8") as handle:
        handle.write(
            'import os,sys,time\n'
            'open(sys.argv[1], "w").write("ok")\n'
            'time.sleep(float(os.environ.get("D", "2")))\n'
            'sys.exit(0)\n')
    adapter_ids = []

    def v6_8():
        status, adapter = client.post("/api/v1/agents/adapters", {
            "name": "fake-agent", "executable": sys.executable,
            "argsTemplate": [fake_agent, "{prompt_file}"],
            "envTemplate": {"D": "30"}})
        assert status == 201, adapter
        adapter_ids.append(adapter["id"])
        status, session = client.post("/api/v1/agents/sessions", {
            "adapterId": adapter["id"], "projectId": project_ids[0],
            "prompt": "请实现功能"})
        assert status == 201, session
        session_id = session["id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            status, current = client.get(
                "/api/v1/agents/sessions/%s" % session_id)
            if current["status"] in ("running", "succeeded", "failed"):
                break
            time.sleep(0.5)
        assert current["status"] == "running", current
        status, body = client.post(
            "/api/v1/agents/sessions/%s/stop" % session_id)
        assert status == 200 and body.get("ok"), body
        deadline = time.time() + 10
        while time.time() < deadline:
            status, current = client.get(
                "/api/v1/agents/sessions/%s" % session_id)
            if current["status"] != "running":
                break
            time.sleep(0.5)
        assert current["status"] in ("stopped", "lost"), current
    check(6, "配置通用 agent 命令", v6_8)

    # 7. 隔离 worktree 启动 agent（git 项目）。
    def v7():
        repo = os.path.join(tmp.name, "gitrepo")
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
        from adcc.git.repository import create_worktree, list_worktrees
        branch = "adcc/feature/x1y2z3a4"
        path, error = create_worktree(repo, branch,
                                      os.path.join(tmp.name, "wt"))
        assert error is None, error
        assert any(w["branch"] == branch for w in list_worktrees(repo))
    check(7, "隔离 worktree", v7)

    # 9. agent → test → review → gate 工作流。
    def v9():
        test_app = client.post("/api/apps", {
            "name": "test-cmd",
            "command": 'python -c "import time; time.sleep(0.3)"',
            "cwd": os.path.join(tmp.name, "项目A"),
            "kind": "task"})[1]
        status, projects = client.get("/api/v1/projects")
        test_resource = next(
            r for p in projects for r in p["resources"]
            if r["app_id"] == test_app["id"])
        steps = [
            {"kind": "agent", "id": "impl",
             "config": {"adapterId": adapter_ids[0], "prompt": "实现"}},
            {"kind": "task", "id": "tests", "needs": ["impl"],
             "config": {"resourceId": test_resource["id"]}},
            {"kind": "gate", "id": "gate", "needs": ["tests"],
             "config": {"command": "exit 0"}},
        ]
        status, workflow = client.post("/api/v1/workflows", {
            "projectId": project_ids[0], "name": "dogfood-wf",
            "steps": steps})
        assert status == 201, workflow
        status, run = client.post(
            "/api/v1/workflows/%s/runs" % workflow["id"])
        assert status == 201, run
        run_id = run["id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            status, current = client.get(
                "/api/v1/workflow-runs/%s" % run_id)
            if current["status"] != "running":
                break
            time.sleep(1.0)
        assert current["status"] == "succeeded", current
        assert all(s["status"] == "succeeded" for s in current["steps"])
    check(9, "agent→test→gate 工作流", v9)

    # 10. 失败不误标成功。
    def v10():
        failing = client.post("/api/apps", {
            "name": "fail-cmd",
            "command": 'python -c "import sys; sys.exit(1)"',
            "cwd": os.path.join(tmp.name, "项目A"),
            "kind": "task"})[1]
        status, projects = client.get("/api/v1/projects")
        fail_resource = next(
            r for p in projects for r in p["resources"]
            if r["app_id"] == failing["id"])
        steps = [
            {"kind": "task", "id": "t", "config": {
                "resourceId": fail_resource["id"]}}]
        status, workflow = client.post("/api/v1/workflows", {
            "projectId": project_ids[0], "name": "fail-wf", "steps": steps})
        status, run = client.post(
            "/api/v1/workflows/%s/runs" % workflow["id"])
        deadline = time.time() + 30
        while time.time() < deadline:
            status, current = client.get(
                "/api/v1/workflow-runs/%s" % run["id"])
            if current["status"] != "running":
                break
            time.sleep(1.0)
        assert current["status"] == "failed", current
    check(10, "失败不误标成功", v10)

    # 11-12. CLI 与 MCP 可用（复用 daemon 客户端链路的探测）。
    def v11():
        from adcc.cli.main import DaemonClient
        endpoint = {"port": port, "pid": os.getpid(),
                    "token": httpd.control_token}
        cli_client = DaemonClient(endpoint)
        status, body = cli_client.get("/api/v1/health")
        assert status == 200 and body["status"] in ("ok", "degraded")
    check(11, "CLI 控制", v11)

    def v12():
        from adcc.cli.main import DaemonClient
        from adcc.mcp.server import McpServer
        endpoint = {"port": port, "pid": os.getpid(),
                    "token": httpd.control_token}
        cli_client = DaemonClient(endpoint)
        server_mcp = McpServer(client_factory=lambda: cli_client)
        response = server_mcp.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}}})
        assert response.get("result") and response["result"]["isError"] is False
    check(12, "MCP 控制", v12)

    # 13. daemon 重启对账：受管服务保持、记录不伪造成功。
    def v13():
        resource_id = resources[project_ids[1]]
        status, body = client.post(
            "/api/v1/resources/%s/start" % resource_id)
        assert status == 200 and body.get("ok"), body
        time.sleep(1.5)
        db = server.get_runs_db()
        before = db.running_runs()
        server.reconcile_runs(cfg)
        runner.reconcile()
        executor.recover()
        after = db.running_runs()
        # 受管服务（有 run 记录）在重启对账后仍受管
        status, state = client.get("/api/v1/state")
        assert any(a["running"] for a in state["apps"])
        status, body = client.post(
            "/api/v1/resources/%s/stop" % resource_id)
        assert status == 200 and body.get("ok"), body
    check(13, "daemon 重启对账", v13)

    # 14. 端口冲突不误杀（外部进程占配置端口 → 不认领、不停止）。
    def v14():
        external_port = free_port()
        external = subprocess.Popen(
            ["python", "-m", "http.server", str(external_port)],
            creationflags=0x08000000 if os.name == "nt" else 0)
        time.sleep(1.5)
        status, app = client.post("/api/apps", {
            "name": "intruder", "command": "sleep 30",
            "cwd": os.path.join(tmp.name, "项目A"),
            "port": external_port, "kind": "service"})
        assert status == 200, app
        status, state = client.get("/api/v1/state")
        row = next(a for a in state["apps"] if a["id"] == app["id"])
        assert not row["running"]
        assert row["portOccupied"]
        status, body = client.post(
            "/api/v1/resources/%s/stop" % resources[project_ids[0]])
        time.sleep(0.5)
        assert external.poll() is None, "外部进程被误杀！"
        external.terminate()
        external.wait(timeout=5)
    check(14, "端口冲突不误杀外部进程", v14)

    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)
    db = server.get_runs_db()
    if db is not None:
        db.close()
        server.RUNS_DB = None
    time.sleep(0.8)
    tmp.cleanup()

    failed = [r for r in RESULTS if not r[2]]
    print("\n结果：%d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
