"""Architecture placeholder: platforms without a concrete adapter.

Every capability fails with a typed :class:`PlatformUnsupportedError`
instead of an arbitrary import/attribute error, per SPEC section 7.
"""

from adcc.platform.base import PlatformAdapter, PlatformUnsupportedError


class UnsupportedPlatformAdapter(PlatformAdapter):

    name = "unsupported"

    def __init__(self, platform):
        self._platform = platform

    def _unsupported(self, name):
        return PlatformUnsupportedError(
            "%s 平台暂不支持: %s" % (self._platform, name))

    def current_user_id(self):
        raise self._unsupported("current_user_id")

    def process_user_id(self, pid):
        raise self._unsupported("process_user_id")

    def process_snapshot(self, pids=None, with_uid=True):
        raise self._unsupported("process_snapshot")

    def origin_snapshot(self):
        raise self._unsupported("origin_snapshot")

    def group_members_map(self):
        raise self._unsupported("group_members_map")

    def process_cwds(self, pids):
        raise self._unsupported("process_cwds")

    def process_tree_of(self, pid):
        raise self._unsupported("process_tree_of")

    def listeners(self):
        raise self._unsupported("listeners")

    def start_process(self, cwd, env, log_fd, command, marker):
        raise self._unsupported("start_process")

    def signal_pid(self, pid, sig):
        raise self._unsupported("signal_pid")

    def signal_group(self, group_id, sig):
        raise self._unsupported("signal_group")

    def group_alive(self, group_id):
        raise self._unsupported("group_alive")

    def pid_group(self, pid):
        raise self._unsupported("pid_group")

    def current_pgrp(self):
        raise self._unsupported("current_pgrp")

    def kill_process(self, pid, force):
        raise self._unsupported("kill_process")

    def terminate_tree(self, pid, force):
        raise self._unsupported("terminate_tree")

    def acquire_lock(self, path):
        raise self._unsupported("acquire_lock")

    def release_lock(self, lock_handle):
        raise self._unsupported("release_lock")

    def launch_env(self, token, environ=None):
        raise self._unsupported("launch_env")

    def choose_path(self, what):
        raise self._unsupported("choose_path")

    def show_dialog(self, title, message, buttons, default_index=0):
        raise self._unsupported("show_dialog")

    def show_alert(self, title, message):
        raise self._unsupported("show_alert")
