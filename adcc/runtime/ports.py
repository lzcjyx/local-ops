"""Pure port parsing and normalization helpers.

These functions preserve the legacy ``server.py`` data shapes so callers can
continue to use listener snapshots as either a mapping or an old-style set of
``(pid, port)`` pairs.
"""

import re


def validate_port(value):
    """→ (port|None, error|None)。接受 null / 整数 / 数字字符串，范围 1-65535。"""
    if value is None or value == "":
        return None, None
    if isinstance(value, bool):
        return None, "port 必须是 1-65535 的整数"
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        return None, "port 必须是 1-65535 的整数"
    if not (1 <= port <= 65535):
        return None, "port 必须在 1-65535 之间"
    return port, None


def parse_lsof_listeners(output):
    """Parse ``lsof`` listener text into the legacy listener snapshot.

    The result is ``{(pid, port): {bind_host, ...}}``.  Keeping this exact
    mapping shape preserves both old membership iteration and IPv6-aware open
    link selection without making this module responsible for running lsof.
    """
    found = {}
    for line in output.splitlines():
        if not line or line.startswith("COMMAND"):
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        # NAME looks like *:8791 / 127.0.0.1:8080 / [::1]:8765 and may be
        # followed by "(LISTEN)".
        port = None
        bind_host = None
        for tok in reversed(parts):
            match = re.search(r":(\d+)$", tok)
            if match:
                port = int(match.group(1))
                bind_host = tok[:match.start()]
                if bind_host.startswith("[") and bind_host.endswith("]"):
                    bind_host = bind_host[1:-1]
                break
        if port is None:
            continue
        found.setdefault((pid, port), set()).add(bind_host or "")
    return found


def listener_open_host(listeners, port, pids=None):
    """Return the loopback host appropriate for a listener snapshot.

    An old-style set snapshot intentionally retains the legacy IPv4 fallback.
    """
    if not isinstance(listeners, dict):
        return "127.0.0.1"
    allowed_pids = set(pids) if pids is not None else None
    hosts = set()
    for (pid, listening_port), values in listeners.items():
        if listening_port != port or (
                allowed_pids is not None and pid not in allowed_pids):
            continue
        if isinstance(values, str):
            hosts.add(values)
        elif isinstance(values, (set, list, tuple)):
            hosts.update(value for value in values if isinstance(value, str))
    normalized = {host.strip("[]").casefold() for host in hosts if host}
    ipv4_capable = any(
        host in ("*", "0.0.0.0") or host.startswith("127.")
        for host in normalized)
    ipv6_loopback_only = bool(normalized) and not ipv4_capable and all(
        host in ("::", "::1", "localhost") for host in normalized)
    return "localhost" if ipv6_loopback_only else "127.0.0.1"
