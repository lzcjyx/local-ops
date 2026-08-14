"""Read-only project detection helpers (M3).

These wrappers reuse the existing server detection capabilities; they must
never execute project code or install dependencies.  ``git_root`` shells
out to `git` only (read-only query), everything else is file parsing.
"""

import json
import os
import subprocess

MAX_DETECT_BYTES = 2 * 1024 * 1024


def git_root(path):
    """Return the Git repository root for ``path`` or None.

    ``path`` must exist and be a directory; the command is a read-only
    ``git rev-parse`` query.  Any failure (not a repo, git missing)
    degrades to None — never guessed.
    """
    if not path or not os.path.isdir(path):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    return os.path.normpath(value)


def _read_small_json(root, name):
    path = os.path.join(root, name)
    try:
        if os.path.getsize(path) > MAX_DETECT_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, ValueError):
        return None


def detect_mcp_servers(root):
    """Find likely MCP server definitions (read-only, best effort).

    Sources: ``.mcp.json`` and the ``mcp`` field of ``package.json``.
    Returns a list of candidate dicts shaped like legacy candidates:
    ``{command, label, source, kind: "mcp_server", detail}``.
    """
    candidates = []
    mcp_json = _read_small_json(root, ".mcp.json")
    if isinstance(mcp_json, dict):
        servers = mcp_json.get("mcpServers")
        if isinstance(servers, dict):
            for name, definition in servers.items():
                if not isinstance(definition, dict):
                    continue
                command = definition.get("command")
                if isinstance(command, str) and command.strip():
                    args = definition.get("args") or []
                    if isinstance(args, list):
                        for argument in args:
                            if isinstance(argument, str):
                                command += " " + argument
                    candidates.append({
                        "command": command,
                        "label": "MCP 服务器：%s" % name,
                        "source": ".mcp.json",
                        "kind": "mcp_server",
                        "detail": "项目级 MCP 服务器（%s）" % name,
                    })
    package = _read_small_json(root, "package.json")
    if isinstance(package, dict):
        mcp_field = package.get("mcp")
        if isinstance(mcp_field, dict):
            servers = mcp_field.get("servers")
            if isinstance(servers, dict):
                for name, definition in servers.items():
                    if not isinstance(definition, dict):
                        continue
                    command = definition.get("command")
                    if isinstance(command, str) and command.strip():
                        args = definition.get("args") or []
                        if isinstance(args, list):
                            for argument in args:
                                if isinstance(argument, str):
                                    command += " " + argument
                        candidates.append({
                            "command": command,
                            "label": "MCP 服务器：%s" % name,
                            "source": "package.json mcp",
                            "kind": "mcp_server",
                            "detail": "npm 脚本级 MCP 服务器（%s）" % name,
                        })
    return candidates
