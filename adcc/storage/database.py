"""SQLite operational database (M4): runs history and event metadata.

Standard-library ``sqlite3`` only.  Log *content* stays in files; this
database indexes runs and stores run metadata (SPEC §13.2).  The single
connection is guarded by a lock so any thread may use it.
"""

import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    app_id TEXT,
    project_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    process_group_id INTEGER,
    run_token TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    exit_code INTEGER,
    log_path TEXT,
    origin TEXT,
    correlation_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_app ON runs(app_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    workflow_run_id TEXT,
    workflow_step_id TEXT,
    status TEXT NOT NULL,
    pid INTEGER,
    run_token TEXT,
    started_at INTEGER,
    ended_at INTEGER,
    exit_code INTEGER,
    log_path TEXT,
    prompt_ref TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON agent_sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON agent_sessions(started_at);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    locks_held TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wf_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_wf_runs_workflow ON workflow_runs(workflow_id);

CREATE TABLE IF NOT EXISTS workflow_step_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    retries INTEGER NOT NULL DEFAULT 0,
    run_ref TEXT,
    started_at INTEGER,
    ended_at INTEGER,
    error TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wf_step_runs_run ON workflow_step_runs(run_id);
"""

_WORKFLOW_RUN_FIELDS = (
    "id", "workflow_id", "workflow_version", "project_id", "name",
    "status", "started_at", "ended_at", "locks_held", "created_at",
)

_STEP_RUN_FIELDS = (
    "id", "run_id", "step_id", "kind", "status", "retries", "run_ref",
    "started_at", "ended_at", "error", "created_at",
)

_SESSION_FIELDS = (
    "id", "project_id", "adapter_id", "workflow_run_id", "workflow_step_id",
    "status", "pid", "run_token", "started_at", "ended_at", "exit_code",
    "log_path", "prompt_ref", "created_at",
)

_RUN_FIELDS = (
    "id", "app_id", "project_id", "kind", "status", "pid",
    "process_group_id", "run_token", "started_at", "ended_at",
    "exit_code", "log_path", "origin", "correlation_id", "created_at",
)


def _row_to_run(row):
    return dict(row) if row is not None else None


def _row_to_session(row):
    return dict(row) if row is not None else None


class RunDatabase:
    """Operational run history in one SQLite file."""

    def __init__(self, path):
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate()

    def _migrate(self):
        cursor = self._conn.cursor()
        cursor.executescript(_SCHEMA_SQL)
        try:
            version = cursor.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            version = 0
        if version < SCHEMA_VERSION:
            cursor.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, int(time.time())))
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ runs

    def insert_run(self, run):
        values = [run.get(field) for field in _RUN_FIELDS]
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (%s) VALUES (%s)"
                % (", ".join(_RUN_FIELDS),
                   ", ".join("?" for _ in _RUN_FIELDS)),
                values)
            self._conn.commit()

    def update_run(self, run_id, fields):
        if not fields:
            return
        allowed = {field for field in _RUN_FIELDS if field != "id"}
        assignments = []
        values = []
        for key, value in fields.items():
            if key in allowed:
                assignments.append("%s = ?" % key)
                values.append(value)
        if not assignments:
            return
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET %s WHERE id = ?" % ", ".join(assignments),
                values)
            self._conn.commit()

    def get_run(self, run_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM runs WHERE id = ?" % ", ".join(_RUN_FIELDS),
                (run_id,)).fetchone()
        return _row_to_run(row)

    def list_runs(self, limit=50, *, app_id=None, status=None, before=None):
        clauses = []
        values = []
        if app_id is not None:
            clauses.append("app_id = ?")
            values.append(app_id)
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if before is not None:
            clauses.append("started_at < ?")
            values.append(before)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 500))
        query = (
            "SELECT %s FROM runs %s ORDER BY started_at DESC LIMIT ?"
            % (", ".join(_RUN_FIELDS), where))
        values.append(limit)
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [_row_to_run(row) for row in rows]

    def running_runs(self):
        """Runs recorded as still running (daemon restart reconciliation)."""
        return self.list_runs(status="running", limit=500)

    def latest_run_for_app(self, app_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM runs WHERE app_id = ? "
                "ORDER BY started_at DESC LIMIT 1" % ", ".join(_RUN_FIELDS),
                (app_id,)).fetchone()
        return _row_to_run(row)

    # ------------------------------------------------------------ sessions

    def insert_session(self, session):
        values = [session.get(field) for field in _SESSION_FIELDS]
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_sessions (%s) VALUES (%s)"
                % (", ".join(_SESSION_FIELDS),
                   ", ".join("?" for _ in _SESSION_FIELDS)),
                values)
            self._conn.commit()

    def update_session(self, session_id, fields):
        if not fields:
            return
        allowed = {field for field in _SESSION_FIELDS if field != "id"}
        assignments = []
        values = []
        for key, value in fields.items():
            if key in allowed:
                assignments.append("%s = ?" % key)
                values.append(value)
        if not assignments:
            return
        values.append(session_id)
        with self._lock:
            self._conn.execute(
                "UPDATE agent_sessions SET %s WHERE id = ?"
                % ", ".join(assignments), values)
            self._conn.commit()

    def get_session(self, session_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM agent_sessions WHERE id = ?"
                % ", ".join(_SESSION_FIELDS), (session_id,)).fetchone()
        return _row_to_session(row)

    def list_sessions(self, limit=50, *, status=None, project_id=None):
        clauses = []
        values = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 500))
        query = (
            "SELECT %s FROM agent_sessions %s ORDER BY created_at DESC LIMIT ?"
            % (", ".join(_SESSION_FIELDS), where))
        values.append(limit)
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [_row_to_session(row) for row in rows]

    def running_sessions(self):
        return self.list_sessions(status="running", limit=500)

    # ------------------------------------------------------------ workflows

    def insert_workflow_run(self, run):
        values = [run.get(field) for field in _WORKFLOW_RUN_FIELDS]
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_runs (%s) VALUES (%s)"
                % (", ".join(_WORKFLOW_RUN_FIELDS),
                   ", ".join("?" for _ in _WORKFLOW_RUN_FIELDS)),
                values)
            self._conn.commit()

    def update_workflow_run(self, run_id, fields):
        allowed = {field for field in _WORKFLOW_RUN_FIELDS if field != "id"}
        assignments = []
        values = []
        for key, value in fields.items():
            if key in allowed:
                assignments.append("%s = ?" % key)
                values.append(value)
        if not assignments:
            return
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_runs SET %s WHERE id = ?"
                % ", ".join(assignments), values)
            self._conn.commit()

    def get_workflow_run(self, run_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM workflow_runs WHERE id = ?"
                % ", ".join(_WORKFLOW_RUN_FIELDS), (run_id,)).fetchone()
        return _row_to_run(row)

    def list_workflow_runs(self, limit=50, status=None):
        clauses = []
        values = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 500))
        query = ("SELECT %s FROM workflow_runs %s ORDER BY created_at DESC "
                 "LIMIT ?" % (", ".join(_WORKFLOW_RUN_FIELDS), where))
        values.append(limit)
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [_row_to_run(row) for row in rows]

    def get_running_workflow_runs(self):
        return self.list_workflow_runs(status="running", limit=100)

    def insert_step_run(self, step_run):
        values = [step_run.get(field) for field in _STEP_RUN_FIELDS]
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_step_runs (%s) VALUES (%s)"
                % (", ".join(_STEP_RUN_FIELDS),
                   ", ".join("?" for _ in _STEP_RUN_FIELDS)),
                values)
            self._conn.commit()

    def update_step_run(self, step_run_id, fields):
        allowed = {field for field in _STEP_RUN_FIELDS if field != "id"}
        assignments = []
        values = []
        for key, value in fields.items():
            if key in allowed:
                assignments.append("%s = ?" % key)
                values.append(value)
        if not assignments:
            return
        values.append(step_run_id)
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_step_runs SET %s WHERE id = ?"
                % ", ".join(assignments), values)
            self._conn.commit()

    def get_step_run(self, step_run_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT %s FROM workflow_step_runs WHERE id = ?"
                % ", ".join(_STEP_RUN_FIELDS), (step_run_id,)).fetchone()
        return _row_to_run(row)

    def list_step_runs(self, run_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT %s FROM workflow_step_runs WHERE run_id = ? "
                "ORDER BY created_at" % ", ".join(_STEP_RUN_FIELDS),
                (run_id,)).fetchall()
        return [_row_to_run(row) for row in rows]


def run_origin_label(app):
    """Origin string for a run record; pure mapping, no OS access."""
    origin = app.get("origin") if isinstance(app.get("origin"), dict) else None
    if origin:
        return origin.get("label") or None
    return None


__all__ = ["RunDatabase", "run_origin_label"]
