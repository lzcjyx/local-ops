# M6 MCP Server

## 结论

`adcc/mcp/server.py` 提供安全 Agent 工具集：JSON-RPC 2.0 over stdio，
零依赖，全部工具经 CLI 同款 `DaemonClient` 调 daemon `/api/v1`。
详见 `docs/adr/0006`。

```text
coding agent (MCP client)
        │ stdio JSON-RPC
        ▼
adcc.mcp.server ── DaemonClient ──> daemon /api/v1 ──> 受管身份校验
```

## 工具清单

| 工具 | 说明 |
| --- | --- |
| list_projects / get_project | 项目（含资源） |
| list_resources / get_resource_status | 资源定义 + 运行状态 |
| start_resource / stop_resource / restart_resource | 仅受管资源 |
| run_task | 批处理任务（等价 start_resource） |
| list_runs / get_run / get_run_logs | 运行历史 + 有界日志 |
| get_port_owner | 端口监听者查询 |

安全：无 kill/shell 工具；错误 typed（-32000 业务 / -32001 daemon
不可达 / -32603 内部）；输出有界（logs≤2000、runs≤100、
resources≤500）。

## 使用

```text
python -m adcc.mcp.server                # 从 daemon.json 发现端点
python -m adcc.mcp.server --data-dir X   # 覆盖数据目录
```

harness 配置模板见 `mcp.example.json`。

## Exit gate 验证（契约测试）

1. list projects ✓  2. inspect resource ✓  3. start/run task ✓
4. bounded logs ✓  5. stop 仅受管（未受管 → typed error）✓
6. unsafe/invalid 动作 typed error ✓
