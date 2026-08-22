import argparse
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "sentry_codex_autofix.py"
)
spec = importlib.util.spec_from_file_location("sentry_codex_autofix", SCRIPT_PATH)
assert spec is not None
assert spec.loader is not None
autofix = importlib.util.module_from_spec(spec)
sys.modules["sentry_codex_autofix"] = autofix
spec.loader.exec_module(autofix)


class TestPayloadParsing(unittest.TestCase):
    def test_issue_webhook_payload(self):
        payload = {"data": {"issue": {"id": "123"}}}
        self.assertEqual(autofix.issue_id_from_payload(payload), "123")

    def test_alert_event_payload(self):
        payload = {"data": {"event": {"groupID": "456"}}}
        self.assertEqual(autofix.issue_id_from_payload(payload), "456")

    def test_missing_issue_id(self):
        self.assertIsNone(autofix.issue_id_from_payload({"data": {}}))


class TestSettings(unittest.TestCase):
    def test_load_settings_uses_env_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("SENTRY_AUTH_TOKEN=token\n", encoding="utf-8")
            args = argparse.Namespace(
                org="org",
                project="proj",
                repo_path=tmpdir,
                state_dir=tmpdir,
                sentry_base_url="https://example.test/",
                codex_bin="codex",
                codex_model=None,
                test_command=None,
                no_resolve=True,
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("pathlib.Path.cwd", return_value=Path(tmpdir)),
            ):
                settings = autofix.load_settings(args)
            self.assertEqual(settings.sentry_auth_token, "token")
            self.assertEqual(settings.sentry_base_url, "https://example.test")
            self.assertFalse(settings.resolve_on_success)


if __name__ == "__main__":
    unittest.main()
