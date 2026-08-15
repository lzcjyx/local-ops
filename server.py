#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""总控台后端（单文件，仅 Python 3 标准库）。

本地服务监控 + 快速启动台：
    python3 server.py  →  绑定 127.0.0.1，端口 9600 起（被占 +1，最多 10 个）
API 契约与实现要点见 AGENTS.md。
"""

import glob
import functools
import errno
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import fcntl  # macOS 单实例锁；Windows 由 PlatformAdapter 处理
except ImportError:  # pragma: no cover - platform-specific
    fcntl = None

from adcc.core.constants import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_UI_THEME,
    RUN_TOKEN_ARG_PREFIX,
    RUN_TOKEN_ENV,
    TASK_CANCELED_EXIT_CODE,
)
from adcc.core.errors import ConfigSchemaError, FutureConfigSchemaError
from adcc.core.events import EventBus
from adcc.agents import AgentRunner, make_adapter, validate_adapter
from adcc.orchestrator import (
    ExecutorHooks,
    LockManager,
    WorkflowExecutor,
    make_workflow,
    validate_workflow,
)
from adcc.git.repository import (
    create_worktree,
    detect_repo,
    list_worktrees,
)
from adcc.platform import get_platform_adapter
from adcc.projects import (
    assign_resources_from_apps,
    project_summary,
)
from adcc.projects.detection import detect_mcp_servers, git_root
from adcc.runtime.runs import (
    finalize_run_status,
    make_run,
    public_run,
)
from adcc.storage.database import RunDatabase, run_origin_label
from adcc.runtime.lifecycle import (
    legacy_candidate_pids as core_legacy_candidate_pids,
    legacy_identity_applicable as core_legacy_identity_applicable,
    legacy_managed_pid as core_legacy_managed_pid,
    listener_app_owners as core_listener_app_owners,
    managed_candidate_pids as core_managed_candidate_pids,
    managed_process_index as core_managed_process_index,
    managed_process_index_windows as core_managed_process_index_windows,
)
from adcc.runtime.ports import (
    listener_open_host,
    parse_lsof_listeners,
    validate_port,
)
from adcc.runtime.processes import (
    DEV_KEYWORDS,
    HOME_DIR,
    SYSTEM_PATH_PREFIXES,
    attribute_origin,
    classify_group,
    parse_etime,
    parse_lsof_cwds,
    parse_origin_snapshot,
    parse_pgid_members,
    parse_ps_snapshot,
    project_name,
)
from adcc.runtime.tasks import classify_task_exit, public_last_exit
from adcc.storage.config import (
    CONFIG_MIGRATIONS,
    Config as CoreConfig,
    migrate_config,
    migrate_config_v0_to_v1,
)

PLATFORM = get_platform_adapter()
IS_MACOS = PLATFORM.name == "macos"
IS_WINDOWS = PLATFORM.name == "windows"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_PATH = os.path.join(BASE_DIR, "VERSION")
LEGACY_DATA_DIR = os.path.join(BASE_DIR, "data")
if IS_WINDOWS:
    _app_support = os.environ.get("APPDATA") or os.path.expanduser("~")
    DEFAULT_DATA_DIR = os.path.join(_app_support, "总控台")
    DEFAULT_LOGS_DIR = os.path.join(_app_support, "总控台", "logs")
else:
    DEFAULT_DATA_DIR = os.path.expanduser(
        "~/Library/Application Support/总控台")
    DEFAULT_LOGS_DIR = os.path.expanduser("~/Library/Logs/总控台")


def resolve_runtime_dir(name, default):
    """解析专用运行目录，拒绝空值、相对路径和过宽目标。"""
    if name not in os.environ:
        return os.path.abspath(default), False
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        raise RuntimeError("%s 不能为空" % name)
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise RuntimeError("%s 必须是绝对路径" % name)
    path = os.path.abspath(expanded)
    forbidden = {os.path.abspath(os.sep), os.path.abspath(os.path.expanduser("~")),
                 os.path.abspath(BASE_DIR)}
    if path in forbidden:
        raise RuntimeError("%s 必须指向专用子目录" % name)
    return path, True


DATA_DIR, DATA_DIR_OVERRIDDEN = resolve_runtime_dir(
    "CONSOLE_DATA_DIR", DEFAULT_DATA_DIR)
ICONS_DIR = os.path.join(DATA_DIR, "icons")
LOGS_DIR, LOGS_DIR_OVERRIDDEN = resolve_runtime_dir(
    "CONSOLE_LOG_DIR", DEFAULT_LOGS_DIR)
STATIC_DIR = os.path.join(BASE_DIR, "static")
THEMES_DIR = os.path.join(STATIC_DIR, "themes")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
INSTANCE_LOCK_PATH = os.path.join(DATA_DIR, "console.lock")

def read_project_version(path=VERSION_PATH):
    """读取根目录 VERSION。失败时保持服务可诊断，但标记为降级。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read(128).strip()
        if not re.fullmatch(
                r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
                r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", value):
            raise ValueError("VERSION 不是合法的 SemVer")
        return value, None
    except (OSError, UnicodeError, ValueError) as e:
        return "0.0.0+unknown", str(e)


APP_VERSION, VERSION_LOAD_ERROR = read_project_version()

HOST = "127.0.0.1"
PORT_START = 9600
PORT_TRIES = 10
SUBPROCESS_TIMEOUT = 5          # lsof/ps 等子进程超时（秒）
MAX_ICON_BYTES = 5 * 1024 * 1024
MAX_JSON_BYTES = 1 * 1024 * 1024
MAX_DETECT_FILE_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3
LOG_MAINTENANCE_SEC = 30
STARTUP_PROBE_SEC = 0.25
APP_STOP_TIMEOUT_SEC = 5.0
SELF_PID = os.getpid()
SELF_UID = PLATFORM.current_user_id()
SIGKILL = getattr(signal, "SIGKILL", 9)  # Windows 的 signal 模块无 SIGKILL
ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".ico")
LOG = logging.getLogger("console")
LOG_LOCK = threading.RLock()

# ---------------------------------------------------------------- M4 运行历史
RUNS_DB_PATH = os.path.join(DATA_DIR, "console.sqlite3")
EVENTS = EventBus()
RUNS_DB = None
_runs_db_lock = threading.Lock()
AGENT_RUNNER = None
_agent_runner_lock = threading.Lock()


def get_agent_runner(cfg=None):
    """惰性创建 AgentRunner（依赖 DB 与数据目录就绪后）。"""
    global AGENT_RUNNER
    if AGENT_RUNNER is None:
        with _agent_runner_lock:
            if AGENT_RUNNER is None:
                db = get_runs_db()
                if db is None:
                    return None
                AGENT_RUNNER = AgentRunner(
                    cfg=cfg, db=db, platform=PLATFORM,
                    logs_dir=LOGS_DIR,
                    prompts_dir=os.path.join(DATA_DIR, "prompts"),
                    current_user=SELF_UID, events=EVENTS)
    if cfg is not None and AGENT_RUNNER is not None and AGENT_RUNNER._cfg is None:
        AGENT_RUNNER._cfg = cfg
    return AGENT_RUNNER


# ---------------------------------------------------------------- M8 编排
WORKFLOW_EXECUTOR = None
_workflow_executor_lock = threading.Lock()


class ServerExecutorHooks(ExecutorHooks):
    """把 executor 接到真实 daemon 资源/agent 操作上。"""

    def __init__(self, cfg):
        self._cfg = cfg

    def _snapshot(self):
        return self._cfg.snapshot()

    def resolve_resource(self, resource_id):
        snapshot = self._snapshot()
        return next(
            (r for r in snapshot.get("resources") or []
             if r.get("id") == resource_id), None)

    def get_workflow_definition(self, workflow_id):
        snapshot = self._snapshot()
        return next(
            (w for w in snapshot.get("workflows") or []
             if w.get("id") == workflow_id), None)

    def start_resource(self, resource_id):
        resource = self.resolve_resource(resource_id)
        if resource is None:
            return False, "资源不存在", None
        app_id = resource.get("app_id")
        if not app_id:
            return False, "资源未关联受管应用", None
        app = find_app(self._snapshot(), app_id)
        if app is None:
            return False, "应用不存在", None
        if app_alive_sign(app):
            return True, None, {"running": True}
        ok, err, proc, pgid, token = start_app(app)
        if not ok:
            return False, err, None
        if not persist_started_app(self._cfg, app_id, proc, pgid, token):
            stop_pid_tree(pgid)
            return False, "应用已被删除", None
        return True, None, {"pid": proc.pid}

    def stop_resource(self, resource_id):
        resource = self.resolve_resource(resource_id)
        if resource is None:
            return False, "资源不存在"
        app_id = resource.get("app_id")
        if not app_id:
            return False, "资源未关联受管应用"
        app = find_app(self._snapshot(), app_id)
        if app is None:
            return False, "应用不存在"
        ok, error = stop_app_and_clear(self._cfg, app)
        return ok, error

    def resource_alive(self, resource_id):
        resource = self.resolve_resource(resource_id)
        if resource is None:
            return False
        app = find_app(self._snapshot(), resource.get("app_id") or "")
        return bool(app and app_running(app))

    def resource_run_status(self, resource_id):
        """Latest managed run status for the resource's app (task waiting)."""
        resource = self.resolve_resource(resource_id)
        if resource is None or not resource.get("app_id"):
            return None
        db = get_runs_db()
        run = db.latest_run_for_app(resource["app_id"]) if db else None
        if run is None:
            return None
        return run.get("status")

    def start_agent_session(self, adapter_id, project_id, prompt):
        runner = get_agent_runner(self._cfg)
        if runner is None:
            return None, "运行历史数据库不可用"
        return runner.start(adapter_id, project_id, prompt=prompt or "")

    def stop_agent_session(self, session_id):
        runner = get_agent_runner(self._cfg)
        if runner is None:
            return False, "运行历史数据库不可用"
        return runner.stop(session_id)

    def get_agent_session(self, session_id):
        runner = get_agent_runner(self._cfg)
        return runner.get_session(session_id) if runner else None

    def agent_session_alive(self, session_id):
        session = self.get_agent_session(session_id)
        return bool(session and session.get("status") in (
            "running", "starting"))

    def project_root(self, project_id):
        snapshot = self._snapshot()
        project = next(
            (p for p in snapshot.get("projects") or []
             if p.get("id") == project_id), None)
        return project.get("root_path") if project else None


def get_workflow_executor(cfg=None):
    """惰性创建 WorkflowExecutor（hooks 绑定 cfg）。"""
    global WORKFLOW_EXECUTOR
    if WORKFLOW_EXECUTOR is None:
        with _workflow_executor_lock:
            if WORKFLOW_EXECUTOR is None:
                db = get_runs_db()
                if db is None:
                    return None
                WORKFLOW_EXECUTOR = WorkflowExecutor(
                    db=db,
                    hooks=ServerExecutorHooks(cfg),
                    locks=LockManager(),
                    events=EVENTS)
    if cfg is not None and WORKFLOW_EXECUTOR is not None:
        if isinstance(WORKFLOW_EXECUTOR._hooks, ServerExecutorHooks):
            WORKFLOW_EXECUTOR._hooks._cfg = cfg
    return WORKFLOW_EXECUTOR


def get_runs_db():
    """惰性打开运行历史数据库；失败降级为 None（API 返回空，不阻塞运行）。"""
    global RUNS_DB
    if RUNS_DB is None:
        with _runs_db_lock:
            if RUNS_DB is None:
                try:
                    RUNS_DB = RunDatabase(RUNS_DB_PATH)
                except Exception:
                    LOG.exception("打开运行历史数据库失败")
                    RUNS_DB = False
    return RUNS_DB or None
MANUAL_STOP_LOCK = threading.RLock()
MANUAL_STOP_TOKENS = set()

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".otf": "font/otf",
    ".woff2": "font/woff2",
}

PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>总控台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f7;color:#1d1d1f}
.card{background:#fff;border:1px solid rgba(0,0,0,.06);border-radius:14px;padding:36px 44px;box-shadow:0 8px 30px rgba(0,0,0,.08);max-width:540px;text-align:center}
h1{font-size:20px;margin:0 0 14px}p{color:#6e6e73;font-size:14px;line-height:1.8;margin:6px 0}
code{background:#f5f5f7;border:1px solid rgba(0,0,0,.05);border-radius:6px;padding:2px 7px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
</style></head>
<body><div class="card">
<h1>🖥 总控台后端运行中</h1>
<p>前端文件 <code>static/index.html</code> 尚未提供，界面暂不可用。</p>
<p>API 已就绪：<code>GET /api/state</code></p>
</div></body></html>"""

APP_ROUTE_RE = re.compile(
    r"^/api/apps/([0-9a-fA-F]{8})(?:/(start|stop|restart|icon|logs|favicon|diagnose|attach))?$")


# ---------------------------------------------------------------- 运行目录

def _ensure_private_dir(path):
    if os.path.islink(path):
        raise OSError("私有运行目录不能是符号链接: %s" % path)
    PLATFORM.ensure_private_dir(path)
    if os.path.islink(path) or not os.path.isdir(path):
        raise OSError("私有运行路径不是安全目录: %s" % path)


def _copy_private_regular_file(source, target):
    """不跟随符号链接地复制普通文件，目标权限固定为 0600。"""
    try:
        source_stat = os.lstat(source)
    except OSError:
        return False
    if not stat.S_ISREG(source_stat.st_mode):
        return False
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    try:
        target_fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(os.dup(source_fd), "rb") as src, \
                    os.fdopen(target_fd, "wb") as dst:
                target_fd = -1
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
        finally:
            if target_fd >= 0:
                os.close(target_fd)
    finally:
        os.close(source_fd)
    os.chmod(target, 0o600)
    return True


def _install_migrated_directory(target, populate):
    """在目标不存在时原子安装一份迁移副本。"""
    if os.path.lexists(target):
        return False
    parent = os.path.dirname(target) or "."
    # parent 可能是用户共用的 ~/Library/Application Support，
    # 只确保存在，不擅自改它的现有权限。
    os.makedirs(parent, mode=0o700, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".console-migration-", dir=parent)
    installed = False
    try:
        os.chmod(staging, 0o700)
        populate(staging)
        try:
            os.rename(staging, target)
            installed = True
        except OSError as e:
            # 另一个同时启动的实例可能已经完成迁移。
            if not os.path.lexists(target) or e.errno not in (
                    errno.EEXIST, errno.ENOTEMPTY):
                raise
        return installed
    finally:
        if not installed and os.path.isdir(staging):
            shutil.rmtree(staging)


def migrate_legacy_runtime_data(
        data_dir=DATA_DIR, logs_dir=LOGS_DIR,
        legacy_data_dir=LEGACY_DATA_DIR,
        data_overridden=DATA_DIR_OVERRIDDEN,
        logs_overridden=LOGS_DIR_OVERRIDDEN):
    """首次运行时将项目内旧数据复制到 macOS 用户目录。

    只在对应目标完全不存在且没有显式环境变量覆盖时执行。
    旧文件不会被删除或改权限。
    """
    result = {"dataMigrated": False, "logsMigrated": False}
    legacy_data_dir = os.path.abspath(legacy_data_dir)
    data_dir = os.path.abspath(data_dir)
    logs_dir = os.path.abspath(logs_dir)

    if (not data_overridden and data_dir != legacy_data_dir
            and os.path.isdir(legacy_data_dir)
            and not os.path.lexists(data_dir)):
        def populate_data(staging):
            for name in ("config.json", "config.json.bak"):
                _copy_private_regular_file(
                    os.path.join(legacy_data_dir, name),
                    os.path.join(staging, name))
            source_icons = os.path.join(legacy_data_dir, "icons")
            if os.path.isdir(source_icons) and not os.path.islink(source_icons):
                target_icons = os.path.join(staging, "icons")
                os.mkdir(target_icons, 0o700)
                for name in os.listdir(source_icons):
                    if os.path.basename(name) != name:
                        continue
                    _copy_private_regular_file(
                        os.path.join(source_icons, name),
                        os.path.join(target_icons, name))

        result["dataMigrated"] = _install_migrated_directory(
            data_dir, populate_data)

    legacy_logs = os.path.join(legacy_data_dir, "logs")
    if (not logs_overridden and logs_dir != legacy_logs
            and os.path.isdir(legacy_logs) and not os.path.islink(legacy_logs)
            and not os.path.lexists(logs_dir)):
        def populate_logs(staging):
            for name in os.listdir(legacy_logs):
                if os.path.basename(name) != name:
                    continue
                _copy_private_regular_file(
                    os.path.join(legacy_logs, name),
                    os.path.join(staging, name))

        result["logsMigrated"] = _install_migrated_directory(
            logs_dir, populate_logs)
    return result


def prepare_runtime_storage():
    migration = migrate_legacy_runtime_data()
    for private_dir in (DATA_DIR, ICONS_DIR, LOGS_DIR):
        _ensure_private_dir(private_dir)
    for path in (CONFIG_PATH, CONFIG_PATH + ".bak", INSTANCE_LOCK_PATH):
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.chmod(path, 0o600)
        except OSError:
            pass
    for directory in (ICONS_DIR, LOGS_DIR):
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        os.chmod(entry.path, 0o600)
                except OSError:
                    LOG.warning("无法收紧文件权限: %s", entry.path)
    return migration


def write_private_bytes(path, payload):
    """以 0600 权限写入用户数据文件（Windows 上权限为尽力而为）。"""
    fd = PLATFORM.open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    PLATFORM.chmod_private(path, 0o600)


# ---------------------------------------------------------------- 配置


class Config(CoreConfig):
    """Legacy entrypoint wired to the extracted configuration Module."""

    def __init__(self, path):
        super().__init__(
            path,
            on_change=invalidate_state_cache,
            logger=LOG,
            ensure_private_dir=_ensure_private_dir,
        )


def acquire_instance_lock(path=INSTANCE_LOCK_PATH):
    """Acquire the per-project process lock and keep its file object alive.

    Port fallback alone is not a single-instance guarantee: two servers on
    :9600/:9601 would still update the same config.  The lock ties
    exclusivity to this data directory and is released automatically if the
    process crashes (macOS flock / Windows byte-range lock).
    """
    return PLATFORM.acquire_lock(path)


def release_instance_lock(lock_file):
    PLATFORM.release_lock(lock_file)


# ---------------------------------------------------------------- 子进程与解析

def run_cmd(args, timeout=SUBPROCESS_TIMEOUT):
    """运行命令并返回 stdout；任何异常/超时都返回空串，绝不上抛。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        LOG.exception("命令执行失败: %r", args)
        return ""


def scan_listeners():
    """lsof 监听快照 → {(pid, port): {bind_host, ...}}。

    字典仍可像旧集合一样迭代/判断 ``(pid, port)``，同时保留监听地址，
    供前端区分仅监听 ``::1`` 的服务（需通过 localhost 打开）。
    """
    return PLATFORM.listeners()


def ps_snapshot(pids=None, with_uid=True):
    """批量进程信息 → {pid: {"uid","comm","args","cpu","mem","etime"}}。

    pids=None 表示全部进程。采集与解析由 PlatformAdapter 负责
    （macOS: ps；Windows: CIM），返回结构与旧 ps 解析完全一致。
    """
    return PLATFORM.process_snapshot(pids, with_uid=with_uid)


def lsof_cwds(pids):
    """进程工作目录 → {pid: cwd}（Windows 不可得时返回 {}）。"""
    return PLATFORM.process_cwds(pids)


def pid_alive(pid):
    return PLATFORM.pid_alive(pid)


# ---------------------------------------------------------------- 状态构建

def origin_snapshot():
    """进程溯源表 → {pid: (ppid, args)}，供来源溯源。"""
    return PLATFORM.origin_snapshot()


def build_services(cfg, groups=None):
    """返回 (services, listeners)。只含当前用户进程，排除控制台自身。"""
    listeners = scan_listeners()
    snap = ps_snapshot({pid for pid, _ in listeners}, with_uid=True)
    mine_pids = [pid for pid, _ in listeners
                 if pid != SELF_PID and pid in snap
                 and snap[pid].get("uid") == SELF_UID]
    cwds = lsof_cwds(mine_pids)
    origin_table = origin_snapshot()

    hidden = set(cfg.get("hidden") or [])
    pinned = set(cfg.get("pinned") or [])
    promoted = set(cfg.get("promoted") or [])
    # “配置了相同端口”不代表“拥有当前监听进程”。只有 run token / 进程组
    # 校验通过（或严格命中旧版身份）的进程才关联启动台卡片。
    app_by_pid = listener_app_owners(
        cfg.get("apps") or [], listeners, snap, cwds, groups)

    services = []
    for pid, port in sorted(listeners, key=lambda x: (x[1], x[0])):
        if pid == SELF_PID:
            continue
        info = snap.get(pid)
        if not info or info.get("uid") != SELF_UID:
            continue
        comm = info.get("comm") or ""
        args = info.get("args") or comm
        name = os.path.basename(comm) if comm else "?"
        key = "%s:%d" % (name, port)
        cwd = cwds.get(pid)
        app = app_by_pid.get(pid)
        services.append({
            "key": key,
            # key 保持 name:port 以兼容既有隐藏/置顶配置；instanceKey 用于
            # 区分同名同端口在不同时间出现的新进程，以及极少数共享监听。
            "instanceKey": "%d:%d" % (pid, port),
            "pid": pid, "name": name, "port": port,
            "openHost": listener_open_host(listeners, port, {pid}),
            "cwd": cwd, "project": project_name(cwd), "cmd": args,
            "cpu": info["cpu"], "mem": info["mem"], "uptimeSec": info["etime"],
            "group": classify_group(key, name, comm, args, cwd, promoted),
            "pinned": key in pinned, "hidden": key in hidden,
            "promoted": key in promoted,
            "appId": app["id"] if app else None,
            "appName": app["name"] if app else None,
            # 来源溯源（尽力判断）：哪个应用/AI 助手启动了这个进程
            "origin": attribute_origin(pid, origin_table),
        })
    return services, listeners


def build_watched(keywords):
    """关注进程：每个 PID 只返回一次，并合并它命中的全部关键字。"""
    normalized = []
    seen_keywords = set()
    for keyword in (keywords or []):
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        keyword = keyword.strip()
        lowered = keyword.casefold()
        if lowered in seen_keywords:
            continue
        seen_keywords.add(lowered)
        normalized.append((keyword, lowered))
    if not normalized:
        return []
    snap = ps_snapshot(None, with_uid=True)
    result = []
    for pid, info in sorted(snap.items()):
        if pid == SELF_PID or info.get("uid") != SELF_UID:
            continue
        name = os.path.basename(info.get("comm") or "") or "?"
        if name in ("ps", "lsof"):
            continue
        args = info.get("args") or ""
        args_lower = args.casefold()
        matched = [keyword for keyword, lowered in normalized
                   if lowered in args_lower]
        if not matched:
            continue
        result.append({"pid": pid, "name": name, "cmd": args,
                       "cpu": info["cpu"], "mem": info["mem"],
                       "uptimeSec": info["etime"],
                       # keyword 保留给旧前端，keywords 提供无损结构化数据。
                       "keyword": "、".join(matched), "keywords": matched})
    return result


def pgid_members_map():
    """进程组 → {pgid: [pid, ...]}。

    macOS 由 ps 采集；Windows 无进程组语义，由 adapter 返回空表，
    受管身份走 PID + token + 进程树通道。
    """
    return PLATFORM.group_members_map()


def managed_process_index(apps, groups=None):
    """批量校验应用的受控进程，返回 (appId -> [pid], ps, groups)。

    macOS：属于记录的进程组、属于当前用户、argv 中带本次启动的随机 token
    三者同时满足才算受控；Windows：lastPid 存活 + 当前用户 + 命令行携带
    token 标记的 cmd 包装进程及其后代树。即使 PID 被系统复用，也不会把
    无关进程当成应用或停止它。
    """
    if IS_WINDOWS:
        return _managed_process_index_windows(apps, groups)
    if groups is None:
        needs_groups = any(
            app.get("runToken")
            and isinstance(app.get("lastPgid") or app.get("lastPid"), int)
            for app in apps)
        groups = pgid_members_map() if needs_groups else {}
    all_pids = set()
    for app in apps:
        all_pids.update(core_managed_candidate_pids(app, groups))
    snap = ps_snapshot(all_pids, with_uid=True) if all_pids else {}
    result = core_managed_process_index(
        apps,
        groups,
        snap,
        current_uid=SELF_UID,
        run_token_arg_prefix=RUN_TOKEN_ARG_PREFIX,
    )
    return result, snap, groups


def _managed_process_index_windows(apps, groups=None):
    """Windows 受管身份：cmd 包装进程带 token 标记，后代树并入受管集。"""
    controllers = {}
    for app in apps:
        token = app.get("runToken")
        pid = app.get("lastPid")
        if (isinstance(token, str) and token
                and isinstance(pid, int) and pid > 0):
            controllers[app.get("id")] = pid
    if not controllers:
        return {}, {}, {}
    snap = ps_snapshot(list(controllers.values()), with_uid=True)
    origin = origin_snapshot()
    result = core_managed_process_index_windows(
        apps,
        snap,
        origin,
        current_user=SELF_UID,
        # Windows 批处理文件名 console-run-<token>.cmd（冒号非法字符）
        run_token_marker="console-run-",
    )
    return result, snap, {}


def managed_pids(app, groups=None):
    index, _, _ = managed_process_index([app], groups)
    return index.get(app.get("id"), [])


def legacy_managed_pid(app, listeners=None, snap=None, cwds=None):
    """识别升级前身份或用户明确认领的外部监听进程。

    普通旧数据仍只接受原 lastPid。明确 ``attached`` 的卡片允许监听子进程
    换 PID，但仍必须在配置端口上按当前 UID + 真实 cwd 唯一命中；因此
    Next/Vite 等重建子进程后不会丢失关联，也不会只凭端口误认其他项目。
    """
    if not core_legacy_identity_applicable(app):
        return None
    if listeners is None:
        listeners = scan_listeners()
    port_pids = core_legacy_candidate_pids(app, listeners)
    if not port_pids:
        return None
    if snap is None:
        snap = ps_snapshot(port_pids, with_uid=True)
    if cwds is None:
        cwds = lsof_cwds(port_pids)
    return core_legacy_managed_pid(
        app,
        listeners,
        snap,
        cwds,
        current_uid=SELF_UID,
        cwd_equal=lambda actual, expected: (
            os.path.realpath(actual) == os.path.realpath(expected)),
    )


def listener_app_owners(apps, listeners, snap, cwds, groups=None):
    """返回真实受管监听进程的 ``pid -> app`` 映射。

    端口只是配置与网络资源，不能作为进程所有权证明。映射沿用应用状态的
    run token / PGID / UID 校验，并为升级前的进程保留严格 legacy 识别。
    如果异常配置让同一 PID 同时命中多张卡片，则不做关联，避免误导 UI。
    """
    managed, _, _ = managed_process_index(apps, groups)
    if cwds is None:
        legacy_pids = set()
        for app in apps:
            legacy_pids.update(core_legacy_candidate_pids(app, listeners))
        cwds = lsof_cwds(legacy_pids) if legacy_pids else {}
    return core_listener_app_owners(
        apps,
        listeners,
        snap,
        cwds,
        groups,
        current_uid=SELF_UID,
        cwd_equal=lambda actual, expected: (
            os.path.realpath(actual) == os.path.realpath(expected)),
        run_token_arg_prefix=RUN_TOKEN_ARG_PREFIX,
        managed_by_app=managed,
    )


def build_apps(cfg, listeners, groups=None):
    """token 校验通过或严格命中旧版身份的进程才算 running。

    多张卡片可共享配置端口；只有当前真实监听者不属于本卡片时才返回
    “端口被其他进程占用”，不再把任意监听者误当成应用本身。
    """
    port_map = {}
    for pid, port in listeners:
        port_map.setdefault(port, []).append(pid)
    apps_cfg = cfg.get("apps") or []
    managed, snap, _ = managed_process_index(apps_cfg, groups)
    # M3：resource → legacy app 关联，供前端按项目分组
    project_by_app = {}
    if cfg.get("projects"):
        project_names = {
            project.get("id"): project.get("name")
            for project in cfg.get("projects") or []}
        for resource in cfg.get("resources") or []:
            app_id = resource.get("app_id")
            if app_id:
                project_by_app[app_id] = (
                    resource.get("project_id"),
                    project_names.get(resource.get("project_id")))
    listen_by_pid = {}
    for pid, port in listeners:
        listen_by_pid.setdefault(pid, []).append(port)
    configured_ports = {
        app["port"] for app in apps_cfg if app.get("port")}

    # 端口诊断需要展示占用者的真实身份，一次批量取详情，避免逐卡 ps。
    configured_listener_pids = {
        pid for port in configured_ports for pid in port_map.get(port, [])}
    listener_snap = (ps_snapshot(configured_listener_pids, with_uid=True)
                     if configured_listener_pids else {})
    listener_cwds = lsof_cwds(configured_listener_pids)
    verified_owner = listener_app_owners(
        apps_cfg, listeners, listener_snap, listener_cwds)

    apps = []
    for app in apps_cfg:
        managed_live = managed.get(app["id"], [])
        legacy_pid = None if managed_live else legacy_managed_pid(
            app, listeners, listener_snap, listener_cwds)
        if (legacy_pid and
                (verified_owner.get(legacy_pid) or {}).get("id") != app.get("id")):
            legacy_pid = None
        live = managed_live or ([legacy_pid] if legacy_pid else [])
        lp = app.get("lastPid")
        pid = lp if lp in live else (live[0] if live else None)
        port = app.get("port")
        configured_listeners = port_map.get(port, []) if port else []
        listening = bool(port and any(p in live for p in configured_listeners))
        occupied = bool(port and configured_listeners and not listening)
        owner_pid = configured_listeners[0] if occupied else None
        owner_info = listener_snap.get(owner_pid, {}) if owner_pid else {}
        owner_app = verified_owner.get(owner_pid)
        owner_cwd = listener_cwds.get(owner_pid) if owner_pid else None
        port_owner = None
        if owner_pid:
            comm = owner_info.get("comm") or ""
            port_owner = {
                "pid": owner_pid,
                "openHost": listener_open_host(
                    listeners, port, {owner_pid}),
                "name": os.path.basename(comm) or "?",
                "cmd": owner_info.get("args") or comm,
                "cwd": owner_cwd,
                "project": project_name(owner_cwd),
                "uid": owner_info.get("uid"),
                "currentUser": owner_info.get("uid") == SELF_UID,
                "uptimeSec": owner_info.get("etime"),
                "appId": owner_app.get("id") if owner_app else None,
                "appName": owner_app.get("name") if owner_app else None,
            }
        actual_ports = sorted({p for member in live
                               for p in listen_by_pid.get(member, [])})
        open_hosts = {
            str(actual_port): listener_open_host(
                listeners, actual_port, set(live))
            for actual_port in actual_ports
        }
        try:
            health = inspect_app_health(app)
        except Exception as exc:
            LOG.warning("检查应用配置失败（%s）：%s", app.get("id"), exc)
            health = {"status": "unknown", "blocking": False, "issues": []}
        apps.append({
            "id": app["id"], "name": app["name"], "command": app["command"],
            "cwd": app.get("cwd"), "port": port,
            "emoji": app.get("emoji"), "glyph": app.get("glyph"), "icon": app.get("icon"),
            "favicon": app.get("favicon"),
            "running": bool(live), "pid": pid,
            "uptimeSec": ((snap.get(pid) or listener_snap.get(pid) or {}).get("etime")
                          if pid else None),
            "kind": app.get("kind") or "service",
            "attached": bool(app.get("attached")),
            "lastExit": public_last_exit(app),
            "health": health,
            "ports": actual_ports,
            "openHosts": open_hosts,
            "listening": listening,
            "portOccupied": occupied,
            "portOccupiedPid": configured_listeners[0] if occupied else None,
            "portOwner": port_owner,
            # 多张停止卡片可以共享常见开发端口；只有真正启动时的监听占用
            # 才是冲突。字段保留给旧前端兼容，但不再表示配置重复。
            "portConflict": False,
            "portConflictApps": [],
            "legacyManaged": bool(legacy_pid),
            "projectId": (project_by_app.get(app["id"]) or (None, None))[0],
            "projectName": (project_by_app.get(app["id"]) or (None, None))[1],
        })
    return apps


def ensure_project_domain(cfg):
    """legacy apps → projects 一次性幂等填充（M3 §9.2）。"""
    try:
        cfg.update(assign_resources_from_apps)
    except Exception:
        LOG.exception("项目域填充失败")


def build_project_summaries(cfg, app_rows):
    """项目摘要：资源计数 + 运行计数（运行时身份仍来自 legacy apps）。"""
    projects = cfg.get("projects") or []
    if not projects:
        return []
    resources = cfg.get("resources") or []
    running_app_ids = {
        row.get("id") for row in app_rows if row.get("running")}
    running_resource_ids = {
        resource.get("id") for resource in resources
        if resource.get("app_id") in running_app_ids}
    return [
        project_summary(project, resources, running_resource_ids)
        for project in projects
    ]


def _v1_projects(snapshot):
    """/api/v1/projects：项目 + 其资源（含 app_id 桥）。"""
    projects = snapshot.get("projects") or []
    resources = snapshot.get("resources") or []
    result = []
    for project in projects:
        item = {
            key: project.get(key)
            for key in ("id", "name", "root_path", "repo_path",
                        "workspace_id", "environment", "tags")
        }
        item["resources"] = [
            dict(resource) for resource in resources
            if resource.get("project_id") == project.get("id")]
        result.append(item)
    return result


def _v1_resources(snapshot):
    return [dict(resource) for resource in snapshot.get("resources") or []]


def _v1_runs(query):
    db = get_runs_db()
    if not db:
        return {"runs": [], "total": 0}
    runs = db.list_runs(
        query.get("limit", 50),
        app_id=query.get("app_id"),
        status=query.get("status"))
    return {"runs": [public_run(run) for run in runs], "total": len(runs)}


def _v1_int(value, default):
    try:
        return max(1, min(int(value), 500))
    except (TypeError, ValueError):
        return default


def _public_session(session):
    """Agent session API projection."""
    if session is None:
        return None
    return {
        "id": session.get("id"),
        "projectId": session.get("project_id"),
        "adapterId": session.get("adapter_id"),
        "workflowRunId": session.get("workflow_run_id"),
        "workflowStepId": session.get("workflow_step_id"),
        "status": session.get("status"),
        "pid": session.get("pid"),
        "startedAt": session.get("started_at"),
        "endedAt": session.get("ended_at"),
        "exitCode": session.get("exit_code"),
        "logPath": session.get("log_path"),
        "promptRef": session.get("prompt_ref"),
        "durationSec": (
            round(max(0.0, session["ended_at"] - session["started_at"]), 3)
            if session.get("ended_at") and session.get("started_at") else None),
    }


def _public_workflow_run(run, db):
    """Workflow run API projection (steps included)."""
    if run is None:
        return None
    step_runs = db.list_step_runs(run["id"]) if db else []
    return {
        "id": run.get("id"),
        "workflowId": run.get("workflow_id"),
        "workflowVersion": run.get("workflow_version"),
        "projectId": run.get("project_id"),
        "name": run.get("name"),
        "status": run.get("status"),
        "startedAt": run.get("started_at"),
        "endedAt": run.get("ended_at"),
        "steps": [{
            "stepId": sr.get("step_id"),
            "kind": sr.get("kind"),
            "status": sr.get("status"),
            "retries": sr.get("retries", 0),
            "runRef": sr.get("run_ref"),
            "startedAt": sr.get("started_at"),
            "endedAt": sr.get("ended_at"),
            "error": sr.get("error"),
        } for sr in step_runs],
    }


def build_state(cfg, console_port, config_health=None):
    degraded_reasons = []
    # 一次 pgid 快照供 build_services / build_apps 共享，避免每轮两次全量 ps。
    needs_groups = any(
        app.get("runToken")
        and isinstance(app.get("lastPgid") or app.get("lastPid"), int)
        for app in cfg.get("apps") or [])
    groups = pgid_members_map() if needs_groups else None
    try:
        services, listeners = build_services(cfg, groups)
    except Exception as e:
        LOG.exception("构建服务监控状态失败")
        services, listeners = [], set()
        degraded_reasons.append({"component": "services"})
    try:
        watched = build_watched(cfg.get("watchedKeywords"))
    except Exception as e:
        LOG.exception("构建关注进程状态失败")
        watched = []
        degraded_reasons.append({"component": "watched"})
    try:
        apps = build_apps(cfg, listeners, groups)
    except Exception as e:
        LOG.exception("构建启动台状态失败")
        apps = []
        degraded_reasons.append({"component": "apps"})
    if VERSION_LOAD_ERROR:
        degraded_reasons.append(
            {"component": "version", "error": VERSION_LOAD_ERROR})
    for issue in (config_health or {}).get("issues", []):
        degraded_reasons.append({"component": "config", "error": issue})
    try:
        projects = build_project_summaries(cfg, apps)
    except Exception:
        LOG.exception("构建项目摘要失败")
        projects = []
    return {
        "services": services,
        "watched": watched,
        "apps": apps,
        "projects": projects,
        "watchedKeywords": cfg.get("watchedKeywords") or [],
        "consolePort": console_port,
        "consolePid": SELF_PID,
        "consoleCwd": BASE_DIR,
        "version": APP_VERSION,
        "schemaVersion": cfg.get("schemaVersion", CURRENT_SCHEMA_VERSION),
        "degraded": bool(degraded_reasons),
        "degradedReasons": degraded_reasons,
        "configHealth": dict(config_health or {}),
        "uiTheme": cfg.get("uiTheme") or DEFAULT_UI_THEME,
        "themes": list_themes(),
    }


# ---------------------------------------------------------------- 状态快照缓存
# 每次快照要跑约十余个 ps/lsof 子进程。TTL 略大于前端 2s 轮询周期：
# 单标签页约每 2-3 轮重建一次，多标签页请求自动合并（锁内构建排队后
# 第二个请求直接命中缓存）。配置/进程变更时 invalidate 立即失效。
STATE_CACHE_TTL = 2.2  # 秒
_state_cache_lock = threading.Lock()
_state_cache = {"mono": 0.0, "state": None}


def invalidate_state_cache():
    with _state_cache_lock:
        _state_cache["state"] = None


def get_state_snapshot(cfg, console_port):
    now = time.monotonic()
    with _state_cache_lock:
        cached = _state_cache["state"]
        if cached is not None and now - _state_cache["mono"] < STATE_CACHE_TTL:
            return cached
        state = build_state(cfg.snapshot(), console_port, cfg.health_info())
        _state_cache["mono"] = time.monotonic()
        _state_cache["state"] = state
        return state


def build_health(cfg):
    """不执行 ps/lsof 的轻量健康检查。"""
    health = cfg.health_info()
    issues = list(health.get("issues") or [])
    if VERSION_LOAD_ERROR:
        issues.append("VERSION 读取失败: %s" % VERSION_LOAD_ERROR)
    for label, path in (("data", DATA_DIR), ("icons", ICONS_DIR),
                        ("logs", LOGS_DIR)):
        if not os.path.isdir(path):
            issues.append("%s 目录不存在" % label)
        elif not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            issues.append("%s 目录不可读写" % label)
        else:
            try:
                mode = os.lstat(path).st_mode
                # Windows 无 POSIX 权限位，跳过 0700 位检查
                if not IS_WINDOWS and (stat.S_ISLNK(mode) or mode & 0o077):
                    issues.append("%s 目录权限不是 0700" % label)
            except OSError as e:
                issues.append("无法检查 %s 目录: %s" % (label, e))
    for label, path in (("config", CONFIG_PATH),
                        ("configBackup", CONFIG_PATH + ".bak")):
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if label == "config":
                issues.append("主配置文件不存在")
            continue
        except OSError as e:
            issues.append("无法检查 %s: %s" % (label, e))
            continue
        if not stat.S_ISREG(mode):
            issues.append("%s 不是普通文件" % label)
        elif not IS_WINDOWS and mode & 0o077:
            issues.append("%s 文件权限不是 0600" % label)
    degraded = bool(issues)
    snapshot = cfg.snapshot()
    return {
        "ok": not degraded,
        "status": "degraded" if degraded else "ok",
        "version": APP_VERSION,
        "schemaVersion": snapshot.get(
            "schemaVersion", CURRENT_SCHEMA_VERSION),
        "degraded": degraded,
        "issues": issues,
        "config": health,
    }


def list_themes():
    """扫描 static/themes/*.json 主题清单（css 文件必须存在），供注册切换。
    默认主题固定排在首位，其余按文件名排序。"""
    themes = []
    try:
        names = sorted(os.listdir(THEMES_DIR))
    except OSError:
        return themes
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(THEMES_DIR, name), "r", encoding="utf-8") as f:
                meta = json.load(f)
            theme_id = str(meta.get("id") or os.path.splitext(name)[0])
            if not theme_id or not os.path.isfile(
                    os.path.join(THEMES_DIR, theme_id + ".css")):
                continue
            themes.append({
                "id": theme_id,
                "name": str(meta.get("name") or theme_id),
                "author": str(meta.get("author") or ""),
                "desc": str(meta.get("desc") or ""),
                "colors": [str(c) for c in (meta.get("colors") or [])][:6],
            })
        except Exception:
            LOG.exception("读取主题清单失败: %s", name)
    themes.sort(key=lambda t: t["id"] != DEFAULT_UI_THEME)
    return themes


# ---------------------------------------------------------------- 进程/应用操作

def process_uid(pid):
    """返回进程用户标识（macOS 为 uid，Windows 为用户名）；不存在返回 None。"""
    return PLATFORM.process_user_id(pid)


def kill_process(pid, force):
    """结束单个进程；只允许当前用户的进程。返回 (ok, error)。"""
    if pid == SELF_PID:
        return False, "不能结束总控台自身进程"
    uid = process_uid(pid)
    if uid is None:
        return False, "进程不存在"
    if uid != SELF_UID:
        return False, "只能结束当前用户的进程"
    return PLATFORM.kill_process(pid, force)


def stop_pid_tree(pid, sig=signal.SIGTERM):
    """停止受控进程树：macOS 对进程组发信号；Windows 走 taskkill /T。

    ProcessLookupError means the target completed between validation and the
    signal and is therefore an idempotent success. Permission and other OS
    failures must never be swallowed: callers use them to retain management
    identity instead of creating an orphan process.
    """
    if IS_WINDOWS:
        return PLATFORM.terminate_tree(pid, force=(sig == SIGKILL))
    return PLATFORM.signal_group(pid, sig)


def app_running(app, listeners=None):
    return bool(managed_pids(app) or legacy_managed_pid(app, listeners))


def app_alive_sign(app, listeners=None):
    """start/stop 的存活判断：新版 token 或严格校验通过的旧版身份。"""
    return app_running(app, listeners)


def build_launch_env(token, environ=None):
    """构建无 Terminal 启动时仍可找到常见开发工具的环境。

    macOS 补入 Homebrew、npm/pnpm、Volta、NVM、fnm 等目录；Windows 继承
    当前 PATH。均由 PlatformAdapter 实现。
    """
    return PLATFORM.launch_env(token, environ)


def start_app(app):
    """返回 (ok, error, proc|None, pgid|None, token|None)。"""
    _ensure_private_dir(LOGS_DIR)
    log_path = os.path.join(LOGS_DIR, "%s.log" % app["id"])
    rotate_log_file(log_path)
    cwd = app.get("cwd") or os.path.expanduser("~")
    try:
        log_fd = PLATFORM.open_private(
            log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        logf = os.fdopen(log_fd, "ab", buffering=0)
    except OSError as e:
        return False, "无法打开日志文件: %s" % e, None, None, None
    token = secrets.token_urlsafe(24)
    env = build_launch_env(token)
    try:
        header = "\n===== 启动于 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")
        logf.write(header.encode("utf-8"))
        proc, group_id = PLATFORM.start_process(cwd, env, logf, app["command"], token)
    except Exception as e:
        logf.close()
        return False, "启动失败: %s" % e, None, None, None
    logf.close()  # 子进程已持有副本，父进程关闭避免 fd 泄漏
    return True, None, proc, group_id, token


def startup_failure_message(app_id, code):
    """从日志末尾提取一行可直接显示给用户的启动错误。"""
    text = read_log_tail(app_id, 30)
    for line in reversed(text.splitlines()):
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
        if line and not line.startswith("====="):
            if len(line) > 180:
                line = line[:179] + "…"
            return "启动命令立即退出（exit %s）：%s" % (code, line)
    return "启动命令立即退出（exit %s），请查看日志" % code


def project_id_for_app(cfg_snapshot, app_id):
    """resource(app_id 桥) → project_id；无桥时为 None（M3 兼容）。"""
    for resource in cfg_snapshot.get("resources") or []:
        if resource.get("app_id") == app_id:
            return resource.get("project_id")
    return None


def record_run_start(cfg, app_id, proc, pgid, token):
    """受控启动成功后创建 durable run 记录（M4 §14）。"""
    db = get_runs_db()
    if not db:
        return None
    snapshot = cfg.snapshot()
    app = find_app(snapshot, app_id)
    if app is None:
        return None
    kind = app.get("kind") or "service"
    if kind not in ("service", "task"):
        kind = "service"
    run = make_run(
        app_id=app_id,
        project_id=project_id_for_app(snapshot, app_id),
        kind=kind,
        pid=proc.pid,
        process_group_id=pgid,
        run_token=token,
        log_path=os.path.join(LOGS_DIR, "%s.log" % app_id),
    )
    try:
        db.insert_run(run)
        EVENTS.publish("run.created", public_run(run))
    except Exception:
        LOG.exception("写入运行历史失败")
        return None
    return run


def finalize_runs_for_app(app_id, code, manual_stop, ended_at):
    """把最新 running run 归一到终态（幂等：只转换 running）。"""
    db = get_runs_db()
    if not db:
        return
    run = db.latest_run_for_app(app_id)
    if not run or run.get("status") != "running":
        return
    status = finalize_run_status(run, code, manual_stop=manual_stop)
    try:
        db.update_run(run["id"], {
            "status": status,
            "ended_at": int(ended_at),
            "exit_code": code if code is not None else None,
        })
        EVENTS.publish("run.updated", public_run(db.get_run(run["id"])))
    except Exception:
        LOG.exception("更新运行历史失败")


def reconcile_runs(cfg):
    """daemon 重启对账：running 记录按当前受管身份重验，消失的标记 lost。

    绝不把 vanished 的工作标记为成功（SPEC §12.3）。"""
    db = get_runs_db()
    if not db:
        return
    snapshot = cfg.snapshot()
    for run in db.running_runs():
        alive = False
        app = find_app(snapshot, run.get("app_id")) if run.get("app_id") else None
        if app is not None:
            try:
                alive = bool(app_running(app))
            except Exception:
                alive = False
        if not alive:
            try:
                db.update_run(run["id"], {
                    "status": "lost",
                    "ended_at": int(time.time()),
                })
                EVENTS.publish("run.updated", public_run(db.get_run(run["id"])))
            except Exception:
                LOG.exception("运行对账失败")


def start_run_guard(cfg):
    """低频率监护：对账后仍 running 的 run（重启前启动、无 watch 线程的
    独立进程）在进程消失时标记 lost，不误报成功。"""
    def _guard():
        while True:
            time.sleep(15)
            try:
                reconcile_runs(cfg)
                runner = get_agent_runner(cfg)
                if runner is not None:
                    runner.reconcile()
                executor = get_workflow_executor(cfg)
                if executor is not None:
                    executor.recover()
            except Exception:
                LOG.exception("运行监护失败")
    thread = threading.Thread(target=_guard, daemon=True)
    thread.start()
    return thread


def watch_app_exit(cfg, app_id, proc, token, started_at=None):
    """后台线程等子进程退出：若期间未被手动 stop/重启（lastPid 仍指向它），
    记录 lastExit（退出码、结束时间和运行耗时）。保留 lastPid 作为进程组锚点——
    脚本可能把服务放后台后退出，后续的运行判定/停止都靠 pgid 找到存活成员。"""
    started_at = time.time() if started_at is None else started_at

    def _wait():
        code = proc.wait()
        ended_at = time.time()
        duration = round(max(0.0, ended_at - started_at), 3)

        with MANUAL_STOP_LOCK:
            manually_stopped = (app_id, token) in MANUAL_STOP_TOKENS

        def op(c):
            target = find_app(c, app_id)
            if (not manually_stopped and target
                    and target.get("lastPid") == proc.pid
                    and target.get("runToken") == token):
                last_exit = {
                    "code": code,
                    "at": int(ended_at),
                    "startedAt": int(started_at * 1000),
                    "durationSec": duration,
                }
                if (target.get("kind") or "service") == "task":
                    last_exit["status"] = classify_task_exit(code)
                target["lastExit"] = last_exit
        cfg.update(op)
        finalize_runs_for_app(app_id, code, manually_stopped, ended_at)
        if IS_WINDOWS:
            try:
                os.remove(os.path.join(
                    tempfile.gettempdir(), "console-run-%s.cmd" % token))
            except OSError:
                pass
        rotate_log_file(os.path.join(LOGS_DIR, "%s.log" % app_id))
    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()
    return thread


def persist_started_app(cfg, app_id, proc, pgid, token):
    """保存新的受控身份并启动退出监视线程。"""
    started_at = time.time()

    def op(c):
        target = find_app(c, app_id)
        if target:
            target["lastPid"] = proc.pid
            target["lastPgid"] = pgid
            target["runToken"] = token
            target["attached"] = False
            # 批处理任务运行时先保留上一次结果；自然退出或手动停止后再原子覆盖。
            if (target.get("kind") or "service") != "task":
                target["lastExit"] = None
            return True
        return False
    saved = cfg.update(op)
    if saved:
        if hasattr(PLATFORM, "invalidate_cache"):
            try:
                PLATFORM.invalidate_cache()
            except Exception:
                pass
        record_run_start(cfg, app_id, proc, pgid, token)
        watch_app_exit(cfg, app_id, proc, token, started_at)
    return saved


def clear_app_runtime(cfg, app_id, expected_token=None, last_exit=None):
    """清除受控身份；可用 token 防竞态，并可原子写入本次退出结果。"""
    def op(c):
        target = find_app(c, app_id)
        if not target:
            return False
        if expected_token is not None and target.get("runToken") != expected_token:
            return False
        target["lastPid"] = None
        target["lastPgid"] = None
        target["runToken"] = None
        target["attached"] = False
        if last_exit is not None:
            target["lastExit"] = last_exit
        return True
    return cfg.update(op)


def stop_app_for_update(cfg, app, timeout=5.0):
    """为修改运行参数安全停止应用；返回 (ok, error, stopped)。"""
    if not app_alive_sign(app):
        return True, None, False
    ok, error = stop_app_and_clear(cfg, app, timeout)
    return ok, error, bool(ok)


def pick_path(what):
    """原生文件/目录选择框（macOS osascript / Windows WinForms）。"""
    return PLATFORM.choose_path(what)


def command_for_script(path):
    """按脚本类型生成可直接保存的 shell 命令，并安全引用任意文件名。

    macOS 生成 bash 风格命令；Windows 生成 cmd.exe 可执行的语法
    （cmd 用双引号引用路径，无 POSIX 引号转义）。
    """
    normalized = os.path.abspath(os.path.expanduser(str(path)))
    if IS_WINDOWS:
        return _command_for_script_windows(normalized)
    quoted = shlex.quote(normalized)
    suffix = os.path.splitext(normalized)[1].lower()
    if suffix == ".py":
        return "python3 -- %s" % quoted
    if suffix == ".zsh":
        return "/bin/zsh -- %s" % quoted
    if suffix in (".sh", ".bash"):
        return "/bin/bash -- %s" % quoted
    if os.access(normalized, os.X_OK):
        return quoted
    # .command 常见于 Finder 双击脚本；没有执行位时仍可明确交给 bash。
    return "/bin/bash -- %s" % quoted


def _command_for_script_windows(path):
    quoted = '"%s"' % path.replace('"', '""')
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".py":
        return "python %s" % quoted
    if suffix == ".ps1":
        return "powershell -NoProfile -ExecutionPolicy Bypass -File %s" % quoted
    if suffix in (".bat", ".cmd"):
        return quoted
    return quoted


def _python_cmd(module_mode=True):
    """候选命令里的 Python 前缀（macOS: python3；Windows: python）。"""
    if IS_WINDOWS:
        return "python -m" if module_mode else "python"
    return "python3 -m" if module_mode else "python3"


SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".command"}
SHELL_BUILTINS = {
    ".", ":", "[", "alias", "break", "cd", "command", "continue", "echo",
    "eval", "exec", "exit", "export", "false", "printf", "pwd", "read",
    "return", "set", "shift", "source", "test", "true", "type", "ulimit",
    "umask", "unalias", "unset", "wait",
}


def _simple_command_tokens(command):
    """解析无管道/重定向/展开的简单命令；不确定时返回 None。"""
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        lexer = shlex.shlex(
            command, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens:
        return []
    if any(token and all(char in "|&;<>()" for char in token)
           for token in tokens):
        return None
    # 健康检查绝不展开变量、通配符或命令替换；这类命令照常允许运行。
    if any(any(char in token for char in ("$", "*", "?", "[", "]", "`"))
           for token in tokens):
        return None
    return tokens


def _resolve_command_path(value, cwd):
    value = os.path.expanduser(value)
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(cwd, value))


def _script_target(tokens, cwd):
    """提取 (路径, 是否直接执行, 原路径是否相对)，否则返回空。"""
    if not tokens:
        return None, False, False
    index = 0
    while index < len(tokens) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    if index >= len(tokens):
        return None, False, False
    executable = tokens[index]
    base = os.path.basename(executable)
    args = tokens[index + 1:]

    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", base):
        if "-m" in args or "-c" in args:
            return None, False, False
        if args and args[0] == "--":
            args = args[1:]
        candidate = next((arg for arg in args if not arg.startswith("-")), None)
        if candidate and (os.path.splitext(candidate)[1].lower() in SCRIPT_SUFFIXES
                          or "/" in candidate):
            return (_resolve_command_path(candidate, cwd), False,
                    not os.path.isabs(os.path.expanduser(candidate)))
        return None, False, False

    if base in {"bash", "sh", "zsh"}:
        if any(arg == "--command"
               or (arg.startswith("-") and "c" in arg[1:])
               for arg in args):
            return None, False, False
        if args and args[0] == "--":
            args = args[1:]
        candidate = next((arg for arg in args if not arg.startswith("-")), None)
        if candidate and (os.path.splitext(candidate)[1].lower() in SCRIPT_SUFFIXES
                          or "/" in candidate):
            return (_resolve_command_path(candidate, cwd), False,
                    not os.path.isabs(os.path.expanduser(candidate)))
        return None, False, False

    suffix = os.path.splitext(executable)[1].lower()
    if suffix in SCRIPT_SUFFIXES or "/" in executable:
        return (_resolve_command_path(executable, cwd), True,
                not os.path.isabs(os.path.expanduser(executable)))
    return None, False, False


def inspect_app_health(app):
    """静态检查配置是否可运行；只读文件系统，绝不执行或展开用户命令。"""
    issues = []

    def add(kind, title, detail, fix, action):
        issues.append({
            "kind": kind,
            "severity": "error",
            "title": title,
            "detail": detail,
            "fix": fix,
            "action": action,
        })

    configured_cwd = app.get("cwd")
    cwd = configured_cwd or os.path.expanduser("~")
    cwd_ok = os.path.isdir(cwd)
    if configured_cwd and not cwd_ok:
        add(
            "cwd-missing", "工作目录不可用",
            "找不到配置的工作目录：%s" % configured_cwd,
            "编辑这个项目，重新选择工作区文件夹。",
            "pick-cwd",
        )

    tokens = _simple_command_tokens(app.get("command") or "")
    if tokens is None:
        return {
            "status": "error" if issues else "unknown",
            "blocking": bool(issues),
            "issues": issues,
        }

    script_path, direct, script_was_relative = _script_target(tokens, cwd)
    if script_path and (cwd_ok or not script_was_relative):
        if not os.path.isfile(script_path):
            add(
                "script-missing", "脚本不可用",
                "找不到脚本：%s" % script_path,
                "编辑这个任务，重新选择脚本或修改执行命令。",
                "pick-script",
            )
        elif not os.access(script_path, os.R_OK):
            add(
                "path-unreadable", "脚本不可读取",
                "当前用户没有读取权限：%s" % script_path,
                "检查脚本权限，或重新选择一个可读取的脚本。",
                "pick-script",
            )
        elif direct and not os.access(script_path, os.X_OK):
            add(
                "script-not-executable", "脚本不可执行",
                "直接运行的脚本没有执行权限：%s" % script_path,
                "给脚本执行权限，或改为使用 bash / python3 执行。",
                "edit-command",
            )

    # 直接脚本已由上面的文件检查覆盖；其他简单命令检查首个运行时。
    index = 0
    while tokens and index < len(tokens) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    executable = tokens[index] if tokens and index < len(tokens) else ""
    executable_base = os.path.basename(executable)
    if executable and not direct and executable_base not in SHELL_BUILTINS:
        if "/" in executable:
            runtime = _resolve_command_path(executable, cwd)
            runtime_ok = os.path.isfile(runtime) and os.access(runtime, os.X_OK)
        else:
            runtime = executable
            runtime_ok = bool(shutil.which(
                executable, path=build_launch_env("health-check").get("PATH")))
        if not runtime_ok:
            add(
                "runtime-missing", "找不到 %s" % executable_base,
                "总控台的运行环境里找不到命令：%s" % executable,
                "安装对应运行时，或在编辑中修改执行命令。",
                "edit-command",
            )

    return {
        "status": "error" if issues else "ok",
        "blocking": bool(issues),
        "issues": issues,
    }


# ---------------------------------------------------------------- 项目启动识别

def _read_project_text(root, name):
    """只读取项目根目录下的小型文本配置；不存在、过大或不可读均返回 None。"""
    path = os.path.join(root, name)
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_DETECT_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_DETECT_FILE_BYTES + 1)
    except OSError:
        return None


