"""ManagedRun model and run-status policy (M4).

Pure helpers: no SQLite, no OS, no HTTP.  The canonical status enum from
SPEC §6 is the single source of truth for GUI/CLI/API/MCP.
"""

import secrets
import time

RUN_STATUSES = (
    "queued", "starting", "running", "succeeded", "failed", "canceled",
    "stopped", "timed_out", "lost",
)

RUN_KINDS = ("service", "task", "agent", "workflow_step")

# task 用户主动取消的退出码（与 legacy task 协议一致）
TASK_CANCELED_EXIT_CODE = 130


def validate_run_status(status):
    if status not in RUN_STATUSES:
        raise ValueError("run.status 必须是 %s 之一" % "/".join(RUN_STATUSES))


def validate_run_kind(kind):
    if kind not in RUN_KINDS:
        raise ValueError("run.kind 必须是 %s 之一" % "/".join(RUN_KINDS))


def new_run_id():
    return secrets.token_hex(4)


def make_run(*, app_id, kind, project_id=None, pid=None,
             process_group_id=None, run_token=None, log_path=None,
             correlation_id=None, status="running"):
    """Create a ManagedRun record (started_at = now, status running)."""
    validate_run_kind(kind)
    validate_run_status(status)
    now = int(time.time())
    return {
        "id": new_run_id(),
        "app_id": app_id,
        "project_id": project_id,
        "kind": kind,
        "status": status,
        "pid": pid,
        "process_group_id": process_group_id,
        "run_token": run_token,
        "started_at": now,
        "ended_at": None,
        "exit_code": None,
        "log_path": log_path,
        "origin": None,
        "correlation_id": correlation_id,
        "created_at": now,
    }


def finalize_run_status(run, code, *, manual_stop=False):
    """Map an exit code to the canonical run status.

    - manual stop (总控台中止) → ``stopped``, regardless of code;
    - task exit 130 (用户主动取消) → ``canceled``;
    - exit 0 → ``succeeded``;
    - anything else → ``failed``.
    """
    if manual_stop:
        return "stopped"
    if code == 0:
        return "succeeded"
    if code == TASK_CANCELED_EXIT_CODE:
        return "canceled"
    return "failed"


def public_run(run):
    """API projection: strip nothing, keep stable field order/shape."""
    if run is None:
        return None
    return {
        "id": run.get("id"),
        "appId": run.get("app_id"),
        "projectId": run.get("project_id"),
        "kind": run.get("kind"),
        "status": run.get("status"),
        "pid": run.get("pid"),
        "processGroupId": run.get("process_group_id"),
        "startedAt": run.get("started_at"),
        "endedAt": run.get("ended_at"),
        "exitCode": run.get("exit_code"),
        "logPath": run.get("log_path"),
        "origin": run.get("origin"),
        "correlationId": run.get("correlation_id"),
        "durationSec": (
            round(max(0.0, run["ended_at"] - run["started_at"]), 3)
            if run.get("ended_at") and run.get("started_at") else None),
    }
