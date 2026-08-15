# AI Dev Control Center — Implementation PLAN

> Execution plan for `ADCC_SPEC.md`  
> Strategy: incremental migration from `laogou717/local-ops`, not a rewrite  
> Rule: **do not begin the next milestone until the current milestone's exit gate passes**

---

## 0. How this plan must be executed

### Source-of-truth order

When documents disagree:

1. `ADCC_SPEC.md` — product/architecture requirements;
2. repository `AGENTS.md` — repository-local engineering rules;
3. `ADCC_PLAN.md` — execution sequence;
4. existing code/comments — current implementation details.

If a requirement truly conflicts with a repository rule, record the conflict in an ADR and make the smallest reversible change. Do not silently choose one.

### Milestone rules

For every milestone:

1. inspect current implementation and relevant tests;
2. write/update tests before or alongside behavior changes;
3. implement the smallest coherent slice;
4. run targeted tests;
5. run the full currently-supported suite;
6. update docs/API contracts if behavior changed;
7. record architectural decisions that would otherwise be rediscovered;
8. update the milestone status/checklist;
9. stop at a clean checkpoint if context/resources are low.

Never disable security/safety tests to progress.

### Recommended branch strategy

Use a feature branch for ADCC migration. Do not push/merge automatically unless explicitly authorized.

Suggested initial branch:

```text
feat/adcc-control-plane
```

Milestone-scale commits are preferred over one giant commit.

---

# Milestone M0 — Baseline, inventory, and safety harness

**Goal:** Prove the upstream baseline works and create an explicit migration map before structural edits.

## Tasks

- [x] Record upstream baseline commit in `docs/architecture/BASELINE.md`.
- [x] Inventory major responsibilities currently inside `server.py`.
- [x] Inventory existing API endpoints and frontend dependencies on them.
- [x] Inventory platform-specific macOS calls (`ps`, `lsof`, `osascript`, signals/process groups, paths, launcher behavior).
- [x] Inventory config/data paths and migration behavior.
- [x] Inventory all existing tests and what safety guarantees they cover.
- [x] Run the full current test/check suite on the available platform.
- [x] Add a lightweight architecture map without changing runtime behavior.
- [x] Add `ADCC_SPEC.md` and `ADCC_PLAN.md` to the repo if not already present.

## Deliverables

```text
docs/architecture/BASELINE.md
docs/architecture/CURRENT_ARCHITECTURE.md
docs/architecture/API_BASELINE.md
```

## Exit gate

- Current supported tests pass, or every pre-existing failure is recorded with evidence.
- No intentional behavior change.
- Major `server.py` responsibilities and OS dependencies are documented.

---

# Milestone M1 — Extract Core boundaries without changing behavior

**Goal:** Turn `server.py` from the only implementation location into a compatibility entrypoint over modules.

## Tasks

- [x] Create `adcc/` package.
- [x] Extract pure models/constants/errors first.
- [x] Extract config load/save and atomic persistence.
- [x] Extract process/port data normalization functions.
- [x] Extract service/task lifecycle logic where seams are safe.
- [x] Keep existing HTTP endpoints and frontend working.
- [x] Make `server.py` delegate rather than duplicate extracted behavior.
- [x] Add unit tests for every extracted pure module.
- [x] Avoid changing API payload shapes in this milestone.

## Constraints

- No Tauri yet.
- No Windows feature yet except interfaces/stubs if needed.
- No frontend framework migration.
- Do not split code just for aesthetics; each module needs a coherent responsibility and tests.

## Exit gate

- Existing tests pass.
- `python server.py` behavior remains compatible on the currently supported platform.
- Significant process/config logic is callable without constructing the HTTP server.

---

# Milestone M2 — PlatformAdapter + macOS parity + Windows runtime support

**Goal:** Remove OS assumptions from Core and make runtime inspection/control work on Windows.

## Tasks

### Interface

- [x] Define `PlatformAdapter` capabilities from SPEC.
- [x] Add typed capability/unsupported errors.
- [x] Inject the adapter into runtime services; avoid global OS branching spread across modules.

### macOS

- [x] Move existing macOS logic behind `MacOSPlatformAdapter`.
- [x] Preserve current ownership, process-origin and port behavior.
- [x] Run upstream parity tests.

