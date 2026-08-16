"""Agent adapter and session models (M7).

Pure validation and template rendering: no OS, no SQLite, no HTTP.
The generic command adapter renders user-configurable argv/env templates
with session/project variables (SPEC §10.1).
"""

import secrets
import time

SESSION_STATUSES = (
    "queued", "starting", "running", "succeeded", "failed", "canceled",
    "stopped", "timed_out", "lost",
)

ID_RE = r"^[0-9a-f]{8}$"


def new_id():
    return secrets.token_hex(4)


def _check_id(value, label):
    import re
    if not isinstance(value, str) or not re.fullmatch(ID_RE, value):
        raise ValueError("%s 必须是 8 位十六进制 id" % label)


def _check_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("name 不能为空")


def adapter_default():
    return {
        "id": None,
        "name": None,
        "type": "command",
        "executable": None,
        "args_template": [],
        "env_template": {},
        "cwd_template": None,
        "stdin_mode": "file",  # none | file | stdin
        "supports_noninteractive": True,
        # P1：cost/token 元数据（展示用，不参与调度）
        "cost": None,          # {"model": str, "inputPer1k": float, "outputPer1k": float}
        "token_budget": None,  # 每次会话 token 预算上限（int）
        "created_at": 0,
    }


def session_default():
    return {
        "id": None,
        "project_id": None,
        "adapter_id": None,
        "workflow_run_id": None,
        "workflow_step_id": None,
        "status": "queued",
        "pid": None,
        "run_token": None,
        "started_at": None,
        "ended_at": None,
        "exit_code": None,
        "log_path": None,
        "prompt_ref": None,
        "created_at": 0,
    }


def make_adapter(*, name, executable, args_template=None, env_template=None,
                 cwd_template=None, stdin_mode="file",
                 supports_noninteractive=True, cost=None, token_budget=None):
    adapter = adapter_default()
    adapter.update({
        "id": new_id(),
        "name": name,
        "executable": executable,
        "args_template": list(args_template or []),
        "env_template": dict(env_template or {}),
        "cwd_template": cwd_template,
        "stdin_mode": stdin_mode,
        "supports_noninteractive": bool(supports_noninteractive),
        "cost": cost,
        "token_budget": token_budget,
        "created_at": int(time.time()),
    })
    validate_adapter(adapter)
    return adapter


def make_session(*, project_id, adapter_id, prompt_ref=None,
                 workflow_run_id=None, workflow_step_id=None):
    session = session_default()
    session.update({
        "id": new_id(),
        "project_id": project_id,
        "adapter_id": adapter_id,
        "workflow_run_id": workflow_run_id,
        "workflow_step_id": workflow_step_id,
        "prompt_ref": prompt_ref,
        "status": "queued",
        "created_at": int(time.time()),
    })
    validate_session(session)
    return session


def validate_adapter(adapter):
    if not isinstance(adapter, dict):
        raise ValueError("adapter 必须是对象")
    _check_id(adapter.get("id"), "adapter.id")
    _check_name(adapter.get("name"))
    if adapter.get("type") != "command":
        raise ValueError("type 目前仅支持 command")
    executable = adapter.get("executable")
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("executable 不能为空")
    args = adapter.get("args_template")
    if not isinstance(args, list) or any(
            not isinstance(item, str) for item in args):
        raise ValueError("args_template 必须是字符串数组")
    env = adapter.get("env_template")
    if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in env.items()):
        raise ValueError("env_template 必须是字符串映射")
    if adapter.get("stdin_mode") not in ("none", "file", "stdin"):
        raise ValueError("stdin_mode 必须是 none/file/stdin")
    cost = adapter.get("cost")
    if cost is not None:
        if not isinstance(cost, dict):
            raise ValueError("cost 必须是对象或 null")
        model = cost.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError("cost.model 必须是字符串")
    budget = adapter.get("token_budget")
    if budget is not None and (
            not isinstance(budget, int) or isinstance(budget, bool)
            or budget <= 0):
        raise ValueError("token_budget 必须是正整数或 null")


def validate_session(session):
    if not isinstance(session, dict):
        raise ValueError("session 必须是对象")
    _check_id(session.get("id"), "session.id")
    _check_id(session.get("project_id"), "session.project_id")
    _check_id(session.get("adapter_id"), "session.adapter_id")
    status = session.get("status")
    if status not in SESSION_STATUSES:
        raise ValueError("session.status 必须是 %s 之一"
                         % "/".join(SESSION_STATUSES))


# ---------------------------------------------------------------- 模板渲染

_VARIABLES = ("project_id", "session_id", "project_root", "prompt_file",
              "worktree_path", "run_id")


def _render(value, variables):
    """Replace ``{name}`` placeholders; unknown names are left verbatim."""
    if not isinstance(value, str):
        return value
    result = value
    for name in _VARIABLES:
        replacement = variables.get(name)
        if replacement is not None:
            result = result.replace("{%s}" % name, str(replacement))
    return result


def render_command(adapter, variables):
    """Build the full argv from an adapter + resolved variables.

    ``variables`` must contain at least ``session_id``; ``prompt_file``
    is expected when ``stdin_mode == "file"`` (produced by the runner).
    """
    executable = _render(adapter.get("executable"), variables)
    args = [_render(item, variables)
            for item in adapter.get("args_template") or []]
    return [executable] + args


def render_env(adapter, variables):
    return {
        key: _render(value, variables)
        for key, value in (adapter.get("env_template") or {}).items()
    }


def render_cwd(adapter, variables):
    template = adapter.get("cwd_template")
    if not template:
        return None
    return _render(template, variables)


def session_variables(session, prompt_file=None, project_root=None,
                      worktree_path=None, run_id=None):
    return {
        "project_id": session.get("project_id"),
        "session_id": session.get("id"),
        "project_root": project_root,
        "prompt_file": prompt_file,
        "worktree_path": worktree_path,
        "run_id": run_id,
    }
