"""Pure process-text parsing and runtime identity policy helpers.

This module does not execute OS commands, inspect live processes, or select a
platform.  It accepts already-collected text/facts so the existing macOS
wrappers can delegate here unchanged during M1.
"""

import os
import re

from adcc.core.constants import RUN_TOKEN_ARG_PREFIX

SYSTEM_PATH_PREFIXES = (
    "/usr/libexec/", "/usr/sbin/", "/sbin/", "/System/", "/usr/lib/")

# 开发服务关键词：命中 name/args 时优先归为 "mine"（覆盖 .app 规则，
# 例如 ollama 守护进程在 Ollama.app 内、Docker 在 Docker.app 内）
DEV_KEYWORDS = (
    "python", "node", "ruby", "php", "nginx", "caddy", "postgres",
    "mysql", "redis", "mongo", "ollama", "docker", "deno", "bun",
    "uvicorn", "gunicorn", "hugo", "vite", "streamlit", "jupyter",
    "ngrok", "frp", "code-server", "java",
)

HOME_DIR = os.path.expanduser("~")

# 向上爬时要跳过的包装层（按 argv[0] 基名匹配）：壳、包管理器与任务执行器
_ORIGIN_SKIP_NAMES = {
    "zsh", "bash", "sh", "dash", "fish", "login", "su", "sudo", "env",
    "command", "xargs", "nohup", "setsid", "script", "expect", "caffeinate",
    "launchd",
    "npm", "npx", "pnpm", "yarn", "corepack", "make", "just",
    "node", "tsx", "nodemon", "deno", "bun", "bunx",
    "python", "python3", "uv", "poetry", "pip", "pipx",
    "ruby", "php", "java", "dotnet", "go", "cargo",
}

# 已知 AI 编程助手签名（在祖先 args 中做词边界匹配，按顺序取先命中者）
_ORIGIN_AGENT_PATTERNS = (
    (re.compile(r"\bcodex\b", re.I), "Codex"),
    (re.compile(r"claude-code|\bclaude\b", re.I), "Claude Code"),
    (re.compile(r"\bkimi\b", re.I), "Kimi"),
    (re.compile(r"\bgemini\b", re.I), "Gemini"),
    (re.compile(r"\baider\b", re.I), "Aider"),
    (re.compile(r"\bopencode\b", re.I), "OpenCode"),
    (re.compile(r"\bgoose\b", re.I), "Goose"),
    (re.compile(r"\bcursor-agent\b", re.I), "Cursor"),
    (re.compile(r"\bcopilot\b", re.I), "Copilot"),
    (re.compile(r"\bqwen\b", re.I), "Qwen"),
    (re.compile(r"\bqoder\b", re.I), "Qoder"),
    (re.compile(r"\bamp\b", re.I), "Amp"),
    (re.compile(r"\bcodebuddy\b", re.I), "CodeBuddy"),
)

# .app 包名 → (展示名, 图标)。未列出的包按原名 + package 图标展示
_ORIGIN_APP_ALIASES = {
    "visual studio code": ("VS Code", "code"),
    "visual studio code - insiders": ("VS Code", "code"),
    "cursor": ("Cursor", "code"),
    "trae": ("Trae", "code"),
    "windsurf": ("Windsurf", "code"),
    "zed": ("Zed", "code"),
    "sublime text": ("Sublime", "code"),
    "webstorm": ("WebStorm", "code"),
    "intellij idea": ("IDEA", "code"),
    "goland": ("GoLand", "code"),
    "pycharm": ("PyCharm", "code"),
    "nova": ("Nova", "code"),
    "xcode": ("Xcode", "code"),
    "iterm2": ("iTerm", "terminal"),
    "iterm": ("iTerm", "terminal"),
    "terminal": ("终端", "terminal"),
    "warp": ("Warp", "terminal"),
    "kitty": ("kitty", "terminal"),
    "alacritty": ("Alacritty", "terminal"),
    "wezterm": ("WezTerm", "terminal"),
    "docker": ("Docker", "package"),
    "ollama": ("Ollama", "package"),
    "obsidian": ("Obsidian", "package"),
}
_ORIGIN_BUNDLE_RE = re.compile(r"/([^/]+)\.app/Contents/MacOS/", re.I)

# 终端复用器（直接以 comm 命名，不进跳过表）
_ORIGIN_MULTIPLEXERS = {"tmux": "tmux", "screen": "screen"}


def parse_etime(value):
    """Parse BSD ``ps`` etime ``[[dd-]hh:]mm:ss`` into seconds.

    Malformed input deliberately retains the legacy zero fallback.
    """
    try:
        value = value.strip()
        days = 0
        if "-" in value:
            day, value = value.split("-", 1)
            days = int(day)
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 2:
            hours, minutes, secs = 0, parts[0], parts[1]
        elif len(parts) == 3:
            hours, minutes, secs = parts
        else:
            return 0
        return days * 86400 + hours * 3600 + minutes * 60 + secs
    except Exception:
        return 0