def _port_from_command(command):
    """从常见 CLI 参数和环境变量中提取显式端口。"""
    patterns = (
        r"(?:^|\s)--port(?:=|\s+)(\d{1,5})(?=\s|$)",
        r"(?:^|\s)-p\s+(\d{1,5})(?=\s|$)",
        r"(?:^|\s)PORT\s*=\s*(\d{1,5})(?=\s|$)",
        r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{1,5})",
        r"\bhttp\.server\s+(\d{1,5})(?=\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return None


def _package_default_port(script_name, command, dependencies):
    """根据直接依赖和脚本内容给出开发服务器的惯用端口。"""
    haystack = " ".join((script_name, command, " ".join(dependencies))).lower()
    defaults = (
        (("hexo",), 4000),
        (("gatsby",), 8000),
        (("@docusaurus/", "docusaurus"), 3000),
        (("vuepress",), 8080),
        (("docsify",), 3000),
        (("eleventy", "@11ty/eleventy"), 8080),
        (("astro",), 4321),
        (("next", "nextjs"), 3000),
        (("nuxt",), 3000),
        (("react-scripts",), 3000),
        (("vue-cli-service", "@vue/cli-service"), 8080),
        (("vite",), 4173 if script_name == "preview" else 5173),
    )
    for needles, port in defaults:
        if any(needle in haystack for needle in needles):
            return port
    return None


