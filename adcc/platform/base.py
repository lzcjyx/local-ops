"""PlatformAdapter: the typed seam between OS-neutral Core and host platforms.

M2 boundary.  Adapters only collect OS facts and perform OS control
primitives; parsing/normalisation of collected text stays in
``adcc.runtime.*`` (pure functions) exactly as in M1.  `server.py` keeps
its existing function names as thin wrappers over the active adapter, so
HTTP handlers, tests that monkeypatch `server.*`, and payload shapes are
unchanged.
"""

import subprocess
import sys
import threading


class PlatformUnsupportedError(RuntimeError):
    """A capability does not exist on this platform adapter.

    Raised for capabilities that are architecture-absent (e.g. process
    groups on Windows), never for transient failures.
    """


class PlatformCapabilityError(RuntimeError):
    """A capability exists but is not usable in this environment.

    Raised e.g. when native directory selection is unavailable on
    Windows for the current context.  Callers must surface it as a typed
    error instead of fabricating data.
    """


class ProcessControlError(RuntimeError):
    """A process control primitive failed with an OS-level error."""


class PlatformAdapter:
    """Abstract adapter.  Subclasses implement platform specifics.

    Contract:
    - ``collect_*`` methods return raw tool text; parsers in
      ``adcc.runtime`` turn it into structures.
    - ``listeners()``/``process_snapshot()``/... return the *same*
      structures that `server.py` already passes to the runtime parsers,
      so HTTP payload shapes never change.
    - capability that cannot be implemented must raise
      :class:`PlatformUnsupportedError`, never silently return fake data.
    """

    name = "abstract"

    # ------------------------------------------------------------ identity

    def current_user_id(self):
        """Return an opaque current-user identifier.

        macOS returns the numeric uid; Windows returns the username.
        The value is only ever compared with ``process_user_id``.
        """
        raise PlatformUnsupportedError("%s: current_user_id" % self.name)

    def process_user_id(self, pid):
        """Return the owning user identifier for ``pid`` (same type as
        ``current_user_id``) or None when the process does not exist."""
        raise PlatformUnsupportedError("%s: process_user_id" % self.name)

    # ------------------------------------------------------------ processes

    def process_snapshot(self, pids=None, with_uid=True):
        """Return ``{pid: {"uid", "comm", "args", "cpu", "mem", "etime"}}``.

        ``pids=None`` means every process of the current user context
        available to the platform.  Fields that cannot be determined must
        use the neutral values defined in `adcc.runtime.processes` and be
        documented per adapter.
        """
        raise PlatformUnsupportedError("%s: process_snapshot" % self.name)

    def origin_snapshot(self):
        """Return ``{pid: (ppid, args)}`` for ancestry attribution."""
        raise PlatformUnsupportedError("%s: origin_snapshot" % self.name)

    def group_members_map(self):
        """Return ``{group_id: [pid, ...]}``.

        macOS returns process-group (pgid) membership; Windows has no
        process groups and must return {} with group semantics handled by
        ``process_tree_of``.
        """
        raise PlatformUnsupportedError("%s: group_members_map" % self.name)

    def process_cwds(self, pids):
        """Return ``{pid: cwd}``.  Missing entries mean unknown, not empty."""
        raise PlatformUnsupportedError("%s: process_cwds" % self.name)

    def pid_alive(self, pid):
        """Best-effort liveness probe; True on permission errors."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        import os
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def process_tree_of(self, pid):
        """Return live descendant PIDs of ``pid`` (excluding itself).

        Windows uses parent/child ancestry (CIM ParentProcessId).
        """
        raise PlatformUnsupportedError("%s: process_tree_of" % self.name)

    # ------------------------------------------------------------ listeners

    def listeners(self):
        """Return ``{(pid, port): {"bind_host": str|None}}``.

        Same structure the macOS lsof parser already produces; Windows
        parses `netstat -ano` into the identical shape.
        """
        raise PlatformUnsupportedError("%s: listeners" % self.name)

    # ------------------------------------------------------------ control

    def start_process(self, cwd, env, log_fd, command, marker):
        """Start ``command`` detached with the run marker.

        Returns ``(proc, group_id)``; ``group_id`` is a POSIX pgid on
        macOS and None on Windows (identity uses PID + creation time).
        ``marker`` is an argv-embedded opaque run token.
        """
        raise PlatformUnsupportedError("%s: start_process" % self.name)

    def signal_pid(self, pid, sig):
        """Send ``sig`` to one PID.  ``(ok, error)``; missing PID is
        idempotent success."""
        raise PlatformUnsupportedError("%s: signal_pid" % self.name)

    def signal_group(self, group_id, sig):
        """Send ``sig`` to a POSIX process group.  ``(ok, error)``."""
        raise PlatformUnsupportedError("%s: signal_group" % self.name)

    def group_alive(self, group_id):
        """Liveness probe for a process group."""
        raise PlatformUnsupportedError("%s: group_alive" % self.name)

    def pid_group(self, pid):
        """Return the process group of ``pid`` or None."""
        raise PlatformUnsupportedError("%s: pid_group" % self.name)

    def current_pgrp(self):
        """Return the caller's own process group."""
        raise PlatformUnsupportedError("%s: current_pgrp" % self.name)

    def kill_process(self, pid, force):
        """End one process; caller is responsible for ownership checks.

        Returns ``(ok, error)``.
        """
        raise PlatformUnsupportedError("%s: kill_process" % self.name)

    def terminate_tree(self, pid, force):
        """End ``pid`` and its descendants (best effort).

        Returns ``(ok, error)``.  macOS targets the process group of
        ``pid``; Windows walks the parent/child tree via
        ``process_tree_of`` and terminates each member.
        """
        raise PlatformUnsupportedError("%s: terminate_tree" % self.name)

    # ------------------------------------------------------------ filesystem

    def ensure_private_dir(self, path):
        """Create ``path`` with user-only permissions when the platform
        supports them (no-op on Windows)."""
        import os
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def open_private(self, path, flags, mode=0o600):
        """``os.open`` with user-only permissions; Windows ignores mode."""
        import os
        fd = os.open(path, flags, mode)
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
        return fd

    def chmod_private(self, path, mode=0o600):
        """Best-effort user-only permissions (no-op on Windows)."""
        import os
        try:
            os.chmod(path, mode)
        except (AttributeError, OSError):
            pass

    def acquire_lock(self, path):
        """Take an exclusive, crash-released lock on ``path``.

        Returns a lock handle or None when already locked elsewhere.
        """
        raise PlatformUnsupportedError("%s: acquire_lock" % self.name)

    def release_lock(self, lock_handle):
        """Release a handle returned by ``acquire_lock``."""
        raise PlatformUnsupportedError("%s: release_lock" % self.name)

    # ------------------------------------------------------------ shell/env

    def launch_env(self, token, environ=None):
        """Environment for child processes: run marker plus platform
        PATH augmentation (macOS adds Homebrew/nvm/etc. dirs; Windows
        keeps the inherited PATH)."""
        raise PlatformUnsupportedError("%s: launch_env" % self.name)

    # ------------------------------------------------------------ UX helpers

    def choose_path(self, what):
        """Native chooser: ``what`` in ("dir", "script").

        Returns ``(path|None, canceled)``.
        """
        raise PlatformUnsupportedError("%s: choose_path" % self.name)

    def open_url(self, url):
        """Open ``url`` in the default browser."""
        import webbrowser
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False

    def show_dialog(self, title, message, buttons):
        """Native message dialog; returns chosen button label or None."""
        raise PlatformUnsupportedError("%s: show_dialog" % self.name)

    def show_alert(self, title, message):
        """Native critical alert; best effort."""
        raise PlatformUnsupportedError("%s: show_alert" % self.name)


def _detect_platform():
    if sys.platform == "darwin":
        from .macos import MacOSPlatformAdapter
        return MacOSPlatformAdapter()
    if sys.platform.startswith("win"):
        from .windows import WindowsPlatformAdapter
        return WindowsPlatformAdapter()
    if sys.platform.startswith("linux"):
        from .linux import LinuxPlatformAdapter
        return LinuxPlatformAdapter()
    from .unsupported import UnsupportedPlatformAdapter
    return UnsupportedPlatformAdapter(sys.platform)


_adapter_lock = threading.Lock()
_active_adapter = None


def get_platform_adapter():
    """Return the process-wide platform adapter (created once)."""
    global _active_adapter
    if _active_adapter is None:
        with _adapter_lock:
            if _active_adapter is None:
                _active_adapter = _detect_platform()
    return _active_adapter


def run_cmd(args, timeout=5.0):
    """Run a command and return stdout; never raises.

    Shared helper used by adapters and wrappers.  Timeout and all
    failures yield "" so callers keep a degraded-but-live server.
    """
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""
