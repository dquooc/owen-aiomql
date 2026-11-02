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
from unittest.mock import Mock, patch


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
                                 "history_deals_get", "last_error", "shutdown", "order_send",
                                 "ACCOUNT_TRADE_MODE_DEMO"])
        self.client.ACCOUNT_TRADE_MODE_DEMO = 0
        self.client.initialize.return_value = True
        self.client.login.return_value = True
        self.client.account_info.return_value = self.account
        self.client.terminal_info.return_value = SimpleNamespace(connected=True)
        self.client.history_deals_get.return_value = ()
        self.client.last_error.return_value = (-6, self.secret)
        self.client.order_send = Mock()

    def run_smoke(self):
        return self.smoke.run_readonly(self.client, login=10013418577,
                                       password=self.secret, server="MetaQuotes-Demo")

    def test_empty_history_succeeds_with_only_read_operations_and_seven_day_range(self):
        before = datetime.now(timezone.utc)
        report = self.run_smoke()
        after = datetime.now(timezone.utc)
        self.assertEqual([call[0] for call in self.client.mock_calls],
                         ["initialize", "account_info", "login", "account_info", "terminal_info",
                          "history_deals_get", "account_info", "shutdown"])
        self.client.initialize.assert_called_once_with(timeout=10000)
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
        self.client.account_info.assert_called_once()
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
        self.client.account_info.side_effect = [self.account, self.account,
                                               SimpleNamespace(login=1, server="Other", trade_mode=0)]
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
        self.assertIn("skipped=2", completed.stderr)

    def run_recovery(self):
        runner = getattr(self.smoke, "run_recovery_readonly", None)
        self.assertTrue(callable(runner), "The guarded recovery acceptance runner is missing")
        return runner(self.client, login=10013418577, password=self.secret, server="MetaQuotes-Demo")

    def test_recovery_empty_batch_preserves_unverified_pending_and_cleans_temp_sqlite(self):
        report = self.run_recovery()
        self.assertEqual(report["source_fetch_count"], 1)
        self.assertEqual(report["account_guards"], ["before:PASS", "after:PASS"])
        self.assertEqual(report["retrieved"], 0)
        self.assertEqual(report["ingestion_status"], "SUCCESS_EMPTY")
        self.assertEqual(report["source_assurance"], "CALL_COMPLETED_UNVERIFIED")
        self.assertEqual(report["reconciliation_status"], "INDETERMINATE")
        self.assertEqual(report["reconciliation_reason"], "EVIDENCE_INSUFFICIENT")
        self.assertEqual(report["recovery_outcome"], "PENDING_RECONCILIATION")
        self.assertIsNone(report["checkpoint_before"])
        self.assertIsNone(report["checkpoint_after"])
        self.assertEqual(report["checkpoint_advancement_calls"], 0)
        self.assertEqual(report["snapshot_reads"], 1)
        self.assertEqual(report["clock_calls"], 1)
        self.assertEqual(report["persisted_deals"], 0)
        self.assertTrue(report["temporary_sqlite"])
        self.assertTrue(report["temporary_sqlite_cleaned"])
        self.assertTrue(report["shutdown"])
        self.assertFalse(report["persistent_database_modified"])
        self.assertEqual(report["native_order_send_calls"], 0)
        self.assertEqual(report["broker_mutation_attempts"], 0)
        self.client.order_send.assert_not_called()
        self.client.history_deals_get.assert_called_once()
        self.client.shutdown.assert_called_once()
        start, end = self.client.history_deals_get.call_args.args
        self.assertIs(start.tzinfo, timezone.utc)
        self.assertIs(end.tzinfo, timezone.utc)
        self.assertEqual(end - start, timedelta(days=7))
        self.assertNotIn(self.secret, str(report))

    def test_recovery_nonempty_batch_is_normalized_and_persisted_only_in_temp_sqlite(self):
        from test_deal_normalizer import raw_deal
        now = datetime.now(timezone.utc) - timedelta(hours=1)
        self.client.history_deals_get.return_value = (raw_deal(time=int(now.timestamp()),
                                                            time_msc=int(now.timestamp() * 1000)),)
        report = self.run_recovery()
        self.assertEqual((report["retrieved"], report["inserted"], report["persisted_deals"]), (1, 1, 1))
        self.assertEqual(report["ingestion_status"], "SUCCESS")
        self.assertEqual(report["recovery_outcome"], "PENDING_RECONCILIATION")
        self.assertTrue(report["temporary_sqlite_cleaned"])

    def test_recovery_guard_failure_or_unavailable_history_never_retries(self):
        changed = SimpleNamespace(login=1, server="Other", trade_mode=0)
        for stage in ("before", "fetch", "after"):
            with self.subTest(stage=stage):
                self.client.reset_mock()
                self.client.account_info.side_effect = [self.account, self.account,
                                                       changed if stage == "before" else self.account,
                                                       changed if stage == "after" else self.account]
                self.client.history_deals_get.return_value = None if stage == "fetch" else ()
                self.assertTrue(callable(getattr(self.smoke, "run_recovery_readonly", None)),
                                "The guarded recovery acceptance runner is missing")
                with self.assertRaises(self.smoke.SmokeFailure) as raised:
                    self.run_recovery()
                self.assertNotIn(self.secret, str(raised.exception))
                self.assertEqual(self.client.history_deals_get.call_count, 0 if stage == "before" else 1)
                self.client.order_send.assert_not_called()
                self.client.shutdown.assert_called_once()

    def test_recovery_sdk_exception_is_sanitized(self):
        self.assertTrue(callable(getattr(self.smoke, "run_recovery_readonly", None)),
                        "The guarded recovery acceptance runner is missing")
        self.client.history_deals_get.side_effect = RuntimeError(self.secret)
        with self.assertRaises(self.smoke.SmokeFailure) as raised:
            self.run_recovery()
        self.assertNotIn(self.secret, str(raised.exception))
        self.client.shutdown.assert_called_once()

    def test_recovery_rejects_checkpoint_write_before_mutation_and_cleans_database(self):
        self.assertTrue(callable(getattr(self.smoke, "load_ledger", None)),
                        "The SDK-independent ledger loader is missing")
        ledger = self.smoke.load_ledger()
        original = ledger.RecoveryManager.recover
        paths = []

        async def unexpected_advance(manager, **kwargs):
            paths.append(manager.account_lookup.path)
            manager.checkpoints.advance_checkpoint_if_expected(kwargs["account"], datetime.now(timezone.utc),
                                                               expected=None)
            return await original(manager, **kwargs)

        with patch.object(ledger.RecoveryManager, "recover", unexpected_advance):
            with self.assertRaisesRegex(self.smoke.SmokeFailure, "checkpoint advancement"):
                self.run_recovery()
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].parent.exists())
        self.client.shutdown.assert_called_once()

    def test_recovery_rejects_wrong_current_account_without_login_or_history(self):
        self.assertTrue(callable(getattr(self.smoke, "run_recovery_readonly", None)),
                        "The guarded recovery acceptance runner is missing")
        self.client.account_info.return_value = SimpleNamespace(login=1, server="Other", trade_mode=0)
        with self.assertRaises(self.smoke.SmokeFailure):
            self.run_recovery()
        self.client.initialize.assert_called_once_with(timeout=10000)
        self.client.login.assert_not_called()
        self.client.history_deals_get.assert_not_called()
        self.client.shutdown.assert_called_once()

    def test_native_writer_trap_blocks_accidental_direct_access_before_call(self):
        native_writer = self.client.order_send
        ledger = self.smoke.load_ledger()

        async def accidental_write(manager, **kwargs):
            self.client.order_send({"action": "DEAL"})

        with patch.object(ledger.RecoveryManager, "recover", accidental_write):
            with self.assertRaisesRegex(self.smoke.SmokeFailure, "native writer was not invoked"):
                self.run_recovery()
        native_writer.assert_not_called()
        self.assertIs(self.client.order_send, native_writer)
        self.client.shutdown.assert_called_once()

    def test_readonly_facade_never_forwards_an_unknown_or_mutating_operation(self):
        readonly = self.smoke._ReadOnlySDK(self.client)
        for name in ("order_send", "position_close", "future_write"):
            with self.subTest(name=name), self.assertRaises(self.smoke.SmokeFailure):
                getattr(readonly, name)({})
        self.assertEqual(readonly.broker_mutation_attempts, 3)
        self.assertEqual(self.client.mock_calls, [])

    def test_engine_readonly_path_denies_local_writer_without_loading_sdk_or_database(self):
        probe = getattr(self.smoke, "check_engine_readonly_safety", None)
        self.assertTrue(callable(probe), "The engine read-only acceptance probe is missing")
        with patch("sqlite3.connect", side_effect=AssertionError("persistent database access")):
            report = probe()
        self.assertEqual(report["mode"], "read_only")
        self.assertTrue(report["mt5_enabled"])
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["recovery_enabled"])
        self.assertFalse(report["order_submission"])
        self.assertFalse(report["live_trading"])
        self.assertEqual(report["underlying_fake_writer_calls"], 0)
        self.assertEqual(report["broker_results"], [])
        self.assertTrue(report["automatic_recovery_rejected"])


if __name__ == "__main__":
    unittest.main()