def detect_project(root):
    """只读分析项目根目录，返回可由启动台直接使用的启动候选。"""
    if not isinstance(root, str) or not root.strip():
        return None, "请选择项目文件夹"
    root = os.path.abspath(os.path.expanduser(root.strip()))
    if not os.path.isdir(root):
        return None, "项目文件夹不存在或不可访问"

    candidates = []
    detected_files = []

    def note_file(name, text=None):
        path = os.path.join(root, name)
        exists = text is not None or os.path.isfile(path)
        if exists and name not in detected_files:
            detected_files.append(name)
        return exists

    def add(command, label, source, port=None, priority=50, detail=None,
            kind="service"):
        if not command or any(item["command"] == command for item in candidates):
            return
        if port is not None and not (isinstance(port, int) and 1 <= port <= 65535):
            port = None
        candidates.append({
            "command": command,
            "label": label,
            "source": source,
            "port": port,
            "kind": "task" if kind == "task" else "service",
            "detail": detail,
            "_priority": priority,
        })

    # Node / 前端 / 博客项目：优先读取 package.json 的 scripts。
    package = {}
    scripts = {}
    deps = set()
    hexo_config = os.path.isfile(os.path.join(root, "_config.yml"))
    is_hexo = hexo_config and (
        os.path.isdir(os.path.join(root, "source")) or
        os.path.isdir(os.path.join(root, "scaffolds")) or
        os.path.isdir(os.path.join(root, "themes")))
    package_text = _read_project_text(root, "package.json")
    if package_text is not None:
        note_file("package.json", package_text)
        try:
            package = json.loads(package_text)
        except (TypeError, ValueError):
            package = {}
        scripts = package.get("scripts") if isinstance(package, dict) else {}
        if not isinstance(scripts, dict):
            scripts = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            values = package.get(key) if isinstance(package, dict) else None
            if isinstance(values, dict):
                deps.update(str(name).lower() for name in values)
        is_hexo = (is_hexo or "hexo" in deps or
                   (isinstance(package, dict) and isinstance(package.get("hexo"), dict)))

        if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")):
            runner = "pnpm run"
            note_file("pnpm-lock.yaml")
        elif (os.path.isfile(os.path.join(root, "bun.lock")) or
              os.path.isfile(os.path.join(root, "bun.lockb"))):
            runner = "bun run"
            note_file("bun.lock" if os.path.isfile(os.path.join(root, "bun.lock")) else "bun.lockb")
        elif os.path.isfile(os.path.join(root, "yarn.lock")):
            runner = "yarn"
            note_file("yarn.lock")
        else:
            runner = "npm run"

        labels = {
            "dev": "开发服务器", "develop": "开发服务器",
            "start": "正式启动", "serve": "本地服务", "server": "本地服务",
            "preview": "本地预览", "docs": "文档站",
            "storybook": "组件预览",
        }
        preferred = ("dev", "develop", "start", "serve", "server", "preview", "docs", "storybook")
        ordered = [name for name in preferred if name in scripts]
        service_name = re.compile(r"(?:^|[:_-])(dev|develop|start|serve|server|preview|watch|docs|storybook|web|blog)(?:$|[:_-])", re.I)
        ordered.extend(name for name in scripts if name not in ordered and service_name.search(str(name)))
        for index, name in enumerate(ordered[:8]):
            script = scripts.get(name)
            if not isinstance(script, str):
                continue
            if is_hexo and str(name).lower() == "server" and re.search(
                    r"\bhexo\s+(?:s|server)\b", script, re.I):
                continue  # 下方提供更短、更通用的 hexo s，不重复同一操作
            command = "%s %s" % (runner, shlex.quote(str(name)))
            port = _port_from_command(script)
            if port is None:
                port = _package_default_port(str(name).lower(), script, deps)
            add(command, labels.get(str(name).lower(), "项目脚本：%s" % name),
                "package.json · scripts.%s" % name, port,
                10 + index, "由项目自己的脚本定义")

    # Hexo 即使没有 scripts 也有稳定 CLI：服务与清缓存分别作为服务/任务。
    if is_hexo:
        if hexo_config:
            note_file("_config.yml")
        add("hexo s", "Hexo 本地服务", "Hexo 项目结构", 4000, 8,
            "等同于 hexo server")
        add("hexo cl", "Hexo 清除缓存", "Hexo 项目结构", None, 9,
            "清除缓存和已生成文件，不启动服务", kind="task")

    # 常见博客与静态站点生成器。
    hugo_config = next((name for name in ("hugo.toml", "hugo.yaml", "hugo.yml")
                        if os.path.isfile(os.path.join(root, name))), None)
    if hugo_config or (os.path.isdir(os.path.join(root, "content")) and
                       os.path.isdir(os.path.join(root, "layouts")) and
                       os.path.isfile(os.path.join(root, "config.toml"))):
        source = hugo_config or "config.toml"
        note_file(source)
        add("hugo server -D", "Hugo 本地预览", source, 1313, 18,
            "包含草稿内容")

    gemfile = _read_project_text(root, "Gemfile")
    if gemfile is not None:
        note_file("Gemfile", gemfile)
        if "jekyll" in gemfile.lower():
            add("bundle exec jekyll serve", "Jekyll 本地预览", "Gemfile", 4000, 19)

    # Python Web 项目。
    pyproject = _read_project_text(root, "pyproject.toml")
    requirements = _read_project_text(root, "requirements.txt")
    if pyproject is not None:
        note_file("pyproject.toml", pyproject)
    if requirements is not None:
        note_file("requirements.txt", requirements)
    py_deps = "\n".join(text for text in (pyproject, requirements) if text).lower()
    python_runner = "uv run" if os.path.isfile(os.path.join(root, "uv.lock")) else _python_cmd(True)
    if os.path.isfile(os.path.join(root, "uv.lock")):
        note_file("uv.lock")
    if os.path.isfile(os.path.join(root, "manage.py")):
        note_file("manage.py")
        prefix = "uv run python" if python_runner == "uv run" else _python_cmd(False)
        add(prefix + " manage.py runserver", "Django 开发服务器", "manage.py", 8000, 20)
    else:
        for module_file in ("app.py", "main.py", "server.py"):
            module_text = _read_project_text(root, module_file)
            if module_text is None:
                continue
            module = os.path.splitext(module_file)[0]
            imports_streamlit = re.search(
                r"(?m)^\s*(?:import\s+streamlit\b|from\s+streamlit\b)", module_text)
            imports_fastapi = re.search(
                r"(?m)^\s*(?:import\s+fastapi\b|from\s+fastapi\b)", module_text)
            imports_flask = re.search(
                r"(?m)^\s*(?:import\s+flask\b|from\s+flask\b)", module_text)
            if "streamlit" in py_deps or imports_streamlit:
                note_file(module_file, module_text)
                prefix = "uv run" if python_runner == "uv run" else _python_cmd(True)
                add(prefix + " streamlit run " + module_file,
                    "Streamlit 应用", module_file, 8501, 22)
                break
            if "fastapi" in py_deps or imports_fastapi:
                note_file(module_file, module_text)
                prefix = "uv run" if python_runner == "uv run" else _python_cmd(True)
                add(prefix + " uvicorn %s:app --reload" % module,
                    "FastAPI 开发服务器", module_file, 8000, 23)
                break
            if "flask" in py_deps or imports_flask:
                note_file(module_file, module_text)
                prefix = "uv run" if python_runner == "uv run" else _python_cmd(True)
                add(prefix + " flask --app %s run --debug" % module,
                    "Flask 开发服务器", module_file, 5000, 24)
                break

    # Docker Compose、Go、Rust 和已有的常用启动脚本。
    compose_name = next((name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
                         if os.path.isfile(os.path.join(root, name))), None)
    if compose_name:
        compose_text = _read_project_text(root, compose_name)
        note_file(compose_name, compose_text)
        port = None
        if compose_text:
            match = re.search(r"[\"']?(\d{2,5})\s*:\s*\d{2,5}[\"']?", compose_text)
            if match and 1 <= int(match.group(1)) <= 65535:
                port = int(match.group(1))
        add("docker compose up", "Docker Compose", compose_name, port, 55,
            "以前台方式运行，停止按钮可正常关闭")
    if os.path.isfile(os.path.join(root, "go.mod")):
        note_file("go.mod")
        add("go run .", "Go 项目", "go.mod", None, 60)
    if os.path.isfile(os.path.join(root, "Cargo.toml")):
        note_file("Cargo.toml")
        add("cargo run", "Rust 项目", "Cargo.toml", None, 61)

    for script_name in ("start.command", "dev.command", "run.command", "start.sh", "dev.sh", "run.sh"):
        if os.path.isfile(os.path.join(root, script_name)):
            note_file(script_name)
            add("bash %s" % shlex.quote("./" + script_name),
                "现有启动脚本", script_name, None, 70,
                "也可以继续使用“选择脚本”手动指定")
            break

    # 纯静态站点最后兜底，避免把 Vite/Next 等项目误当成普通文件目录。
    if not candidates and os.path.isfile(os.path.join(root, "index.html")):
        note_file("index.html")
        add(_python_cmd(True) + " http.server 8000", "静态网站预览", "index.html", 8000, 90)

    # M3：MCP 服务器候选（.mcp.json / package.json mcp 字段，只读检测）
    for mcp in detect_mcp_servers(root):
        candidates.append(mcp)

    candidates.sort(key=lambda item: item.pop("_priority"))
    return {
        "ok": True,
        "cwd": root,
        "name": os.path.basename(root) or root,
        "repoPath": git_root(root),
        "files": detected_files,
        "candidates": candidates[:8],
    }, None


def _current_user_group_members(pgid):
    """Return live current-user members of a previously verified group.

    Once SIGTERM is sent the token-bearing controller may exit before a child
    that ignores SIGTERM.  Requiring the marker again would incorrectly report
    success, so the wait phase follows the already-verified PGID until empty.
    """
    members = pgid_members_map().get(pgid, [])
    if not members:
        return []
    snap = ps_snapshot(members, with_uid=True)
    return sorted(pid for pid in members
                  if snap.get(pid, {}).get("uid") == SELF_UID)


def resolve_app_stop_target(app, listeners=None):
    """Resolve and validate a stop target before any signal is sent."""
    current = managed_pids(app)
    if current:
        if IS_WINDOWS:
            # Windows 无进程组语义：以 token 校验通过的 cmd 控制器为目标，
            # 由 adapter 的 taskkill /T 树停止语义覆盖全部后代。
            controller = app.get("lastPid")
            if not (isinstance(controller, int) and controller in current):
                controller = current[0]
            return {"kind": "tree", "id": controller,
                    "members": list(current)}, None
        pgid = app.get("lastPgid") or app.get("lastPid")
        if isinstance(pgid, int) and pgid > 0:
            return {"kind": "group", "id": pgid, "members": list(current)}, None
        return None, "受控进程组信息无效"
    legacy_pid = legacy_managed_pid(app, listeners)
    if legacy_pid:
        if app.get("attached") and not IS_WINDOWS:
            try:
                pgid = PLATFORM.pid_group(legacy_pid)
            except Exception:
                pgid = None
            if isinstance(pgid, int) and pgid > 0 and pgid != PLATFORM.current_pgrp():
                members = _current_user_group_members(pgid)
                member_cwds = lsof_cwds(members)
                expected_cwd = app.get("cwd")
                try:
                    safe_group = bool(members and expected_cwd) and all(
                        member_cwds.get(pid)
                        and os.path.realpath(member_cwds[pid])
                        == os.path.realpath(expected_cwd)
                        for pid in members
                    )
                except OSError:
                    safe_group = False
                if safe_group:
                    return {
                        "kind": "group",
                        "id": pgid,
                        "members": list(members),
                    }, None
        return {"kind": "pid", "id": legacy_pid, "members": [legacy_pid]}, None
    return None, "无法确认受控进程，未执行停止"


def signal_app_stop(target, sig=signal.SIGTERM):
    """Signal a target returned by resolve_app_stop_target."""
    ident = target["id"]
    if target["kind"] == "group":
        return PLATFORM.signal_group(ident, sig)
    if target["kind"] == "tree":
        return PLATFORM.terminate_tree(ident, force=(sig == SIGKILL))
    return PLATFORM.signal_pid(ident, sig)


def stop_target_alive(target, expected_uid=None):
    if target["kind"] == "group":
        return PLATFORM.group_alive(target["id"])
    if PLATFORM.pid_alive(target["id"]):
        if expected_uid is None:
            expected_uid = process_uid(target["id"])
        return expected_uid == SELF_UID
    return False


