"""Pure managed-process identity policy.

The functions in this module consume already collected runtime facts.  They
must not inspect the OS, persist configuration, start/stop processes, or know
about HTTP.  That lets the current compatibility server keep its macOS probes
while later platform adapters provide the same facts on other platforms.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from adcc.core.constants import RUN_TOKEN_ARG_PREFIX


App = Mapping[str, Any]
ProcessSnapshot = Mapping[int, Mapping[str, Any]]
GroupMembers = Mapping[int, Iterable[int]]
Listeners = Iterable[tuple[int, int]]
CwdEqual = Callable[[str, str], bool]


def _same_cwd(actual_cwd: str, expected_cwd: str) -> bool:
    """Default comparison for facts that callers already canonicalized."""
    return actual_cwd == expected_cwd


def managed_candidate_pids(app: App, groups: GroupMembers) -> set[int]:
    """Return the recorded process-group members before identity validation."""
    token = app.get("runToken")
    pgid = app.get("lastPgid") or app.get("lastPid")
    if (not isinstance(token, str) or not token
            or not isinstance(pgid, int) or pgid <= 0):
        return set()
    return set(groups.get(pgid, ()))


def managed_process_index(
        apps: Iterable[App],
        groups: GroupMembers,
        process_snapshot: ProcessSnapshot,
        *,
        current_uid: int,
        run_token_arg_prefix: str = RUN_TOKEN_ARG_PREFIX,
) -> dict[Any, list[int]]:
    """Resolve token-managed processes from collected PGID/ps facts.

    A group is managed only when a current-user member carries the matching
    random argv marker.  Once that controller is verified, every current-user
    member in that recorded group is a managed descendant.  The return shape
    intentionally mirrors the legacy ``app id -> [pid, ...]`` result, without
    exposing collection mechanics.
    """
    app_list = list(apps)
    candidates = {
        app.get("id"): managed_candidate_pids(app, groups)
        for app in app_list
    }
    result: dict[Any, list[int]] = {}
    for app in app_list:
        app_id = app.get("id")
        token = app.get("runToken")
        marker = run_token_arg_prefix + token if token else None
        current_user = sorted(
            pid for pid in candidates.get(app_id, set())
            if process_snapshot.get(pid, {}).get("uid") == current_uid)
        controller_found = bool(marker and any(
            marker in process_snapshot.get(pid, {}).get("args", "")
            for pid in current_user))
        result[app_id] = current_user if controller_found else []
    return result


def managed_pids(
        app: App,
        groups: GroupMembers,
        process_snapshot: ProcessSnapshot,
        *,
        current_uid: int,
        run_token_arg_prefix: str = RUN_TOKEN_ARG_PREFIX,
) -> list[int]:
    """Convenience form of :func:`managed_process_index` for one app."""
    return managed_process_index(
        [app], groups, process_snapshot, current_uid=current_uid,
        run_token_arg_prefix=run_token_arg_prefix,
    ).get(app.get("id"), [])


def managed_process_index_windows(
        apps: Iterable[App],
        process_snapshot: ProcessSnapshot,
        origin_table: Mapping[int, tuple[int, str]],
        *,
        current_user: Any,
        run_token_marker: str,
) -> dict[Any, list[int]]:
    """Windows managed identity from collected PID/ancestry facts.

    Windows has no POSIX process groups and no portable way to read
    another process's environment.  The adapter starts every managed app
    through a ``cmd.exe /c`` batch file named ``console-run-<token>.cmd``,
    so the marker prefix (``run_token_marker``, typically
    ``RUN_TOKEN_ARG_PREFIX``) appears in the batch's CommandLine.  Identity
    is therefore: recorded ``lastPid`` alive, owned by the current user,
    and carrying the matching marker in its command line.  Once the
    controller is verified, its descendant tree (built from
    ``origin_table``) is treated as managed, mirroring the macOS
    group-member rule.
    """
    app_list = list(apps)
    descendants: dict[Any, list[int]] = {
        app.get("id"): [] for app in app_list}
    for app in app_list:
        token = app.get("runToken")
        controller = app.get("lastPid")
        if (not isinstance(token, str) or not token
                or not isinstance(controller, int) or controller <= 0):
            continue
        entry = process_snapshot.get(controller)
        if not entry:
            continue
        if entry.get("uid") != current_user:
            continue
        marker = run_token_marker + token
        if marker not in entry.get("args", ""):
            continue
        children: dict[int, list[int]] = {}
        for child, (parent, _) in origin_table.items():
            children.setdefault(parent, []).append(child)
        members = [controller]
        stack = list(children.get(controller, []))
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen or current == controller:
                continue
            seen.add(current)
            members.append(current)
            stack.extend(children.get(current, []))
        descendants[app.get("id")] = members
    return descendants


def legacy_identity_applicable(app: App) -> bool:
    """Whether an app can use the legacy/attached identity seam."""
    port = app.get("port")
    expected_cwd = app.get("cwd")
    return (
        not app.get("runToken")
        and isinstance(port, int)
        and port > 0
        and isinstance(expected_cwd, str)
        and bool(expected_cwd)
    )


def legacy_candidate_pids(app: App, listeners: Listeners) -> set[int]:
    """Return listener PIDs whose facts are needed for legacy validation."""
    if not legacy_identity_applicable(app):
        return set()
    recorded_pid = app.get("lastPid")
    port_pids = {
        pid for pid, listening_port in listeners
        if listening_port == app.get("port")
    }
    if not app.get("attached"):
        if not isinstance(recorded_pid, int) or recorded_pid <= 0:
            return set()
        port_pids.intersection_update({recorded_pid})
    return port_pids


def legacy_managed_pid(
        app: App,
        listeners: Listeners,
        process_snapshot: ProcessSnapshot,
        cwd_by_pid: Mapping[int, str],
        *,
        current_uid: int,
        cwd_equal: CwdEqual = _same_cwd,
) -> int | None:
    """Resolve a legacy or explicitly attached listener from collected facts.

    Normal legacy data accepts only the saved PID.  An explicitly attached
    card may follow a replacement listener, but only if exactly one current-
    user listener on the configured port has the expected real cwd.  Callers
    that need symlink-aware comparison pass a canonicalizing ``cwd_equal``;
    this module itself performs no filesystem access.
    """
    if not legacy_identity_applicable(app):
        return None
    recorded_pid = app.get("lastPid")
    expected_cwd = app.get("cwd")
    port_pids = legacy_candidate_pids(app, listeners)
    if not port_pids:
        return None

    matches = []
    for pid in sorted(port_pids):
        if process_snapshot.get(pid, {}).get("uid") != current_uid:
            continue
        actual_cwd = cwd_by_pid.get(pid)
        if not actual_cwd:
            continue
        try:
            same_cwd = bool(cwd_equal(actual_cwd, expected_cwd))
        except OSError:
            same_cwd = False
        if same_cwd:
            matches.append(pid)
    if recorded_pid in matches:
        return recorded_pid
    return matches[0] if app.get("attached") and len(matches) == 1 else None


def listener_app_owners(
        apps: Iterable[App],
        listeners: Listeners,
        process_snapshot: ProcessSnapshot,
        cwd_by_pid: Mapping[int, str],
        groups: GroupMembers | None = None,
        *,
        current_uid: int,
        cwd_equal: CwdEqual = _same_cwd,
        run_token_arg_prefix: str = RUN_TOKEN_ARG_PREFIX,
        managed_by_app: Mapping[Any, Iterable[int]] | None = None,
) -> dict[int, App]:
    """Map a listener PID to one verified owner, never to an ambiguous card.

    Port occupancy is not ownership.  A PID is returned only when exactly one
    application satisfies token/PGID/UID identity or strict legacy/attached
    identity.
    """
    app_list = list(apps)
    # State projection often has only listener process details.  Its caller
    # may already have resolved group-wide token identity from a fuller ps
    # snapshot; redoing it here with listener-only facts would lose a valid
    # owner.  Supplied results are therefore authoritative for this call.
    managed = managed_by_app
    if managed is None:
        managed = managed_process_index(
            app_list, groups or {}, process_snapshot, current_uid=current_uid,
            run_token_arg_prefix=run_token_arg_prefix,
        )
    candidates: dict[int, list[App]] = {}
    for app in app_list:
        live = managed.get(app.get("id"), [])
        if not live:
            legacy_pid = legacy_managed_pid(
                app, listeners, process_snapshot, cwd_by_pid,
                current_uid=current_uid, cwd_equal=cwd_equal,
            )
            live = [legacy_pid] if legacy_pid else []
        for pid in live:
            candidates.setdefault(pid, []).append(app)
    return {
        pid: owners[0]
        for pid, owners in candidates.items()
        if len(owners) == 1
    }