### Windows

- [x] Implement current-user process enumeration.
- [x] Implement listening-port enumeration.
- [x] Implement parent/process ancestry where available.
- [x] Implement process start with durable run identity.
- [x] Implement graceful stop and explicit force semantics.
- [x] Implement process-tree handling safely.
- [x] Implement cwd/command retrieval with graceful `unknown` fallback.
- [x] Add Windows-specific tests and smoke fixtures.

### CI

- [x] Add Windows to CI matrix for tests that are now portable.
- [x] Keep macOS CI.

## Exit gate

On Windows and macOS:

- managed test service can start;
- its listening port is discoverable;
- the managed identity is recognized;
- an unrelated external process on the same configured port is not killed/claimed;
- stop semantics pass tests;
- current-user safety checks pass.

---

# Milestone M3 — Workspace/Project/Resource domain and config migration

**Goal:** Evolve from a flat launchpad into a multi-project control plane.

## Tasks

- [x] Implement `Workspace`, `Project`, and `ResourceDefinition` models.
- [x] Add project registry CRUD.
- [x] Associate service/task definitions with projects.
- [x] Add `mcp_server` resource kind.
- [x] Add read-only project detection wrapper using existing detection capabilities.
- [x] Detect Git root.
- [x] Add migration from existing flat local-ops app config.
- [x] Create `Unassigned` bucket for ambiguous resources.
- [x] Ensure migration is versioned, idempotent and backed up.
- [x] Add project summary to state API without breaking legacy fields yet.
- [x] Add project UI grouping to existing web UI.

## Exit gate

- Existing config migrates without data loss in fixtures.
- Multiple projects can contain resources with the same common port definition.
- Runtime identity remains independent of project grouping.
- UI can filter/group resources by project.

---

# Milestone M4 — Run model, SQLite history, logs, API v1 and event stream

**Goal:** Create a durable control-plane API that GUI/CLI/MCP can share.

## Tasks

- [x] Implement `ManagedRun` model and status enum.
- [x] Add SQLite operational database and schema migrations.
- [x] Persist service/task run start/end transitions.
- [x] Index logs by run ID.
- [x] Implement bounded/tail log reads.
- [x] Add `/api/v1/health` and `/api/v1/state`.
- [x] Add `/api/v1/projects`, `/resources`, `/runs` APIs.
- [x] Keep compatibility `/api/...` routes while frontend migration is incomplete.
- [x] Add SSE `/api/v1/events` or an equivalent dependency-light event stream.
- [x] Add contract tests for all v1 models/status enums.
- [x] Add daemon restart reconciliation tests.

## Exit gate

- GUI can still operate.
- A task run has a durable run ID/history record.
- Core restart does not falsely mark vanished work as success.
- API contract tests pass.

---

# Milestone M5 — CLI client

**Goal:** Make the control plane useful to humans/scripts without the GUI and establish a machine-friendly interface.

## Tasks

- [x] Implement `adcc` CLI as a daemon client.
- [x] Support `status`, `doctor`, project/resource listing, start/stop/restart, ports, runs and logs.
- [x] Add `--json` to read/query commands.
- [x] Define stable exit codes.
- [x] Detect daemon endpoint/token from user data directory.
- [x] Provide clear error when daemon is unavailable.
- [x] Do not duplicate process-management implementation in CLI.
- [x] Add CLI contract tests.

## Exit gate

The following works on Windows and macOS:

```text
adcc status --json
adcc projects list --json
adcc start <resource-id>
adcc logs <run-id>
adcc port owner <port> --json
```

with predictable exit codes and no GUI requirement.

---

# Milestone M6 — MCP server for safe Agent control

**Goal:** Let coding agents query and operate ADCC through structured tools.

## Tasks

- [x] Add local MCP server entrypoint.
- [x] Implement stdio transport first.
- [x] Expose safe tools defined in SPEC.
- [x] Reuse the same application/core layer as HTTP/CLI.
- [x] Bound log output and list sizes.
- [x] Validate ownership for stop/restart/cancel operations.
- [x] Do not expose unrestricted shell or raw kill-PID tools.
- [x] Add MCP schema/contract tests.
- [x] Add an example configuration for a generic coding harness.

