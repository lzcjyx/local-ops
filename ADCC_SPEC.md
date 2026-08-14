# AI Dev Control Center — SPEC

> Working name: **AI Dev Control Center (ADCC)**  
> Upstream baseline: `laogou717/local-ops`  
> Baseline branch: `main`  
> Baseline commit inspected while drafting this spec: `a5c3adae1f1fa0bd9f0ac7b090ec422e285d0c0f`  
> Status: implementation specification v0.1

---

## 1. Product definition

ADCC is a **local-first development control plane for multi-project, multi-agent software development**.

It combines two concerns while keeping their implementation boundaries separate:

1. **Local runtime control**, derived from local-ops:
   - processes;
   - listening ports;
   - long-running services;
   - finite tasks/builds/tests;
   - logs;
   - process ownership and origin attribution;
   - safe start/stop/restart;
   - project detection and diagnostics.

2. **Agent orchestration**:
   - projects and workspaces;
   - external coding-agent sessions;
   - MCP servers;
   - Git worktrees;
   - milestones and workflows;
   - dependency/lock-aware execution;
   - automated test/review gates;
   - run history and failure recovery.

ADCC is **not itself a foundation model or a replacement for OpenCode, ZCode, Codex, Claude Code, OMP, or other coding-agent harnesses**. It orchestrates and observes those tools through adapters.

The core product should feel like a local "mission control" for AI-assisted development:

```text
Human
  |
  v
Desktop GUI -------------------------+
  |                                  |
  v                                  |
ADCC Core / local daemon <--- CLI ---+
  ^          ^             ^
  |          |             |
 MCP      Orchestrator   REST/SSE
  ^          |
  |          +--> Agent adapters --> OpenCode / ZCode / OMP / custom commands
  |          +--> Git worktrees
  |          +--> Services / Tasks / Tests
  |
Coding agents
```

---

## 2. Why this exists

Modern local development increasingly involves several simultaneous projects and many background processes:

- frontend dev servers;
- API servers;
- model or RAG services;
- Unity/Blender MCP servers;
- build/test jobs;
- browser automation servers;
- multiple coding agents;
- multiple Git worktrees.

The user needs one authoritative place to answer:

- Which projects are active?
- Which services belong to each project?
- Who started this process?
- Who owns this port?
- Which agents are currently working?
- Which branch/worktree is each agent changing?
- Which milestone/task is an agent executing?
- What failed, and what are the relevant logs?
- Is it safe to stop/restart a service?
- Can another agent work in parallel without colliding with this one?

---

## 3. Product principles

### 3.1 Local-first

ADCC binds management APIs to loopback by default. It must not become a public remote-admin panel in the first release.

### 3.2 Core and UI are independent

The Desktop GUI is a client of ADCC Core, not the owner of runtime state.

Closing the main window must not terminate managed services or active agent sessions unless explicitly configured.

### 3.3 Preserve local-ops safety semantics

Never equate "same port" with "same managed process".

Process ownership must continue to rely on explicit run identity plus OS/process identity checks. External processes must never be killed merely because they occupy a configured port.

### 3.4 Orchestrate existing agents rather than replacing them

Agent integrations use adapters. The first implementation MUST support generic command-based adapters so any harness can be integrated without ADCC importing a provider-specific SDK.

### 3.5 Milestone-driven, test-gated development

Both ADCC itself and projects controlled by ADCC should support explicit workflow gates. A downstream step must not be marked successful when its required verification failed.

### 3.6 Reversible architecture changes

During migration from local-ops, maintain compatibility shims until equivalent new modules are tested. Avoid a big-bang rewrite.

### 3.7 Cross-platform by architecture

Windows and macOS are first-class targets. Linux is a supported architectural target and should be added after Windows/macOS parity.

---

## 4. Scope

## 4.1 P0 — required for first usable release

### Runtime control

- list current-user listening services;
- list current-user processes relevant to managed projects;
- CPU/memory/uptime where available;
- start/stop/restart managed services safely;
- run/cancel finite tasks;
- capture and stream logs;
- port ownership lookup;
- process-origin attribution;
- persisted service/task definitions;
- health/diagnostic checks;
- Windows and macOS adapters.

