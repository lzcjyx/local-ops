# ADR 0004: ManagedRun 历史、SQLite 操作库与 /api/v1

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M4

## 背景

M3 之前所有「运行」都是无持久化的瞬时状态（config 里的 lastPid/lastExit）。
SPEC §6/§13/§14 要求 durable run 记录（GUI/CLI/MCP 共享）与日志按 run
索引；SPEC §15 要求版本化 `/api/v1` 同时保留 legacy `/api` 兼容面。

## 决策

### ManagedRun 与状态枚举

- `adcc/runtime/runs.py`：canonical 状态枚举（queued/starting/running/
  succeeded/failed/canceled/stopped/timed_out/lost）与 kind
  （service/task/agent/workflow_step），纯校验与投影函数
  （`public_run` 稳定字段顺序，durationSec 由 ended-started 推导）。
- 终态映射 `finalize_run_status`：手动停止 → stopped；task 130 → canceled；
  exit 0 → succeeded；其余 → failed。与 legacy lastExit 四态语义一致。

### SQLite 操作库

- `adcc/storage/database.py`：标准库 `sqlite3` 单连接 + RLock；
  `schema_migrations` 表做版本化迁移（v1：runs 表 + 索引）。
- 日志内容仍为文件（`LOGS_DIR/{app_id}.log`），数据库只存 run 元数据
  （含 log_path），符合 SPEC §13.2「不把无界日志放进 SQLite」。
- server 惰性打开（`get_runs_db`），失败降级为 None（API 返回空、
  历史不落库，但启停/监控不受影响）。

### Run 生命周期接入

- `record_run_start`（persist_started_app 成功后）：插入 running 记录
  （app_id/project_id 经 M3 的 resource.app_id 桥/kind/pid/pgid/token/
  log_path），发布 `run.created` 事件。
- `watch_app_exit` 与 `stop_app_and_clear` 双入口 `finalize_runs_for_app`
  （幂等：只转换 running）；手动停止 → stopped。
- `reconcile_runs`（daemon 启动时 + 15s 监护线程）：running 记录按当前
  受管身份重验，进程消失 → **lost**（绝不伪造成功，SPEC §12.3）。

### /api/v1（兼容 /api 保留）

- `GET /api/v1/health|state`：现有健康/状态投影的子集。
- `GET|POST /api/v1/projects`、`GET /api/v1/resources` 与
  `POST /api/v1/resources/{id}/start|stop|restart`（经 app_id 桥委托
  legacy app 操作，操作锁仍由 `@serialized_app_operation` 承担）。
- `GET /api/v1/runs[?limit|appId|status]`、`/runs/{id}`、
  `/runs/{id}/logs`（tail 有界）。
- `GET /api/v1/events`：SSE（`text/event-stream`），`EventBus` 内存队列
  每订阅者 100 条、满时丢最旧，15s 心跳注释，断线自动退订；事件
  `run.created/run.updated/project.updated`。

### Windows 启动包装修复（本里程碑发现的平台坑）

cmd 包装 `set "CONSOLE_RUN_TOKEN=x" && <command>` 在用户命令含引号时
（如 `python -c "..."`）会被 subprocess 的 argv 再引号化导致命令损坏。
改为：把命令写入临时批处理 `console-run-<token>.cmd`（文件名携带
token，CommandLine 可查 → 身份校验与 macOS 同构），`cmd /c <batch>`
执行，`exit /b %errorlevel%` 传播退出码；watch 线程退出后删除文件。
Windows 受管身份 marker 前缀统一为 `console-run-`（文件名合法字符，
冒号在 Windows 文件名非法）。

## 结果

- 任务 run 有 durable run id/history；daemon 重启把消失进程标 lost。
- 189 项测试通过（含 runs 契约 10 项、/api/v1 HTTP 契约 11 项、
  SSE 事件流、重启对账、Windows 身份纯函数）。
- GUI/legacy /api 全部照常（兼容面保留）。

代价与限制：

- runs 表只保留本机一个库，尚无保留期/清理策略（M11 安全加固项）。
- 资源启停仍是 app_id 桥的过渡实现；资源直连 ManagedRun（M7 前后接管）。
- SSE 无 last-event-id 重放（轮询兜底仍在）。

## 未采用方案

- run 状态直接写 config.json：会让配置文件无界增长并破坏「配置=人类
  可编辑状态」的语义。
- 日志入库：SPEC 明确禁止无界日志进 SQLite。
- `/api/v1` 独立端口/进程：无必要，与既有 server 同进程分路由。
