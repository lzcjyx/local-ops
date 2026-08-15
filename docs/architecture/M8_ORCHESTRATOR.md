# M8 Git Worktrees、锁与 Orchestrator MVP

## 结论

可声明 DAG 工作流（service/task/agent/gate）由 `WorkflowExecutor`
调度执行：拓扑就绪 → 并行上限 → 锁获取 → 步骤执行 → 失败阻断/策略
重试/取消传播/重启恢复。Git 提供 ADCC 命名空间 worktree 创建与安全
清理。详见 `docs/adr/0008`。

```text
workflow def (config.json)
        │ POST /api/v1/workflows/{id}/runs
        ▼
WorkflowExecutor ── 锁 ──> step 执行
   │  ├─ service: 启动即成功
   │  ├─ task:    等待 ManagedRun 终态
   │  ├─ agent:   等待 AgentRunner 会话
   │  └─ gate:    验证命令 exit 0
   ▼
workflow_runs / workflow_step_runs (SQLite, migration v3)
```

## 关键语义

| 场景 | 行为 |
| --- | --- |
| 失败步骤 | 阻断下游必需步骤（pending）；run failed；continue_on_error 豁免 |
| 重试 | 仅按 retry_policy（max_retries/delay_sec），读最新 DB 计数 |
| 取消 | pending→canceled；running 步骤终止底层（run_ref 即时持久化） |
| 超时 | timeout_sec 到期 → timed_out |
| 重启恢复 | running 步骤底层存活保持，否则 lost；锁 restore；继续调度 |
| 并行写 agent | ADCC 分支命名空间 `adcc/<slug>/<run-id8>`；清理拒绝非 ADCC |

## 接口

- API：`GET|POST /api/v1/workflows`、`GET /api/v1/workflows/{id}`、
  `POST /api/v1/workflows/{id}/runs`、`GET /api/v1/workflow-runs`、
  `GET /api/v1/workflow-runs/{id}`、`POST .../{id}/cancel`、
  `GET /api/v1/git/worktrees`
- CLI：`workflows list` / `workflow run <id>` / `workflow cancel <run-id>`
- MCP：`run_workflow` / `get_workflow_run` / `cancel_workflow_run`

## Exit gate 验证（集成 fixture：agent→test→reviewer→gate）

- 并行写 agent 默认独立 worktree（命名空间 + 清理安全）✓
- 冲突锁步骤不同时运行 ✓
- 失败测试阻断下游必需步骤 ✓
- 重试仅按策略（无策略 retries=0；有策略 retries=1）✓
- 取消停止待办并终止运行中受管工作 ✓
- 重启不发明成功（底层不可验证 → lost）✓