### Project control

- Workspace -> Project -> Resource hierarchy;
- project root and optional Git repository association;
- service/task/MCP definitions grouped by project;
- per-project environment variables and command templates;
- project status summary.

### Agent control

- register external agent adapter definitions;
- start an agent session in a project/worktree;
- capture PID/process tree, logs, start/end time and exit status;
- associate an agent session with a milestone/task;
- stop a session safely;
- concurrency limits;
- resource locks;
- generic command adapter.

### Git/worktree control

- detect Git repositories;
- list branches/worktrees;
- create a dedicated worktree for an agent run;
- prevent two write agents from silently sharing the same mutable worktree unless explicitly allowed;
- clean up ADCC-owned worktrees only after safety checks.

### Orchestration

- declarative workflow definition;
- DAG dependencies;
- sequential and bounded-parallel execution;
- step types for service, task, agent and manual/verification gate;
- retries with limits;
- timeout handling;
- cancellation propagation;
- run history;
- resume from a safe checkpoint when possible.

### Interfaces

- Desktop GUI using Tauri 2 shell;
- local web UI assets hosted/loaded locally;
- CLI with JSON output mode;
- local MCP server exposing a safe subset of control-plane tools;
- versioned local HTTP API.

### Desktop behavior

- system tray;
- minimize/close-to-tray option;
- launch/open daemon;
- native project-folder picker;
- native notifications;
- daemon health indicator;
- no requirement to keep a browser tab open.

## 4.2 P1 — after first usable release

- Linux platform adapter;
- richer workflow editor/visual DAG;
- reusable project templates;
- Agent capability discovery;
- cost/token metadata supplied by adapters;
- optional per-project secrets integration using OS credential stores;
- local plugin/adapter SDK;
- import/export project manifests;
- remote read-only dashboard over an explicitly enabled secure transport.

## 4.3 Explicitly out of scope for MVP

- building a new LLM/coding model;
- direct dependence on one model provider;
- team/multi-user cloud control plane;
- public internet exposure;
- replacing a full IDE;
- Kubernetes or distributed cluster orchestration;
- arbitrary remote shell execution;
- autonomous Git push/merge by default;
- rewriting the entire upstream codebase in Rust;
- requiring Docker for normal use;
- requiring a JS framework migration before runtime features work.

---

## 5. Technical strategy

## 5.1 Migration strategy

The existing local-ops behavior is the compatibility baseline. The project currently has a large Python `server.py`, native ES-module frontend, and an existing Python/JS test suite.

Migration MUST follow a strangler pattern:

1. establish baseline tests;
2. extract pure/domain logic;
3. introduce interfaces around OS-specific behavior;
4. route existing server behavior through those interfaces;
5. add Windows implementation;
6. add new project/agent/orchestrator domains;
7. add new versioned API;
8. add CLI/MCP/Desktop clients;
9. only then remove obsolete compatibility paths.

A full rewrite before behavior parity is prohibited.

## 5.2 Target repository shape

The exact names may evolve, but responsibilities MUST converge toward the following boundaries:

```text
/
├─ server.py                    # temporary compatibility entrypoint
├─ adcc/
│  ├─ __init__.py
│  ├─ core/
│  │  ├─ models.py              # domain models / identifiers
│  │  ├─ errors.py
│  │  ├─ events.py
│  │  └─ clock.py
│  ├─ runtime/
│  │  ├─ services.py
│  │  ├─ tasks.py
│  │  ├─ processes.py
│  │  ├─ ports.py
│  │  └─ logs.py
│  ├─ projects/
│  │  ├─ registry.py
│  │  ├─ detection.py
│  │  └─ manifests.py
│  ├─ agents/
│  │  ├─ models.py
│  │  ├─ registry.py
│  │  ├─ runner.py
│  │  └─ adapters/
│  │     ├─ base.py
│  │     └─ command.py
│  ├─ git/
│  │  ├─ repository.py
│  │  └─ worktrees.py
│  ├─ orchestrator/
│  │  ├─ models.py
│  │  ├─ scheduler.py
│  │  ├─ executor.py
│  │  ├─ locks.py
│  │  └─ recovery.py
│  ├─ platform/
│  │  ├─ base.py
│  │  ├─ macos.py
│  │  ├─ windows.py
│  │  └─ linux.py             # may initially be unsupported stub
│  ├─ storage/
│  │  ├─ config.py
│  │  ├─ database.py
│  │  └─ migrations.py
│  ├─ api/
│  │  ├─ server.py
│  │  ├─ routes_v1.py
│  │  └─ auth.py
│  ├─ cli/
│  │  └─ main.py
│  └─ mcp/
│     └─ server.py
├─ static/                      # retained web frontend during migration
├─ desktop/
│  ├─ src-tauri/
│  └─ ...                       # desktop-local UI integration
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ contract/
│  └─ platform/
└─ docs/
   ├─ adr/
   └─ architecture/
```

