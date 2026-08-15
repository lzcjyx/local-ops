"""OS-neutral constants shared by the ADCC core Modules."""

CURRENT_SCHEMA_VERSION = 2
DEFAULT_UI_THEME = "ops"

RUN_TOKEN_ENV = "CONSOLE_RUN_TOKEN"
RUN_TOKEN_ARG_PREFIX = "console-run:"
TASK_CANCELED_EXIT_CODE = 130

CONFIG_DEFAULT = {
    "schemaVersion": CURRENT_SCHEMA_VERSION,
    "apps": [],
    "hidden": [],
    "pinned": [],
    "promoted": [],
    "watchedKeywords": [],
    "uiTheme": DEFAULT_UI_THEME,
    "workspaces": [],
    "projects": [],
    "resources": [],
    "agent_adapters": [],
    "agent_policy": {"global_max": 3, "per_project_max": 1},
    "workflows": [],
}

APP_DEFAULT = {
    "id": None,
    "name": "",
    "command": "",
    "cwd": None,
    "port": None,
    "emoji": None,
    "glyph": None,
    "icon": None,
    "favicon": None,
    "kind": "service",
    "lastPid": None,
    "lastPgid": None,
    "runToken": None,
    "attached": False,
    "lastExit": None,
    "createdAt": 0,
}

