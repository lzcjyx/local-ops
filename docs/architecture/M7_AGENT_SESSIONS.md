# M7 Agent 适配器与会话

## 结论

外部 coding agent 成为一等受管执行对象：通用 command 适配器
（可配置模板）+ 会话生命周期（启动/观察/停止/对账）+ 全局/每项目
并发排队。详见 `docs/adr/0007`。

```text
POST /api/v1/agents/sessions
        │
        v
AgentRunner ── 并发检查 ──> queued（唤醒机制）
        │ 通过
        v
渲染模板 → prompt 文件 → PlatformAdapter.start_process(token 身份)
        │
        v
watch 线程 → finalize（succeeded/failed/stopped/lost）
```

## 适配器模板

| 占位符 | 含义 |
| --- | --- |
| {project_id} {session_id} | 会话标识 |
| {project_root} | 项目根路径 |
| {prompt_file} | 生成的 prompt 文件（stdin_mode=file） |
| {worktree_path} | worktree 路径（M8 提供） |
| {run_id} | 运行标识 |

未知占位符原样保留（不猜测）。

## 接口

- API：adapters CRUD、sessions start/list/get/stop
- CLI：`agents list` / `agent run` / `agent stop`
- MCP：`list_agent_sessions`（只读）
- 状态枚举与 ManagedRun 一致（queued 新增于会话）

## Exit gate 验证

- fake agent（Python 脚本 fixture）启动 → 观察 → 停止 ✓
- 真实用户命令经模板启动（无代码修改）✓
- 日志与退出状态出现在 API/CLI ✓
- 并发策略：全局/每项目上限排队 + 结束时唤醒 + queued cancel ✓