Do not mechanically move every file at once. Extract one responsibility at a time under tests.

---

## 6. Core domain model

Identifiers MUST be stable opaque IDs, not display names, PIDs, paths or ports.

### Workspace

A user-level grouping of projects.

Minimum fields:

```text
id
name
project_ids[]
created_at
updated_at
```

### Project

```text
id
workspace_id
name
root_path
repo_path?             # Git root if present
active_worktree?
default_agent_adapter?
environment{}
tags[]
created_at
updated_at
```

### ResourceDefinition

Common definition for executable project resources.

```text
id
project_id
name
kind                   # service | task | mcp_server
command
cwd
environment{}
port?
health_check?
locks[]
```

### ManagedRun

Represents one execution instance.

```text
id
resource_id?
project_id
kind                   # service | task | agent | workflow_step
status
pid?
process_group_id?
run_token
started_at
ended_at?
exit_code?
log_path
origin
correlation_id?
```

Canonical run status set:

```text
queued
starting
running
succeeded
failed
canceled
stopped
timed_out
lost
```

### AgentAdapter

```text
id
name
type                   # command initially
executable
args_template[]
env_template{}
capabilities[]
stdin_mode
supports_noninteractive
```

### AgentSession

```text
id
project_id
adapter_id
workflow_run_id?
workflow_step_id?
worktree_id?
prompt_ref?
status
pid?
started_at
ended_at?
exit_code?
log_path
```

The prompt may be persisted only according to explicit privacy/storage settings. Logs may contain sensitive data and must be treated accordingly.

### Worktree

```text
id
project_id
path
branch
base_ref
owned_by_adcc
owner_session_id?
status
created_at
```

### WorkflowDefinition

```text
id
project_id
name
version
steps{}
created_at
updated_at
```

### WorkflowStep

```text
id
kind                   # service | task | agent | gate | command
needs[]
config{}
timeout_sec?
retry_policy?
locks[]
continue_on_error=false
```

### WorkflowRun

```text
id
workflow_id
workflow_version
project_id
status
started_at
ended_at?
step_runs[]
correlation_id
```

---

## 7. Platform abstraction

OS-specific logic MUST sit behind explicit interfaces.

Required capabilities:

```python
class PlatformAdapter(Protocol):
    def list_processes(...): ...
    def list_listeners(...): ...
    def get_process(...): ...
    def get_process_tree(...): ...
    def start_process(...): ...
    def terminate_process(...): ...
    def terminate_process_tree(...): ...
    def process_belongs_to_current_user(...): ...
    def process_cwd(...): ...
    def choose_directory(...): ...
    def choose_file(...): ...
    def open_url(...): ...
    def reveal_path(...): ...
```

### macOS adapter

May reuse the existing implementation based on system tools and process metadata. Behavior must remain compatible before internal cleanup.

### Windows adapter

Should prefer built-in Windows facilities available from a standard Python installation plus OS tools. No mandatory external Python dependency is required for the first implementation.

It must support at minimum:

- TCP listening-port enumeration;
- PID -> command/cwd/parent/process ownership data where the OS makes it available;
- current-user verification;
- process-tree termination semantics;
- detached/background process launch;
- native directory/file selection through the desktop shell when available;
- fallback behavior when optional metadata cannot be retrieved.

