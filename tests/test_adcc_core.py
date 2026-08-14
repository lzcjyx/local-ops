import unittest

from adcc.core import (
    APP_DEFAULT,
    CONFIG_DEFAULT,
    CURRENT_SCHEMA_VERSION,
    DEFAULT_UI_THEME,
    ConfigSchemaError,
    FutureConfigSchemaError,
)
from adcc.core.models import LastExit, ProcessInfo


class CoreContractTests(unittest.TestCase):
    def test_legacy_schema_and_defaults_are_stable(self):
        self.assertEqual(CURRENT_SCHEMA_VERSION, 2)
        self.assertEqual(DEFAULT_UI_THEME, "ops")
        self.assertEqual(CONFIG_DEFAULT["schemaVersion"], 2)
        self.assertEqual(CONFIG_DEFAULT["uiTheme"], "ops")
        # M3：v2 增加项目域骨架，legacy apps 数组保留
        self.assertIn("workspaces", CONFIG_DEFAULT)
        self.assertIn("projects", CONFIG_DEFAULT)
        self.assertIn("resources", CONFIG_DEFAULT)
        self.assertEqual(APP_DEFAULT["kind"], "service")
        self.assertIsNone(APP_DEFAULT["lastPgid"])
        self.assertFalse(APP_DEFAULT["attached"])

    def test_future_schema_error_is_a_schema_error(self):
        self.assertTrue(issubclass(FutureConfigSchemaError, ConfigSchemaError))

    def test_legacy_structural_models_remain_plain_mappings(self):
        process: ProcessInfo = {"uid": 501, "args": "python app.py"}
        last_exit: LastExit = {"status": "succeeded", "code": 0}
        self.assertEqual(process["uid"], 501)
        self.assertEqual(last_exit["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