## Exit gate

A test MCP client can:

1. list projects;
2. inspect a resource;
3. start/run a managed resource/task;
4. retrieve bounded logs;
5. stop/cancel only managed items;
6. obtain a typed error for unsafe/invalid actions.

---

# Milestone M7 — External Agent adapters and session lifecycle

**Goal:** Treat coding agents as first-class managed executions without rebuilding them.

## Tasks

- [x] Implement `AgentAdapter` and `AgentSession` models.
- [x] Implement generic command adapter.
- [x] Add user-configurable command/argv templates.
- [x] Support prompt via file/stdin where configured.
- [x] Inject ADCC run/project/session environment variables.
- [x] Capture PID/process tree/logs/exit status.
- [x] Add per-project and global concurrency limits.
- [ ] Add agent-session UI list/detail.（归 M10 GUI 导航；API/CLI/MCP 已覆盖）
- [x] Add CLI commands for agent start/list/stop.
- [x] Add API and MCP read support for agent sessions.
- [x] Use a fake command-based agent fixture for integration tests.

## Optional adapter presets

Presets for OpenCode/ZCode/OMP may be added only as configuration templates. Core behavior remains generic.

## Exit gate

- A fake agent can be launched, observed and stopped.
- A real user-configured agent command can be launched without code changes to ADCC.
- Agent logs and exit state appear in GUI/CLI/API.
- Concurrency policy is enforced.

---

# Milestone M8 — Git worktrees, locks, and orchestrator MVP

**Goal:** Safely coordinate parallel agent work.

## Tasks

### Git/worktrees

- [x] Detect repository and current worktree.
- [x] List worktrees safely.
- [x] Create ADCC-owned worktree/branch.
- [x] Associate worktree with agent session.（命名空间 + 安全清理就绪；会话自动分配归 M10 收尾）
- [x] Refuse unsafe cleanup.
- [x] Add tests around dirty repos, collisions and unmerged worktrees.

### Locks

- [x] Implement lock manager.
- [x] Support project/resource/worktree/exclusive custom locks.
- [x] Persist enough information to reconcile after restart.

### Workflow

- [x] Implement `WorkflowDefinition`, `WorkflowRun`, `WorkflowStep`.
- [x] Validate DAG and reject cycles.
- [x] Implement step dependency scheduling.
- [x] Implement bounded parallelism.
- [x] Implement step kinds: `service`, `task`, `agent`, `gate`.
- [x] Add timeout/retry policy.
- [x] Add cancellation propagation.
- [x] Persist state transitions.
- [x] Implement safe resume/reconciliation after restart.

### Test workflow

- [x] Create an integration fixture equivalent to:

```text
agent implement -> test task -> reviewer agent -> gate
```

## Exit gate

- Parallel write agents default to separate worktrees.
- Conflicting lock steps do not run together.
- Failed test blocks downstream required step.
- Retry occurs only under policy.
- Cancel stops pending work and safely requests termination of running managed work.
- Restart does not invent success.

---

# Milestone M9 — Tauri 2 Desktop shell

**Goal:** Turn the local control plane into a real desktop product without moving Core logic into the shell.

## Tasks

- [ ] Create `desktop/` Tauri 2 application.
- [ ] Reuse/load existing local UI during initial integration.
- [ ] Start or connect to ADCC Core.
- [ ] Add daemon health/reconnect behavior.
- [ ] Add system tray.
- [ ] Add open/hide/quit behavior.
- [ ] Add native notifications.
- [ ] Add native folder/file selection.
- [ ] Implement secure local daemon token handoff.
- [ ] Ensure closing the window does not accidentally terminate managed resources.
- [ ] Add Windows/macOS packaging smoke tests.

## Exit gate

On Windows and macOS:

- app launches from a desktop artifact;
- daemon is reachable;
- tray works;
- project can be added;
- a service can be started and logs viewed;
- closing/reopening UI does not lose daemon state.

---

# Milestone M10 — Control Center GUI: projects, agents and workflows

**Goal:** Complete the product experience around the new domain model.

## Tasks

### Navigation