Feature degradation MUST be explicit rather than fabricated. Unknown is better than incorrect data.

### Linux adapter

Architecture placeholder during P0. It must fail with a typed unsupported-platform/capability error, never with random import failures.

---

## 8. Runtime ownership and process safety

This section is normative.

### 8.1 Managed identity

A managed process is not identified by port alone.

The runtime should combine as many of the following as applicable:

- ADCC-generated `run_token`;
- PID;
- process creation/start time;
- parent/process group/job object identity;
- current-user identity;
- expected cwd;
- process ancestry;
- configured executable/command fingerprint.

### 8.2 Stop semantics

Default stop:

1. validate ownership;
2. request graceful termination;
3. wait bounded time;
4. report still-running state;
5. require explicit force action before hard termination, except where a configured policy allows escalation.

### 8.3 Port conflicts

Port occupancy is a diagnostic signal, not ownership proof.

If a configured port is occupied by another process:

- block start unless an explicit alternative-port policy applies;
- show owner metadata;
- never silently kill the owner;
- never attach automatically without satisfying attach identity rules.

---

## 9. Project and workspace behavior

### 9.1 Project registration

A user can add a project by selecting a root directory.

ADCC performs read-only detection for:

- Git root;
- package/build manifests;
- common run/test/build commands;
- likely MCP servers;
- existing local-ops service definitions that point into the project.

Detection may propose configuration but MUST NOT execute project code or install dependencies.

### 9.2 Import from upstream local-ops config

Existing app/task definitions must be migratable.

Migration rules:

- preserve IDs where feasible;
- preserve command/cwd/port/kind;
- infer project from cwd when unambiguous;
- put unassigned resources into an "Unassigned" project/group instead of dropping them;
- keep a backup of the old config;
- migrations are versioned and idempotent.

---

## 10. Agent integration

## 10.1 Generic command adapter is mandatory

The first adapter MUST be able to launch tools such as OpenCode, ZCode, OMP or a user-defined harness through a configurable command template.

Example conceptual config:

```json
{
  "id": "opencode-default",
  "name": "OpenCode",
  "type": "command",
  "executable": "opencode",
  "argsTemplate": ["run", "--prompt-file", "{prompt_file}"],
  "cwd": "{worktree_path}",
  "environment": {
    "ADCC_PROJECT_ID": "{project_id}",
    "ADCC_RUN_ID": "{run_id}"
  }
}
```

The exact command is user-configurable. ADCC must not assume every harness exposes the same flags.

## 10.2 Adapter contract

An adapter reports:

- how to launch;
- whether noninteractive execution is supported;
- capabilities supplied by configuration;
- process/session lifecycle;
- optional machine-readable result file if supported.

P0 does not require deep vendor APIs.

## 10.3 Prompt handling

Prompt sources may be:

- literal text;
- file path;
- generated workflow context;
- project SPEC/PLAN reference.

Avoid placing very large prompts directly in process command lines. Prefer temporary files or stdin where the adapter supports them.

## 10.4 Resource locks

Agent sessions can require locks such as:

```text
git-worktree:<id>:write
unity-project:<path>:exclusive
blender-instance:<id>:exclusive
port:<port>
resource:<resource-id>
```

Scheduler MUST not start conflicting steps concurrently.

---

## 11. Git and worktree rules

### 11.1 Parallel write agents

Default policy:

- one writable agent session per worktree;
- parallel agents get separate ADCC-owned worktrees/branches;
- explicit user override is possible but must be marked unsafe.

### 11.2 Worktree creation

ADCC may create:

```text
<managed-root>/<project>/<run-id>/
```

with a branch such as:

```text
adcc/<workflow-or-task>/<short-run-id>
```

Names must be sanitized and collision-safe.

### 11.3 Destructive Git operations

P0 must NOT automatically:

- force push;
- delete unmerged user branches;
- reset user worktrees;
- merge to main/default branch.

Cleanup only applies to ADCC-owned resources after safety validation.

