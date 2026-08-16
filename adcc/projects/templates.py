"""Reusable project templates and manifest import/export (P1).

Templates are pure presets: applying one creates resource definitions
under a project (no detection, no execution).  Manifests are a JSON
serialization of projects + resources, versioned and idempotent on
import (existing ids are skipped, not overwritten).
"""

import json

from adcc.projects.models import make_resource
from adcc.projects.registry import create_resource, get_project

MANIFEST_VERSION = 1

TEMPLATES = [
    {
        "id": "web-frontend",
        "name": "Web 前端",
        "description": "Vite/Next 等前端开发服务",
        "resources": [
            {"name": "前端开发服务器", "kind": "service",
             "command": "npm run dev", "port": 5173},
        ],
    },
    {
        "id": "python-api",
        "name": "Python API",
        "description": "FastAPI/Flask 等 API 服务 + 测试任务",
        "resources": [
            {"name": "API 开发服务器", "kind": "service",
             "command": "python -m uvicorn app.main:app --reload",
             "port": 8000},
            {"name": "运行测试", "kind": "task",
             "command": "python -m pytest"},
        ],
    },
    {
        "id": "static-site",
        "name": "静态网站",
        "description": "静态站点预览",
        "resources": [
            {"name": "静态预览", "kind": "service",
             "command": "python -m http.server 8000", "port": 8000},
        ],
    },
    {
        "id": "mcp-server",
        "name": "MCP 服务器",
        "description": "通用 MCP 服务器（npx 启动）",
        "resources": [
            {"name": "MCP 服务器", "kind": "mcp_server",
             "command": "npx @modelcontextprotocol/server"},
        ],
    },
]


def list_templates():
    return [
        {"id": t["id"], "name": t["name"], "description": t["description"]}
        for t in TEMPLATES
    ]


def apply_template(data, project_id, template_id):
    """Create template resources under a project; idempotent by name."""
    template = next((t for t in TEMPLATES if t["id"] == template_id), None)
    if template is None:
        raise ValueError("未知模板: %s" % template_id)
    if get_project(data, project_id) is None:
        raise ValueError("项目不存在: %s" % project_id)
    created = []
    existing_names = {
        r.get("name") for r in data.get("resources") or []
        if r.get("project_id") == project_id}
    for spec in template["resources"]:
        if spec["name"] in existing_names:
            continue
        resource = create_resource(
            data, project_id, spec["name"], spec["kind"],
            spec["command"], port=spec.get("port"))
        created.append(resource)
    return created


def export_manifest(data):
    """Projects + resources as a versioned manifest dict."""
    return {
        "manifestVersion": MANIFEST_VERSION,
        "exportedAt": int(__import__("time").time()),
        "projects": data.get("projects") or [],
        "resources": data.get("resources") or [],
    }


def import_manifest(data, manifest):
    """Merge manifest projects/resources; idempotent (skip existing ids)."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须是对象")
    if manifest.get("manifestVersion") != MANIFEST_VERSION:
        raise ValueError("不支持的 manifest 版本: %s"
                         % manifest.get("manifestVersion"))
    existing_project_ids = {
        p.get("id") for p in data.get("projects") or []}
    existing_resource_ids = {
        r.get("id") for r in data.get("resources") or []}
    imported_projects = 0
    imported_resources = 0
    for project in manifest.get("projects") or []:
        if not isinstance(project, dict) or not project.get("id"):
            continue
        if project["id"] in existing_project_ids:
            continue
        data.setdefault("projects", []).append(json.loads(
            json.dumps(project, ensure_ascii=False)))
        imported_projects += 1
    for resource in manifest.get("resources") or []:
        if not isinstance(resource, dict) or not resource.get("id"):
            continue
        if resource["id"] in existing_resource_ids:
            continue
        data.setdefault("resources", []).append(json.loads(
            json.dumps(resource, ensure_ascii=False)))
        imported_resources += 1
    return {"projects": imported_projects, "resources": imported_resources}
