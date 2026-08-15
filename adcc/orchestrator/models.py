"""Orchestrator models: workflow definitions, steps, runs; DAG validation.

Pure functions only — no OS, no scheduling.  A workflow is a DAG of
steps; validation rejects cycles and unknown references up front so the
scheduler can trust the graph.
"""

import secrets
import time

WORKFLOW_STATUSES = (
    "queued", "running", "succeeded", "failed", "canceled", "timed_out",
    "lost",
)
STEP_STATUSES = (
    "pending", "running", "succeeded", "failed", "canceled", "timed_out",
    "skipped", "lost",
)
STEP_KINDS = ("service", "task", "agent", "gate", "command")


def new_id():
    return secrets.token_hex(4)


def workflow_default():
    return {
        "id": None,
        "project_id": None,
        "name": None,
        "version": 1,
        "steps": [],
        "created_at": 0,
        "updated_at": 0,
    }


def step_default():
    return {
        "id": None,
        "kind": "task",
        "needs": [],
        "config": {},
        "timeout_sec": None,
        "retry_policy": None,
        "locks": [],
        "continue_on_error": False,
    }


def make_workflow(*, project_id, name, steps, version=1):
    workflow = workflow_default()
    workflow.update({
        "id": new_id(),
        "project_id": project_id,
        "name": name,
        "version": version,
        "steps": [dict(step) for step in steps],
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    })
    validate_workflow(workflow)
    return workflow


def make_step(*, kind, config=None, needs=None, timeout_sec=None,
              retry_policy=None, locks=None, continue_on_error=False,
              step_id=None):
    step = step_default()
    step.update({
        "id": step_id or new_id(),
        "kind": kind,
        "config": dict(config or {}),
        "needs": list(needs or []),
        "timeout_sec": timeout_sec,
        "retry_policy": dict(retry_policy or {})
        if retry_policy else None,
        "locks": list(locks or []),
        "continue_on_error": bool(continue_on_error),
    })
    validate_step(step)
    return step


def config_value(config, *keys):
    """Read a config key tolerating camelCase and snake_case spellings."""
    for key in keys:
        value = config.get(key)
        if value is not None:
            return value
    return None


def validate_step(step):
    if not isinstance(step, dict):
        raise ValueError("step 必须是对象")
    step_id = step.get("id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise ValueError("step.id 不能为空")
    if step.get("kind") not in STEP_KINDS:
        raise ValueError("step.kind 必须是 %s 之一" % "/".join(STEP_KINDS))
    needs = step.get("needs")
    if not isinstance(needs, list) or any(
            not isinstance(item, str) or not item.strip() for item in needs):
        raise ValueError("step.needs 必须是步骤 id 列表")
    config = step.get("config")
    if not isinstance(config, dict):
        raise ValueError("step.config 必须是对象")
    if step.get("kind") in ("service", "task"):
        if not config_value(config, "resource_id", "resourceId"):
            raise ValueError("%s 步骤需要 config.resource_id" % step.get("kind"))
    if step.get("kind") == "agent":
        if not config_value(config, "adapter_id", "adapterId"):
            raise ValueError("agent 步骤需要 config.adapter_id")
    if step.get("kind") == "gate":
        if not config.get("command"):
            raise ValueError("gate 步骤需要 config.command（验证命令）")
    timeout = step.get("timeout_sec")
    if timeout is not None and (
            not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError("timeout_sec 必须是正数或 null")
    retry = step.get("retry_policy")
    if retry is not None:
        if not isinstance(retry, dict):
            raise ValueError("retry_policy 必须是对象")
        max_retries = retry.get("max_retries", 0)
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("retry_policy.max_retries 必须是非负整数")
    locks = step.get("locks")
    if not isinstance(locks, list) or any(
            not isinstance(item, str) or not item.strip() for item in locks):
        raise ValueError("step.locks 必须是锁名列表")


def validate_workflow(workflow):
    if not isinstance(workflow, dict):
        raise ValueError("workflow 必须是对象")
    if not isinstance(workflow.get("id"), str) or not workflow.get("id"):
        raise ValueError("workflow.id 不能为空")
    if not isinstance(workflow.get("name"), str) or not workflow.get("name"):
        raise ValueError("workflow.name 不能为空")
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("workflow.steps 不能为空")
    ids = set()
    for step in steps:
        validate_step(step)
        step_id = step.get("id")
        if step_id in ids:
            raise ValueError("重复的步骤 id: %s" % step_id)
        ids.add(step_id)


def validate_dag(workflow):
    """Topological check of step dependencies; raises on cycles/missing refs.

    Returns the topological order (list of step ids)."""
    steps = {step["id"]: step for step in workflow.get("steps", [])}
    for step_id, step in steps.items():
        for need in step.get("needs") or []:
            if need not in steps:
                raise ValueError("步骤 %s 依赖不存在的步骤 %s"
                                 % (step_id, need))
    indegree = {step_id: 0 for step_id in steps}
    dependents = {step_id: [] for step_id in steps}
    for step_id, step in steps.items():
        for need in step.get("needs") or []:
            indegree[step_id] += 1
            dependents[need].append(step_id)
    ready = [step_id for step_id, count in indegree.items() if count == 0]
    order = []
    while ready:
        ready.sort()
        current = ready.pop(0)
        order.append(current)
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(steps):
        raise ValueError("工作流存在环（无法拓扑排序）")
    return order


def validate_workflow_run(run):
    if not isinstance(run, dict):
        raise ValueError("run 必须是对象")
    if run.get("status") not in WORKFLOW_STATUSES:
        raise ValueError("workflow run status 非法: %s" % run.get("status"))