---

## 12. Orchestrator

The orchestrator is a deterministic scheduler/executor around external tools, not an unconstrained autonomous loop.

### 12.1 Workflow example

```yaml
name: unity-feature
steps:
  ensure_mcp:
    kind: service
    resource: unity-mcp

  implement:
    kind: agent
    adapter: opencode-default
    needs: [ensure_mcp]
    worktree: isolated
    locks: ["unity-project:exclusive"]

  tests:
    kind: task
    resource: unity-editmode-tests
    needs: [implement]

  review:
    kind: agent
    adapter: reviewer-agent
    needs: [tests]

  gate:
    kind: gate
    needs: [review]
```

YAML support is optional if it introduces a mandatory dependency. JSON is acceptable for P0; the UI can author the model directly.

### 12.2 Scheduler requirements

- topological dependency validation;
- cycle rejection;
- global concurrency limit;
- per-project concurrency limit;
- lock acquisition before launch;
- fair-enough queueing;
- explicit queued reason;
- cancellation;
- retries only according to policy;
- timeout;
- state persisted before and after significant transitions.

### 12.3 Recovery

On daemon restart:

- reconcile recorded running sessions with OS reality;
- mark unverifiable vanished processes as `lost`, not `succeeded`;
- reattach only when identity checks pass;
- workflows may resume only from a safe persisted boundary;
- non-idempotent steps must not be blindly rerun.

---

## 13. Storage

Use two storage classes.

### 13.1 Human/configuration state

Versioned JSON for:

- settings;
- workspaces/projects;
- resource definitions;
- agent adapter definitions;
- user preferences.

Requirements:

- atomic writes;
- backup of last known-good state;
- strict schema version;
- migration chain;
- permissions restricted to current user where supported.

### 13.2 Operational/history state

Use Python standard-library `sqlite3` for:

- managed runs;
- agent sessions;
- workflow runs;
- step transitions;
- worktree ownership metadata;
- event history/index metadata.

Do not put unbounded logs inside SQLite. Store logs as files and index them by run ID.

---

## 14. Logging and observability

Every executable run has a stable run ID.

Log metadata:

```text
run_id
project_id
resource_id?
agent_session_id?
workflow_run_id?
step_id?
started_at
ended_at?
exit_code?
```

Requirements:

- stdout/stderr captured with timestamps when feasible;
- tail endpoint;
- bounded read chunks;
- log rotation/retention configuration;
- no unbounded API response;
- structured core diagnostic log;
- sensitive values from configured secret fields should be redacted where possible.

---

## 15. HTTP API

Introduce `/api/v1` while temporarily keeping existing `/api/...` behavior as a compatibility surface.

Representative endpoints:

```text
GET    /api/v1/health
GET    /api/v1/state
GET    /api/v1/projects
POST   /api/v1/projects
GET    /api/v1/projects/{id}
PUT    /api/v1/projects/{id}

GET    /api/v1/resources
POST   /api/v1/resources/{id}/start
POST   /api/v1/resources/{id}/stop
POST   /api/v1/resources/{id}/restart

GET    /api/v1/runs
GET    /api/v1/runs/{id}
GET    /api/v1/runs/{id}/logs

GET    /api/v1/agents/adapters
POST   /api/v1/agents/sessions
GET    /api/v1/agents/sessions
POST   /api/v1/agents/sessions/{id}/stop

GET    /api/v1/workflows
POST   /api/v1/workflows/{id}/runs
POST   /api/v1/workflow-runs/{id}/cancel

GET    /api/v1/git/worktrees

GET    /api/v1/events
```

`/api/v1/events` may use Server-Sent Events (SSE) so the core can remain simple and dependency-light. Polling fallback remains supported.

API models must be contract-tested.

---

## 16. Local security model

### 16.1 Network

- bind loopback only by default;
- reject unexpected Host headers;
- mutating requests must enforce local authentication/CSRF defenses appropriate to the client;
- no wildcard CORS;
- remote binding requires a future explicit feature, not a hidden environment switch.

### 16.2 Local API token

