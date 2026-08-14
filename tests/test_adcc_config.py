"""Direct, Windows-safe tests for the extracted configuration Module."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from adcc.core.constants import CONFIG_DEFAULT, CURRENT_SCHEMA_VERSION
from adcc.core.errors import ConfigSchemaError, FutureConfigSchemaError
from adcc.storage.config import Config, migrate_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigModuleTests(unittest.TestCase):
    def _write_json(self, path, value):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_direct_import_does_not_import_legacy_http_server(self):
        script = (
            "import sys; import adcc.storage.config; "
            "assert 'server' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_config_is_independent_from_class_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            original = json.loads(json.dumps(Config.DEFAULT))
            config = Config(os.path.join(directory, "config.json"))
            config.update(lambda data: data["watchedKeywords"].append("node"))
            self.assertEqual(Config.DEFAULT, original)

    def test_update_writes_previous_good_backup_and_notifies_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            self._write_json(path, {**CONFIG_DEFAULT, "watchedKeywords": ["node"]})
            changes = []
            config = Config(path, on_change=lambda: changes.append("changed"))

            result = config.update(
                lambda data: data["watchedKeywords"].append("ffmpeg"))

            self.assertIsNone(result)
            with open(path, "r", encoding="utf-8") as handle:
                current = json.load(handle)
            with open(path + ".bak", "r", encoding="utf-8") as handle:
                backup = json.load(handle)
            self.assertEqual(current["watchedKeywords"], ["node", "ffmpeg"])
            self.assertEqual(backup["watchedKeywords"], ["node"])
            self.assertEqual(changes, ["changed"])

    def test_load_recovers_primary_from_valid_backup_and_preserves_health(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            self._write_json(
                path + ".bak",
                {**CONFIG_DEFAULT, "watchedKeywords": ["node"]},
            )
            logger = mock.Mock()

            config = Config(path, logger=logger)

            self.assertEqual(config.snapshot()["watchedKeywords"], ["node"])
            self.assertTrue(config.health_info()["recoveredFromBackup"])
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["watchedKeywords"], ["node"])
            logger.warning.assert_called_once()

    def test_legacy_schema_migrates_once_and_keeps_raw_legacy_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            legacy = {
                key: value for key, value in CONFIG_DEFAULT.items()
                if key != "schemaVersion"
            }
            legacy["watchedKeywords"] = ["ffmpeg"]
            self._write_json(path, legacy)

            config = Config(path, logger=mock.Mock())

            self.assertEqual(
                config.snapshot()["schemaVersion"], CURRENT_SCHEMA_VERSION)
            self.assertEqual(config.health_info()["migratedFromSchema"], 0)
            with open(path, "r", encoding="utf-8") as handle:
                migrated = json.load(handle)
            with open(path + ".bak", "r", encoding="utf-8") as handle:
                backup = json.load(handle)
            self.assertEqual(migrated["schemaVersion"], 1)
            self.assertNotIn("schemaVersion", backup)

            with open(path + ".bak", "rb") as handle:
                backup_bytes = handle.read()
            second = Config(path)
            self.assertIsNone(second.health_info()["migratedFromSchema"])
            with open(path + ".bak", "rb") as handle:
                self.assertEqual(handle.read(), backup_bytes)

    def test_future_schema_never_falls_back_to_or_overwrites_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            future = {
                **CONFIG_DEFAULT,
                "schemaVersion": CURRENT_SCHEMA_VERSION + 1,
                "watchedKeywords": ["future-data"],
            }
            backup = {**CONFIG_DEFAULT, "watchedKeywords": ["old-data"]}
            self._write_json(path, future)
            self._write_json(path + ".bak", backup)

            config = Config(path, logger=mock.Mock())

            self.assertFalse(config.health_info()["writable"])
            with self.assertRaises(OSError):
                config.update(lambda data: data["watchedKeywords"].append("x"))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), future)
            with open(path + ".bak", "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), backup)

    def test_unreadable_primary_and_backup_enter_read_only_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not-json")
            with open(path + ".bak", "w", encoding="utf-8") as handle:
                handle.write("also-not-json")
            with open(path, "rb") as handle:
                primary_before = handle.read()
            with open(path + ".bak", "rb") as handle:
                backup_before = handle.read()

            config = Config(path, logger=mock.Mock())

            self.assertFalse(config.health_info()["writable"])
            self.assertIn("主配置与备份均不可读", config.health_info()["issues"][-1])
            with self.assertRaises(OSError):
                config.update(lambda data: data.clear())
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), primary_before)
            with open(path + ".bak", "rb") as handle:
                self.assertEqual(handle.read(), backup_before)

    def test_injected_private_directory_policy_controls_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            calls = []

            def ensure_private_dir(value):
                calls.append(value)
                os.makedirs(value, exist_ok=True)

            config = Config(path, ensure_private_dir=ensure_private_dir)
            config.update(lambda data: data.__setitem__("uiTheme", "custom"))

            self.assertEqual(calls, [directory, directory, directory])
            self.assertEqual(config.snapshot()["uiTheme"], "custom")

    def test_failed_atomic_update_restores_memory_and_skips_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            calls = []
            ensure_calls = 0

            def ensure_private_dir(value):
                nonlocal ensure_calls
                ensure_calls += 1
                if ensure_calls > 1:
                    raise OSError("write blocked")
                os.makedirs(value, exist_ok=True)

            config = Config(
                path,
                on_change=lambda: calls.append("changed"),
                ensure_private_dir=ensure_private_dir,
            )
            before = config.snapshot()

            with self.assertRaisesRegex(OSError, "write blocked"):
                config.update(lambda data: data.__setitem__("uiTheme", "custom"))

            self.assertEqual(config.snapshot(), before)
            self.assertEqual(calls, [])

    def test_migrate_config_rejects_invalid_and_future_schemas(self):
        with self.assertRaises(ConfigSchemaError):
            migrate_config([])
        with self.assertRaises(ConfigSchemaError):
            migrate_config({"schemaVersion": True})
        with self.assertRaises(FutureConfigSchemaError):
            migrate_config({"schemaVersion": CURRENT_SCHEMA_VERSION + 1})


if __name__ == "__main__":
    unittest.main()
