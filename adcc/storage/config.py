"""Versioned JSON configuration persistence for the extracted ADCC Core.

This Module intentionally has no dependency on ``server.py`` or HTTP classes.
It preserves the legacy console's schema migration, backup recovery, atomic
write, and read-only-protection semantics while allowing host wiring to inject
cache invalidation and private-directory policy.
"""

import json
import logging
import os
import threading

from adcc.core.constants import (
    APP_DEFAULT,
    CONFIG_DEFAULT,
    CURRENT_SCHEMA_VERSION,
    DEFAULT_UI_THEME,
)
from adcc.core.errors import ConfigSchemaError, FutureConfigSchemaError


def migrate_config_v0_to_v1(raw):
    """Old configuration has no schemaVersion; v1 establishes it explicitly."""
    migrated = dict(raw)
    migrated["schemaVersion"] = 1
    return migrated


CONFIG_MIGRATIONS = {0: migrate_config_v0_to_v1}


def migrate_config(raw):
    """Migrate a supported configuration through each schema version in order."""
    if not isinstance(raw, dict):
        raise ConfigSchemaError("配置根节点必须是 JSON 对象")
    version = raw.get("schemaVersion", 0)
    if type(version) is not int or version < 0:
        raise ConfigSchemaError("schemaVersion 必须是非负整数")
    if version > CURRENT_SCHEMA_VERSION:
        raise FutureConfigSchemaError(
            "配置 schemaVersion=%d 新于当前程序支持的 %d" %
            (version, CURRENT_SCHEMA_VERSION))
    source_version = version
    migrated = json.loads(json.dumps(raw, ensure_ascii=False))
    while version < CURRENT_SCHEMA_VERSION:
        migration = CONFIG_MIGRATIONS.get(version)
        if migration is None:
            raise ConfigSchemaError("缺少 schemaVersion=%d 的迁移器" % version)
        migrated = migration(migrated)
        next_version = migrated.get("schemaVersion")
        if next_version != version + 1:
            raise ConfigSchemaError("配置迁移器未正确递增 schemaVersion")
        version = next_version
    return migrated, source_version


def _default_ensure_private_dir(path, logger):
    """Legacy private-directory policy used when a host does not inject one."""
    if os.path.islink(path):
        raise OSError("私有运行目录不能是符号链接: %s" % path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path) or not os.path.isdir(path):
        raise OSError("私有运行路径不是安全目录: %s" % path)
    try:
        os.chmod(path, 0o700)
    except OSError:
        logger.warning("无法收紧目录权限: %s", path)