CLI/MCP/Desktop may use a daemon-generated local token stored in a user-only file.

Requirements:

- generated with a cryptographically secure source;
- not printed to ordinary logs;
- file permissions restricted where supported;
- rotatable;
- browser/web compatibility flow must not expose it to arbitrary origins.

### 16.3 Command execution

- user-saved commands are powerful and must be treated as code execution;
- API payloads must not be interpolated into shell strings without explicit escaping/template semantics;
- prefer argv execution where possible;
- adapter templates distinguish literal args from shell mode;
- shell mode is explicit and visually marked.

### 16.4 Kill/attach operations

Validate current-user ownership and identity before destructive process actions.

---

## 17. CLI

Working binary name: `adcc`.

P0 commands:

```text
adcc status [--json]
adcc doctor [--json]
adcc projects list [--json]
adcc project show <id> [--json]
adcc resources list [--project <id>] [--json]
adcc start <resource-id>
adcc stop <resource-id>
adcc restart <resource-id>
adcc ports [--json]
adcc port owner <port> [--json]
adcc runs list [--json]
adcc logs <run-or-resource-id> [--follow]
adcc agents list [--json]
adcc agent run --project <id> --adapter <id> --prompt-file <path>
adcc agent stop <session-id>
adcc workflows list [--json]
adcc workflow run <workflow-id> [--json]
adcc workflow cancel <run-id>
```

CLI is a client of the daemon API. It should not contain a second independent implementation of runtime rules.

Exit codes must be documented and stable enough for scripting.

---

## 18. MCP server

The MCP server is an Agent-facing adapter to the same Core API/application layer.

P0 tools:

```text
list_projects
get_project
list_resources
get_resource_status
start_resource
stop_resource
restart_resource
list_runs
get_run
get_run_logs
get_port_owner
list_agent_sessions
run_task
run_workflow
get_workflow_run
cancel_workflow_run
```

Safety rules:

- do not expose arbitrary `kill(pid)` as the default high-level tool;
- do not expose unrestricted shell execution;
- write/destructive tools validate managed-resource identity;
- output is structured and bounded;
- logs use tail/limit parameters;
- tool errors are typed and actionable.

The first transport should favor local stdio for broad coding-harness compatibility. Other local transports can be added later.

---

## 19. Desktop application

### 19.1 Architecture

Use **Tauri 2** as the desktop shell.

P0 should reuse the existing local web UI concepts and assets rather than blocking on a full frontend framework rewrite.

Desktop responsibilities:

- launch/connect to ADCC Core;
- health-check Core;
- tray lifecycle;
- single-instance/open-window behavior where practical;
- native folder/file picker;
- notifications;
- package/update integration later;
- secure local token handoff.

Desktop MUST NOT duplicate process/orchestrator business logic in Rust.

### 19.2 Main views

```text
Overview
Projects
Agents
Services & MCP
Tasks & Runs
Workflows
Logs
Settings
```

### 19.3 Overview requirements

Show at minimum:

- active projects;
- running agents;
- running services;
- active/failed tasks;
- workflow failures requiring attention;
- port conflicts;
- daemon status.

### 19.4 Project detail

Each project page should show:

- repository/worktree state;
- current agent sessions;
- services/MCP servers;
- task/test/build history;
- workflows;
- recent logs/events.

---

## 20. UX behavior

### Status language

Use consistent states across GUI/CLI/API/MCP.

### Destructive actions

Human GUI requires confirmation for destructive actions such as force stop, deleting definitions or removing an ADCC-owned worktree.

Agent-facing tools use capability/ownership checks rather than GUI confirmations.

### Unknown state

If platform data cannot be reliably determined, display `unknown` with a reason. Never manufacture CPU, cwd, owner, branch or completion results.

---

## 21. Performance targets

P0 targets on a normal developer machine:

- daemon idle CPU should remain low enough for continuous tray use;
- normal state refresh must not execute expensive full scans more frequently than required;
- state API p95 target under 500 ms for typical local workloads after cache/reconciliation work;
- GUI interaction should not block on long process scans;
- log retrieval must be paginated/tail-bounded;
- workflow scheduler must not busy-loop.