def _to_float(token, default=0.0):
    try:
        return float(token)
    except (TypeError, ValueError):
        return default


def parse_ps_snapshot(fixed_output, args_output, with_uid=True):
    """Parse the two legacy BSD ``ps`` outputs into a process snapshot."""
    snapshot = {}
    fixed = 5 if with_uid else 4  # pid [uid] etime cpu mem 之后的都是 comm
    for line in fixed_output.splitlines():
        tokens = line.split()
        if len(tokens) < fixed + 1:
            continue
        try:
            pid = int(tokens[0])
        except ValueError:
            continue  # 表头行
        index = 1
        entry = {"args": ""}
        if with_uid:
            try:
                entry["uid"] = int(tokens[1])
            except ValueError:
                entry["uid"] = -1
            index = 2
        entry["etime"] = parse_etime(tokens[index])
        entry["cpu"] = _to_float(tokens[index + 1])
        entry["mem"] = _to_float(tokens[index + 2])
        entry["comm"] = " ".join(tokens[index + 3:])
        snapshot[pid] = entry
    for line in args_output.splitlines():
        tokens = line.split(None, 1)
        if not tokens:
            continue
        try:
            pid = int(tokens[0])
        except ValueError:
            continue
        if pid in snapshot:
            snapshot[pid]["args"] = tokens[1] if len(tokens) > 1 else ""
    return snapshot


def parse_lsof_cwds(output):
    """Parse ``lsof -Fn`` cwd output into ``{pid: cwd}``."""
    result = {}
    current_pid = None
    for line in output.splitlines():
        if line.startswith("p"):
            try:
                current_pid = int(line[1:])
            except ValueError:
                current_pid = None
        elif line.startswith("n") and current_pid is not None:
            result[current_pid] = line[1:]
    return result


def parse_pgid_members(output):
    """Parse ``ps pid,pgid`` output into ``{pgid: [pid, ...]}``."""
    groups = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        groups.setdefault(pgid, []).append(pid)
    return groups


def parse_origin_snapshot(output):
    """Parse ``ps pid,ppid,args`` output for presentation-only origin lookup."""
    table = {}
    for line in output.splitlines():
        tokens = line.split(None, 2)
        if len(tokens) < 2:
            continue
        try:
            pid, ppid = int(tokens[0]), int(tokens[1])
        except ValueError:
            continue
        table[pid] = (ppid, tokens[2] if len(tokens) > 2 else "")
    return table


def classify_group(key, name, comm, args, cwd, promoted):
    if key in promoted:
        return "mine"
    text = name.lower()
    if any(keyword in text for keyword in DEV_KEYWORDS):
        return "mine"
    if ".app/Contents/" in comm or ".app/Contents/" in args:
        return "background"
    if comm.startswith(SYSTEM_PATH_PREFIXES):
        return "background"
    if "/Library/Containers/" in comm or "/Library/Containers/" in (cwd or ""):
        return "background"
    return "mine"


def project_name(cwd):
    """Infer a project name from a cwd's final path component."""
    if not cwd:
        return None
    cwd = cwd.rstrip("/")
    if not cwd or cwd == "/" or cwd == HOME_DIR:
        return None
    return os.path.basename(cwd) or None


def attribute_origin(pid, table):
    """Classify a supplied PPID table for display without inspecting the OS."""
    current, seen, candidate = pid, set(), None
    for _ in range(12):
        entry = table.get(current)
        if not entry:
            break
        ppid, _ = entry
        if ppid in seen:
            break
        seen.add(ppid)
        parent_args = (table.get(ppid) or (0, ""))[1] or ""
        if ppid <= 1:
            return candidate or {"label": "系统", "icon": "server"}
        if RUN_TOKEN_ARG_PREFIX in parent_args:
            return {"label": "总控台", "icon": "rocket"}
        haystack = parent_args.casefold()
        for pattern, label in _ORIGIN_AGENT_PATTERNS:
            if pattern.search(haystack):
                return {"label": label, "icon": "bot"}
        bundle = _ORIGIN_BUNDLE_RE.search(parent_args)
        if bundle:
            app_name = bundle.group(1)
            label, icon = _ORIGIN_APP_ALIASES.get(
                app_name.casefold(), (app_name, "package"))
            return {"label": label, "icon": icon}
        base = os.path.basename(
            parent_args.split()[0]).lstrip("-") if parent_args.split() else ""
        if base in _ORIGIN_MULTIPLEXERS:
            return {"label": _ORIGIN_MULTIPLEXERS[base], "icon": "terminal"}
        if base and base not in _ORIGIN_SKIP_NAMES and candidate is None:
            candidate = {"label": base, "icon": "package"}
        current = ppid
    return candidate
