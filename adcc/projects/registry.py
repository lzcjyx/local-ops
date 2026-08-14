"""Project/resource registry operations over the versioned JSON config.

These functions mutate the config mapping *inside* a ``Config.update``
callback (or operate on a snapshot for reads); they never touch the
filesystem or the OS.  Host wiring (server.py) decides when to persist.
"""

from adcc.projects.models import (
    make_project,
    make_resource,
    make_workspace,
    validate_project,
    validate_resource,
    validate_workspace,
)

DEFAULT_WORKSPACE_ID = "00000001"
UNASSIGNED_PROJECT_ID = "00000000"
UNASSIGNED_PROJECT_NAME = "未分配"

_KIND_BY_APP_KIND = {"service": "service", "task": "task"}
_KIND_TO_APP = {value: key for key, value in _KIND_BY_APP_KIND.items()}


def _bump(record):
    import time
    record["updated_at"] = int(time.time())
    return record


def ensure_default_workspace(data):
    """Create the single default workspace when none exists (idempotent)."""
    workspaces = data.setdefault("workspaces", [])
    for workspace in workspaces:
        if workspace.get("id") == DEFAULT_WORKSPACE_ID:
            return workspace
    workspace = make_workspace("默认工作区", [])
    workspace["id"] = DEFAULT_WORKSPACE_ID
    workspaces.insert(0, workspace)
    return workspace


def ensure_unassigned_project(data):
    """Create the Unassigned bucket when none exists (idempotent)."""
    projects = data.setdefault("projects", [])
    for project in projects:
        if project.get("id") == UNASSIGNED_PROJECT_ID:
            return project
    project = make_project(UNASSIGNED_PROJECT_NAME, "/", DEFAULT_WORKSPACE_ID)
    project["id"] = UNASSIGNED_PROJECT_ID
    projects.append(project)
    ensure_default_workspace(data)
    return project


def list_projects(data):
    return list(data.get("projects") or [])


def get_project(data, project_id):
    for project in data.get("projects") or []:
        if project.get("id") == project_id:
            return project
    return None


def create_project(data, name, root_path, workspace_id=None, *, repo_path=None,
                   environment=None, tags=None):
    workspace = ensure_default_workspace(data)
    project = make_project(
        name, root_path,
        workspace_id=workspace_id or workspace.get("id"),
        repo_path=repo_path, environment=environment, tags=tags)
    data.setdefault("projects", []).append(project)
    if project.get("workspace_id") not in workspace.get("project_ids", []):
        workspace.setdefault("project_ids", []).append(project["id"])
    return project


def update_project(data, project_id, fields):
    project = get_project(data, project_id)
    if project is None:
        return None
    allowed = {"name", "root_path", "repo_path", "workspace_id",
               "environment", "tags"}
    for key, value in fields.items():
        if key in allowed:
            project[key] = value
    validate_project(project)
    _bump(project)
    return project


def delete_project(data, project_id):
    """Delete a project; its resources move to the Unassigned bucket."""
    if project_id == UNASSIGNED_PROJECT_ID:
        raise ValueError("不能删除未分配项目")
    project = get_project(data, project_id)
    if project is None:
        return False
    data["projects"] = [p for p in data["projects"] if p.get("id") != project_id]
    for workspace in data.get("workspaces") or []:
        workspace["project_ids"] = [
            pid for pid in workspace.get("project_ids", [])
            if pid != project_id]
    unassigned = ensure_unassigned_project(data)
    for resource in data.get("resources") or []:
        if resource.get("project_id") == project_id:
            resource["project_id"] = unassigned["id"]
    return True


def list_resources(data, project_id=None):
    resources = data.get("resources") or []
    if project_id is None:
        return list(resources)
    return [r for r in resources if r.get("project_id") == project_id]


def get_resource(data, resource_id):
    for resource in data.get("resources") or []:
        if resource.get("id") == resource_id:
            return resource
    return None


def create_resource(data, project_id, name, kind, command, *, cwd=None,
                    port=None, environment=None):
    ensure_default_workspace(data)
    project = get_project(data, project_id)
    if project is None:
        raise ValueError("项目不存在: %s" % project_id)
    resource = make_resource(
        name, kind, command, project_id=project_id, cwd=cwd, port=port,
        environment=environment)
    data.setdefault("resources", []).append(resource)
    return resource


def update_resource(data, resource_id, fields):
    resource = get_resource(data, resource_id)
    if resource is None:
        return None
    allowed = {"name", "kind", "command", "cwd", "port", "environment",
               "project_id"}
    for key, value in fields.items():
        if key in allowed:
            resource[key] = value
    validate_resource(resource)
    _bump(resource)
    return resource


def delete_resource(data, resource_id):
    before = len(data.get("resources") or [])
    data["resources"] = [
        r for r in data["resources"] if r.get("id") != resource_id]
    return len(data["resources"]) != before


def assign_resources_from_apps(data):
    """Migrate legacy flat ``apps`` into project-scoped resources.

    Idempotent: runs only while ``resources`` is empty.  Apps sharing the
    same real cwd share one project; apps without a usable cwd go to the
    Unassigned bucket.  The ``apps`` array itself is retained untouched —
    runtime identity (M2) still reads it.
    """
    apps = data.get("apps") or []
    if data.get("resources") or not apps:
        return False
    import os

    ensure_default_workspace(data)
    project_by_cwd = {}
    assigned = 0
    for app in apps:
        app_id = app.get("id")
        if not app_id:
            continue
        raw_cwd = app.get("cwd")
        cwd = None
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            try:
                cwd = os.path.realpath(raw_cwd)
            except OSError:
                cwd = None
        project_id = None
        if cwd:
            project = project_by_cwd.get(cwd)
            if project is None:
                project = make_project(
                    os.path.basename(cwd.rstrip("/\\")) or cwd,
                    cwd, DEFAULT_WORKSPACE_ID)
                data.setdefault("projects", []).append(project)
                project_by_cwd[cwd] = project
            project_id = project["id"]
        if project_id is None:
            unassigned = ensure_unassigned_project(data)
            project_id = unassigned["id"]
        kind = _KIND_BY_APP_KIND.get(app.get("kind") or "service", "service")
        resource = make_resource(
            app.get("name") or app_id, kind, app.get("command") or "",
            project_id=project_id, cwd=app.get("cwd"),
            port=app.get("port"))
        # legacy 桥：资源与运行时 app 的关联，供项目状态投影使用
        resource["app_id"] = app_id
        data.setdefault("resources", []).append(resource)
        assigned += 1
    return assigned > 0


__all__ = [
    "DEFAULT_WORKSPACE_ID",
    "UNASSIGNED_PROJECT_ID",
    "UNASSIGNED_PROJECT_NAME",
    "assign_resources_from_apps",
    "create_project",
    "create_resource",
    "delete_project",
    "delete_resource",
    "ensure_default_workspace",
    "ensure_unassigned_project",
    "get_project",
    "get_resource",
    "list_projects",
    "list_resources",
    "update_project",
    "update_resource",
]