Do not optimize prematurely by weakening correctness or safety.

---

## 22. Testing requirements

### Existing tests

Existing upstream tests are behavior guards and must continue to pass until a behavior is intentionally superseded by a documented migration.

### New test layers

#### Unit

- domain model validation;
- process identity logic;
- locks;
- DAG validation;
- retry/timeout transitions;
- config migrations;
- command adapter templating;
- worktree naming/safety.

#### Contract

- `/api/v1` JSON shapes;
- stable status enums;
- CLI `--json` output;
- MCP tool input/output.

#### Platform

Use fake adapters for most tests plus real smoke tests on CI runners.

Required CI platforms before first release:

- Windows;
- macOS.

Linux added when the Linux adapter becomes supported.

#### Integration

- start -> discover -> stop managed service;
- task success/failure/cancel;
- port conflict does not kill external process;
- daemon restart reconciliation;
- agent session lifecycle using a fake command agent;
- isolated Git worktree lifecycle;
- workflow success/failure/cancel/retry;
- lock contention;
- desktop launches/connects to daemon.

### Testing prohibitions

Coding agents MUST NOT:

- delete failing tests to make CI green;
- broadly skip tests without documented reason;
- loosen ownership/security assertions for convenience;
- rely only on mocks for platform-critical behavior.

---

## 23. CI and release

### CI

Move from macOS-only CI to a matrix as milestones make platforms available.

Minimum gates:

```text
lint/static checks
Python unit tests
frontend tests
contract tests
platform smoke tests
release/package checks
```

### Release

P0 artifact targets:

- Windows desktop installer/package;
- macOS desktop package;
- standalone/core development mode;
- CLI/MCP entrypoints bundled or installed with the application.

Signing/notarization may initially be documented as pre-release limitations, but packaging must not silently disable OS security controls.

---

## 24. Compatibility and attribution

The upstream project is MIT-licensed. Preserve required copyright/license notices in substantial derived portions.

During migration:

- retain compatibility with existing local-ops config where practical;
- provide backup/migration path;
- avoid silently overwriting user data;
- document intentional breaking changes.

---

## 25. Architecture invariants

These are non-negotiable unless SPEC is explicitly revised:

1. **Core runtime state must not live only in the GUI.**
2. **No port-only process ownership.**
3. **No mandatory vendor-specific Agent SDK in Core.**
4. **No arbitrary shell MCP tool.**
5. **No big-bang Rust rewrite.**
6. **No destructive Git operations by default.**
7. **Parallel write agents use separate worktrees by default.**
8. **All OS-specific process logic goes behind platform adapters.**
9. **CLI, MCP and GUI share the same application/core behavior.**
10. **State transitions are persisted and testable.**
11. **Unknown platform state is represented as unknown, not guessed.**
12. **Existing safety tests remain valid unless a deliberate migration replaces them.**

---

## 26. Definition of the first usable release

The first usable release is achieved when a Windows or macOS user can:

1. install/open the Desktop GUI;
2. add at least two local development projects;
3. see each project's services/tasks and current ports;
4. start/stop services safely;
5. configure an MCP server as a project resource;
6. configure a generic external coding-agent command;
7. launch that agent in an isolated Git worktree;
8. view the agent's live logs and final exit status;
9. run a workflow of `agent -> test -> review/gate`;
10. observe failure without later steps being falsely marked successful;
11. inspect/control the same system through CLI;
12. expose safe control functions to a coding agent through MCP;
13. restart ADCC Core and correctly reconcile surviving processes;
14. pass Windows/macOS CI tests for process safety and core workflow behavior.

---

## 27. Success criteria

The product is successful if it materially reduces the cost of running multiple AI-assisted development projects at once:

- less port/process confusion;
- fewer accidental kills;
- fewer agent/worktree collisions;
- one place for logs and run history;
- repeatable test/build/agent workflows;
- coding agents can query real runtime state instead of guessing from static context;
- project instructions such as `AGENTS.md`, `SPEC.md`, and `PLAN.md` stay focused on development policy rather than ephemeral runtime state.