class Config:
    """Configuration load/save with schema migration, backup, and atomic writes.

    ``on_change`` is invoked only after an update has durably written both the
    last-good backup and primary configuration.  ``ensure_private_dir`` takes a
    single path argument and lets a host preserve its existing directory policy.
    """

    DEFAULT = CONFIG_DEFAULT
    APP_DEFAULT = APP_DEFAULT

    def __init__(self, path, *, on_change=None, logger=None,
                 ensure_private_dir=None):
        self._lock = threading.RLock()
        self._path = path
        self._on_change = on_change if on_change is not None else (lambda: None)
        self._logger = logger if logger is not None else logging.getLogger("console")
        if ensure_private_dir is None:
            self._ensure_private_dir = lambda directory: _default_ensure_private_dir(
                directory, self._logger)
        else:
            self._ensure_private_dir = ensure_private_dir
        self._writable = True
        self._recovered_from_backup = False
        self._migration_from = None
        self._health_issues = []
        self._data = self._load()

    @staticmethod
    def _payload(data):
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    @classmethod
    def _normalize(cls, raw):
        data = {"schemaVersion": CURRENT_SCHEMA_VERSION}
        for key, default in cls.DEFAULT.items():
            if key == "schemaVersion":
                continue
            value = raw.get(key)
            if isinstance(value, type(default)):
                data[key] = (json.loads(json.dumps(value, ensure_ascii=False))
                             if isinstance(value, (list, dict)) else value)
            else:
                data[key] = list(default) if isinstance(default, list) else default
        apps = []
        for item in data["apps"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            app = dict(cls.APP_DEFAULT)
            for key in app:
                if key in item:
                    app[key] = item[key]
            apps.append(app)
        data["apps"] = apps
        return data

    def _load(self):
        paths = (self._path, self._path + ".bak")
        found_candidate = False
        for index, path in enumerate(paths):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                migrated, source_version = migrate_config(raw)
                data = self._normalize(migrated)
                if index:
                    self._recovered_from_backup = True
                    self._logger.warning("主配置不可读，已从备份恢复: %s", path)
                if source_version < CURRENT_SCHEMA_VERSION:
                    self._migration_from = source_version
                self._persist_loaded_state(
                    data, raw, source_index=index,
                    source_version=source_version)
                return data
            except FileNotFoundError:
                continue
            except FutureConfigSchemaError as exc:
                # Never let an older program overwrite a newer-schema primary
                # configuration with its stale backup.
                found_candidate = True
                self._health_issues.append(str(exc))
                self._logger.error("拒绝降级读取配置: %s", path)
                break
            except (OSError, UnicodeError, json.JSONDecodeError,
                    ConfigSchemaError, TypeError, ValueError):
                found_candidate = True
                self._logger.exception("读取配置失败: %s", path)
        data = self._normalize(self.DEFAULT)
        if found_candidate:
            # Present an empty in-memory view, but never overwrite files that
            # may still be recoverable by the user.
            self._writable = False
            self._health_issues.append(
                "主配置与备份均不可读，已进入只读保护状态")
            return data
        try:
            self._write_atomic(self._path, self._payload(data))
        except OSError as exc:
            self._writable = False
            self._health_issues.append("无法创建配置文件: %s" % exc)
        return data

    def _persist_loaded_state(self, data, raw, source_index, source_version):
        """Persist recovery/migration without destroying a known-good backup."""
        needs_migration = source_version < CURRENT_SCHEMA_VERSION
        if not source_index and not needs_migration:
            return
        try:
            if not source_index and needs_migration:
                # The pre-migration primary is the previous good version.
                self._write_atomic(self._path + ".bak", self._payload(raw))
            # On backup recovery, restore only the primary and retain the
            # already validated backup.
            self._write_atomic(self._path, self._payload(data))
        except OSError as exc:
            self._writable = False
            self._health_issues.append("配置恢复/迁移落盘失败: %s" % exc)
            self._logger.exception("配置恢复/迁移落盘失败")

    def snapshot(self):
        """Return a deep copy because all configuration values are JSON data."""
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def health_info(self):
        with self._lock:
            return {
                "writable": self._writable,
                "recoveredFromBackup": self._recovered_from_backup,
                "migratedFromSchema": self._migration_from,
                "issues": list(self._health_issues),
            }

    def update(self, fn):
        """Mutate under the lock, persist atomically, then return fn's result."""
        with self._lock:
            if not self._writable:
                raise OSError("配置处于只读保护状态，请先恢复配置或权限")
            previous = json.loads(json.dumps(self._data, ensure_ascii=False))
            try:
                result = fn(self._data)
                payload = self._payload(self._data)
                previous_payload = self._payload(previous)
                # Write last-known-good content before replacing the primary.
                self._write_atomic(self._path + ".bak", previous_payload)
                self._write_atomic(self._path, payload)
                self._on_change()
                return result
            except Exception:
                self._data = previous
                raise

    def _write_atomic(self, path, payload):
        self._ensure_private_dir(os.path.dirname(path) or ".")
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)


__all__ = [
    "Config",
    "ConfigSchemaError",
    "FutureConfigSchemaError",
    "CONFIG_MIGRATIONS",
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_UI_THEME",
    "migrate_config",
    "migrate_config_v0_to_v1",
]
