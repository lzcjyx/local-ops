"""Workspace / Project / ResourceDefinition domain models (M3).

Pure data validation only: no persistence, no process queries.  The
registry layer turns these into/from the versioned JSON configuration.
"""

import re
import time

ID_RE = re.compile(r"^[0-9a-f]{8}$")
RESOURCE_KINDS = ("service", "task", "mcp_server")


def new_id():
    import secrets
    return secrets.token_hex(4)


def _check_id(value, label):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("%s 必须是 8 位十六进制 id" % label)


def _check_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("name 不能为空")


def _check_kind(value):
    if value not in RESOURCE_KINDS:
        raise ValueError("kind 必须是 service/task/mcp_server 之一")


def _check_port(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("port 必须是 1-65535 的整数")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not (1 <= value <= 65535):
        raise ValueError("port 必须在 1-65535 之间")
    return value


def _now():
    return int(time.time())


def workspace_default():
    return {
        "id": None,
        "name": "",
        "project_ids": [],
        "created_at": 0,
        "updated_at": 0,
    }


def project_default():
    return {
        "id": None,
        "workspace_id": None,
        "name": None,
        "root_path": None,
        "repo_path": None,
        "environment": {},
        "tags": [],
        "created_at": 0,
        "updated_at": 0,
    }


def resource_default():
    return {
        "id": None,
        "project_id": None,
        "name": None,
        "kind": "service",
        "command": None,
        "cwd": None,
        "environment": {},
        "port": None,
        "created_at": 0,
        "updated_at": 0,
    }


def make_workspace(name, project_ids=None):
    workspace = workspace_default()
    workspace.update({
        "id": new_id(),
        "name": name,
        "project_ids": list(project_ids or []),
        "created_at": _now(),
        "updated_at": _now(),
    })
    validate_workspace(workspace)
    return workspace


def make_project(name, root_path, workspace_id=None, repo_path=None,
                 environment=None, tags=None):
    project = project_default()
    project.update({
        "id": new_id(),
        "name": name,
        "root_path": root_path,
        "workspace_id": workspace_id,
        "repo_path": repo_path,
        "environment": dict(environment or {}),
        "tags": list(tags or []),
        "created_at": _now(),
        "updated_at": _now(),
    })
    validate_project(project)
    return project


def make_resource(name, kind, command, project_id=None, cwd=None, port=None,
                  environment=None):
    resource = resource_default()
    resource.update({
        "id": new_id(),
        "name": name,
        "kind": kind,
        "command": command,
        "project_id": project_id,
        "cwd": cwd,
        "port": port,
        "environment": dict(environment or {}),
        "created_at": _now(),
        "updated_at": _now(),
    })
    validate_resource(resource)
    return resource


def validate_workspace(workspace):
    if not isinstance(workspace, dict):
        raise ValueError("workspace 必须是对象")
    _check_id(workspace.get("id"), "workspace.id")
    _check_name(workspace.get("name"))
    project_ids = workspace.get("project_ids")
    if not isinstance(project_ids, list) or any(
            not isinstance(value, str) or not ID_RE.fullmatch(value)
            for value in project_ids):
        raise ValueError("project_ids 必须是 8 位十六进制 id 列表")


def validate_project(project):
    if not isinstance(project, dict):
        raise ValueError("project 必须是对象")
    _check_id(project.get("id"), "project.id")
    _check_name(project.get("name"))
    root = project.get("root_path")
    if not isinstance(root, str) or not root.strip():
        raise ValueError("root_path 不能为空")
    workspace_id = project.get("workspace_id")
    if workspace_id is not None:
        _check_id(workspace_id, "project.workspace_id")
    repo = project.get("repo_path")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        raise ValueError("repo_path 必须是非空路径或 null")
    env = project.get("environment")
    if not isinstance(env, dict):
        raise ValueError("environment 必须是对象")
    tags = project.get("tags")
    if not isinstance(tags, list) or any(
            not isinstance(value, str) for value in tags):
        raise ValueError("tags 必须是字符串列表")


def validate_resource(resource):
    if not isinstance(resource, dict):
        raise ValueError("resource 必须是对象")
    _check_id(resource.get("id"), "resource.id")
    _check_name(resource.get("name"))
    _check_kind(resource.get("kind"))
    command = resource.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 不能为空")
    project_id = resource.get("project_id")
    if project_id is not None:
        _check_id(project_id, "resource.project_id")
    port = resource.get("port")
    if resource.get("kind") == "task" and port is not None:
        raise ValueError("task 资源不允许配置端口")
    _check_port(port)
    cwd = resource.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise ValueError("cwd 必须是非空路径或 null")
    env = resource.get("environment")
    if not isinstance(env, dict):
        raise ValueError("environment 必须是对象")


def project_summary(project, resources, running_ids):
    """Public project summary; pure projection, no OS access."""
    project_resources = [r for r in resources if r.get("project_id") == project.get("id")]
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "rootPath": project.get("root_path"),
        "repoPath": project.get("repo_path"),
        "workspaceId": project.get("workspace_id"),
        "tags": list(project.get("tags") or []),
        "resourceCount": len(project_resources),
        "runningCount": sum(
            1 for r in project_resources if r.get("id") in running_ids),
    }
