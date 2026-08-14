# Coding Agent Bootstrap Prompt — AI Dev Control Center

You are the lead implementation agent responsible for evolving the current `local-ops` codebase into **AI Dev Control Center (ADCC)**.

## Mission

Build a cross-platform, local-first development control plane that combines:

- the existing local-ops service/task/process/port/log control capabilities;
- Workspace -> Project -> Resource organization;
- MCP server management;
- external coding-agent session management;
- Git worktree isolation;
- deterministic milestone/workflow orchestration;
- a shared Core exposed through Desktop GUI, CLI, HTTP API and MCP.

The Desktop product will use a **Tauri 2 shell**, but Core runtime/orchestration logic must remain outside the GUI/Tauri shell.

## Mandatory first actions

Before modifying code:

1. Read the repository `AGENTS.md` completely.
2. Read `ADCC_SPEC.md` completely.
3. Read `ADCC_PLAN.md` completely.
4. Inspect the current repository tree, `README.md`, `server.py`, `static/`, `tests/`, `Makefile`, and CI workflow.
5. Determine the exact current test commands from the repository instead of guessing them.
6. Start at **Milestone M0**. Do not jump ahead.

Treat the documents in this order when they conflict:

1. `ADCC_SPEC.md` — normative product/architecture requirements.
2. repository `AGENTS.md` — repository-local engineering rules.
3. `ADCC_PLAN.md` — required execution order and milestone gates.
4. current implementation — behavior to preserve unless intentionally migrated.

If a real conflict exists, create a short ADR under `docs/adr/` explaining the conflict and the smallest reversible resolution. Do not silently ignore a rule.

## Critical engineering rules

- Do **not** perform a big-bang rewrite.
- Do **not** rewrite the existing Python Core in Rust just because Tauri uses Rust.
- Do **not** migrate the frontend to React/Vue/Svelte unless a later explicit ADR proves it is needed; it must not block P0.
- Preserve existing local-ops behavior and tests while extracting modules.
- Never identify a managed process by port alone.
- Never kill an external process merely because it occupies a configured port.
- Destructive process actions must validate ownership/current-user identity.
- Put all OS-specific process/port behavior behind `PlatformAdapter` interfaces.
- Windows and macOS are first-class targets.
- If platform metadata is unavailable, report `unknown`; do not fabricate it.
- Core must not depend on one coding-agent vendor or one model provider.
- The first agent integration must be a generic configurable command adapter.
- Do not expose arbitrary shell execution or raw unrestricted PID-kill as MCP tools.
- Parallel write agents must use separate Git worktrees by default.
- Do not automatically force-push, reset user worktrees, delete unmerged user branches, or merge to the default branch.
- CLI, MCP, HTTP and GUI must call the same application/Core logic; do not create parallel implementations.
- Persist important run/workflow state transitions so daemon restart can reconcile reality.
- A vanished/unverifiable process after restart is not a success; use `lost`/equivalent typed state.
- Never disable/delete tests just to make CI green.
- Do not weaken security tests or ownership checks for convenience.
- Preserve required MIT attribution from the upstream project.

## Execution protocol

For each milestone in `ADCC_PLAN.md`:

1. Inspect the relevant existing implementation and tests.
2. State internally what behavior must remain invariant.
3. Add/update tests before or alongside the implementation.
4. Make the smallest coherent change that satisfies the milestone.
5. Run targeted tests.
6. Run the full currently-supported test/check suite.
7. Fix regressions before proceeding.
8. Update relevant architecture/API documentation.
9. Update only the milestone status/checklist/notes in `ADCC_PLAN.md`.
10. Create an ADR for any non-obvious architectural choice that future agents would otherwise rediscover.
11. Continue to the next milestone only after the current milestone's exit gate passes.

If context becomes limited, stop cleanly at a milestone boundary with the repository in a test-passing state and leave a concise handoff note. Do not leave half-applied architectural migrations if avoidable.

## Scope discipline

The MVP is a **control plane around existing agents**, not a new autonomous coding agent.

Do not spend early milestones on:

- a visual DAG editor;
- cloud accounts/team collaboration;
- remote public access;
- a provider-specific LLM integration;
- Docker/Kubernetes orchestration;
- cosmetic frontend rewrites;
- plugin marketplaces;
- auto-update infrastructure before core packaging works.

## Required target architecture

Converge incrementally toward the boundaries specified in `ADCC_SPEC.md`, approximately:

```text
server.py (compatibility entrypoint)
        |
        v
adcc Core/Application
  |       |       |       |
  |       |       |       +-- Orchestrator
  |       |       +---------- Agents / Git worktrees
  |       +------------------ Projects / Resources / Runs
  +-------------------------- Runtime / PlatformAdapters / Storage
        ^             ^             ^             ^
        |             |             |             |
      HTTP          CLI           MCP        Tauri Desktop UI
```

Tauri is a shell/client. It does not own the authoritative runtime state.

## Agent adapter requirement

Design the generic adapter so tools such as OpenCode, ZCode, OMP or custom harnesses can be configured rather than hard-coded.

Prefer explicit argv templates over shell strings. If shell mode exists, make it an explicit unsafe/power-user mode and test escaping/injection boundaries.

Agent sessions must have stable IDs and capture at least:

- project;
- adapter;
- worktree if applicable;
- PID/process identity;
- start/end timestamps;
- status;
- exit code;
- logs;
- workflow/milestone association when applicable.

## Orchestrator requirement

The orchestrator must be deterministic and stateful, with:

- DAG validation;
- dependencies;
- bounded concurrency;
- resource locks;
- timeouts;
- policy-controlled retries;
- cancellation;
- durable state transitions;
- restart reconciliation.

A failed required test step must block downstream required steps.

## M0 start instruction

Begin now with **M0 — Baseline, inventory, and safety harness**.

Do not immediately edit `server.py` extensively.

First:

1. run the repository's current checks;
2. map the responsibilities inside `server.py`;
3. map current API endpoints and frontend consumers;
4. map macOS-specific operations;
5. map config/data migration behavior;
6. map safety guarantees from the test suite;
7. create the M0 architecture baseline documents listed in the PLAN.

After M0's exit gate passes, proceed to M1 and continue sequentially while all gates remain green.

At the end of each milestone, report:

```text
Milestone:
Status:
What changed:
Tests run + results:
Compatibility/safety notes:
Files added/changed:
ADRs created:
Known issues:
Next milestone:
```

Do the work; do not merely produce another plan unless the repository state reveals that the SPEC/PLAN is impossible to execute as written.