def stop_app_and_wait(app, timeout=APP_STOP_TIMEOUT_SEC, listeners=None):
    """Signal a verified app and wait until the exact target is gone.

    Returns (ok, error).  A timeout is deliberately not escalated to SIGKILL;
    the caller keeps the runtime token so the user can retry or choose a force
    action without losing control of a still-live process.
    """
    target, error = resolve_app_stop_target(app, listeners)
    if target is None:
        return False, error
    ok, error = signal_app_stop(target)
    if not ok:
        return False, error
    deadline = time.monotonic() + max(0.0, timeout)
    # uid 只查一次：信号已在循环外发出，循环仅做存活探测，
    # 避免 50ms 一次的 ps 子进程（PID 复用时最坏多等一个超时周期，无副作用）。
    expected_uid = (process_uid(target["id"]) if target["kind"] == "pid"
                    else None)
    while stop_target_alive(target, expected_uid):
        if time.monotonic() >= deadline:
            remaining = (target["members"] if target["kind"] in ("pid", "tree")
                         else _current_user_group_members(target["id"]))
            suffix = "（PID %s）" % "、".join(str(p) for p in remaining) if remaining else ""
            return False, "应用未在 %.1f 秒内退出%s，仍保留管理状态" % (timeout, suffix)
        time.sleep(0.05)
    return True, None


def stop_app_and_clear(cfg, app, timeout=APP_STOP_TIMEOUT_SEC, listeners=None):
    """Manual stop transaction: wait first, clear persisted identity last."""
    marker = (app.get("id"), app.get("runToken"))
    with MANUAL_STOP_LOCK:
        MANUAL_STOP_TOKENS.add(marker)
    try:
        ok, error = stop_app_and_wait(app, timeout, listeners)
        if not ok:
            return False, error
        # M4：手动停止归一到 stopped（watch 线程可能已先归一并幂等跳过）
        finalize_runs_for_app(app["id"], None, True, time.time())
        last_exit = None
        if (app.get("kind") or "service") == "task":
            # 覆盖可能保留的旧成功记录，避免“刚刚手动停止”仍显示上次成功。
            last_exit = {
                "status": "stopped",
                "code": None,
                "at": int(time.time()),
            }
        if not clear_app_runtime(
                cfg, app["id"], app.get("runToken"), last_exit=last_exit):
            return False, "进程已停止，但应用状态已变化，请刷新后重试"
        return True, None
    finally:
        with MANUAL_STOP_LOCK:
            MANUAL_STOP_TOKENS.discard(marker)


def inspect_attach_process(cfg, app, pid):
    """只读校验待认领进程，返回其可信工作目录。

    创建卡片时先调用本函数，再把卡片与运行身份一次写入配置，避免前端
    “先创建、再认领”只完成一半。已有卡片的手动认领也复用同一套校验。"""
    if (app.get("kind") or "service") != "service":
        return False, "批处理任务没有端口，无法认领进程", {"status": 422}
    port = app.get("port")
    if not isinstance(port, int) or port <= 0:
        return False, "卡片未配置端口，无法认领进程", {"status": 422}
    if app_alive_sign(app):
        return False, "应用已在运行", {"status": 409}
    if pid == os.getpid():
        return False, "不能认领总控台自身", {"status": 409}
    listeners = scan_listeners()
    if (pid, port) not in listeners:
        return False, "PID %d 并未监听端口 %d，进程可能已退出" % (pid, port), {"status": 409}
    snap = ps_snapshot({pid}, with_uid=True)
    if snap.get(pid, {}).get("uid") != SELF_UID:
        return False, "该进程不属于当前用户，不能认领", {"status": 403}
    cfg_now = cfg.snapshot()
    owners = listener_app_owners(cfg_now.get("apps") or [], listeners, snap, None)
    if pid in owners:
        return False, "该进程已由卡片「%s」管理" % owners[pid].get("name", ""), {"status": 409}
    actual_cwd = lsof_cwds({pid}).get(pid)
    if not actual_cwd:
        return False, "无法读取进程工作目录，已取消认领", {"status": 409}
    return True, None, {"status": 200, "cwd": actual_cwd}


def attach_app_process(cfg, app_id, app, pid):
    """把已在监听配置端口的当前用户进程认领为本卡片受管进程。

    认领走旧版身份通道（lastPid + 监听端口 + 当前 UID + 真实 cwd 四重校验），
    与卡片 cwd 不一致时原子同步卡片 cwd。认领后卡片显示运行中，可正常
    停止/重启（重启后转为 token 受管）。返回 (ok, error, info)。"""
    ok, error, identity = inspect_attach_process(cfg, app, pid)
    if not ok:
        return False, error, identity
    actual_cwd = identity["cwd"]
    cwd_updated = False
    pid_conflict = False

    def op(c):
        nonlocal cwd_updated, pid_conflict
        target = find_app(c, app_id)
        if not target:
            return False
        # 认领检查与写入必须同锁：inspect 用的是旧快照，并发请求可能同时
        # 通过校验。在写锁内重验 pid 是否已被其他卡片认领。
        if any(other.get("lastPid") == pid
               for other in c.get("apps") or [] if other.get("id") != app_id):
            pid_conflict = True
            return False
        target["lastPid"] = pid
        target["lastPgid"] = None
        target["runToken"] = None
        target["attached"] = True
        target["lastExit"] = None
        try:
            same = (isinstance(target.get("cwd"), str) and target["cwd"]
                    and os.path.realpath(target["cwd"]) == os.path.realpath(actual_cwd))
        except OSError:
            same = False
        if not same:
            target["cwd"] = actual_cwd
            cwd_updated = True
        return True

    if not cfg.update(op):
        if pid_conflict:
            return False, "该进程已由其他卡片管理", {"status": 409}
        return False, "应用已被删除", {"status": 404}
    info = {}
    if cwd_updated:
        info["cwdUpdated"] = True
        info["cwd"] = actual_cwd
    return True, None, info


# ---------------------------------------------------------------- 日志

def rotate_log_file(path, max_bytes=MAX_LOG_BYTES, backups=LOG_BACKUPS):
    """超限后 copy-truncate，保持子进程已打开的文件描述符继续可写。"""
    with LOG_LOCK:
        try:
            if os.path.getsize(path) <= max_bytes:
                return False
        except OSError:
            return False
        try:
            for index in range(backups, 1, -1):
                older = "%s.%d" % (path, index - 1)
                newer = "%s.%d" % (path, index)
                if os.path.exists(older):
                    os.replace(older, newer)
            shutil.copyfile(path, path + ".1")
            os.chmod(path + ".1", 0o600)
            with open(path, "r+b") as f:
                f.truncate(0)
            os.chmod(path, 0o600)
            return True
        except OSError:
            LOG.exception("轮转日志失败: %s", path)
            return False


