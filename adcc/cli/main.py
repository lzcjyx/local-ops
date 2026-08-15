"""adcc CLI — daemon client for the ADCC control plane (M5).

The CLI is a thin client of the daemon's /api/v1 endpoints; it never
duplicates process-management logic.  Exit codes are stable for scripting:

    0  success
    1  request/business error (daemon replied with an error)
    2  usage error (argparse default)
    3  daemon unavailable (no endpoint file, unreachable, or stale)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

ENDPOINT_FILENAME = "daemon.json"
PORT_START = 9600
PORT_TRIES = 10


class DaemonUnavailable(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def default_data_dir():
    if os.environ.get("CONSOLE_DATA_DIR"):
        return os.path.abspath(os.path.expanduser(os.environ["CONSOLE_DATA_DIR"]))
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "总控台")
    return os.path.expanduser("~/Library/Application Support/总控台")


def discover_endpoint(data_dir=None):
    """Read the daemon endpoint file; raise DaemonUnavailable when absent."""
    path = os.path.join(data_dir or default_data_dir(), ENDPOINT_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        raise DaemonUnavailable(
            "找不到 daemon 端点文件 %s。请先启动总控台（python server.py）。"
            % path)
    port = payload.get("port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise DaemonUnavailable("daemon.json 中的端口无效: %r" % port)
    return {
        "port": port,
        "pid": payload.get("pid"),
        "token": payload.get("token"),
    }


class DaemonClient:
    """Loopback HTTP client for the local daemon (no cookie needed for
    headerless local requests; mutating requests send JSON bodies only)."""

    def __init__(self, endpoint):
        self.base = "http://127.0.0.1:%d" % endpoint["port"]
        self.endpoint = endpoint

    def request(self, method, path, body=None, timeout=15):
        data = None
        headers = {"Accept": "application/json"}
        if self.endpoint.get("token"):
            headers["X-ADCC-Token"] = self.endpoint["token"]
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                try:
                    return response.status, json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return response.status, raw
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload = {"error": "HTTP %d" % exc.code}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DaemonUnavailable("无法连接总控台 daemon: %s" % exc)

    def get(self, path, timeout=15):
        return self.request("GET", path, timeout=timeout)

    def post(self, path, body=None):
        # 无 body 的写操作也发送空 JSON：server 的 POST 统一要求
        # application/json（CSRF 防护），handler 会 discard。
        return self.request("POST", path, body={} if body is None else body)


# ---------------------------------------------------------------- 输出

def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def run_table(runs):
    lines = []
    for run in runs:
        duration = run.get("durationSec")
        duration = ("%.1fs" % duration) if duration is not None else "-"
        ended = "运行中" if run.get("status") == "running" else (
            time.strftime("%m-%d %H:%M", time.localtime(run["startedAt"]))
            if run.get("startedAt") else "-")
        lines.append(
            "%s  %-10s %-8s pid=%-6s %s" % (
                run.get("id"), run.get("kind"), run.get("status"),
                run.get("pid") or "-", ended))
    return "\n".join(lines) if lines else "（无运行记录）"


# ---------------------------------------------------------------- 命令

def cmd_status(client, args):
    status, body = client.get("/api/v1/health")
    if status != 200:
        raise ApiError(body.get("error", "健康检查失败"), status)
    if args.json:
        emit(body, True)
        return EXIT_OK
    print("总控台状态: %s" % body.get("status"))
    print("版本: %s (schema %s)" % (body.get("version"), body.get("schemaVersion")))
    print("降级: %s" % ("是" if body.get("degraded") else "否"))
    for issue in body.get("issues") or []:
        print("  - %s" % issue)
    return EXIT_OK


def cmd_doctor(client, args):
    status, body = client.get("/api/v1/health")
    if status != 200:
        raise ApiError(body.get("error", "健康检查失败"), status)
    report = {
        "daemon": "ok",
        "status": body.get("status"),
        "version": body.get("version"),
        "issues": body.get("issues") or [],
        "config": body.get("config") or {},
        "endpoint": client.endpoint.get("port"),
    }
    if args.json:
        emit(report, True)
        return EXIT_OK if body.get("status") == "ok" else EXIT_ERROR
    print("daemon 端口: %s" % report["endpoint"])
    print("状态: %s" % body.get("status"))
    for issue in body.get("issues") or []:
        print("  - %s" % issue)
    return EXIT_OK if body.get("status") == "ok" else EXIT_ERROR


def cmd_projects_list(client, args):
    status, body = client.get("/api/v1/projects")
    if status != 200:
        raise ApiError(body.get("error", "获取项目失败"), status)
    if args.json:
        emit(body, True)
        return EXIT_OK
    for project in body:
        print("%s  %-20s %s  %d 资源" % (
            project.get("id"), project.get("name"),
            project.get("root_path") or "-",
            len(project.get("resources") or [])))
    return EXIT_OK


def cmd_project_show(client, args):
    status, body = client.get("/api/v1/projects/" + args.id)
    if status != 200:
        raise ApiError(body.get("error", "项目不存在"), status)
    if args.json:
        emit(body, True)
        return EXIT_OK
    print("%s  %s" % (body.get("id"), body.get("name")))
    print("路径: %s" % (body.get("root_path") or "-"))
    print("Git: %s" % (body.get("repo_path") or "非 Git 仓库"))
    for resource in body.get("resources") or []:
        print("  %s  %-8s %s  %s" % (
            resource.get("id"), resource.get("kind"),
            resource.get("name"), resource.get("command") or ""))
    return EXIT_OK


def cmd_resources_list(client, args):
    status, body = client.get("/api/v1/resources")
    if status != 200:
        raise ApiError(body.get("error", "获取资源失败"), status)
    if args.project:
        body = [r for r in body if r.get("project_id") == args.project]
    if args.json:
        emit(body, True)
        return EXIT_OK
    for resource in body:
        print("%s  %-8s %-20s %s  %s" % (
            resource.get("id"), resource.get("kind"),
            resource.get("name"), resource.get("command") or "",
            ("port %s" % resource.get("port")) if resource.get("port") else ""))
    return EXIT_OK


def _resource_action(client, resource_id, action):
    status, body = client.post(
        "/api/v1/resources/%s/%s" % (resource_id, action))
    if status == 200 and body.get("ok", True):
        emit(body, False)
        return EXIT_OK
    raise ApiError(body.get("error", "%s 失败" % action), status)


def cmd_start(client, args):
    return _resource_action(client, args.resource_id, "start")


def cmd_stop(client, args):
    return _resource_action(client, args.resource_id, "stop")


def cmd_restart(client, args):
    return _resource_action(client, args.resource_id, "restart")


def cmd_ports(client, args):
    status, state = client.get("/api/v1/state")
    if status != 200:
        raise ApiError(state.get("error", "获取状态失败"), status)
    rows = []
    for service in state.get("services") or []:
        rows.append({
            "port": service.get("port"),
            "pid": service.get("pid"),
            "name": service.get("name"),
            "project": service.get("project"),
            "cmd": service.get("cmd"),
        })
    rows.sort(key=lambda row: (row["port"] or 0, row["pid"] or 0))
    if args.json:
        emit(rows, True)
        return EXIT_OK
    for row in rows:
        print("%-6s %-8s %-20s %s" % (
            row["port"], row["pid"], row["name"] or "?",
            (row["cmd"] or "")[:80]))
    return EXIT_OK


def cmd_port_owner(client, args):
    port = int(args.port)
    status, state = client.get("/api/v1/state")
    if status != 200:
        raise ApiError(state.get("error", "获取状态失败"), status)
    owners = []
    for service in state.get("services") or []:
        if service.get("port") == port:
            owners.append(service)
    result = {
        "port": port,
        "listeners": owners,
        "found": bool(owners),
    }
    if args.json:
        emit(result, True)
        return EXIT_OK if owners else EXIT_ERROR
    if not owners:
        print("端口 %d 当前没有监听者" % port)
        return EXIT_ERROR
    for owner in owners:
        print("PID %s  %s  %s" % (
            owner.get("pid"), owner.get("name"), owner.get("cmd") or ""))
    return EXIT_OK


def cmd_runs_list(client, args):
    query = "?limit=%d" % (args.limit or 50)
    if args.app_id:
        query += "&appId=" + args.app_id
    status, body = client.get("/api/v1/runs" + query)
    if status != 200:
        raise ApiError(body.get("error", "获取运行记录失败"), status)
    if args.json:
        emit(body, True)
        return EXIT_OK
    print(run_table(body.get("runs") or []))
    return EXIT_OK


def cmd_agents_list(client, args):
    query = "?limit=%d" % (args.limit or 50)
    status, body = client.get("/api/v1/agents/sessions" + query)
    if status != 200:
        raise ApiError(body.get("error", "获取会话失败"), status)
    if args.json:
        emit(body, True)
        return EXIT_OK
    for session in body.get("sessions") or []:
        duration = session.get("durationSec")
        duration = ("%.1fs" % duration) if duration is not None else "-"
        print("%s  %-10s %-8s pid=%-6s %s" % (
            session.get("id"), session.get("adapterId"),
            session.get("status"), session.get("pid") or "-", duration))
    return EXIT_OK


def cmd_agent_run(client, args):
    body = {"adapterId": args.adapter_id, "projectId": args.project_id}
    if args.prompt_file:
        body["promptFile"] = args.prompt_file
    if args.prompt:
        body["prompt"] = args.prompt
    status, response = client.post("/api/v1/agents/sessions", body)
    if status == 201:
        session = response
        if args.json:
            emit(session, True)
        else:
            print("会话 %s 已启动（%s）" % (session.get("id"),
                                        session.get("status")))
        return EXIT_OK
    raise ApiError(response.get("error", "启动会话失败"), status)


def cmd_agent_stop(client, args):
    status, response = client.post(
        "/api/v1/agents/sessions/%s/stop" % args.session_id)
    if status == 200 and response.get("ok", True):
        emit(response, False)
        return EXIT_OK
    raise ApiError(response.get("error", "停止会话失败"), status)


def cmd_workflows_list(client, args):
    status, body = client.get("/api/v1/workflows")
    if status != 200:
        raise ApiError(body.get("error", "获取工作流失败"), status)
    if args.json:
        emit(body, True)
        return EXIT_OK
    for workflow in body:
        print("%s  %-24s %d 步骤 %s" % (
            workflow.get("id"), workflow.get("name"),
            len(workflow.get("steps") or []),
            workflow.get("project_id") or ""))
    return EXIT_OK


def cmd_workflow_run(client, args):
    status, body = client.post(
        "/api/v1/workflows/%s/runs" % args.workflow_id)
    if status == 201:
        if args.json:
            emit(body, True)
        else:
            print("工作流运行 %s 已启动（%s）" % (body.get("id"),
                                              body.get("status")))
        return EXIT_OK
    raise ApiError(body.get("error", "启动工作流失败"), status)


def cmd_workflow_cancel(client, args):
    status, body = client.post(
        "/api/v1/workflow-runs/%s/cancel" % args.run_id)
    if status == 200 and body.get("ok", True):
        emit(body, False)
        return EXIT_OK
    raise ApiError(body.get("error", "取消失败"), status)


def cmd_logs(client, args):
    target = args.run_id
    run_id = None
    status, body = client.get("/api/v1/runs/" + target)
    if status == 200 and body.get("appId"):
        run_id = target
        app_id = body.get("appId")
    else:
        # 按资源/app id 找最新 run
        status, body = client.get("/api/v1/runs?appId=" + target + "&limit=1")
        if status == 200 and body.get("runs"):
            run_id = body["runs"][0].get("id")
            app_id = target
        else:
            raise ApiError("找不到运行记录: %s" % target, 404)
    tail = args.tail or 300
    seen_lines = 0
    deadline = time.monotonic() + (args.follow and args.timeout or 0)
    while True:
        status, body = client.get(
            "/api/v1/runs/%s/logs?tail=%d" % (run_id, tail))
        if status != 200:
            raise ApiError(body.get("error", "获取日志失败"), status)
        text = body.get("text") or ""
        lines = text.splitlines()
        if len(lines) > seen_lines:
            for line in lines[seen_lines:]:
                print(line)
            seen_lines = len(lines)
        if not args.follow:
            return EXIT_OK
        if deadline and time.monotonic() >= deadline:
            return EXIT_OK
        time.sleep(1.0)


# ---------------------------------------------------------------- 解析

def build_parser():
    parser = argparse.ArgumentParser(
        prog="adcc",
        description="ADCC 控制平面命令行客户端（daemon 客户端，不重复实现运行时逻辑）")
    parser.add_argument("--data-dir", metavar="DIR",
                        help="覆盖 daemon 数据目录（默认同总控台）")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="daemon 健康状态")
    status.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="诊断 daemon 与配置健康")
    doctor.add_argument("--json", action="store_true")

    projects = sub.add_parser("projects", help="项目")
    projects_sub = projects.add_subparsers(dest="sub", required=True)
    list_p = projects_sub.add_parser("list", help="列出项目")
    list_p.add_argument("--json", action="store_true")
    show_p = projects_sub.add_parser("show", help="项目详情")
    show_p.add_argument("id")
    show_p.add_argument("--json", action="store_true")

    resources = sub.add_parser("resources", help="资源")
    resources_sub = resources.add_subparsers(dest="sub", required=True)
    resources_list = resources_sub.add_parser("list", help="列出资源")
    resources_list.add_argument("--project", metavar="ID", help="按项目过滤")
    resources_list.add_argument("--json", action="store_true")

    for name in ("start", "stop", "restart"):
        action = sub.add_parser(name, help="%s 资源" % name)
        action.add_argument("resource_id")

    ports = sub.add_parser("ports", help="监听端口列表")
    ports.add_argument("--json", action="store_true")

    owner = sub.add_parser("port", help="端口归属")
    owner_sub = owner.add_subparsers(dest="sub", required=True)
    owner_show = owner_sub.add_parser("owner", help="查询端口占用者")
    owner_show.add_argument("port")
    owner_show.add_argument("--json", action="store_true")

    runs = sub.add_parser("runs", help="运行记录")
    runs_sub = runs.add_subparsers(dest="sub", required=True)
    runs_list = runs_sub.add_parser("list", help="列出运行记录")
    runs_list.add_argument("--limit", type=int, default=50)
    runs_list.add_argument("--app-id", dest="app_id")
    runs_list.add_argument("--json", action="store_true")

    logs = sub.add_parser("logs", help="查看日志（run 或资源 id）")
    logs.add_argument("run_id")
    logs.add_argument("--tail", type=int, default=300)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--timeout", type=float, default=0,
                      help="--follow 的最长跟踪秒数（默认无限）")

    agents = sub.add_parser("agents", help="agent 会话")
    agents_sub = agents.add_subparsers(dest="sub", required=True)
    agents_list = agents_sub.add_parser("list", help="列出会话")
    agents_list.add_argument("--limit", type=int, default=50)
    agents_list.add_argument("--json", action="store_true")

    agent = sub.add_parser("agent", help="agent 操作")
    agent_sub = agent.add_subparsers(dest="sub", required=True)
    agent_run = agent_sub.add_parser("run", help="启动 agent 会话")
    agent_run.add_argument("--project", dest="project_id", required=True)
    agent_run.add_argument("--adapter", dest="adapter_id", required=True)
    agent_run.add_argument("--prompt-file", dest="prompt_file")
    agent_run.add_argument("--prompt")
    agent_run.add_argument("--json", action="store_true")
    agent_stop = agent_sub.add_parser("stop", help="停止会话")
    agent_stop.add_argument("session_id")

    workflows = sub.add_parser("workflows", help="工作流")
    workflows_sub = workflows.add_subparsers(dest="sub", required=True)
    workflows_list = workflows_sub.add_parser("list", help="列出工作流")
    workflows_list.add_argument("--json", action="store_true")

    workflow = sub.add_parser("workflow", help="工作流操作")
    workflow_sub = workflow.add_subparsers(dest="sub", required=True)
    workflow_run = workflow_sub.add_parser("run", help="运行工作流")
    workflow_run.add_argument("workflow_id")
    workflow_run.add_argument("--json", action="store_true")
    workflow_cancel = workflow_sub.add_parser("cancel", help="取消运行")
    workflow_cancel.add_argument("run_id")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        endpoint = discover_endpoint(args.data_dir)
        client = DaemonClient(endpoint)
    except DaemonUnavailable as exc:
        print("adcc: %s" % exc, file=sys.stderr)
        return EXIT_UNAVAILABLE
    handlers = {
        "status": cmd_status,
        "doctor": cmd_doctor,
        "projects": lambda client, args: (
            cmd_projects_list(client, args) if args.sub == "list"
            else cmd_project_show(client, args)),
        "resources": cmd_resources_list,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "ports": cmd_ports,
        "port": cmd_port_owner,
        "runs": cmd_runs_list,
        "logs": cmd_logs,
        "agents": cmd_agents_list,
        "agent": lambda client, args: (
            cmd_agent_run(client, args) if args.sub == "run"
            else cmd_agent_stop(client, args)),
        "workflows": cmd_workflows_list,
        "workflow": lambda client, args: (
            cmd_workflow_run(client, args) if args.sub == "run"
            else cmd_workflow_cancel(client, args)),
    }
    try:
        args.command  # noqa: B018
        handler = handlers[args.command]
        if args.command == "resources":
            if getattr(args, "sub", None) != "list":
                print("adcc: resources 需要子命令 list", file=sys.stderr)
                return EXIT_USAGE
        return handler(client, args)
    except ApiError as exc:
        message = "adcc: %s" % exc
        print(message, file=sys.stderr)
        return EXIT_ERROR
    except DaemonUnavailable as exc:
        print("adcc: %s" % exc, file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (ValueError, TypeError) as exc:
        print("adcc: 参数错误: %s" % exc, file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
