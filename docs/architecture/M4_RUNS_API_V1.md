# M4 运行历史、SQLite 与 API v1

## 结论

M4 引入 durable ManagedRun 历史（SQLite 操作库）、/api/v1 版本化路由与
SSE 事件流；`/api` legacy 面完全保留。详见 `docs/adr/0004`。

```text
start/stop/exit ──> record_run_start / finalize_runs_for_app
                          │
                          v
                 console.sqlite3 (runs 表, schema_migrations)
                          │
                          v
        /api/v1/runs, /runs/{id}, /runs/{id}/logs
        /api/v1/events (SSE via EventBus)
```

## Run 生命周期

| 阶段 | 入口 | 结果 |
| --- | --- | --- |
| 启动 | persist_started_app → record_run_start | running + run.created |
| 自然退出 | watch_app_exit → finalize_runs_for_app | succeeded/canceled/failed |
| 手动停止 | stop_app_and_clear → finalize_runs_for_app | stopped（幂等） |
| daemon 重启 | reconcile_runs（启动 + 15s 监护） | 身份重验；消失 → lost |

状态枚举（SPEC §6 canonical）：queued/starting/running/succeeded/failed/
canceled/stopped/timed_out/lost。

## SQLite 操作库

- `adcc/storage/database.py`：标准库 sqlite3，单连接 + RLock，
  schema_migrations 版本化，runs 表（app_id/project_id/kind/status/pid/
  pgid/token/started_at/ended_at/exit_code/log_path/origin/
  correlation_id），索引 app_id/status/started_at。
- 日志内容在文件，库内只存元数据；server 惰性打开、失败降级 None。

## /api/v1 契约

- `GET /api/v1/health` — status/version/schemaVersion/degraded/issues/config
- `GET /api/v1/state` — services/apps/projects/watched/consolePort/version
- `GET|POST /api/v1/projects`；`GET /api/v1/projects/{id}`
- `GET /api/v1/resources`；`GET /api/v1/resources/{id}`
- `POST /api/v1/resources/{id}/start|stop|restart`（app_id 桥）
- `GET /api/v1/runs[?limit=50&appId=&status=]` → `{runs:[public_run], total}`
- `GET /api/v1/runs/{id}`；`GET /api/v1/runs/{id}/logs?tail=300`
- `GET /api/v1/events` — SSE，`data: {type,data,at}\n\n`，15s 心跳

## Windows 平台坑（本里程碑修复）

- cmd `/c` + subprocess argv 再引号化 → 用户命令含引号时损坏；
  改用临时批处理 `console-run-<token>.cmd`（token 进文件名 → CommandLine
  可查，身份 marker 前缀 `console-run-` 与 macOS 同构），退出码经
  `exit /b %errorlevel%` 传播，watch 退出后删除。
- Windows 身份纯函数测试补齐（marker/uid/树）。

## Exit gate 验证

- GUI 照常操作（legacy /api 全保留，189 测试含 hardening 全套）。
- 任务 run 有 durable id/history（/api/v1/runs 契约测试）。
- daemon 重启不伪造成功（reconcile → lost 测试）。
- API 契约测试通过（runs 枚举/投影/CRUD + HTTP 层 11 项 + SSE）。
