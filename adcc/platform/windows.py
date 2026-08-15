"""Windows platform adapter.

Uses only Python standard library plus OS facilities: `netstat -ano`,
PowerShell `Get-CimInstance Win32_Process` and `taskkill`.  Facts that
Windows cannot provide cheaply (process cwd, POSIX process groups, cpu
percentage) degrade explicitly to unknown values instead of fabricating
data.

Identity notes
--------------
Windows has no POSIX process groups and no portable way to read another
process's environment.  Managed identity therefore uses:
- recorded `lastPid` of the `cmd.exe` wrapper that carries the run marker
  in its command line (``CONSOLE_RUN_TOKEN=<token>`` prefix);
- current-user check via session id / owner;
- descendants obtained from parent/child ancestry, not a process group.
"""

import getpass
import json
import os
import subprocess
import time
from datetime import datetime

from adcc.core.constants import RUN_TOKEN_ENV
from adcc.platform.base import PlatformAdapter, run_cmd
from adcc.runtime.ports import parse_netstat_listeners

CIM_TIMEOUT = 10.0
NETSTAT_TIMEOUT = 10.0
TASKKILL_TIMEOUT = 10.0
CIM_CACHE_TTL = 2.0  # 全量 CIM 查询 ~1.5s，短 TTL 供一轮 state 构建复用


def _entry_pid(entry):
    try:
        return int(entry.get("pid"))
    except (TypeError, ValueError):
        return -1

