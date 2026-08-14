"""macOS platform adapter.

Migrates the legacy `server.py` OS interactions without changing their
behaviour: BSD `ps`, `lsof`, `osascript`, flock-based instance locking,
process-group signal semantics and the Finder-friendly PATH augmentation.
Parsing of collected text is delegated to `adcc.runtime.*` pure parsers.
"""

import errno
import glob
import os
import signal
import subprocess

from adcc.core.constants import RUN_TOKEN_ARG_PREFIX, RUN_TOKEN_ENV
from adcc.platform.base import (
    PlatformAdapter,
    ProcessControlError,
    run_cmd,
)
from adcc.runtime.ports import parse_lsof_listeners
from adcc.runtime.processes import (
    parse_lsof_cwds,
    parse_origin_snapshot,
    parse_pgid_members,
    parse_ps_snapshot,
)

PS_TIMEOUT = 5.0
LSOF_TIMEOUT = 5.0


class MacOSPlatformAdapter(PlatformAdapter):

    name = "macos"

    # ------------------------------------------------------------ identity

    def current_user_id(self):
        return os.getuid()

    def process_user_id(self, pid):
        out = run_cmd(["ps", "-o", "uid=", "-p", str(int(pid))],
                      timeout=PS_TIMEOUT)
        tokens = out.split()
        if not tokens:
            return None
        try:
            return int(tokens[0])
        except ValueError:
            return None

    # ------------------------------------------------------------ processes

    def process_snapshot(self, pids=None, with_uid=True):
        base = ["ps"]
        if pids is None:
            base.append("-ax")
        else:
            pids = [int(p) for p in pids]
            if not pids:
                return {}
            base += ["-p", ",".join(str(p) for p in pids)]
        # comm 必须放在最后一列：macOS ps 只保证最后一列不被定宽截断。
        fields = (["pid"] + (["uid"] if with_uid else []) +
                  ["etime", "%cpu", "%mem", "comm"])
        out1 = run_cmd(base + ["-o", ",".join(fields)], timeout=PS_TIMEOUT)
        out2 = run_cmd(base + ["-o", "pid,args"], timeout=PS_TIMEOUT)
        return parse_ps_snapshot(out1, out2, with_uid=with_uid)

    def origin_snapshot(self):
        output = run_cmd(["ps", "-axo", "pid=,ppid=,args"], timeout=PS_TIMEOUT)
        return parse_origin_snapshot(output)

    def group_members_map(self):
        output = run_cmd(["ps", "-axo", "pid=,pgid="], timeout=PS_TIMEOUT)
        return parse_pgid_members(output)

    def process_cwds(self, pids):
        pids = [int(p) for p in pids]
        if not pids:
            return {}
        out = run_cmd(
            ["lsof", "-a", "-p", ",".join(str(p) for p in pids),
             "-d", "cwd", "-Fn"], timeout=LSOF_TIMEOUT)
        return parse_lsof_cwds(out)

    def process_tree_of(self, pid):
        """Descendants are reached via the POSIX process group."""
        pgid = self.pid_group(pid)
        if not pgid:
            return []
        return self.group_members_map().get(pgid, [])

    # ------------------------------------------------------------ listeners

    def listeners(self):
        output = run_cmd(["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
                         timeout=LSOF_TIMEOUT)
        return parse_lsof_listeners(output)

    # ------------------------------------------------------------ control

    def start_process(self, cwd, env, log_fd, command, marker):
        outer_script = '/bin/bash -c "$1"\nconsole_status=$?\nexit "$console_status"'
        inner_script = (command + '\nconsole_status=$?\nwait\nexit "$console_status"')
        proc = subprocess.Popen(
            ["/bin/bash", "-c", outer_script,
             RUN_TOKEN_ARG_PREFIX + marker, inner_script],
            cwd=cwd, stdout=log_fd, stderr=subprocess.STDOUT,
            start_new_session=True, env=env)
        return proc, proc.pid

    def signal_pid(self, pid, sig):
        try:
            os.kill(int(pid), sig)
            return True, None
        except ProcessLookupError:
            return True, None
        except PermissionError:
            return False, "没有权限停止受控进程"
        except OSError as e:
            return False, "停止受控进程失败: %s" % e

    def signal_group(self, group_id, sig):
        try:
            os.killpg(int(group_id), sig)
            return True, None
        except ProcessLookupError:
            return True, None
        except PermissionError:
            return False, "没有权限停止受控进程组"
        except OSError as e:
            return False, "停止受控进程组失败: %s" % e

    def group_alive(self, group_id):
        try:
            os.killpg(int(group_id), 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True

    def pid_group(self, pid):
        try:
            return os.getpgid(int(pid))
        except (ProcessLookupError, PermissionError, OSError):
            return None

    def current_pgrp(self):
        return os.getpgrp()

    def kill_process(self, pid, force):
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            return False, "进程不存在"
        except PermissionError:
            return False, "没有权限结束该进程"
        except OSError as e:
            return False, "结束失败: %s" % e
        return True, None

    def terminate_tree(self, pid, force):
        sig = signal.SIGKILL if force else signal.SIGTERM
        pgid = self.pid_group(pid)
        if pgid is None:
            return self.signal_pid(pid, sig)
        return self.signal_group(pgid, sig)

    # ------------------------------------------------------------ filesystem

    def acquire_lock(self, path):
        import fcntl
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        lock_file = os.fdopen(fd, "r+", encoding="ascii")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            lock_file.close()
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return None
            raise
        try:
            os.fchmod(lock_file.fileno(), 0o600)
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write("%d\n" % os.getpid())
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except OSError:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise
        return lock_file

    def release_lock(self, lock_handle):
        if lock_handle is None:
            return
        import fcntl
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()

    # ------------------------------------------------------------ shell/env

    def launch_env(self, token, environ=None):
        env = dict(os.environ if environ is None else environ)
        home = os.path.expanduser("~")
        preferred = [
            os.path.join(home, ".local", "bin"),
            os.path.join(home, ".volta", "bin"),
            os.path.join(home, ".bun", "bin"),
            os.path.join(home, "Library", "pnpm"),
            os.path.join(home, ".asdf", "shims"),
            "/opt/homebrew/bin", "/opt/homebrew/sbin",
            "/usr/local/bin", "/usr/local/sbin",
        ]
        preferred.extend(sorted(
            glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")),
            reverse=True))
        preferred.extend(sorted(
            glob.glob(os.path.join(home, ".fnm", "node-versions", "*",
                                   "installation", "bin")),
            reverse=True))
        preferred.extend((env.get("PATH") or "").split(os.pathsep))
        preferred.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
        seen = set()
        env["PATH"] = os.pathsep.join(
            path for path in preferred if path and not (path in seen or seen.add(path)))
        env.setdefault("PNPM_HOME", os.path.join(home, "Library", "pnpm"))
        env[RUN_TOKEN_ENV] = token
        return env

    # ------------------------------------------------------------ UX helpers

    def choose_path(self, what):
        if what == "dir":
            script = 'POSIX path of (choose folder with prompt "选择工作目录")'
        else:
            script = 'POSIX path of (choose file with prompt "选择批处理脚本")'
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=180)
        except Exception:
            return None, False
        if r.returncode != 0:  # 用户按了取消（"User canceled."）
            return None, True
        return r.stdout.strip().rstrip("/") or None, False

    def show_dialog(self, title, message, buttons, default_index=0):
        if not buttons:
            return None
        quoted = ["%s" % _osascript_quote(b) for b in buttons]
        default = quoted[default_index] if 0 <= default_index < len(quoted) else quoted[0]
        buttons_decl = ", ".join(quoted)
        script = (
            'on run argv\n'
            'set messageText to item 1 of argv\n'
            'display dialog messageText with title "总控台" '
            'buttons {%s} default button %s with icon note\n'
            'return button returned of result\n'
            'end run' % (buttons_decl, default))
        try:
            r = subprocess.run(["osascript", "-e", script, message],
                               capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def show_alert(self, title, message):
        script = ('on run argv\n'
                  'display alert "总控台" message (item 1 of argv) as critical\n'
                  'end run')
        try:
            subprocess.run(["osascript", "-e", script, message],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _osascript_quote(value):
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')
