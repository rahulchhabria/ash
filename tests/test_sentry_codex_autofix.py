import argparse
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "sentry_codex_autofix.py"
)
spec = importlib.util.spec_from_file_location("sentry_codex_autofix", MODULE_PATH)
autofix = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = autofix
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


class TestOpsPlaybooks(unittest.TestCase):
    def test_matches_email_port_collision(self):
        detail = {
            "title": "[Errno 98] error while attempting to bind on address ('127.0.0.1', 8787): address already in use",
        }
        self.assertTrue(autofix.issue_matches_email_port_collision(detail))

    def test_does_not_match_unrelated_issue(self):
        self.assertFalse(
            autofix.issue_matches_email_port_collision({"title": "parser failed"})
        )

    def test_matches_transient_telegram_network_issue(self):
        detail = {
            "title": "Failed to fetch updates - TelegramNetworkError: HTTP Client says - ServerDisconnectedError: Server disconnected",
        }
        self.assertTrue(autofix.issue_matches_transient_telegram_network(detail))

    def test_run_issue_resolves_with_ops_remediation_before_codex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = autofix.Settings(
                org="org",
                project="proj",
                repo_path=Path(tmpdir),
                state_dir=Path(tmpdir) / "state",
                sentry_base_url="https://example.test",
                sentry_auth_token="token",
                webhook_secret=None,
                codex_bin="codex",
                codex_model=None,
                test_command=None,
                resolve_on_success=True,
            )
            client = Mock()
            client.issue_detail.return_value = {
                "title": "[Errno 98] error while attempting to bind on address ('127.0.0.1', 8787): address already in use",
            }
            with (
                patch.object(autofix, "SentryClient", return_value=client),
                patch.object(
                    autofix, "remediate_email_port_collision", return_value=True
                ),
                patch.object(autofix, "run_command") as run_command,
            ):
                self.assertTrue(autofix.run_issue(settings, "123"))
            client.resolve_issue.assert_called_once_with("123")
            run_command.assert_not_called()
            self.assertTrue((settings.state_dir / "123" / "DONE").exists())


class TestPoller(unittest.TestCase):
    def test_poll_runs_sweep_and_sleeps(self):
        settings = Mock()
        with (
            patch.object(autofix, "sweep") as sweep,
            patch.object(autofix.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                autofix.poll(settings, limit=3, interval=9)
        sweep.assert_called_once_with(settings, 3)


class TestLegacyReceiverRemediation(unittest.TestCase):
    def test_disable_legacy_email_receiver_disables_enabled_unit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Mock(repo_path=Path(tmpdir))
            issue_dir = Path(tmpdir) / "issue"
            issue_dir.mkdir()
            calls = []

            def fake_command(args, *, cwd, timeout=30):
                calls.append(args)
                if args[:2] == ["systemctl", "is-enabled"]:
                    return 0, "enabled\n"
                if args[:4] == ["sudo", "-n", "systemctl", "disable"]:
                    return 0, "Removed unit link\n"
                if args[:2] == ["systemctl", "is-active"]:
                    return 3, "inactive\n"
                return 1, "unexpected"

            with patch.object(autofix, "command_output", side_effect=fake_command):
                self.assertTrue(
                    autofix.disable_legacy_email_receiver(settings, issue_dir)
                )

            self.assertIn(
                [
                    "sudo",
                    "-n",
                    "systemctl",
                    "disable",
                    "--now",
                    autofix.LEGACY_EMAIL_SYSTEM_UNIT,
                ],
                calls,
            )

    def test_run_issue_resolves_transient_telegram_without_codex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = autofix.Settings(
                org="org",
                project="proj",
                repo_path=Path(tmpdir),
                state_dir=Path(tmpdir) / "state",
                sentry_base_url="https://example.test",
                sentry_auth_token="token",
                webhook_secret=None,
                codex_bin="codex",
                codex_model=None,
                test_command=None,
                resolve_on_success=True,
            )
            client = Mock()
            client.issue_detail.return_value = {
                "title": "Failed to fetch updates - TelegramNetworkError: HTTP Client says - ServerDisconnectedError: Server disconnected",
            }
            with (
                patch.object(autofix, "SentryClient", return_value=client),
                patch.object(
                    autofix, "remediate_transient_telegram_network", return_value=True
                ),
                patch.object(autofix, "run_command") as run_command,
            ):
                self.assertTrue(autofix.run_issue(settings, "456"))
            client.resolve_issue.assert_called_once_with("456")
            run_command.assert_not_called()
            self.assertTrue((settings.state_dir / "456" / "DONE").exists())


if __name__ == "__main__":
    unittest.main()