_CIM_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$fields = 'ProcessId','ParentProcessId','Name','ExecutablePath','CommandLine',`
'CreationDate','WorkingSetSize','SessionId'
$rows = Get-CimInstance Win32_Process -Property $fields
$total = (Get-CimInstance Win32_OperatingSystem -Property TotalVisibleMemorySize).TotalVisibleMemorySize
$items = foreach ($row in $rows) {
  [pscustomobject]@{
    pid = $row.ProcessId
    ppid = $row.ParentProcessId
    name = $row.Name
    exe = $row.ExecutablePath
    args = $row.CommandLine
    created = $row.CreationDate
    wss = $row.WorkingSetSize
    session = $row.SessionId
  }
}
if ($total -gt 0) {
  $totalKB = [double]$total
} else {
  $totalKB = 0
}
$payload = @{ total_mb = $totalKB; processes = @($items) }
$payload | ConvertTo-Json -Compress -Depth 4
"""


class WindowsPlatformAdapter(PlatformAdapter):

    name = "windows"

    def __init__(self):
        self._session_id = None
        self._full_cache = (0.0, None)

    def pid_alive(self, pid):
        """ctypes liveness probe.

        ``os.kill(pid, 0)`` on Windows maps to TerminateProcess, which
        would kill the probe target — never use it here.
        """
        import ctypes
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    # ------------------------------------------------------------ identity

    def current_user_id(self):
        return getpass.getuser()

    def process_user_id(self, pid):
        payload = self._cim_query([int(pid)])
        if not payload:
            return None
        entries = payload.get("processes") or []
        if not entries:
            return None
        return self._owner_for(entries[0])

    # ------------------------------------------------------------ processes

    def invalidate_cache(self):
        """启动新进程后调用：清空全量 CIM 缓存，使身份识别立即可见。"""
        self._full_cache = (0.0, None)

    def _cim_query(self, pids=None):
        """Full-table CIM query with TTL cache; filtering happens in Python.

        A single unfiltered pass (~1.5s) is faster than PowerShell-side
        pipeline filters and is reused across one state-build round via
        ``CIM_CACHE_TTL``.
        """
        now = time.monotonic()
        cached_at, cached = self._full_cache
        if cached is None or now - cached_at >= CIM_CACHE_TTL:
            payload = self._cim_query_raw()
            if payload is not None:
                self._full_cache = (now, payload)
                cached = payload
        if cached is None:
            return None
        if pids is None:
            return cached
        wanted = set()
        if not isinstance(pids, (set, list, tuple)):
            pids = [pids]
        for pid in pids:
            try:
                wanted.add(int(pid))
            except (TypeError, ValueError):
                pass
        if not wanted:
            return {"total_mb": cached.get("total_mb"), "processes": []}
        return {
            "total_mb": cached.get("total_mb"),
            "processes": [
                entry for entry in cached.get("processes", [])
                if _entry_pid(entry) in wanted
            ],
        }

    def _cim_query_raw(self):
        script = _CIM_SCRIPT
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=CIM_TIMEOUT)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except (ValueError, TypeError):
            return None

    def _owner_for(self, entry):
        """Best-effort owner resolution for one CIM entry.

        Processes in the interactive session are current-user; anything
        else (session 0 services, other logins) reports None so callers
        never mistake it for the current user.
        """
        session = entry.get("session")
        if session is None:
            return None
        try:
            if int(session) == self._current_session_id():
                return self.current_user_id()
        except (TypeError, ValueError):
            return None
        return None

    def _current_session_id(self):
        if self._session_id is not None:
            return self._session_id
        payload = self._cim_query([os.getpid()])
        if not payload:
            return None
        try:
            self._session_id = int(payload["processes"][0].get("session"))
        except (KeyError, IndexError, TypeError, ValueError):
            self._session_id = -1
        return self._session_id if self._session_id >= 0 else None

    def _entry_to_snapshot(self, entry, total_kb):
        try:
            created = entry.get("created")
            etime = 0
            if created:
                parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone()
                etime = max(0, int(time.time() - parsed.timestamp()))
        except (ValueError, TypeError, OverflowError):
            etime = 0
        wss_bytes = entry.get("wss") or 0
        mem = 0.0
        if total_kb and total_kb > 0:
            mem = round((wss_bytes / (total_kb * 1024.0)) * 100.0, 2)
        return {
            "uid": self._owner_for(entry),
            "comm": entry.get("name") or entry.get("exe") or "?",
            "args": entry.get("args") or "",
            "cpu": 0.0,
            "mem": mem,
            "etime": etime,
        }

    def process_snapshot(self, pids=None, with_uid=True):
        payload = self._cim_query(pids)
        if not payload:
            return {}
        total_kb = payload.get("total_mb") or 0
        result = {}
        for entry in payload.get("processes") or []:
            try:
                pid = int(entry.get("pid"))
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            result[pid] = self._entry_to_snapshot(entry, total_kb)
        return result

    def origin_snapshot(self):
        payload = self._cim_query()
        if not payload:
            return {}
        table = {}
        for entry in payload.get("processes") or []:
            try:
                pid = int(entry.get("pid"))
                ppid = int(entry.get("ppid") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            table[pid] = (ppid, entry.get("args") or "")
        return table

    def group_members_map(self):
        return {}

    def process_cwds(self, pids):
        return {}

    def process_tree_of(self, pid):
        """Descendant PIDs via parent/child ancestry (BFS over one CIM pass)."""
        table = self.origin_snapshot()
        children = {}
        for child, (parent, _) in table.items():
            children.setdefault(parent, []).append(child)
        result = []
        stack = list(children.get(int(pid), []))
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen or current == pid:
                continue
            seen.add(current)
            result.append(current)
            stack.extend(children.get(current, []))
        return sorted(result)

    # ------------------------------------------------------------ listeners

    def listeners(self):
        output = run_cmd(["netstat", "-ano"], timeout=NETSTAT_TIMEOUT)
        return parse_netstat_listeners(output)

    # ------------------------------------------------------------ control

    def start_process(self, cwd, env, log_fd, command, marker):
        """Start via a temporary batch file carrying the run marker.

        A direct ``cmd /c "<set ...> && <command>"`` wrapper breaks when
        the user command itself contains quotes (subprocess re-quotes argv
        containing spaces, nesting quotes under ``/c``).  Writing the
        command into a batch file named ``console-run-<token>.cmd`` keeps
        the marker visible in CommandLine (identity check) while the
        command text executes verbatim.  The batch file lives in the OS
        temp dir; the server deletes it once the process exits.
        """
        import tempfile
        creationflags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        batch = os.path.join(
            tempfile.gettempdir(), "console-run-%s.cmd" % marker)
        try:
            with open(batch, "w", encoding="utf-8-sig") as handle:
                handle.write(
                    "@echo off\r\n"
                    "set %s=%s\r\n"
                    "%s\r\n"
                    "exit /b %%errorlevel%%\r\n" % (RUN_TOKEN_ENV, marker, command))
        except OSError:
            return None, None
        proc = subprocess.Popen(
            ["cmd.exe", "/d", "/s", "/c", batch],
            cwd=cwd, stdout=log_fd, stderr=subprocess.STDOUT,
            env=env, creationflags=creationflags, close_fds=True)
        return proc, None

    def signal_pid(self, pid, sig):
        force = (sig == 9)
        return self._taskkill(pid, force=force, tree=False, escalate=not force)

    def signal_group(self, group_id, sig):
        raise _unsupported("signal_group")

    def group_alive(self, group_id):
        raise _unsupported("group_alive")

    def pid_group(self, pid):
        return None

    def current_pgrp(self):
        raise _unsupported("current_pgrp")

    def kill_process(self, pid, force):
        return self._taskkill(pid, force=force, tree=False)

    def terminate_tree(self, pid, force):
        return self._taskkill(pid, force=force, tree=True, escalate=not force)

    def _taskkill(self, pid, force, tree, escalate=False):
        """End a process (tree).  ``escalate`` retries with /F when the
        graceful request fails — Windows console processes have no
        SIGTERM, so a hard terminate is their only stop mechanism.  The
        target has already passed managed-identity validation before this
        primitive is called."""
        base = ["taskkill", "/PID", str(int(pid))]
        if tree:
            base.append("/T")
        attempts = [base] if force else ([base] if not escalate else [base, base + ["/F"]])
        for index, args in enumerate(attempts):
            try:
                r = subprocess.run(args, capture_output=True, text=True,
                                   errors="replace", timeout=TASKKILL_TIMEOUT)
            except Exception as e:
                return False, "结束失败: %s" % e
            if r.returncode == 0:
                return True, None
            if _taskkill_not_found(r):
                return True, None
        return False, _taskkill_error(r) or "结束进程失败"

    # ------------------------------------------------------------ filesystem

    def acquire_lock(self, path):
        import msvcrt
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        lock_file = os.fdopen(fd, "r+", encoding="ascii")
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock_file.close()
            return None
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write("%d\n" % os.getpid())
        lock_file.flush()
        return lock_file

    def release_lock(self, lock_handle):
        if lock_handle is None:
            return
        import msvcrt
        try:
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_handle.close()

    # ------------------------------------------------------------ shell/env

    def launch_env(self, token, environ=None):
        env = dict(os.environ if environ is None else environ)
        env[RUN_TOKEN_ENV] = token
        return env

    # ------------------------------------------------------------ UX helpers

    def choose_path(self, what):
        """Native dialog through PowerShell WinForms (STA console)."""
        if what == "dir":
            dialog = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$f.Description = '选择工作目录';"
                "if ($f.ShowDialog() -eq 'OK') { $f.SelectedPath } else { '' }")
        else:
            dialog = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$f = New-Object System.Windows.Forms.OpenFileDialog;"
                "$f.Title = '选择批处理脚本';"
                "$f.Filter = '脚本文件 (*.py;*.sh;*.bat;*.cmd;*.ps1)|*.py;*.sh;*.bat;*.cmd;*.ps1|所有文件 (*.*)|*.*';"
                "if ($f.ShowDialog() -eq 'OK') { $f.FileName } else { '' }")
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-STA", "-Command", dialog],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180)
        except Exception:
            return None, False
        if r.returncode != 0:
            return None, True
        value = r.stdout.strip()
        return value or None, not bool(value)

    def show_dialog(self, title, message, buttons, default_index=0):
        return None

    def show_alert(self, title, message):
        pass


def _taskkill_error(result):
    for stream in (result.stderr, result.stdout):
        for line in (stream or "").splitlines():
            line = line.strip()
            if line:
                return line
    return None


def _taskkill_not_found(result):
    text = (result.stderr or "") + (result.stdout or "")
    lowered = text.lower()
    return "not found" in lowered or "没有找到进程" in lowered


def _unsupported(name):
    from adcc.platform.base import PlatformUnsupportedError
    return PlatformUnsupportedError("windows: %s（Windows 无 POSIX 进程组语义）" % name)
