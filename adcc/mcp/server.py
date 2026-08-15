"""MCP server (M6): safe Agent-facing tools over the daemon /api/v1.

Stdio JSON-RPC transport (line-delimited JSON), zero dependencies.
All tools are thin wrappers over the same DaemonClient used by the CLI —
no runtime logic is duplicated here.  Tools never expose raw
``kill(pid)`` or unrestricted shell execution; every write goes through
managed-resource identity checks on the daemon.

Entry: ``python -m adcc.mcp.server``
"""

import json
import os
import sys

from adcc.cli.main import (
    DaemonClient,
    DaemonUnavailable,
    discover_endpoint,
)

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_LOG_TAIL = 200
MAX_LOG_TAIL = 2000
MAX_RUNS = 100
MAX_RESOURCES = 500

# MCP 规范要求 UTF-8；Windows 管道默认 locale 编码会破坏中文消息
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _tail_limit(value):
    try:
        return max(1, min(int(value), MAX_LOG_TAIL))
    except (TypeError, ValueError):
        return DEFAULT_LOG_TAIL


def _runs_limit(value):
    try:
        return max(1, min(int(value), MAX_RUNS))
    except (TypeError, ValueError):
        return 50


def _require_text(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("%s 必填（字符串）" % key)
    return value.strip()


def _optional_text(args, key):
    value = args.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


class ToolError(RuntimeError):
    """Typed tool error; rendered as isError: true with a safe message."""


class McpServer:
    """One stdio session: read JSON-RPC requests, write responses."""

    def __init__(self, client_factory=None, data_dir=None):
        self._client_factory = client_factory
        self._data_dir = data_dir
        self._client = None

    # ------------------------------------------------------------ client

    def _daemon(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = DaemonClient(discover_endpoint(self._data_dir))
        return self._client

    # ------------------------------------------------------------ JSON-RPC

    def handle_request(self, request):
        if not isinstance(request, dict):
            return self._error(None, -32600, "请求必须是 JSON 对象")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "缺少 method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return self._error(request_id, -32600, "params 必须是对象")
        try:
            if method == "initialize":
                return self._result(request_id, self._initialize(params))
            if method == "notifications/initialized":
                return None  # 通知无响应
            if method == "tools/list":
                return self._result(request_id, {"tools": TOOLS})
            if method == "tools/call":
                return self._call_tool(request_id, params)
            if method == "ping":
                return self._result(request_id, {})
            return self._error(request_id, -32601, "未知方法: %s" % method)
        except ToolError as exc:
            return self._error(request_id, -32000, str(exc))
        except DaemonUnavailable as exc:
            return self._error(request_id, -32001, "daemon 不可达: %s" % exc)
        except Exception as exc:  # 工具实现不得泄漏内部细节
            return self._error(request_id, -32603, "内部错误: %s" % type(exc).__name__)

    def _initialize(self, params):
        client_info = params.get("clientInfo") or {}
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "adcc-mcp", "version": "0.1.0"},
            "instructions": (
                "ADCC 控制平面工具集。启停/重启仅作用于本机受管资源，"
                "不会触碰未受管进程。"),
            "_client": client_info.get("name"),
        }

    def _call_tool(self, request_id, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return self._error(request_id, -32602, "缺少工具名")
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "arguments 必须是对象")
        tool = TOOL_BY_NAME.get(name)
        if tool is None:
            return self._error(request_id, -32602, "未知工具: %s" % name)
        result = tool["handler"](self, arguments)
        return self._result(request_id, result)

    def _result(self, request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    # ------------------------------------------------------------ loop

    def serve_forever(self):
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                self._write(self._error(None, -32700, "解析 JSON 失败"))
                continue
            response = self.handle_request(request)
            if response is not None:
                self._write(response)
        return 0

    @staticmethod
    def _write(payload):
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------- 工具


def _tool(name, description, schema, handler):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": schema,
        },
        "handler": handler,
    }


def _plain(result):
    return {"content": [{"type": "text", "text": json.dumps(
        result, ensure_ascii=False, indent=2)}], "isError": False}


def _tool_error(result):
    return {"content": [{"type": "text", "text": result}], "isError": True}


def _get(client, path):
    status, body = client.get(path)
    if status != 200:
        raise ToolError(body.get("error", "请求失败") if isinstance(body, dict)
                        else "HTTP %d" % status)
    return body


def _post(client, path):
    status, body = client.post(path)
    if status == 200 and body.get("ok", True):
        return body
    raise ToolError(body.get("error", "操作失败") if isinstance(body, dict)
                    else "HTTP %d" % status)


def t_list_projects(server, args):
    return _plain(_get(server._daemon(), "/api/v1/projects"))


def t_get_project(server, args):
    project_id = _require_text(args, "id")
    return _plain(_get(server._daemon(), "/api/v1/projects/" + project_id))


