# ADR 0008: Git worktrees、锁与 Orchestrator MVP

- 状态：Accepted
- 日期：2026-08-15
- Milestone：M8

## 背景

SPEC §11/§12 要求并行写 agent 默认隔离 worktree、冲突步骤不并行、
可声明 DAG 工作流（service/task/agent/gate）、失败阻断、策略化重试、
取消传播、重启不发明成功。

## 决策

### Git（adcc/git/repository.py）

- 只读命令（rev-parse/worktree list --porcelain）+ ADCC 命名空间内创建/
  删除。分支格式 `adcc/<slug>/<run-id8>`（slug 消毒、碰撞安全）。
- 清理只允许 ADCC 分支（`remove_worktree` 拒绝非 ADCC 分支；
  未合并且非 force 拒绝删除）。绝不触碰用户分支。

### 锁（adcc/orchestrator/locks.py）

- `LockManager`：`try_acquire` 原子获取全部键（`project:write`、
  `port:N`、自定义）；步骤级持有 → 完成/取消释放；`restore` 从持久化
  JSON（workflow_runs.locks_held）恢复，重启语义不变。

### 编排（adcc/orchestrator/models.py + executor.py）

- 定义存 config.json（`workflows[]`）；run/step-run 存 SQLite
  （migration v3）。`validate_dag`（Kahn 拓扑 + 环/悬空引用拒绝）。
- `WorkflowExecutor`：
  - 调度：就绪步骤（needs 全 succeeded）→ 全局并行上限 4 → 锁获取
    → 启动；完成/失败/超时/取消后继续推进。
  - 步骤执行（注入 hooks，server 提供真实实现）：
    - service：启动成功即 succeeded；
    - task：启动并等待其 ManagedRun 终态（succeeded/failed/…）；
    - agent：启动 AgentRunner 会话并等待终态（会话排队时等待）；
    - gate：执行验证命令（cwd=项目根），退出码 0 成功。
  - 失败：continue_on_error 不阻断整体；否则 run failed，下游保持
    pending（绝不伪成功）。
  - 重试：仅按 `retry_policy`（读最新 DB retries，修无限重试 bug）。
  - 取消：pending → canceled；running 步骤终止底层（run_ref 即时
    持久化，session/resource 启动窗口内自查 cancel 并主动停止）。
  - 恢复：daemon 重启对 running run 重验——步骤 run_ref 底层存活则
    保持，否则 lost；锁 restore；继续调度就绪步骤。

## 结果

- exit gate 六项全部通过（集成 fixture：agent→test→reviewer→gate）：
  并行写 agent 隔离命名空间、冲突锁串行、失败阻断下游、重试仅按策略、
  取消终止待办与运行中受管工作、重启不发明成功。
- 17 项新测试；全量 247 项通过。

代价与限制：

- worktree 创建/关联尚未接入 agent 会话（`{worktree_path}` 变量预留）；
  orchestrator 不自动分配 worktree。
- 无可视化 DAG 编辑器（M10 提供结构化列表视图）。
- gate 无人工确认模式（仅验证命令）；人工 gate 属后续。

## 未采用方案

- scheduler 独立于 executor 拆分：M8 规模下合并更内聚。
- 步骤状态写 config.json：与 runs/sessions 同理走 SQLite。
- 自动清理 worktree：破坏性操作需显式（SPEC §11.3）。
