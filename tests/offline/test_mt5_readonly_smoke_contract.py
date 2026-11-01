"""Verify the opt-in live smoke using a fake SDK, without terminal access."""

from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SMOKE_PATH = Path(__file__).resolve().parents[1] / "live" / "test_mt5_readonly_smoke.py"


class ReadonlySmokeContractTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("readonly_smoke_contract_target", SMOKE_PATH)
        self.smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.smoke)
        self.secret = "offline-fake-secret"
        self.account = SimpleNamespace(login=10013418577, server="MetaQuotes-Demo", trade_mode=0,
                                       company="Fake broker", currency="USD", balance=1000, equity=1000)
        self.client = Mock(spec=["initialize", "login", "account_info", "terminal_info",
                                 "history_deals_get", "last_error", "shutdown", "ACCOUNT_TRADE_MODE_DEMO"])
        self.client.ACCOUNT_TRADE_MODE_DEMO = 0
        self.client.initialize.return_value = True
        self.client.login.return_value = True
        self.client.account_info.return_value = self.account
        self.client.terminal_info.return_value = SimpleNamespace(connected=True)
        self.client.history_deals_get.return_value = ()
        self.client.last_error.return_value = (-6, self.secret)

    def run_smoke(self):
        return self.smoke.run_readonly(self.client, login=10013418577,
                                       password=self.secret, server="MetaQuotes-Demo")

    def test_empty_history_succeeds_with_only_read_operations_and_seven_day_range(self):
        before = datetime.now(timezone.utc)
        report = self.run_smoke()
        after = datetime.now(timezone.utc)
        self.assertEqual([call[0] for call in self.client.mock_calls],
                         ["initialize", "login", "account_info", "terminal_info",
                          "history_deals_get", "account_info", "shutdown"])
        start, end = self.client.history_deals_get.call_args.args
        self.assertEqual(end - start, timedelta(days=7))
        self.assertIs(start.tzinfo, timezone.utc)
        self.assertLessEqual(before, end)
        self.assertLessEqual(end, after)
        self.assertEqual(report["history"]["deal_count"], 0)
        self.assertEqual(report["history"]["query"], "PASS")
        self.assertEqual(report["account"]["login"], 10013418577)
        self.assertNotIn(self.secret, str(report))

    def test_nonempty_history_reports_only_ticket_summary(self):
        self.client.history_deals_get.return_value = (SimpleNamespace(ticket=7), SimpleNamespace(ticket=9))
        history = self.run_smoke()["history"]
        self.assertEqual((history["deal_count"], history["first_ticket"], history["last_ticket"]), (2, 7, 9))

    def test_wrong_account_or_non_demo_stops_before_history(self):
        for changes in ({"login": 1}, {"server": "Other"}, {"trade_mode": 2}):
            with self.subTest(changes=changes):
                self.client.reset_mock()
                self.client.account_info.return_value = SimpleNamespace(**(vars(self.account) | changes))
                with self.assertRaises(self.smoke.SmokeFailure):
                    self.run_smoke()
                self.client.history_deals_get.assert_not_called()
                self.client.shutdown.assert_called_once()

    def test_authentication_failure_does_not_fallback_or_expose_error_secret(self):
        self.client.login.return_value = False
        with self.assertRaises(self.smoke.SmokeFailure) as raised:
            self.run_smoke()
        self.assertIn("-6", str(raised.exception))
        self.assertNotIn(self.secret, str(raised.exception))
        self.client.login.assert_called_once()
        self.client.account_info.assert_not_called()
        self.client.shutdown.assert_called_once()

    def test_initialization_failure_stops_and_shuts_down(self):
        self.client.initialize.return_value = False
        with self.assertRaises(self.smoke.SmokeFailure):
            self.run_smoke()
        self.client.login.assert_not_called()
        self.client.shutdown.assert_called_once()

    def test_failed_history_is_not_empty_success_and_shutdown_occurs(self):
        self.client.history_deals_get.return_value = None
        with self.assertRaises(self.smoke.SmokeFailure):
            self.run_smoke()
        self.client.shutdown.assert_called_once()

    def test_changed_account_after_fetch_is_rejected(self):
        self.client.account_info.side_effect = [self.account, SimpleNamespace(login=1, server="Other", trade_mode=0)]
        with self.assertRaises(self.smoke.SmokeFailure):
            self.run_smoke()
        self.client.shutdown.assert_called_once()

    def test_unexpected_sdk_exception_is_sanitized_and_shutdown_occurs(self):
        self.client.history_deals_get.side_effect = RuntimeError(self.secret)
        with self.assertRaises(self.smoke.SmokeFailure) as raised:
            self.run_smoke()
        self.assertNotIn(self.secret, str(raised.exception))
        self.client.shutdown.assert_called_once()

    def test_credentials_require_local_password_and_exact_demo_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('MT5_LOGIN=10013418577\nMT5_SERVER=MetaQuotes-Demo\nMT5_PASSWORD=\n', encoding="utf-8")
            with self.assertRaisesRegex(self.smoke.SmokeFailure, "MT5_PASSWORD is not configured locally"):
                self.smoke.load_credentials(path, environ={})
            path.write_text('MT5_LOGIN=10013418577\nMT5_SERVER=MetaQuotes-Demo\nMT5_PASSWORD="file#secret"\n', encoding="utf-8")
            self.assertEqual(self.smoke.load_credentials(path, environ={})["password"], "file#secret")
            self.assertEqual(self.smoke.load_credentials(path, environ={"MT5_PASSWORD": self.secret})["password"], self.secret)
            for override in ({"MT5_SERVER": "Other"}, {"MT5_LOGIN": "1"}, {"MT5_PASSWORD": ""}):
                with self.subTest(override=override), self.assertRaises(self.smoke.SmokeFailure):
                    self.smoke.load_credentials(path, environ=override)

    def test_without_opt_in_the_entrypoint_skips_before_sdk_import(self):
        environment = dict(os.environ, AIOMQL_RUN_LIVE_MT5_TESTS="0")
        completed = subprocess.run([sys.executable, "-B", str(SMOKE_PATH), "-v"],
                                   env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("skipped=1", completed.stderr)


if __name__ == "__main__":
    unittest.main()