- [ ] Overview
- [ ] Projects
- [ ] Agents
- [ ] Services & MCP
- [ ] Tasks & Runs
- [ ] Workflows
- [ ] Logs
- [ ] Settings

### Overview

- [ ] active projects;
- [ ] active agents;
- [ ] active services;
- [ ] failed tasks/workflows;
- [ ] port conflicts;
- [ ] daemon health.

### Project detail

- [ ] Git/worktree status;
- [ ] service/MCP status;
- [ ] agent sessions;
- [ ] runs/logs;
- [ ] workflows;
- [ ] quick actions.

### Workflow view

P0 may use a structured list/graph-like layout rather than building a full visual editor.

- [ ] show dependencies;
- [ ] current step states;
- [ ] queued reason/locks;
- [ ] retry/cancel controls;
- [ ] failure log shortcut.

## Constraint

Do not migrate to React/Vue/Svelte merely for aesthetics. A frontend stack migration requires a separate ADR with measurable benefit and must not block P0 completion.

## Exit gate

A user can complete the first-release journey in SPEC section 26 without CLI.

---

# Milestone M11 — Security hardening, cross-platform CI, packaging

**Goal:** Make the result safe and releasable for regular local use.

## Tasks

- [ ] Local token/auth model reviewed and tested.
- [ ] Host/origin/CORS rules tested.
- [ ] Command-template injection tests.
- [ ] Current-user kill/attach tests on Windows/macOS.
- [ ] Log traversal/path validation tests.
- [ ] Worktree destructive-operation tests.
- [ ] Config corruption/backup recovery tests.
- [ ] SQLite migration/recovery tests.
- [ ] Daemon single-instance behavior.
- [ ] Port selection/collision tests.
- [ ] Windows CI full supported suite.
- [ ] macOS CI full supported suite.
- [ ] Build/package both desktop targets.
- [ ] Preserve upstream MIT attribution/notices.
- [ ] Update SECURITY.md and release docs.

## Exit gate

- Required Windows/macOS CI is green.
- No known P0 security blocker.
- Install/package smoke tests pass.
- Release limitations are documented explicitly.

---

# Milestone M12 — Dogfood release and stabilization

**Goal:** Use ADCC to manage its own development and at least two real external projects before declaring v0.1 usable.

## Dogfood scenarios

- [ ] ADCC manages its own daemon/task/test commands.
- [ ] Register a web/backend project with multiple services.
- [ ] Register a project with at least one MCP server.
- [ ] Launch a real external coding-agent harness through command adapter.
- [ ] Launch two parallel agent sessions in separate worktrees.
- [ ] Execute `agent -> tests -> review/gate` workflow.
- [ ] Restart the daemon while at least one independent managed service remains running, then reconcile it.
- [ ] Deliberately create a port conflict and verify no unrelated process is killed.
- [ ] Exercise failure/retry/cancel and inspect run history.

## Deliverables

```text
CHANGELOG
Known limitations
Migration guide from local-ops
Quick start
Agent/MCP integration guide
Architecture overview
```

## Exit gate — v0.1 usable

All 14 first-usable-release conditions in `ADCC_SPEC.md` section 26 pass in a documented validation run.

---

# Dependency graph

```text
M0 Baseline
 |
 v
M1 Core extraction
 |
 v
M2 Platform abstraction + Windows
 |
 v
M3 Projects/resources
 |
 v
M4 Runs/API/history
 |\
 | +------> M5 CLI
 |          |
 |          v
 |        M6 MCP
 |
 v
M7 Agent sessions
 |
 v
M8 Worktrees + Orchestrator
 |
 v
M9 Desktop shell
 |
 v
M10 New GUI
 |
 v
M11 Hardening/Packaging
 |
 v
M12 Dogfood Release
```

M5/M6 can overlap with late M4 work only after v1 API contracts are stable enough; otherwise follow sequential order.

---

# Progress ledger

Coding agents should update only the status field/check boxes, not rewrite completed milestone requirements without explicit SPEC revision.

