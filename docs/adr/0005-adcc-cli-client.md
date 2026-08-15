# ADR 0005: adcc CLI（daemon 客户端）

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M5

## 背景

M4 提供了 /api/v1，但 GUI 之外没有面向人类/脚本的接口。SPEC §17
要求 `adcc` CLI：status/doctor/项目/资源/启停/端口/运行/日志，
`--json` 输出、稳定退出码、无 GUI 依赖，且不得重复实现运行时逻辑。

## 决策

### 纯客户端架构

- `adcc/cli/main.py`：全部命令只经 HTTP 调 daemon 的 `/api/v1`；
  不 import `server.py`，不触碰进程/端口逻辑。
- 端点发现：daemon 启动时写 `DATA_DIR/daemon.json`
  （`{port, pid, token}`，0600，`_write_daemon_endpoint`）；
  停止时删除。CLI 读取该文件确定 base URL。
- 认证：现有 loopback 信任边界天然允许无头本地客户端（无
  Origin/Sec-Fetch-Site 头 + `application/json` 的 POST 即放行）；
  `daemon.json` 的 token 字段为 M11 token 模型预留，CLI 当前不携带。

### 命令与退出码

| 命令 | 说明 |
| --- | --- |
| `status [--json]` / `doctor [--json]` | 健康/诊断（doctor 在 degraded 时退出 1） |
| `projects list/show <id> [--json]` | 项目列表/详情 |
| `resources list [--project <id>] [--json]` | 资源列表 |
| `start/stop/restart <resource-id>` | 资源启停（经 app_id 桥委托） |
| `ports [--json]` / `port owner <port> [--json]` | 端口列表/归属 |
| `runs list [--limit N] [--app-id X] [--json]` | 运行记录 |
| `logs <run-or-resource-id> [--tail N] [--follow]` | 日志（follow 轮询） |

退出码：0 成功；1 业务/请求错误；2 用法错误（argparse）；3 daemon
不可达（无端点文件/连接失败/端口非法）。

### 资源启停语义

- POST 统一携带空 JSON body（server 全部 POST 要求
  `application/json`——CSRF 防护）；CLI 只以 `status==200 && ok`
  判定成功（`ok:false` 是业务失败，退出 1）。

## 本里程碑修复的平台/设计缺陷

1. **v1 启停未 discard body**：`/api/v1/resources/{id}/start|stop|restart`
   handler 未读掉请求体 → keep-alive 残留 `{}` 污染下一请求（400，
   AGENTS.md 已记录的陷阱）。补 `discard_body()`。
2. **CIM 缓存掩盖新进程**：Windows 全量 CIM 缓存 TTL 2s，restart 后
   立即查询看不到新进程 → 受管身份识别为空 → stop 报「未在运行」。
   新进程启动成功后显式 `invalidate_cache()`，身份立即可见。
3. **CLI 误把 `ok:false` 当成功**：只看 HTTP 200 → 业务失败被吞。
   改为校验 `body.ok`。
4. **新 app 未注册资源**：`handle_app_create` 未同步创建 resource →
   CLI 按资源 id 无法操作新应用。补 `register_resource_for_app`
   （cwd 匹配项目，否则 Unassigned）；删除 app 时同步清理资源。

## 结果

- exit gate 五条命令全部可用且有稳定退出码；`adcc status --json`
  等 10 项 CLI 契约测试通过（含 daemon 不可达场景）。
- 199 项测试全绿（修复后无进程残留/teardown 竞态）。

代价与限制：

- `logs --follow` 是轮询近似（1s 间隔），非流式；
- `port owner` 只报告当前监听者，无历史；
- 未做 agents/workflows 命令（M7/M8 后补充）。

## 未采用方案

- CLI 内嵌运行时实现：违反 SPEC §17「不得重复实现」。
- 端口扫描发现 daemon：daemon.json 更简单且带身份字段。
- 独立二进制打包：`python -m adcc.cli.main` 足够；发行打包归 M11。
