# ADR 0007: Agent 适配器与会话运行器

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M7

## 背景

SPEC §10 要求把外部 coding agent 作为一等受管执行对象：通用命令
适配器（可配置模板）、会话生命周期、并发限制。不得 import 任何
vendor SDK；首个适配器必须是通用 command（SPEC §3.4）。

## 决策

### 模型（adcc/agents/models.py，纯函数）

- `AgentAdapter`：id/name/type=command/executable/args_template/
  env_template/cwd_template/stdin_mode(none|file|stdin)。
- 模板渲染：`{project_id} {session_id} {project_root} {prompt_file}
  {worktree_path} {run_id}` 预定义变量替换；未知占位符保留原样
  （不猜测）。`render_command/render_env/render_cwd` 纯函数可测。
- `AgentSession`：与 ManagedRun 相同的状态枚举；prompt_ref 统一存
  文本（API 可传 prompt 或 promptFile，启动前读成文本）。

### 运行器（adcc/agents/runner.py）

- 启动：校验 adapter/project → 生成 session（SQLite）→ 并发检查
  （全局 + 每项目上限，来自 config `agent_policy`）→ 超限则 queued
  （`_wake_queued` 在会话结束时按创建时间唤醒排队会话）→ 否则
  `_launch`：写 prompt 文件（data/prompts/{session}.txt）→ 渲染
  argv/env/cwd → 复用 `PlatformAdapter.start_process`（token 身份，
  Windows 批处理文件名/ macOS bash argv 携带 marker）→ DB 更新
  running + watch 线程。
- 身份：`_identity_ok` = pid 存活 + 当前用户 + `console-run-<token>`
  命令行标记（与受管 app 同构，绝不依赖端口）。
- 停止：queued → canceled；running → `terminate_tree` + 等待；
  `_manual_stops` 集合解决 watch/stop 双线程 finalize 竞态（先到先写，
  手动停止恒为 stopped）。
- 对账：daemon 重启 `reconcile()`（身份校验失败 → lost），守护线程
  周期复查；启动新进程后 invalidate CIM 缓存（Windows 可见性）。

### 接口

- API：`GET|POST /api/v1/agents/adapters`、`POST /api/v1/agents/sessions`
  （adapterId/projectId/prompt|promptFile）、`GET /api/v1/agents/sessions`
  、`GET /api/v1/agents/sessions/{id}`、`POST .../{id}/stop`。
- CLI：`adcc agents list` / `adcc agent run --project --adapter
  [--prompt-file|--prompt]` / `adcc agent stop <id>`。
- MCP：`list_agent_sessions`（只读，SPEC §18 工具表补全）。

### 存储

- SQLite `agent_sessions` 表（migration v2，独立于 runs）。
- 配置：config.json `agent_adapters[]` + `agent_policy`
  （global_max=3 / per_project_max=1 默认）。

## 结果

- exit gate 全过：fake command agent 可启动/观察/停止、真实命令
  无需改代码即可运行（模板化）、日志与退出状态出现在 API/CLI、
  并发策略（全局/每项目排队 + 唤醒 + queued cancel）测试通过。
- 14 项新测试；全量 230 项通过。

代价与限制：

- 并发超限是排队而非拒绝；M8 orchestrator 会接管调度语义。
- worktree 关联（worktree_path 变量）预留但 M8 才创建 worktree。
- 无 UI 会话列表（前端 Agent 视图归 M10 导航规划）。

## 未采用方案

- 在 Core 内 import 特定 agent SDK：违反 SPEC §3.4/§10.1。
- 用端口识别 agent 进程：与受管 app 相同原则，只认 token 身份。
- 会话状态写 config.json：与 runs 同理，SQLite 更合适。
