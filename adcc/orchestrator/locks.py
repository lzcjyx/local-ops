"""Lock manager (M8): project/resource/worktree/port/custom locks.

A step declares the locks it needs; the scheduler only launches steps
whose locks are all free.  Held locks are persisted (JSON column on the
workflow run) so a daemon restart can re-acquire the same semantics
without inventing new permission.
"""

import json
import threading
import time


def lock_key(kind, value):
    """Canonical lock identifier, e.g. ``project:write``, ``port:3000``."""
    return "%s:%s" % (kind, value)


def lock_key_of(spec):
    """Turn ``project:write`` / ``port:3000`` / plain names into keys."""
    spec = str(spec)
    if ":" in spec:
        kind, _, value = spec.partition(":")
        return "%s:%s" % (kind.strip(), value.strip())
    return spec.strip()


class LockManager:
    def __init__(self):
        self._held = {}          # key -> {workflow_run_id, step_id, at}
        self._guard = threading.RLock()

    # ------------------------------------------------------------ state

    def snapshot(self):
        with self._guard:
            return {
                key: dict(entry) for key, entry in self._held.items()
            }

    def restore(self, held):
        """Re-acquire persisted locks after daemon restart."""
        with self._guard:
            for key, entry in (held or {}).items():
                self._held[key] = {
                    "workflow_run_id": entry.get("workflow_run_id"),
                    "step_id": entry.get("step_id"),
                    "at": entry.get("at") or int(time.time()),
                }

    def held_keys(self, workflow_run_id=None):
        with self._guard:
            if workflow_run_id is None:
                return set(self._held)
            return {key for key, entry in self._held.items()
                    if entry.get("workflow_run_id") == workflow_run_id}

    # ------------------------------------------------------------ acquire

    def try_acquire(self, keys, workflow_run_id, step_id):
        """Acquire all ``keys`` atomically; returns True or False."""
        with self._guard:
            conflict = next(
                (key for key in keys if key in self._held), None)
            if conflict is not None:
                return False
            for key in keys:
                self._held[key] = {
                    "workflow_run_id": workflow_run_id,
                    "step_id": step_id,
                    "at": int(time.time()),
                }
            return True

    def release(self, keys, workflow_run_id=None):
        with self._guard:
            for key in keys:
                entry = self._held.get(key)
                if entry is None:
                    continue
                if workflow_run_id is not None and (
                        entry.get("workflow_run_id") != workflow_run_id):
                    continue
                self._held.pop(key, None)

    def release_run(self, workflow_run_id):
        with self._guard:
            for key in list(self._held):
                if self._held[key].get("workflow_run_id") == workflow_run_id:
                    self._held.pop(key, None)

    def serialize(self):
        return json.dumps(self.snapshot(), ensure_ascii=False)