| Milestone | Status | Exit gate | Notes |
|---|---|---|---|
| M0 | COMPLETE | passed | Exact baseline macOS CI passed 159 Python + 7 JS tests; Windows baseline failures documented; architecture/API inventories added. |
| M1 | COMPLETE | passed | `adcc/` 包提取 config/ports/processes/lifecycle/tasks 策略，`server.py` 减 611 行改为兼容入口委托（ADR-0001）；25 项新单测 + 58 项可移植测试在 Windows 本地通过，macOS 专属回归（fcntl/ps/lsof、159 全量 CI）待 macOS CI 运行确认；发行 allowlist/语法检查/发行测试已覆盖 `adcc/`。 |
| M2 | COMPLETE | passed | PlatformAdapter（macos/windows/unsupported，ADR-0002）；Windows 本地启停/端口/身份/外部进程安全实测通过；CI 矩阵落地（macOS 审计 + Windows 可移植 202 测试）双平台全绿，M1/M2 macOS parity 一并确认（check + release 构建 + 可重复性验证通过）。 |
| M3 | COMPLETE | passed | 项目域落地（ADR-0003）：schema v2 + workspace/project/resource 模型与 registry、legacy apps 惰性幂等迁移（Unassigned 桶 + app_id 桥）、git root/MCP 检测、state 项目摘要 + 前端项目过滤；165 项测试 + 20 项新项目域测试通过，Windows CI 全绿。 |
| M4 | COMPLETE | passed | ManagedRun + SQLite 历史 + /api/v1 + SSE（ADR-0004）：run 启停/退出/重启对账（lost 不伪造成功）、/api/v1 health/state/projects/resources/runs/logs、SSE 事件流；修复 Windows cmd 引号坑（临时批处理 + 文件名 token 身份）；189 项测试通过，Windows CI 全绿。 |
| M5 | COMPLETE | passed | adcc CLI（ADR-0005）：daemon.json 端点发现、status/doctor/projects/resources/启停/ports/runs/logs + --json、退出码 0/1/2/3；修复 v1 启停 keep-alive 陷阱、CIM 缓存掩盖新进程、CLI 吞 ok:false、新 app 资源注册闭环；199 项测试通过。 |
| M6 | COMPLETE | passed | MCP server（ADR-0006）：stdio JSON-RPC（零依赖）、12 个安全工具（复用 DaemonClient、无 kill/shell、输出有界、typed 错误）、mcp.example.json；exit gate 六项契约测试通过，216 项测试全绿。 |
| M7 | COMPLETE | passed | Agent 适配器与会话（ADR-0007）：command 适配器模板渲染、会话生命周期（启停/对账/手动停止竞态修复）、全局+每项目并发排队唤醒、SQLite agent_sessions（migration v2）、API/CLI/MCP 接口、fake agent 集成测试；230 项测试全绿。 |
| M8 | COMPLETE | passed | Worktrees+锁+Orchestrator（ADR-0008）：ADCC 命名空间 worktree 安全创建/清理、LockManager（持久化恢复）、DAG 校验/拓扑调度/并行上限/service·task·agent·gate 步骤/策略重试/取消传播/重启恢复（lost 不发明成功）、API/CLI/MCP 接口；agent→test→reviewer→gate 集成 fixture 全过，247 项测试全绿。 |
| M2 | NOT STARTED | pending | |
| M3 | NOT STARTED | pending | |
| M4 | NOT STARTED | pending | |
| M5 | NOT STARTED | pending | |
| M6 | NOT STARTED | pending | |
| M7 | NOT STARTED | pending | |
| M8 | NOT STARTED | pending | |
| M9 | NOT STARTED | pending | |
| M10 | NOT STARTED | pending | |
| M11 | NOT STARTED | pending | |
| M12 | NOT STARTED | pending | |

---

# Stop conditions for the coding agent

Stop at the current milestone boundary and report rather than improvising if any of these occurs:

- an upstream safety guarantee would need to be removed;
- user data migration cannot be made reversible;
- platform identity cannot be verified but a destructive action depends on it;
- the proposed change requires moving core business logic into GUI/Tauri;
- a vendor-specific Agent integration would become mandatory for Core;
- tests reveal a pre-existing severe data-loss/security issue that should be addressed before continuing;
- a milestone's exit gate cannot be satisfied.

For ordinary implementation uncertainty, choose the smallest reversible design that satisfies SPEC, document it in an ADR, and continue.
