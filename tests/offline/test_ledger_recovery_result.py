"""Immutable recovery result contracts without orchestration or MT5 imports."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from uuid import UUID

from test_deal_normalizer import ledger


class RecoveryResultTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger, "RecoveryOutcome"), "RecoveryOutcome must be publicly exported")
        self.assertTrue(hasattr(ledger, "RecoveryResult"), "RecoveryResult must be publicly exported")
        self.account = ledger.AccountRef(
            mt5_account_id=UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"),
            broker_namespace="broker-a", server="server-live", login=123456,
            company_observed="Broker A", trade_mode=2)
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.planned = ledger.RecoveryRange(self.start, self.end)
        self.checkpoint = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.start, self.start)
        self.report = ledger.IngestionReport(
            ingestion_id="recovery-1", mt5_account_id=self.account.mt5_account_id,
            requested_from=self.start, requested_to=self.end,
            status=ledger.IngestionStatus.SUCCESS_EMPTY)

    def result(self, **changes):
        values = dict(account=self.account, checkpoint_before=None, planned_range=self.planned,
                      ingestion_report=self.report, outcome=ledger.RecoveryOutcome.PENDING_RECONCILIATION,
                      checkpoint_after=None)
        values.update(changes)
        return ledger.RecoveryResult(**values)

    def test_outcomes_include_completed_after_conditional_advancement(self):
        self.assertEqual(
            {name: member.value for name, member in ledger.RecoveryOutcome.__members__.items()},
            {"PENDING_RECONCILIATION": "PENDING_RECONCILIATION", "FAILED": "FAILED", "COMPLETED": "COMPLETED"})
        self.assertEqual(str(ledger.RecoveryOutcome.PENDING_RECONCILIATION), "PENDING_RECONCILIATION")
        self.assertIs(ledger.RecoveryOutcome("FAILED"), ledger.RecoveryOutcome.FAILED)

    def test_bootstrap_result_preserves_six_supplied_fields_and_defaults_reconciliation(self):
        result = self.result()
        self.assertEqual(tuple(field.name for field in fields(result)),
                         ("account", "checkpoint_before", "planned_range", "ingestion_report",
                          "outcome", "checkpoint_after", "reconciliation_result"))
        self.assertIsNone(result.reconciliation_result)
        self.assertIs(result.account, self.account)
        self.assertIsNone(result.checkpoint_before)
        self.assertIs(result.planned_range, self.planned)
        self.assertIs(result.ingestion_report, self.report)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIsNone(result.checkpoint_after)

    def test_existing_checkpoint_and_input_models_are_preserved(self):
        result = self.result(checkpoint_before=self.checkpoint, checkpoint_after=self.checkpoint)
        self.assertIs(result.checkpoint_before, self.checkpoint)
        self.assertIs(result.checkpoint_after, self.checkpoint)
        self.assertEqual((self.checkpoint.completed_through, self.checkpoint.updated_at),
                         (self.start, self.start))
        self.assertEqual((self.planned.from_time, self.planned.to_time), (self.start, self.end))
        self.assertIs(self.report.status, ledger.IngestionStatus.SUCCESS_EMPTY)

    def test_all_result_fields_are_frozen(self):
        result = self.result()
        for field in fields(result):
            with self.subTest(field=field.name), self.assertRaises(FrozenInstanceError):
                setattr(result, field.name, getattr(result, field.name))
        self.assertFalse(hasattr(result, "__dict__"))

    def test_equivalent_independent_values_compare_equal(self):
        first = self.result(checkpoint_before=self.checkpoint, checkpoint_after=self.checkpoint)
        second = self.result(account=replace(self.account), checkpoint_before=replace(self.checkpoint),
                             checkpoint_after=replace(self.checkpoint),
                             planned_range=replace(self.planned), ingestion_report=replace(self.report))
        self.assertIsNot(first, second)
        self.assertEqual(first, second)

    def test_different_outcomes_compare_unequal(self):
        self.assertNotEqual(self.result(), self.result(outcome=ledger.RecoveryOutcome.FAILED))

    def test_checkpoint_after_may_differ_without_advancing_storage(self):
        later = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.end, self.end)
        result = self.result(checkpoint_before=self.checkpoint, checkpoint_after=later)
        self.assertIs(result.checkpoint_before, self.checkpoint)
        self.assertIs(result.checkpoint_after, later)
        self.assertNotEqual(result.checkpoint_before, result.checkpoint_after)
        self.assertIs(self.result(checkpoint_after=later).checkpoint_after, later)

    def test_outcome_is_explicit_not_inferred_from_ingestion_or_checkpoint(self):
        for status in ledger.IngestionStatus:
            for outcome in ledger.RecoveryOutcome:
                for checkpoint in (None, self.checkpoint):
                    with self.subTest(status=status, outcome=outcome, checkpoint=checkpoint):
                        report = replace(self.report, status=status, retrieved=3, normalized=3,
                                         inserted=1, duplicates=1, conflicts=1)
                        result = self.result(ingestion_report=report, outcome=outcome,
                                             checkpoint_before=checkpoint, checkpoint_after=checkpoint)
                        self.assertIs(result.outcome, outcome)
                        self.assertIs(result.ingestion_report, report)

    def test_invalid_required_field_types_are_rejected_without_coercion(self):
        invalid = {
            "account": (None, object(), str(self.account.mt5_account_id)),
            "planned_range": (None, object(), (self.start, self.end)),
            "ingestion_report": (None, object(), {}),
            "outcome": (None, object(), "FAILED", ledger.IngestionStatus.FAILED),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(field=name, value=value), self.assertRaises(TypeError):
                    self.result(**{name: value})

    def test_invalid_checkpoint_types_are_rejected_on_both_fields(self):
        for name in ("checkpoint_before", "checkpoint_after"):
            for value in (object(), False, self.start, self.planned):
                with self.subTest(field=name, value=value), self.assertRaises(TypeError):
                    self.result(**{name: value})

    def test_non_utc_models_retain_existing_canonicalization(self):
        zone = timezone(timedelta(hours=7))
        planned = ledger.RecoveryRange(datetime(2026, 11, 1, 17, tzinfo=zone),
                                      datetime(2026, 11, 1, 17, 20, tzinfo=zone))
        checkpoint = ledger.LedgerCheckpoint(self.account.mt5_account_id,
                                             datetime(2026, 11, 1, 17, tzinfo=zone), self.start)
        result = self.result(planned_range=planned, checkpoint_before=checkpoint, checkpoint_after=checkpoint)
        self.assertEqual(result.planned_range, self.planned)
        self.assertEqual(result.checkpoint_before, self.checkpoint)
        self.assertIs(result.planned_range.to_time.tzinfo, timezone.utc)

    def test_public_import_uses_real_ledger_exports_with_an_inert_parent(self):
        # Bypass aiomql's eager SDK imports, but execute the real ledger package.
        package_dir = Path(__file__).resolve().parents[2] / "src" / "aiomql"
        script = textwrap.dedent("""
            import sys
            from types import ModuleType

            package = ModuleType("aiomql")
            package.__path__ = [sys.argv[1]]
            sys.modules["aiomql"] = package
            from aiomql.ledger import RecoveryOutcome, RecoveryResult
            from aiomql.ledger import __all__

            assert "RecoveryOutcome" in __all__ and "RecoveryResult" in __all__
            assert RecoveryOutcome.FAILED.value == "FAILED"
            assert RecoveryResult.__name__ == "RecoveryResult"
            assert "MetaTrader5" not in sys.modules
        """)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(package_dir)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
