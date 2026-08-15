# ADR 0006: MCP server（安全 Agent 工具集，stdio）

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M6

## 背景

SPEC §18 要求 coding agent 通过 MCP 安全地查询与操作控制平面：
只暴露受管资源工具，不暴露任意 `kill(pid)` 或任意 shell。PLAN 要求
stdio 优先、复用 HTTP/CLI 同一应用层、输出有界、错误 typed。

## 决策

### 传输与协议

- `adcc/mcp/server.py`：stdlib 手写 JSON-RPC 2.0 over stdio
  （换行分隔 JSON），零依赖。入口 `python -m adcc.mcp.server
  [--data-dir DIR]`。
- 支持方法：`initialize`（2024-11-05）、`notifications/initialized`、
  `ping`、`tools/list`、`tools/call`。
- 输出强制 UTF-8（`sys.stdout.reconfigure`）——Windows 管道默认
  locale 编码会破坏中文消息与 MCP 规范。

### 工具集（全部经 DaemonClient → /api/v1）

list_projects / get_project / list_resources / get_resource_status /
start_resource / stop_resource / restart_resource / run_task /
list_runs / get_run / get_run_logs / get_port_owner。

- 无 `kill`、无 shell 类工具（契约测试断言工具名不含 kill/shell）。
- 启停/重启只作用于受管资源（daemon 侧受管身份校验后才执行）；
  未受管/不存在资源返回 typed 错误。
- 有界输出：logs tail 上限 2000 行（默认 200）、runs 上限 100、
  resources 上限 500。
- 错误模型：`ToolError`（参数/业务）→ -32000；daemon 不可达 → -32001；
  内部异常只暴露类型名（-32603），不泄漏细节。

### 复用与配置

- 复用 CLI 的 `DaemonClient`/`discover_endpoint`——同一应用层，
  无第二份运行时逻辑。
- 示例配置 `mcp.example.json`（通用 harness 的 stdio 配置模板）。

## 结果

- exit gate 六项全部通过契约测试（项目/资源/启停/有界日志/仅受管
  停止/typed 错误），17 项 MCP 测试全绿，全量 216 项通过。
- 修复：MCP 子进程 stdout 编码（UTF-8）、测试 harness 的 root 计算。

代价与限制：

- 无会话/鉴权层（stdio 本地进程边界即信任边界；token 模型归 M11）；
- 无 `list_agent_sessions`/workflow 工具（M7/M8 后补充）；
- 无 SSE/HTTP 传输（stdio 优先，后续可按需加）。

## 未采用方案

- 依赖官方 MCP SDK：违反零依赖约束；手写 JSON-RPC 足够且可测。
- 暴露 `kill(pid)`/`run_shell`：违反 SPEC §18 安全规则。
- MCP server 内嵌 daemon 逻辑：与 CLI 一样，只做客户端。