def _tail_file_lines(path, count, block_size=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            chunks = []
            newlines = 0
            while pos > 0 and newlines <= count:
                size = min(block_size, pos)
                pos -= size
                f.seek(pos)
                chunk = f.read(size)
                if not chunk.strip(b"\x00"):
                    break  # 空洞/被外部截断后残留的 NUL 段：之前没有内容，停止回扫
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        data = b"".join(reversed(chunks))
        return data.decode("utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


def read_log_tail(app_id, count):
    """从当前日志和轮转备份中高效读取最后 count 行。"""
    path = os.path.join(LOGS_DIR, "%s.log" % app_id)
    rotate_log_file(path)
    collected = []
    with LOG_LOCK:
        for candidate in [path] + ["%s.%d" % (path, i)
                                   for i in range(1, LOG_BACKUPS + 1)]:
            remaining = count - len(collected)
            if remaining <= 0:
                break
            lines = _tail_file_lines(candidate, remaining)
            collected = lines + collected
    return "\n".join(collected[-count:])


def start_log_maintenance():
    def _maintain():
        while True:
            try:
                for name in os.listdir(LOGS_DIR):
                    if name.endswith(".log"):
                        rotate_log_file(os.path.join(LOGS_DIR, name))
            except OSError:
                LOG.exception("日志维护失败")
            time.sleep(LOG_MAINTENANCE_SEC)
    threading.Thread(target=_maintain, daemon=True).start()


def sniff_image(data):
    """magic bytes 校验 → "png" / "jpg" / "webp" / None。"""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------- 站点图标抓取

ICON_LINK_RE = re.compile(
    r"<link[^>]+rel=[\"'][^\"']*icon[^\"']*[\"'][^>]*>", re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)


def is_loopback_service_url(url, port):
    """仅允许抓取指定端口的明文 loopback URL，避免 favicon SSRF。"""
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "http"
                and (parsed.hostname or "").lower() in (
                    "127.0.0.1", "localhost", "::1")
                and parsed.port == port
                and not parsed.username and not parsed.password)
    except (TypeError, ValueError, UnicodeError):
        return False


class LoopbackRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只跟随仍停留在同一 loopback 端口的重定向。"""

    def __init__(self, port):
        super().__init__()
        self.port = port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_loopback_service_url(newurl, self.port):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, port, timeout=3, limit=262144):
    """GET → (bytes, content-type) | (None, None)。仅抓同一 loopback 端口。"""
    if not is_loopback_service_url(url, port):
        return None, None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Console/1.0", "Accept": "*/*"})
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), LoopbackRedirectHandler(port))
        with opener.open(req, timeout=timeout) as r:
            return r.read(limit), (r.headers.get("Content-Type") or "")
    except Exception:
        return None, None


def sniff_icon_bytes(data, ctype=""):
    """→ "png" / "jpg" / "webp" / "ico" / None。拒绝主动 SVG 内容。"""
    if len(data) >= 4 and data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    ext = sniff_image(data)
    if ext:
        return ext
    return None


def fetch_favicon(port, host="127.0.0.1"):
    """抓本地站点图标 → (bytes, ext) | (None, None)。
    先解析首页 <link rel=...icon...>（含 apple-touch-icon），兜底 /favicon.ico。"""
    if host not in ("127.0.0.1", "localhost"):
        host = "127.0.0.1"
    base = "http://%s:%d" % (host, port)
    candidates = []
    html, _ = http_get(base + "/", port)
    if html:
        text = html.decode("utf-8", errors="replace")
        for m in ICON_LINK_RE.finditer(text):
            hm = HREF_RE.search(m.group(0))
            if hm:
                url = urllib.parse.urljoin(base + "/", hm.group(1))
                if is_loopback_service_url(url, port):
                    candidates.append(url)
    candidates.append(base + "/favicon.ico")
    for url in candidates[:4]:
        data, ctype = http_get(url, port, limit=1024 * 1024)
        if data:
            ext = sniff_icon_bytes(data, ctype)
            if ext:
                return data, ext
    return None, None


def find_app(cfg, app_id):
    for app in cfg.get("apps") or []:
        if app.get("id") == app_id:
            return app
    return None


def diagnose_app(cfg, app):
    """规则诊断：退出码 + 日志模式 + 文件系统检查 → 可执行的修复建议列表。

    覆盖常见失败：依赖未装、命令/脚本不存在、运行时缺失、npm 脚本名错误、
    端口占用、权限不足、Python 包缺失。
    """
    issues = []

    def add(kind, title, detail, fix, action=None):
        if not any(i["kind"] == kind for i in issues):
            issue = {"kind": kind, "title": title,
                     "detail": detail, "fix": fix}
            if action:
                issue["action"] = action
            issues.append(issue)

    app_id = app.get("id") or ""
    cwd = app.get("cwd") or ""
    last_exit = app.get("lastExit") or {}
    code = last_exit.get("code")
    port = app.get("port")
    log_tail = read_log_tail(app_id, 150) if app_id else ""
    log_lower = log_tail.lower()

    # ---- 配置层检查（不依赖日志） ----
    for health_issue in inspect_app_health(app).get("issues", []):
        add(
            health_issue["kind"],
            health_issue["title"],
            health_issue["detail"],
            health_issue["fix"],
            health_issue.get("action"),
        )

    pkg_json = os.path.join(cwd, "package.json") if cwd else ""
    has_pkg = bool(cwd) and os.path.isfile(pkg_json)
    has_node_modules = bool(cwd) and os.path.isdir(os.path.join(cwd, "node_modules"))
    if has_pkg and not has_node_modules:
        mgr = ("yarn" if os.path.isfile(os.path.join(cwd, "yarn.lock"))
               else "pnpm" if os.path.isfile(os.path.join(cwd, "pnpm-lock.yaml"))
               else "npm")
        add("deps-missing", "依赖未安装（node_modules 缺失）",
            "目录里有 package.json，但没有 node_modules。",
            "终端执行：cd \"%s\" && %s install，装完再启动。" % (cwd, mgr))

    # ---- 日志模式匹配 ----
    m = re.search(r"cannot find module '([^']+)'", log_lower)
    if m:
        add("deps-missing", "找不到模块 %s" % m.group(1),
            "日志报 Cannot find module '%s'，通常是依赖没装或装坏了。" % m.group(1),
            "终端执行：cd \"%s\" && npm install（仍报错再 rm -rf node_modules 后重装）。" % (cwd or "<项目目录>"))

    m = re.search(r"(?:env: )?(\S+): (?:no such file or directory|command not found)", log_lower)
    if m and "cannot find module" not in log_lower:
        add("runtime-missing", "找不到运行时：%s" % m.group(1),
            "系统里找不到 %s 这个命令。" % m.group(1),
            "确认该运行时已安装（如 node / python3 / pnpm）；总控台启动时会补常见 PATH，但程序本身需要存在。")

    if "missing script" in log_lower and has_pkg:
        script_names = []
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                script_names = list((json.load(f).get("scripts") or {}).keys())
        except Exception:
            pass
        hint = ("package.json 里可用的脚本：%s。" % "、".join(script_names)
                if script_names else "package.json 里没有 scripts。")
        add("npm-script", "npm 脚本名写错了",
            "日志报 missing script。%s" % hint,
            "把启动命令改成上面列出的脚本名，例如 npm run %s。" % (script_names[0] if script_names else "dev"))

    if "eaddrinuse" in log_lower or "address already in use" in log_lower:
        add("port-busy", "端口被占用",
            "日志报地址已占用%s。" % ("（:%s）" % port if port else ""),
            "点卡片上的端口数字看是谁占用的，停掉它或给本应用换个端口。")

    if "eacces" in log_lower or "permission denied" in log_lower:
        add("perm", "权限不足",
            "日志报权限不足（EACCES / permission denied）。",
            "检查文件/目录权限；脚本需要可执行权限：chmod +x <脚本>。不要简单用 sudo 运行。")

    m = re.search(r"modulenotfounderror: no module named '([^']+)'", log_lower)
    if m:
        add("pip-missing", "缺少 Python 包：%s" % m.group(1),
            "日志报 ModuleNotFoundError: No module named '%s'。" % m.group(1),
            "建议在项目目录建虚拟环境再装：python3 -m venv .venv && .venv/bin/pip install %s" % m.group(1))

    if re.search(r"no such file or directory", log_lower) and not issues:
        add("file-missing", "命令里的文件/脚本不存在",
            "日志报 No such file or directory，命令里引用的路径可能写错了。",
            "检查启动命令和工作目录里的相对路径是否正确。")

    # ---- 退出码兜底 ----
    if not issues:
        if code == 126:
            add("not-exec", "命令没有执行权限（exit 126）",
                "退出码 126 表示文件不可执行。",
                "给脚本加执行权限：chmod +x <脚本>，或用 bash <脚本> 启动。")
        elif code == 127:
            add("not-found", "命令不存在（exit 127）",
                "退出码 127 表示 shell 找不到这个命令。",
                "确认命令已安装且在 PATH 里；总控台会补常见路径，但程序本身要存在。")
        elif (isinstance(code, int) and code == 0
              and (app.get("kind") or "service") != "task"):
            add("quick-exit", "命令立即正常退出（exit 0）",
                "进程启动后马上正常结束——长期服务命令不应立刻退出。",
                "确认写的是常驻命令（如 hexo s / npm run dev），而不是一次就完成的命令。")
        elif isinstance(code, int) and code < 0:
            add("signaled", "进程被信号终止（signal %d）" % -code,
                "进程不是自然退出，是被系统信号杀掉的。",
                "常见于内存不足被系统回收或外部 kill；查看系统日志确认原因。")

    # ---- 汇总 ----
    if issues:
        summary = "发现 %d 个可能原因，按「修复建议」处理后再启动。" % len(issues)
    elif not log_tail.strip():
        summary = "暂无日志可供诊断；先启动一次让日志产生，再看完整日志定位。"
    elif code is None:
        summary = "该应用还没有退出记录；当前日志未见明显异常。"
    else:
        summary = "日志里没有命中常见错误模式，建议打开完整日志人工排查。"
    return {"ok": True, "issues": issues, "summary": summary}


def validate_app_fields(data, partial):
    """校验/规范化应用字段。partial=True 时仅校验出现的字段。
    返回 (fields, error)：fields 为规范化后的字段子集。"""
    fields = {}
    for key in ("name", "command"):
        if key in data:
            v = data[key]
            if not isinstance(v, str) or not v.strip():
                return None, "字段 %s 必须是非空字符串" % key
            fields[key] = v.strip()
        elif not partial:
            return None, "缺少字段 %s" % key
    if "cwd" in data:
        v = data["cwd"]
        if v is not None and not isinstance(v, str):
            return None, "cwd 必须是字符串或 null"
        fields["cwd"] = (v or "").strip() or None if isinstance(v, str) else None
    elif not partial:
        fields["cwd"] = None
    if "port" in data:
        port, err = validate_port(data["port"])
        if err:
            return None, err
        fields["port"] = port
    elif not partial:
        fields["port"] = None
    if "emoji" in data:
        v = data["emoji"]
        if v is not None and not isinstance(v, str):
            return None, "emoji 必须是字符串或 null"
        fields["emoji"] = (v or None)
    elif not partial:
        fields["emoji"] = None
    if "glyph" in data:
        v = data["glyph"]
        if v is not None and (not isinstance(v, str) or len(v) > 40):
            return None, "glyph 必须是字符串或 null"
        fields["glyph"] = (v or None)
    elif not partial:
        fields["glyph"] = None
    if "kind" in data:
        if data["kind"] not in ("service", "task"):
            return None, "kind 必须是 service/task"
        fields["kind"] = data["kind"]
    elif not partial:
        fields["kind"] = "service"
    if fields.get("kind") == "task":
        fields["port"] = None  # 批处理任务无端口语义
    return fields, None


# ---------------------------------------------------------------- 资源注册桥

def register_resource_for_app(c, app):
    """新 app 创建时同步建立项目资源（M3/M4 补全：cwd 匹配项目，否则 Unassigned）。"""
    from adcc.projects import create_resource, ensure_unassigned_project
    project_id = None
    cwd = app.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        try:
            real = os.path.realpath(cwd)
            for project in c.get("projects") or []:
                try:
                    if os.path.realpath(project.get("root_path")) == real:
                        project_id = project.get("id")
                        break
                except OSError:
                    continue
        except OSError:
            project_id = None
    if project_id is None:
        project_id = ensure_unassigned_project(c).get("id")
    resource = create_resource(
        c, project_id, app.get("name") or app.get("id"),
        app.get("kind") or "service", app.get("command") or "",
        cwd=app.get("cwd"), port=app.get("port"))
    resource["app_id"] = app.get("id")
    return resource


# ---------------------------------------------------------------- HTTP 处理

def serialized_app_operation(fn):
    """Reject overlapping mutations for one app instead of racing/queueing."""
    @functools.wraps(fn)
    def wrapped(self, app_id, *args, **kwargs):
        lock = self.server.try_app_operation(app_id)
        if lock is None:
            self.send_err(409, "该应用正在执行其他操作，请稍后重试")
            return None
        try:
            return fn(self, app_id, *args, **kwargs)
        finally:
            lock.release()
    return wrapped


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, cfg, port):
        super().__init__(addr, handler_cls)
        self.cfg = cfg
        self.console_port = self.server_address[1]
        self.control_token = secrets.token_urlsafe(32)
        self._app_locks = {}
        self._app_locks_guard = threading.Lock()
        self._console_action_guard = threading.Lock()
        self._console_action = None
        self._console_helper_pid = None

    def handle_error(self, request, client_address):
        """空闲连接超时 / 客户端中途断开属正常现象，不刷 traceback。"""
        exc_type, exc, _ = sys.exc_info()
        if exc_type and isinstance(exc, (TimeoutError, BrokenPipeError,
                                         ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def try_app_operation(self, app_id):
        with self._app_locks_guard:
            lock = self._app_locks.setdefault(app_id, threading.Lock())
        return lock if lock.acquire(blocking=False) else None

    def forget_app_lock(self, app_id):
        """应用删除后回收其操作锁（调用方应已持有该锁）。"""
        with self._app_locks_guard:
            self._app_locks.pop(app_id, None)

    def reserve_console_action(self, action):
        with self._console_action_guard:
            if self._console_action is not None:
                return False, self._console_action, self._console_helper_pid
            self._console_action = action
            return True, action, None

    def set_console_helper_pid(self, pid):
        with self._console_action_guard:
            self._console_helper_pid = pid

    def release_console_action(self, action):
        with self._console_action_guard:
            if self._console_action == action:
                self._console_action = None
                self._console_helper_pid = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Console/%s" % APP_VERSION
    # 每连接 socket 超时：慢速/谎报 Content-Length 的客户端无法无限占住
    # 线程（默认 None 会永久阻塞 rfile.read）；空闲 keep-alive 连接也会回收。
    SOCKET_TIMEOUT_SEC = 30.0

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(self.SOCKET_TIMEOUT_SEC)
        except OSError:
            pass

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):
        try:
            if self.path.startswith("/api/state"):
                return  # 2s 轮询不刷日志
        except Exception:
            pass
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))

    def _parsed_request_host(self):
        """Return (hostname, port) only for the exact local console origin."""
        raw = (self.headers.get("Host") or "").strip()
        if not raw or any(ch in raw for ch in "\r\n,@/"):
            return None
        try:
            parsed = urllib.parse.urlsplit("http://" + raw)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except (ValueError, UnicodeError):
            return None
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return None
        if port != self.server.console_port:
            return None
        return hostname, port

    def _request_host_allowed(self):
        if self._parsed_request_host() is None:
            return False
        try:
            return self.client_address[0] in ("127.0.0.1", "::1")
        except (AttributeError, IndexError):
            return False

    def _same_origin(self, origin, host):
        try:
            parsed = urllib.parse.urlsplit(origin)
            port = parsed.port or (80 if parsed.scheme == "http" else 443)
            return (parsed.scheme == "http"
                    and (parsed.hostname or "").lower() == host[0]
                    and port == host[1]
                    and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment)
        except (ValueError, UnicodeError):
            return False

    def _has_control_cookie(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie") or "")
            morsel = cookie.get("console_session")
            return bool(morsel and secrets.compare_digest(
                morsel.value, self.server.control_token))
        except (KeyError, TypeError, ValueError):
            return False

    def _deny_request(self, status, message):
        # Do not consume attacker-controlled bodies. Closing after the bounded
        # JSON error prevents keep-alive request smuggling via leftover bytes.
        self.close_connection = True
        self.send_err(status, message)
        return False

    def _handle_request_error(self, method, exc):
        """请求处理异常统一入口：细节只进日志，响应不回内部信息。"""
        LOG.exception("%s %s 处理失败", method, self.path)
        try:
            self.send_err(500, "服务器错误")
        except Exception:
            pass

    def authorize_request(self, mutating=False, content_kind=None):
        """Enforce the loopback browser trust boundary.

        Browser writes require exact same-origin metadata plus the HttpOnly
        session cookie issued by this process. Headerless local CLI clients stay
        compatible, but JSON/image Content-Type rules keep those paths
        unavailable to simple cross-site HTML forms.
        """
        host = self._parsed_request_host()
        if host is None or not self._request_host_allowed():
            return self._deny_request(421, "请求 Host 不是当前本地控制台")
        if not mutating:
            return True

        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip()
        if site and site not in ("same-origin", "none"):
            return self._deny_request(403, "拒绝跨站控制请求")
        if origin and not self._same_origin(origin, host):
            return self._deny_request(403, "请求 Origin 不是当前控制台")
        if (site or origin) and not self._has_control_cookie():
            return self._deny_request(403, "控制会话已失效，请刷新页面")

        if self.headers.get("Transfer-Encoding"):
            return self._deny_request(400, "不支持 Transfer-Encoding 请求体")

        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        media_type = media_type.strip().lower()
        if content_kind == "json" and media_type != "application/json":
            return self._deny_request(415, "接口仅接受 application/json")
        if content_kind == "image" and media_type not in (
                "image/png", "image/jpeg", "image/webp",
                "application/octet-stream"):
            return self._deny_request(415, "图标接口仅接受 PNG/JPEG/WebP 原始数据")
        if content_kind:
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                return self._deny_request(400, "请求必须包含唯一的 Content-Length")
            try:
                length = int(lengths[0])
            except ValueError:
                return self._deny_request(400, "非法的 Content-Length")
            limit = MAX_ICON_BYTES if content_kind == "image" else MAX_JSON_BYTES
            if length < 0 or length > limit:
                return self._deny_request(413, "请求体过大")
        return True

    def _send(self, body, status=200, ctype="text/plain; charset=utf-8",
              set_cookie=True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; connect-src 'self'; img-src 'self' data: blob:; "
            "font-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'")
        if set_cookie and self._request_host_allowed():
            self.send_header(
                "Set-Cookie",
                "console_session=%s; Path=/; HttpOnly; SameSite=Strict" %
                self.server.control_token)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   status, "application/json; charset=utf-8")

    def send_err(self, status, msg):
        self.send_json({"ok": False, "error": msg}, status)

    def discard_body(self):
        """读掉并丢弃请求体。keep-alive 连接复用前必须清空，
        否则残留字节会污染同一连接上的下一个请求（method 解析错乱 → 501）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def read_json_body(self):
        """→ (data|None, error|None)。非法 JSON / 非对象 / 超限都返回 error。"""
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            return None, "Content-Type 必须是 application/json"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "非法的 Content-Length"
        if length < 0 or length > MAX_JSON_BYTES:
            return None, "请求体过大"
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "请求体不是合法 JSON"
        if not isinstance(data, dict):
            return None, "请求体必须是 JSON 对象"
        return data, None

    def _get_app_or_404(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if app is None:
            self.send_err(404, "应用不存在")
            return None, None
        return cfg, app

    # ---------- GET ----------

    def do_GET(self):
        try:
            if not self.authorize_request():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/favicon.ico":
                self.serve_static("/assets/favicon.ico")
                return
            if path == "/api/health":
                self.send_json(build_health(self.server.cfg))
                return
            if path == "/api/state":
                self.send_json(get_state_snapshot(self.server.cfg,
                                                  self.server.console_port))
                return
            if path == "/api/console/log":
                self.handle_console_log(parsed.query)
                return
            if path == "/api/v1/events":
                self.handle_v1_events()
                return
            if path.startswith("/api/v1/"):
                self.handle_v1_get(path, parsed.query)
                return
            m = APP_ROUTE_RE.match(path)
            if m and m.group(2) == "logs":
                self.handle_logs(m.group(1), parsed.query)
                return
            if path.startswith("/api/"):
                self.send_err(404, "接口不存在")
                return
            if path.startswith("/icons/"):
                self.serve_icon(path)
                return
            self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("GET", e)

    def serve_static(self, path):
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        # realpath 解析后必须仍在 STATIC_DIR 内，防路径穿越与符号链接逃逸。
        try:
            inside = os.path.commonpath(
                [os.path.realpath(STATIC_DIR), os.path.realpath(full)]
            ) == os.path.realpath(STATIC_DIR)
        except (ValueError, OSError):
            inside = False
        if not inside or not os.path.isfile(full):
            if rel == "index.html":
                self._send(PLACEHOLDER_HTML.encode("utf-8"), 200,
                           "text/html; charset=utf-8")
            else:
                self._send(b"404 Not Found", 404, set_cookie=False)
            return
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower(),
                                 "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        self._send(data, 200, ctype, set_cookie=False)

    def serve_icon(self, path):
        name = os.path.basename(urllib.parse.unquote(path[len("/icons/"):]))
        ext = os.path.splitext(name)[1].lower()
        if ext not in ICON_EXTS:
            self._send(b"404 Not Found", 404)
            return
        full = os.path.join(ICONS_DIR, name)
        if not os.path.isfile(full):
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        ctype = STATIC_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(b"404 Not Found", 404, set_cookie=False)
            return
        self._send(data, 200, ctype, set_cookie=False)

    def handle_logs(self, app_id, query):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail(app_id, tail)})

    # ------------------------------------------------------ M4 /api/v1

    V1_ROUTE_RE = re.compile(
        r"^/api/v1/(projects|resources|runs)"
        r"(?:/([0-9a-fA-F]{8}|[0-9a-zA-Z_-]+))?"
        r"(?:/(start|stop|restart|logs))?$")

    def handle_v1_get(self, path, query):
        """/api/v1 GET 路由：health/state/projects/resources/runs/logs。"""
        if path == "/api/v1/health":
            health = build_health(self.server.cfg)
            self.send_json({
                "status": health.get("status"),
                "version": APP_VERSION,
                "schemaVersion": CURRENT_SCHEMA_VERSION,
                "degraded": health.get("degraded"),
                "issues": health.get("issues"),
                "config": health.get("config"),
            })
            return
        if path == "/api/v1/state":
            state = get_state_snapshot(self.server.cfg,
                                       self.server.console_port)
            self.send_json({
                "services": state.get("services"),
                "apps": state.get("apps"),
                "projects": state.get("projects"),
                "watched": state.get("watched"),
                "consolePort": state.get("consolePort"),
                "version": state.get("version"),
                "schemaVersion": state.get("schemaVersion"),
            })
            return
        if path == "/api/v1/projects":
            snapshot = self.server.cfg.snapshot()
            self.send_json(_v1_projects(snapshot))
            return
        if path == "/api/v1/resources":
            snapshot = self.server.cfg.snapshot()
            self.send_json(_v1_resources(snapshot))
            return
        if path == "/api/v1/runs":
            self.send_json(_v1_runs(self._v1_run_query(query)))
            return
        if path == "/api/v1/agents/adapters":
            runner = get_agent_runner(self.server.cfg)
            self.send_json(runner.list_adapters() if runner else [])
            return
        if path == "/api/v1/agents/sessions":
            runner = get_agent_runner(self.server.cfg)
            if runner is None:
                self.send_json({"sessions": [], "total": 0})
                return
            params = urllib.parse.parse_qs(query)
            sessions = runner.list_sessions(
                limit=_v1_int(params.get("limit", ["50"])[0], 50),
                status=params.get("status", [None])[0] or None)
            self.send_json({
                "sessions": [_public_session(s) for s in sessions],
                "total": len(sessions),
            })
            return
        if path.startswith("/api/v1/agents/sessions/"):
            runner = get_agent_runner(self.server.cfg)
            session_id = path[len("/api/v1/agents/sessions/"):]
            session = runner.get_session(session_id) if runner else None
            if session is None:
                self.send_err(404, "会话不存在")
                return
            self.send_json(_public_session(session))
            return
        if path == "/api/v1/workflows":
            snapshot = self.server.cfg.snapshot()
            self.send_json(snapshot.get("workflows") or [])
            return
        if path == "/api/v1/workflow-runs":
            db = get_runs_db()
            runs = db.list_workflow_runs(limit=50) if db else []
            self.send_json({"runs": [_public_workflow_run(r, db)
                                     for r in runs], "total": len(runs)})
            return
        if path.startswith("/api/v1/workflow-runs/"):
            run_id = path[len("/api/v1/workflow-runs/"):]
            db = get_runs_db()
            run = db.get_workflow_run(run_id) if db else None
            if run is None:
                self.send_err(404, "工作流运行不存在")
                return
            self.send_json(_public_workflow_run(run, db))
            return
        if path == "/api/v1/git/worktrees":
            snapshot = self.server.cfg.snapshot()
            result = []
            for project in snapshot.get("projects") or []:
                repo = detect_repo(project.get("root_path"))
                if repo is None:
                    continue
                result.append({
                    "projectId": project.get("id"),
                    "projectName": project.get("name"),
                    "repo": repo,
                    "worktrees": list_worktrees(repo),
                })
            self.send_json(result)
            return
        if path.startswith("/api/v1/workflows/"):
            workflow_id = path[len("/api/v1/workflows/"):]
            snapshot = self.server.cfg.snapshot()
            workflow = next(
                (w for w in snapshot.get("workflows") or []
                 if w.get("id") == workflow_id), None)
            if workflow is None:
                self.send_err(404, "工作流不存在")
                return
            self.send_json(workflow)
            return
        match = self.V1_ROUTE_RE.match(path)
        if not match:
            self.send_err(404, "接口不存在")
            return
        collection, identifier, action = match.groups()
        if collection == "projects" and identifier and not action:
            project = self._v1_find_project(identifier)
            if project is None:
                self.send_err(404, "项目不存在")
                return
            self.send_json(project)
            return
        if collection == "resources" and identifier and not action:
            resource = self._v1_find_resource(identifier)
            if resource is None:
                self.send_err(404, "资源不存在")
                return
            self.send_json(resource)
            return
        if collection == "runs" and identifier:
            db = get_runs_db()
            run = db.get_run(identifier) if db else None
            if run is None:
                self.send_err(404, "运行记录不存在")
                return
            if action == "logs":
                tail = self._parse_log_tail(query)
                self.send_json({
                    "runId": identifier,
                    "text": read_log_tail(run.get("app_id"), tail)
                    if run.get("app_id") else "",
                })
                return
            self.send_json(public_run(run))
            return
        self.send_err(404, "接口不存在")

    def _v1_run_query(self, query):
        params = urllib.parse.parse_qs(query)
        result = {"limit": 50}
        if params.get("limit"):
            try:
                result["limit"] = max(1, min(int(params["limit"][0]), 500))
            except (TypeError, ValueError):
                pass
        if params.get("appId"):
            result["app_id"] = params["appId"][0]
        if params.get("status"):
            result["status"] = params["status"][0]
        return result

    def _v1_find_project(self, identifier):
        snapshot = self.server.cfg.snapshot()
        for project in _v1_projects(snapshot):
            if project["id"] == identifier:
                return project
        return None

    def _v1_find_resource(self, identifier):
        snapshot = self.server.cfg.snapshot()
        for resource in _v1_resources(snapshot):
            if resource["id"] == identifier:
                return resource
        return None

    def handle_v1_post(self, path):
        """/api/v1 写路由：项目/资源 CRUD 与资源启停（经 app_id 桥）。"""
        if path == "/api/v1/projects":
            data, err = self.read_json_body()
            if err:
                self.send_err(400, err)
                return
            name = str(data.get("name") or "").strip()
            root = str(data.get("rootPath") or "").strip()
            if not name or not root:
                self.send_err(400, "name 与 rootPath 必填")
                return
            try:
                def op(c):
                    from adcc.projects import create_project
                    return create_project(
                        c, name, root,
                        repo_path=data.get("repoPath") or None,
                        tags=data.get("tags") or None,
                        environment=data.get("environment") or None)
                project = self.server.cfg.update(op)
            except ValueError as e:
                self.send_err(400, str(e))
                return
            EVENTS.publish("project.updated",
                           {"id": project.get("id")})
            self.send_json(project, 201)
            return
        match = self.V1_ROUTE_RE.match(path)
        if not match:
            self.send_err(404, "接口不存在")
            return
        collection, identifier, action = match.groups()
        if collection == "resources" and identifier and action in (
                "start", "stop", "restart"):
            self.discard_body()  # keep-alive 陷阱：不读掉会污染下一个请求
            self.handle_v1_resource_action(identifier, action)
            return
        self.send_err(404, "接口不存在")

    def handle_v1_workflow_post(self, path):
        """/api/v1/workflows* 写路由：创建定义与运行/取消。"""
        if path == "/api/v1/workflows":
            data, err = self.read_json_body()
            if err:
                self.send_err(400, err)
                return
            project_id = str(data.get("projectId") or "").strip()
            name = str(data.get("name") or "").strip()
            steps = data.get("steps")
            if not project_id or not name:
                self.send_err(400, "projectId 与 name 必填")
                return
            if not isinstance(steps, list) or not steps:
                self.send_err(400, "steps 必须是非空数组")
                return
            try:
                from adcc.orchestrator import make_step
                normalized = []
                for raw in steps:
                    if not isinstance(raw, dict):
                        raise ValueError("step 必须是对象")
                    normalized.append(make_step(
                        kind=raw.get("kind"),
                        config=raw.get("config") or {},
                        needs=raw.get("needs"),
                        timeout_sec=raw.get("timeoutSec"),
                        retry_policy=raw.get("retryPolicy"),
                        locks=raw.get("locks"),
                        continue_on_error=bool(raw.get("continueOnError"))))
                workflow = make_workflow(
                    project_id=project_id, name=name, steps=normalized)
            except ValueError as exc:
                self.send_err(400, str(exc))
                return

            def op(c):
                c.setdefault("workflows", []).append(workflow)
            self.server.cfg.update(op)
            self.send_json(workflow, 201)
            return
        if path.startswith("/api/v1/workflows/"):
            remainder = path[len("/api/v1/workflows/"):]
            if remainder.endswith("/runs"):
                self.discard_body()
                workflow_id = remainder[:-len("/runs")]
                executor = get_workflow_executor(self.server.cfg)
                if executor is None:
                    self.send_err(503, "运行历史数据库不可用")
                    return
                snapshot = self.server.cfg.snapshot()
                workflow = next(
                    (w for w in snapshot.get("workflows") or []
                     if w.get("id") == workflow_id), None)
                if workflow is None:
                    self.send_err(404, "工作流不存在")
                    return
                run, error = executor.start(
                    workflow, workflow.get("project_id"))
                if error:
                    self.send_json({"ok": False, "error": error}, 400)
                    return
                self.send_json(_public_workflow_run(run, get_runs_db()), 201)
                return
        if path.startswith("/api/v1/workflow-runs/"):
            run_id = path[len("/api/v1/workflow-runs/"):]
            if run_id.endswith("/cancel"):
                self.discard_body()
                run_id = run_id[:-len("/cancel")]
                executor = get_workflow_executor(self.server.cfg)
                if executor is None:
                    self.send_err(503, "运行历史数据库不可用")
                    return
                ok, error = executor.cancel(run_id)
                if not ok:
                    self.send_json({"ok": False, "error": error}, 409)
                    return
                self.send_json({"ok": True})
                return
        self.send_err(404, "接口不存在")

    def handle_v1_agents_post(self, path):
        """/api/v1/agents/* 写路由：adapter 注册与 session 启停。"""
        runner = get_agent_runner(self.server.cfg)
        if runner is None:
            self.send_err(503, "运行历史数据库不可用")
            return
        if path == "/api/v1/agents/adapters":
            data, err = self.read_json_body()
            if err:
                self.send_err(400, err)
                return
            try:
                adapter = make_adapter(
                    name=str(data.get("name") or "").strip(),
                    executable=str(data.get("executable") or "").strip(),
                    args_template=data.get("argsTemplate"),
                    env_template=data.get("envTemplate"),
                    cwd_template=data.get("cwdTemplate"),
                    stdin_mode=data.get("stdinMode") or "file",
                )
            except ValueError as exc:
                self.send_err(400, str(exc))
                return
            runner.add_adapter(adapter)
            self.send_json(adapter, 201)
            return
        if path == "/api/v1/agents/sessions":
            data, err = self.read_json_body()
            if err:
                self.send_err(400, err)
                return
            adapter_id = str(data.get("adapterId") or "").strip()
            project_id = str(data.get("projectId") or "").strip()
            if not adapter_id or not project_id:
                self.send_err(400, "adapterId 与 projectId 必填")
                return
            prompt_file = data.get("promptFile")
            if prompt_file is not None and (
                    not isinstance(prompt_file, str) or not prompt_file.strip()):
                self.send_err(400, "promptFile 必须是路径字符串")
                return
            session, error = runner.start(
                adapter_id, project_id,
                prompt=data.get("prompt"),
                prompt_file=prompt_file,
                workflow_run_id=data.get("workflowRunId"),
                workflow_step_id=data.get("workflowStepId"))
            if error:
                self.send_json({"ok": False, "error": error}, 400)
                return
            self.send_json(_public_session(session), 201)
            return
        if path.startswith("/api/v1/agents/sessions/"):
            session_id = path[len("/api/v1/agents/sessions/"):]
            if session_id.endswith("/stop"):
                self.discard_body()
                session_id = session_id[:-len("/stop")]
                ok, error = runner.stop(session_id)
                if not ok:
                    self.send_json({"ok": False, "error": error}, 409)
                    return
                self.send_json({"ok": True})
                return
        self.send_err(404, "接口不存在")

    def handle_v1_resource_action(self, resource_id, action):
        """资源启停：经 app_id 桥委托 legacy app 操作（锁由装饰器承担）。"""
        snapshot = self.server.cfg.snapshot()
        resource = next(
            (r for r in snapshot.get("resources") or []
             if r.get("id") == resource_id), None)
        if resource is None:
            self.send_err(404, "资源不存在")
            return
        app_id = resource.get("app_id")
        if not app_id:
            self.send_err(409, "该资源尚未关联受管应用（等待 M4 完整接管）")
            return
        if action == "start":
            self.handle_app_start(app_id)
        elif action == "stop":
            self.handle_app_stop(app_id)
        else:
            self.handle_app_restart(app_id)

    def handle_v1_events(self):
        """SSE 事件流：text/event-stream，断线自动清理订阅。"""
        subscription = EVENTS.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    event = subscription.get(timeout=1)
                except Exception:
                    event = None
                if event is not None:
                    try:
                        payload = json.dumps(
                            event, ensure_ascii=False)
                        self.wfile.write(
                            ("data: %s\n\n" % payload).encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    deadline = time.monotonic() + 15
                    continue
                if time.monotonic() >= deadline:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    deadline = time.monotonic() + 15
        finally:
            EVENTS.unsubscribe(subscription)

    def handle_v1_console_log(self, query):
        self.handle_console_log(query)

    def handle_logs(self, app_id, query):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail(app_id, tail)})

    def handle_console_log(self, query):
        """总控台自身日志（data/logs/console.log），与维护线程共用轮转。"""
        tail = self._parse_log_tail(query)
        self.send_json({"text": read_log_tail("console", tail)})

    @staticmethod
    def _parse_log_tail(query, default=300):
        try:
            tail = int(urllib.parse.parse_qs(query).get("tail", [default])[0])
        except (ValueError, IndexError):
            tail = default
        return max(1, min(tail, 5000))

    # ---------- POST ----------

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            route_match = APP_ROUTE_RE.match(path)
            content_kind = ("image" if route_match and
                            route_match.group(2) == "icon" else "json")
            if not self.authorize_request(mutating=True,
                                          content_kind=content_kind):
                return
            if path == "/api/kill":
                self.handle_kill()
                return
            if path == "/api/services/flag":
                self.handle_flag()
                return
            if path == "/api/watch":
                self.handle_watch()
                return
            if path == "/api/ui/theme":
                self.handle_ui_theme()
                return
            if path == "/api/pick":
                self.handle_pick()
                return
            if path == "/api/project/detect":
                self.handle_project_detect()
                return
            if path == "/api/console/restart":
                self.discard_body()
                self.handle_console_restart()
                return
            if path == "/api/console/stop":
                self.discard_body()
                self.handle_console_stop()
                return
            if path.startswith("/api/v1/"):
                if path.startswith("/api/v1/agents/"):
                    self.handle_v1_agents_post(path)
                elif path.startswith("/api/v1/workflow"):
                    self.handle_v1_workflow_post(path)
                else:
                    self.handle_v1_post(path)
                return
            if path == "/api/apps":
                self.handle_app_create()
                return
            if path == "/api/apps/reorder":
                self.handle_apps_reorder()
                return
            m = APP_ROUTE_RE.match(path)
            if m:
                app_id, action = m.group(1), m.group(2)
                if action == "start":
                    self.discard_body()
                    self.handle_app_start(app_id)
                    return
                if action == "stop":
                    self.discard_body()
                    self.handle_app_stop(app_id)
                    return
                if action == "restart":
                    self.discard_body()
                    self.handle_app_restart(app_id)
                    return
                if action == "diagnose":
                    self.discard_body()
                    self.handle_app_diagnose(app_id)
                    return
                if action == "attach":
                    self.handle_app_attach(app_id)
                    return
                if action == "icon":
                    self.handle_icon_upload(app_id)
                    return
                if action == "favicon":
                    self.discard_body()
                    self.handle_fetch_favicon(app_id)
                    return
            self.send_err(404, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("POST", e)

    def handle_pick(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        what = data.get("what")
        if what not in ("dir", "script"):
            self.send_err(400, "what 必须是 dir/script")
            return
        path, canceled = pick_path(what)
        if canceled:  # 用户取消不是错误，前端静默
            self.send_json({"ok": True, "canceled": True})
        elif not path:
            self.send_json({"ok": False, "error": "无法打开系统选择框"})
        else:
            result = {"ok": True, "path": path}
            if what == "script":
                result["command"] = command_for_script(path)
            self.send_json(result)

    def handle_project_detect(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        result, err = detect_project(data.get("cwd"))
        if err:
            self.send_err(400, err)
            return
        self.send_json(result)

    def handle_app_diagnose(self, app_id):
        cfg = self.server.cfg.snapshot()
        app = find_app(cfg, app_id)
        if not app:
            self.send_err(404, "应用不存在")
            return
        self.send_json(diagnose_app(cfg, app))

    def handle_ui_theme(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        theme_id = str(data.get("theme") or "")
        known = {t["id"] for t in list_themes()}
        if theme_id not in known:
            self.send_err(400, "未知主题: %s" % theme_id)
            return
        self.server.cfg.update(lambda d: d.__setitem__("uiTheme", theme_id))
        self.send_json({"ok": True, "theme": theme_id})

    def handle_console_restart(self):
        reserved, current, helper_pid = self.server.reserve_console_action("restart")
        if not reserved:
            if current == "restart":
                self.send_json({"ok": True, "pid": SELF_PID,
                                "helperPid": helper_pid,
                                "port": self.server.console_port,
                                "alreadyScheduled": True})
            else:
                self.send_err(409, "总控台正在停止，无法重复重启")
            return
        try:
            helper_pid = schedule_console_restart(
                self.server, self.server.console_port)
        except OSError as e:
            self.server.release_console_action("restart")
            self.send_err(500, "无法启动重启程序: %s" % e)
            return
        self.server.set_console_helper_pid(helper_pid)
        invalidate_state_cache()
        self.send_json({"ok": True, "pid": SELF_PID,
                        "helperPid": helper_pid,
                        "port": self.server.console_port})

    def handle_console_stop(self):
        reserved, current, _ = self.server.reserve_console_action("stop")
        if not reserved:
            if current == "stop":
                self.send_json({"ok": True, "pid": SELF_PID,
                                "port": self.server.console_port,
                                "alreadyScheduled": True})
            else:
                self.send_err(409, "总控台正在重启，无法同时停止")
            return
        schedule_console_stop(self.server)
        invalidate_state_cache()
        self.send_json({"ok": True, "pid": SELF_PID,
                        "port": self.server.console_port})

    def handle_kill(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        pid = data.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            self.send_err(400, "缺少字段 pid（正整数）")
            return
        ok, err = kill_process(pid, bool(data.get("force")))
        if ok:
            invalidate_state_cache()
        self.send_json({"ok": True} if ok else {"ok": False, "error": err})

    def handle_flag(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        key, flag, value = data.get("key"), data.get("flag"), data.get("value")
        if not isinstance(key, str) or not key:
            self.send_err(400, "缺少字段 key")
            return
        if flag not in ("hidden", "pinned", "promoted"):
            self.send_err(400, "flag 必须是 hidden/pinned/promoted")
            return
        if not isinstance(value, bool):
            self.send_err(400, "value 必须是布尔值")
            return

        def op(c):
            lst = c.setdefault(flag, [])
            if value and key not in lst:
                lst.append(key)
            elif not value and key in lst:
                lst.remove(key)

        self.server.cfg.update(op)
        self.send_json({"ok": True})

    def handle_watch(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        keyword, action = data.get("keyword"), data.get("action")
        if not isinstance(keyword, str) or not keyword.strip():
            self.send_err(400, "缺少字段 keyword")
            return
        if action not in ("add", "remove"):
            self.send_err(400, "action 必须是 add/remove")
            return
        keyword = keyword.strip()

        def op(c):
            kws = c.setdefault("watchedKeywords", [])
            if action == "add" and keyword not in kws:
                kws.append(keyword)
            elif action == "remove":
                c["watchedKeywords"] = [k for k in kws if k != keyword]
            return list(c["watchedKeywords"])

        keywords = self.server.cfg.update(op)
        self.send_json({"ok": True, "keywords": keywords})

    def handle_app_create(self):
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        attach_pid = data.get("attachPid")
        if attach_pid is not None and (
                not isinstance(attach_pid, int)
                or isinstance(attach_pid, bool)
                or attach_pid <= 0):
            self.send_err(400, "attachPid 必须是正整数")
            return
        fields, err = validate_app_fields(data, partial=False)
        if err:
            self.send_err(400, err)
            return

        snapshot = self.server.cfg.snapshot()
        new_id = secrets.token_hex(4)
        while find_app(snapshot, new_id):
            new_id = secrets.token_hex(4)
        app = {"id": new_id, "name": fields["name"],
               "command": fields["command"], "cwd": fields["cwd"],
               "port": fields["port"], "emoji": fields["emoji"],
               "glyph": fields["glyph"], "kind": fields["kind"],
               "icon": None, "favicon": None, "lastPid": None,
               "lastPgid": None, "runToken": None,
               "attached": False, "lastExit": None,
               "createdAt": int(time.time())}
        cwd_updated = False
        if attach_pid is not None:
            ok, error, identity = inspect_attach_process(
                self.server.cfg, app, attach_pid)
            if not ok:
                self.send_json(
                    {"ok": False, "error": error},
                    identity.get("status", 409),
                )
                return
            actual_cwd = identity["cwd"]
            try:
                cwd_updated = (
                    not app.get("cwd")
                    or os.path.realpath(app["cwd"]) != os.path.realpath(actual_cwd)
                )
            except OSError:
                cwd_updated = True
            app["cwd"] = actual_cwd
            app["lastPid"] = attach_pid
            app["attached"] = True

        attach_conflict = [False]

        def op(c):
            if find_app(c, new_id):
                return None
            # 与 attach_app_process 同规则：写锁内重验 pid 未被其他卡片认领。
            if attach_pid is not None and any(
                    other.get("lastPid") == attach_pid
                    for other in c.get("apps") or []):
                attach_conflict[0] = True
                return None
            c["apps"].append(app)
            # M3/M4：同步注册项目资源（cwd 匹配项目或 Unassigned）
            try:
                register_resource_for_app(c, app)
            except Exception:
                LOG.exception("注册项目资源失败: %s", new_id)
            return dict(app)

        created = self.server.cfg.update(op)
        if created is None:
            if attach_conflict[0]:
                self.send_json(
                    {"ok": False, "error": "该进程已由其他卡片管理"}, 409)
            else:
                self.send_err(409, "应用标识发生冲突，请重试")
            return
        if attach_pid is not None:
            created.update({
                "attached": True,
                "running": True,
                "pid": attach_pid,
                "cwdUpdated": cwd_updated,
            })
        self.send_json(created)

    @serialized_app_operation
    def handle_fetch_favicon(self, app_id):
        """抓取应用有效端口对应站点的 favicon，存为 data/icons/fav-{id}.{ext}。
        优先级低于用户自定义 icon/glyph，仅作兜底。"""
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        live = set(managed_pids(app))
        port = None
        listeners = scan_listeners()
        configured_port = app.get("port")
        if configured_port and any(pid in live and p == configured_port
                                   for pid, p in listeners):
            port = configured_port
        if not port:
            owned_ports = sorted({p for pid, p in listeners if pid in live})
            port = owned_ports[0] if owned_ports else None
        if not port:
            self.send_json({"ok": False, "error": "应用未运行或无可用端口"})
            return
        host = listener_open_host(listeners, port, live)
        data, ext = fetch_favicon(port, host)
        if not data:
            self.send_json({"ok": False, "error": "未找到站点图标"})
            return
        fname = "fav-%s.%s" % (app_id, ext)
        try:
            _ensure_private_dir(ICONS_DIR)
            write_private_bytes(os.path.join(ICONS_DIR, fname), data)
        except OSError as e:
            self.send_json({"ok": False, "error": "图标保存失败: %s" % e})
            return
        url = "/icons/" + fname

        def op(c):
            target = find_app(c, app_id)
            if target:
                target["favicon"] = url

        self.server.cfg.update(op)
        self.send_json({"ok": True, "favicon": url})

    def handle_apps_reorder(self):
        """按收到的 id 顺序重排 apps（Python sort 稳定：未涉及的 id 相对顺序不变，
        服务/任务两区可独立排序互不干扰）。"""
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        ids = data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            self.send_err(400, "ids 必须是字符串数组")
            return
        order = {i: n for n, i in enumerate(ids)}

        def op(c):
            c["apps"].sort(key=lambda a: order.get(a.get("id"), len(order)))

        self.server.cfg.update(op)
        self.send_json({"ok": True})

    @serialized_app_operation
    def handle_app_start(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        if app_alive_sign(app):
            self.send_json({"ok": False, "error": "应用已在运行"})
            return
        health = inspect_app_health(app)
        if health["blocking"]:
            issue = health["issues"][0]
            self.send_json({
                "ok": False,
                "error": "%s：%s" % (issue["title"], issue["detail"]),
                "health": health,
            }, 422)
            return
        port = app.get("port")
        occupied = [(pid, p) for pid, p in scan_listeners() if p == port] if port else []
        if occupied:
            self.send_json({"ok": False, "error": "端口 %d 已被 PID %d 占用" %
                            (port, occupied[0][0])}, 409)
            return
        ok, err, proc, pgid, token = start_app(app)
        if not ok:
            self.send_json({"ok": False, "error": err})
            return
        if not persist_started_app(self.server.cfg, app_id, proc, pgid, token):
            stop_pid_tree(pgid)
            self.send_json({"ok": False, "error": "应用已被删除，已取消启动"}, 409)
            return
        # 一次性任务的正常形态就是快速退出，不能沿用服务的启动探测逻辑把
        # `echo`、清缓存等成功任务误判成“启动失败”。退出线程会独立记录结果。
        if (app.get("kind") or "service") == "task":
            self.send_json({"ok": True, "pid": proc.pid})
            return
        deadline = time.monotonic() + STARTUP_PROBE_SEC
        code = proc.poll()
        while code is None and time.monotonic() < deadline:
            time.sleep(0.025)
            code = proc.poll()
        if code is not None:
            self.send_json({"ok": False,
                            "error": startup_failure_message(app_id, code)}, 422)
            return
        self.send_json({"ok": True, "pid": proc.pid})

    @serialized_app_operation
    def handle_app_stop(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        if not app_alive_sign(app):
            self.send_json({"ok": False, "error": "应用未在运行"})
            return
        ok, error = stop_app_and_clear(self.server.cfg, app)
        if not ok:
            self.send_json({"ok": False, "error": error}, 409)
            return
        self.send_json({"ok": True})

    @serialized_app_operation
    def handle_app_attach(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        data, err = self.read_json_body()
        if err:
            self.send_err(400, err)
            return
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            self.send_err(400, "pid 必须是正整数")
            return
        ok, error, info = attach_app_process(self.server.cfg, app_id, app, pid)
        if not ok:
            self.send_json({"ok": False, "error": error}, info.get("status", 409))
            return
        resp = {"ok": True, "pid": pid}
        resp.update(info)
        self.send_json(resp)

    @serialized_app_operation
    def handle_app_restart(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        if not app_alive_sign(app):
            self.send_err(409, "应用未在运行")
            return
        # 必须在停止旧服务前预检；配置已失效时保留仍在工作的旧进程。
        health = inspect_app_health(app)
        if health["blocking"]:
            issue = health["issues"][0]
            self.send_json({
                "ok": False,
                "error": "%s：%s。旧服务仍在运行" %
                         (issue["title"], issue["detail"]),
                "health": health,
            }, 422)
            return

        stopped, error = stop_app_and_clear(self.server.cfg, app)
        if not stopped:
            self.send_err(409, error or "旧进程停止失败，已取消重启")
            return

        port = app.get("port")
        occupied = [(pid, p) for pid, p in scan_listeners() if p == port] if port else []
        if occupied:
            self.send_err(409, "端口 %d 已被 PID %d 占用，旧应用已停止" %
                          (port, occupied[0][0]))
            return

        latest = self.server.cfg.snapshot()
        current = find_app(latest, app_id)
        if not current:
            self.send_err(404, "应用已被删除")
            return
        ok, err, proc, pgid, new_token = start_app(current)
        if not ok:
            self.send_err(500, err)
            return
        if not persist_started_app(
                self.server.cfg, app_id, proc, pgid, new_token):
            stop_pid_tree(pgid)
            self.send_err(409, "应用已被删除，已取消重启")
            return
        self.send_json({"ok": True, "pid": proc.pid})

    @serialized_app_operation
    def handle_icon_upload(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            self.send_err(400, "缺少 Content-Length")
            return
        if length > MAX_ICON_BYTES:
            self.send_err(400, "图标大小不能超过 5MB")
            return
        raw = self.rfile.read(length)
        kind = sniff_image(raw)
        if kind is None:
            self.send_err(400, "仅支持 PNG / JPEG / WebP 图片")
            return
        _ensure_private_dir(ICONS_DIR)
        for ext in ICON_EXTS:
            old = os.path.join(ICONS_DIR, app_id + ext)
            if ext != "." + kind and os.path.isfile(old):
                try:
                    os.remove(old)
                except OSError:
                    pass
        fname = "%s.%s" % (app_id, kind)
        try:
            write_private_bytes(os.path.join(ICONS_DIR, fname), raw)
        except OSError as e:
            self.send_err(500, "图标保存失败: %s" % e)
            return
        icon_url = "/icons/" + fname

        def op(c):
            target = find_app(c, app_id)
            if target:
                target["icon"] = icon_url

        self.server.cfg.update(op)
        self.send_json({"ok": True, "icon": icon_url})

    # ---------- PUT ----------

    def do_PUT(self):
        operation_lock = None
        try:
            if not self.authorize_request(mutating=True,
                                          content_kind="json"):
                return
            path = urllib.parse.urlparse(self.path).path
            m = APP_ROUTE_RE.match(path)
            if not (m and m.group(2) is None):
                self.send_err(404, "接口不存在")
                return
            operation_lock = self.server.try_app_operation(m.group(1))
            if operation_lock is None:
                self.send_err(409, "该应用正在执行其他操作，请稍后重试")
                return
            data, err = self.read_json_body()
            if err:
                self.send_err(400, err)
                return
            stop_before_update = data.get("stopBeforeUpdate", False)
            if not isinstance(stop_before_update, bool):
                self.send_err(400, "stopBeforeUpdate 必须是布尔值")
                return
            _, app = self._get_app_or_404(m.group(1))
            if app is None:
                return
            fields, err = validate_app_fields(data, partial=True)
            if err:
                self.send_err(400, err)
                return
            if not fields:
                self.send_err(400, "没有可更新的字段")
                return
            lifecycle_fields = {"command", "cwd", "port", "kind"}
            lifecycle_changed = any(
                key in fields and fields[key] != app.get(key)
                for key in lifecycle_fields)
            stopped_for_update = False
            if lifecycle_changed and app_alive_sign(app):
                if not stop_before_update:
                    stop_label = ("中止任务"
                                  if (app.get("kind") or "service") == "task"
                                  else "停止服务")
                    self.send_json({
                        "ok": False,
                        "error": "应用正在运行，请先在当前编辑面板%s；填写内容会保留" %
                                 stop_label,
                        "requiresStop": True,
                    }, 409)
                    return
                ok, stop_error, stopped_for_update = stop_app_for_update(
                    self.server.cfg, app)
                if not ok:
                    self.send_err(409, stop_error)
                    return

            def op(c):
                target = find_app(c, m.group(1))
                target.update(fields)
                return dict(target)

            updated = self.server.cfg.update(op)
            if stopped_for_update:
                updated = dict(updated)
                updated["stoppedForUpdate"] = True
            self.send_json(updated)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("PUT", e)
        finally:
            if operation_lock is not None:
                operation_lock.release()

    # ---------- DELETE ----------

    def do_DELETE(self):
        try:
            if not self.authorize_request(mutating=True):
                return
            path = urllib.parse.urlparse(self.path).path
            m = APP_ROUTE_RE.match(path)
            if not m:
                self.send_err(404, "接口不存在")
                return
            app_id, action = m.group(1), m.group(2)
            if action is None:
                self.handle_app_delete(app_id)
                return
            if action == "icon":
                self.handle_icon_delete(app_id)
                return
            self.send_err(404, "接口不存在")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._handle_request_error("DELETE", e)

    def do_OPTIONS(self):
        # No CORS endpoint exists. An explicit denial is clearer than the
        # BaseHTTPRequestHandler HTML 501 response and never grants ACAO.
        self._deny_request(403, "控制台不接受跨域预检请求")

    @serialized_app_operation
    def handle_app_delete(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        if app_running(app):
            stopped, error = stop_app_and_clear(self.server.cfg, app)
            if not stopped:
                self.send_err(409, "删除已取消：%s" %
                              (error or "应用未能正常退出"))
                return

        def op(c):
            before = len(c["apps"])
            c["apps"] = [a for a in c["apps"] if a.get("id") != app_id]
            # M3/M4：同步清理对应项目资源（app_id 桥）
            c["resources"] = [
                r for r in c.get("resources") or []
                if r.get("app_id") != app_id]
            return len(c["apps"]) != before

        if not self.server.cfg.update(op):
            self.send_err(404, "应用不存在")
            return
        self.server.forget_app_lock(app_id)

        for ext in ICON_EXTS:
            for fname in (app_id + ext, "fav-" + app_id + ext):
                try:
                    os.remove(os.path.join(ICONS_DIR, fname))
                except OSError:
                    pass
        log_path = os.path.join(LOGS_DIR, "%s.log" % app_id)
        for candidate in [log_path] + ["%s.%d" % (log_path, i)
                                       for i in range(1, LOG_BACKUPS + 1)]:
            try:
                os.remove(candidate)
            except OSError:
                pass

        self.send_json({"ok": True})

    @serialized_app_operation
    def handle_icon_delete(self, app_id):
        _, app = self._get_app_or_404(app_id)
        if app is None:
            return
        for ext in ICON_EXTS:
            try:
                os.remove(os.path.join(ICONS_DIR, app_id + ext))
            except OSError:
                pass

        def op(c):
            target = find_app(c, app_id)
            if target:
                target["icon"] = None

        self.server.cfg.update(op)
        self.send_json({"ok": True})


# ---------------------------------------------------------------- 启动

def open_browser_later(port, delay=0.8):
    def _open():
        try:
            time.sleep(delay)
            webbrowser.open("http://%s:%d/" % (HOST, port))
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def find_console_instances():
    """查找从同一项目目录启动的总控台，用于双击启动器去重。"""
    snap = ps_snapshot(None, with_uid=True)
    candidates = []
    for pid, info in snap.items():
        args = info.get("args") or ""
        if (pid == SELF_PID or info.get("uid") != SELF_UID
                or "server.py" not in args
                or "--restart-helper" in args):
            continue
        candidates.append(pid)
    cwds = lsof_cwds(candidates)
    listener_map = {}
    for pid, port in scan_listeners():
        listener_map.setdefault(pid, []).append(port)
    result = []
    for pid in candidates:
        cwd = cwds.get(pid)
        try:
            same_dir = cwd and os.path.realpath(cwd) == os.path.realpath(BASE_DIR)
        except OSError:
            same_dir = False
        if not same_dir:
            continue
        info = snap.get(pid, {})
        result.append({
            "pid": pid,
            "ports": sorted(listener_map.get(pid, [])),
            "cmd": info.get("args") or "",
            "cwd": cwd,
            "uptimeSec": info.get("etime"),
        })
    return sorted(result, key=lambda item: (item["ports"] or [65536], item["pid"]))


def _launcher_dialog(message):
    return PLATFORM.show_dialog(
        "总控台", message, ["取消", "重新启动", "打开控制台"], default_index=2)


def _launcher_alert(message):
    PLATFORM.show_alert("总控台", message)


def launcher_main():
    """start.command 的无命令启动入口。"""
    instances = find_console_instances()
    if not instances:
        try:
            main(log_to_file=True)
        except Exception:
            _launcher_alert("总控台启动失败。请检查数据目录权限和 console.log。")
            raise
        return
    labels = []
    for item in instances:
        ports = " / ".join(":%d" % p for p in item["ports"]) or "未监听"
        labels.append("%s  ·  PID %d" % (ports, item["pid"]))
    extra = ("\n\n检测到 %d 个同项目实例，重启时会合并为一个。" % len(instances)
             if len(instances) > 1 else "")
    choice = _launcher_dialog(
        "总控台已在运行：\n" + "\n".join(labels) + extra)
    if choice == "打开控制台":
        ports = [p for item in instances for p in item["ports"]]
        port = min(ports) if ports else PORT_START
        webbrowser.open("http://%s:%d/" % (HOST, port))
        return
    if choice != "重新启动":
        return

    preferred_ports = [p for item in instances for p in item["ports"]]
    preferred = min(preferred_ports) if preferred_ports else PORT_START
    targets = [item["pid"] for item in instances]
    for pid in targets:
        if process_uid(pid) == SELF_UID:
            PLATFORM.signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in targets):
        time.sleep(0.1)
    survivors = [pid for pid in targets if pid_alive(pid)]
    if survivors:
        _launcher_alert("旧总控台未能正常退出（PID %s），未强制结束。" %
                        "、".join(str(pid) for pid in survivors))
        return
    try:
        main(preferred_port=preferred, log_to_file=True)
    except Exception:
        _launcher_alert("总控台重启失败。请检查数据目录权限和 console.log。")
        raise


def schedule_console_restart(server, preferred_port):
    """启动独立 helper，响应发出后关闭当前 HTTP 服务。"""
    helper = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--restart-helper",
         str(SELF_PID), str(int(preferred_port))],
        cwd=BASE_DIR, start_new_session=True, close_fds=True)

    def _shutdown():
        time.sleep(0.25)
        server.shutdown()
    threading.Thread(target=_shutdown, daemon=True).start()
    return helper.pid


def schedule_console_stop(server):
    """响应发送完成后关闭 HTTP 服务，不结束启动台里的独立进程组。"""
    def _shutdown():
        time.sleep(0.25)
        server.shutdown()
    threading.Thread(target=_shutdown, daemon=True).start()


def restart_helper(old_pid, preferred_port):
    """等旧进程释放端口后，拉起新总控台（macOS 原地 exec）。

    Windows 不支持 ``os.execv``，改由 helper 以独立子进程方式启动新实例，
    完成后 helper 正常退出。
    """
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and pid_alive(old_pid):
        time.sleep(0.1)
    if pid_alive(old_pid):
        return 1
    args = [sys.executable, os.path.abspath(__file__),
            "--preferred-port", str(int(preferred_port)), "--no-browser"]
    if IS_WINDOWS:
        try:
            subprocess.Popen(args, cwd=BASE_DIR, close_fds=True,
                             creationflags=0x08000000)  # CREATE_NO_WINDOW
        except OSError:
            return 3
        return 0
    os.execv(sys.executable, args)
    return 0


def _run_console(preferred_port=None, open_browser=True):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for private_dir in (DATA_DIR, ICONS_DIR, LOGS_DIR):
        _ensure_private_dir(private_dir)
    start_log_maintenance()
    cfg = Config(CONFIG_PATH)
    ensure_project_domain(cfg)
    reconcile_runs(cfg)
    runner = get_agent_runner(cfg)
    if runner is not None:
        try:
            runner.reconcile()
        except Exception:
            LOG.exception("agent 会话对账失败")
    executor = get_workflow_executor(cfg)
    if executor is not None:
        try:
            executor.recover()
        except Exception:
            LOG.exception("工作流恢复失败")
    start_run_guard(cfg)

    server, port = None, None
    candidates = list(range(PORT_START, PORT_START + PORT_TRIES))
    if isinstance(preferred_port, int) and preferred_port in candidates:
        candidates.remove(preferred_port)
        candidates.insert(0, preferred_port)
    for p in candidates:
        try:
            server = ConsoleServer((HOST, p), Handler, cfg, p)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("错误：端口 %d-%d 均被占用，无法启动。" %
              (PORT_START, PORT_START + PORT_TRIES - 1))
        sys.exit(1)

    print("总控台已启动: http://%s:%d/  (Ctrl+C 停止)" % (HOST, port), flush=True)
    _write_daemon_endpoint(server)
    if open_browser:
        open_browser_later(port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _remove_daemon_endpoint()
        print("已停止", flush=True)


def _daemon_endpoint_path():
    return os.path.join(DATA_DIR, "daemon.json")


def _write_daemon_endpoint(server):
    """供 CLI 发现 daemon 端口/身份（M5 §17）。失败只降级不阻断启动。"""
    try:
        write_private_bytes(_daemon_endpoint_path(), json.dumps({
            "port": server.console_port,
            "pid": SELF_PID,
            "token": server.control_token,
        }).encode("utf-8"))
    except OSError:
        LOG.warning("无法写入 daemon.json（CLI 将无法发现 daemon）")


def _remove_daemon_endpoint():
    try:
        os.remove(_daemon_endpoint_path())
    except OSError:
        pass


def redirect_console_output():
    """在运行目录迁移完成后，将 .app 输出安全追加到 Library Logs。"""
    path = os.path.join(LOGS_DIR, "console.log")
    fd = PLATFORM.open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError):
                pass
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, OSError):
            pass


def main(preferred_port=None, open_browser=True, log_to_file=False):
    """Run exactly one console for this project/data directory."""
    migration = prepare_runtime_storage()
    if log_to_file:
        redirect_console_output()
    if migration["dataMigrated"]:
        print("已将项目内旧配置和图标复制到: %s" % DATA_DIR,
              flush=True)
    if migration["logsMigrated"]:
        print("已将项目内旧日志复制到: %s" % LOGS_DIR,
              flush=True)
    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        print("总控台已在运行（同一数据目录只允许一个实例）。", flush=True)
        if open_browser:
            instances = find_console_instances()
            ports = [port for item in instances for port in item.get("ports", [])]
            if ports:
                webbrowser.open("http://%s:%d/" % (HOST, min(ports)))
        return False
    try:
        _run_console(preferred_port, open_browser)
        return True
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    if "--prepare-storage" in sys.argv:
        # 供安装/诊断流程预先验证迁移和目录权限，不启动 HTTP。
        prepare_runtime_storage()
    elif "--launcher" in sys.argv:
        launcher_main()
    elif "--restart-helper" in sys.argv:
        index = sys.argv.index("--restart-helper")
        try:
            old = int(sys.argv[index + 1])
            preferred = int(sys.argv[index + 2])
        except (ValueError, IndexError):
            sys.exit(2)
        sys.exit(restart_helper(old, preferred))
    else:
        preferred = None
        if "--preferred-port" in sys.argv:
            index = sys.argv.index("--preferred-port")
            try:
                preferred = int(sys.argv[index + 1])
            except (ValueError, IndexError):
                sys.exit(2)
        main(preferred_port=preferred, open_browser="--no-browser" not in sys.argv)