def t_list_resources(server, args):
    project_id = _optional_text(args, "projectId")
    resources = _get(server._daemon(), "/api/v1/resources")
    if project_id:
        resources = [r for r in resources if r.get("project_id") == project_id]
    return _plain(resources[:MAX_RESOURCES])


def t_get_resource_status(server, args):
    resource_id = _require_text(args, "id")
    resources = _get(server._daemon(), "/api/v1/resources")
    resource = next((r for r in resources if r.get("id") == resource_id), None)
    if resource is None:
        raise ToolError("资源不存在: %s" % resource_id)
    state = _get(server._daemon(), "/api/v1/state")
    app_id = resource.get("app_id")
    app = next((a for a in state.get("apps") or [] if a.get("id") == app_id),
               None) if app_id else None
    return _plain({
        "resource": resource,
        "running": bool(app and app.get("running")) if app_id else None,
        "pid": app.get("pid") if app else None,
        "listening": bool(app and app.get("listening")) if app else None,
        "ports": app.get("ports") if app else None,
    })


def t_start_resource(server, args):
    resource_id = _require_text(args, "id")
    return _plain(_post(server._daemon(),
                        "/api/v1/resources/%s/start" % resource_id))


def t_stop_resource(server, args):
    resource_id = _require_text(args, "id")
    return _plain(_post(server._daemon(),
                        "/api/v1/resources/%s/stop" % resource_id))


def t_restart_resource(server, args):
    resource_id = _require_text(args, "id")
    return _plain(_post(server._daemon(),
                        "/api/v1/resources/%s/restart" % resource_id))


def t_list_runs(server, args):
    limit = _runs_limit(args.get("limit", 50))
    path = "/api/v1/runs?limit=%d" % limit
    app_id = _optional_text(args, "appId")
    if app_id:
        path += "&appId=" + app_id
    return _plain(_get(server._daemon(), path))


def t_get_run(server, args):
    run_id = _require_text(args, "id")
    return _plain(_get(server._daemon(), "/api/v1/runs/" + run_id))


def t_get_run_logs(server, args):
    run_id = _require_text(args, "id")
    tail = _tail_limit(args.get("tail", DEFAULT_LOG_TAIL))
    return _plain(_get(server._daemon(),
                       "/api/v1/runs/%s/logs?tail=%d" % (run_id, tail)))


def t_get_port_owner(server, args):
    port = args.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ToolError("port 必须是 1-65535 的整数")
    state = _get(server._daemon(), "/api/v1/state")
    owners = [s for s in state.get("services") or [] if s.get("port") == port]
    return _plain({"port": port, "listeners": owners, "found": bool(owners)})


def t_run_task(server, args):
    """启动批处理任务（等价 start_resource，语义更明确）。"""
    resource_id = _require_text(args, "id")
    return t_start_resource(server, {"id": resource_id})


TOOLS = [
    _tool("list_projects", "列出全部项目及其资源",
          {"projectId": {"type": "string", "description": "可选，过滤项目"}}, t_list_projects),
    _tool("get_project", "获取单个项目详情（含资源）",
          {"id": {"type": "string"}}, t_get_project),
    _tool("list_resources", "列出资源（按项目可选过滤）",
          {"projectId": {"type": "string"}}, t_list_resources),
    _tool("get_resource_status", "资源定义 + 当前运行状态",
          {"id": {"type": "string"}}, t_get_resource_status),
    _tool("start_resource", "启动受管资源（服务或任务）",
          {"id": {"type": "string"}}, t_start_resource),
    _tool("stop_resource", "停止受管资源（仅受管身份）",
          {"id": {"type": "string"}}, t_stop_resource),
    _tool("restart_resource", "重启受管资源（先停后启，失败不中断旧服务）",
          {"id": {"type": "string"}}, t_restart_resource),
    _tool("run_task", "运行批处理任务（等价 start_resource）",
          {"id": {"type": "string"}}, t_run_task),
    _tool("list_runs", "列出运行历史（默认最近 50 条）",
          {"limit": {"type": "integer", "description": "1-100"},
           "appId": {"type": "string"}}, t_list_runs),
    _tool("get_run", "获取单条运行记录",
          {"id": {"type": "string"}}, t_get_run),
    _tool("get_run_logs", "获取运行日志（有界 tail）",
          {"id": {"type": "string"},
           "tail": {"type": "integer", "description": "末尾行数，默认 200，上限 2000"}},
          t_get_run_logs),
    _tool("get_port_owner", "查询端口当前监听者",
          {"port": {"type": "integer"}}, t_get_port_owner),
]

TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def main(argv=None):
    data_dir = None
    if argv is not None and len(argv) > 1 and argv[1] == "--data-dir":
        data_dir = argv[2] if len(argv) > 2 else None
    server = McpServer(data_dir=data_dir)
    return server.serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
