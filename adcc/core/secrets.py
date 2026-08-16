"""Per-project secrets via OS credential stores (P1).

Windows: Credential Manager (advapi32 CredReadW/CredWriteW via ctypes).
macOS: `security` CLI (Keychain generic passwords).
Linux: typed unsupported (no OS store wired).

Secrets are referenced in environment values as ``${secret:<name>}`` and
resolved at launch time; the literal value is never persisted in config.
"""

import subprocess
import sys


class SecretUnavailable(RuntimeError):
    pass


def secret_get(name):
    """Return the secret value or raise SecretUnavailable."""
    if not isinstance(name, str) or not name.strip():
        raise SecretUnavailable("secret 名不能为空")
    name = name.strip()
    if sys.platform.startswith("win"):
        return _windows_credential_get(name)
    if sys.platform == "darwin":
        return _macos_keychain_get(name)
    raise SecretUnavailable("当前平台没有可用的凭据库")


def secret_set(name, value):
    """Store (overwrite) a secret; returns True on success."""
    if sys.platform.startswith("win"):
        return _windows_credential_set(name, value)
    if sys.platform == "darwin":
        return _macos_keychain_set(name, value)
    raise SecretUnavailable("当前平台没有可用的凭据库")


def resolve_environment(environment):
    """Resolve ``${secret:name}`` placeholders in an env mapping.

    Returns (resolved_env, unresolved_names).  Unresolvable references
    are left verbatim so callers can surface a clear error.
    """
    import re
    pattern = re.compile(r"\$\{secret:([^}]+)\}")
    resolved = {}
    unresolved = []
    for key, value in (environment or {}).items():
        if isinstance(value, str):
            match = pattern.search(value)
            if match:
                try:
                    secret = secret_get(match.group(1))
                    value = value[:match.start()] + secret + value[match.end():]
                except SecretUnavailable:
                    unresolved.append(match.group(1))
        resolved[key] = value
    return resolved, unresolved


def _windows_credential_get(name):
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    credential = ctypes.POINTER(CREDENTIAL)()
    ok = ctypes.windll.advapi32.CredReadW(
        "adcc:" + name, 1, 0, ctypes.byref(credential))  # CRED_TYPE_GENERIC
    if not ok:
        raise SecretUnavailable("凭据不存在: %s" % name)
    try:
        size = credential.contents.CredentialBlobSize
        blob = ctypes.string_at(credential.contents.CredentialBlob, size)
        return blob.decode("utf-8", "replace")
    finally:
        ctypes.windll.advapi32.CredFree(credential)


def _windows_credential_set(name, value):
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    payload = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(payload)
    credential = CREDENTIAL()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = "adcc:" + name
    credential.CredentialBlobSize = len(payload)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    ok = ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential), 0)
    return bool(ok)


def _macos_keychain_get(name):
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "adcc:" + name, "-w"],
        capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise SecretUnavailable("凭据不存在: %s" % name)
    return result.stdout.rstrip("\n")


def _macos_keychain_set(name, value):
    result = subprocess.run(
        ["security", "add-generic-password", "-s", "adcc:" + name,
         "-w", value, "-U"],
        capture_output=True, text=True, timeout=10)
    return result.returncode == 0
