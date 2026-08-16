"""Linux platform adapter (P1).

Linux shares POSIX semantics with macOS (signals, process groups,
fcntl, flock) so this adapter reuses the macOS implementation for the
control plane and substitutes Linux-native fact collection (`ps`,
`lsof`, `/proc`-based cwd fallback).  It is exercised on Linux CI only;
Windows/macOS tests mark it skipUnless(linux).
"""

import os
import signal
import subprocess

from adcc.platform.base import PlatformAdapter, run_cmd
from adcc.platform.macos import MacOSPlatformAdapter
from adcc.runtime.ports import parse_lsof_listeners
from adcc.runtime.processes import (
    parse_lsof_cwds,
    parse_origin_snapshot,
    parse_pgid_members,
    parse_ps_snapshot,
)


class LinuxPlatformAdapter(MacOSPlatformAdapter):
    """Linux facts; macOS-identical control primitives via inheritance."""

    name = "linux"

    # macOS 的 osascript/PATH 注入不适用于 Linux：覆盖为 typed 降级。
    def choose_path(self, what):
        from adcc.platform.base import PlatformCapabilityError
        raise PlatformCapabilityError(
            "linux: choose_path（无 osascript；请使用 CLI/手动输入）")

    def show_dialog(self, title, message, buttons, default_index=0):
        return None

    def show_alert(self, title, message):
        pass

    def launch_env(self, token, environ=None):
        env = dict(os.environ if environ is None else environ)
        from adcc.core.constants import RUN_TOKEN_ENV
        env[RUN_TOKEN_ENV] = token
        return env

    def process_cwds(self, pids):
        """lsof 优先，缺失时回退 /proc/<pid>/cwd（仅 Linux）。"""
        result = super().process_cwds(pids)
        missing = [int(pid) for pid in pids if int(pid) not in result]
        for pid in missing:
            link = "/proc/%d/cwd" % pid
            try:
                target = os.path.realpath(link)
                if target and os.path.exists(target):
                    result[pid] = target
            except OSError:
                continue
        return result
