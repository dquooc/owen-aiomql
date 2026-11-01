"""Passive reconciliation result contracts, without evaluation or external I/O."""

from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from uuid import UUID

from test_deal_normalizer import ledger
import test_ledger_recovery_result as recovery_fixtures


class ReconciliationResultTests(unittest.TestCase):
    def setUp(self):
        for name in ("ReconciliationStatus", "ReconciliationReason", "ReconciliationResult"):
            self.assertTrue(hasattr(ledger, name), f"{name} must be publicly exported")
        # Reuse the existing inert account/range/checkpoint/report model fixtures.
        fixture = recovery_fixtures.RecoveryResultTests()
        fixture.setUp()
        self.attempt = ledger.RecoveryAttempt(fixture.account, fixture.checkpoint, fixture.planned)
        self.report = fixture.report

    def result(self, **changes):
        values = dict(attempt=self.attempt, ingestion_report=self.report,
                      status=ledger.ReconciliationStatus.APPROVED, reason=None)
        values.update(changes)
        return ledger.ReconciliationResult(**values)

    def test_status_has_exactly_three_stable_string_members(self):
        self.assertEqual({name: member.value for name, member in ledger.ReconciliationStatus.__members__.items()},
                         {"APPROVED": "APPROVED", "REJECTED": "REJECTED", "INDETERMINATE": "INDETERMINATE"})
        self.assertEqual(str(ledger.ReconciliationStatus.APPROVED), "APPROVED")
        self.assertIs(ledger.ReconciliationStatus("INDETERMINATE"), ledger.ReconciliationStatus.INDETERMINATE)

    def test_reason_has_exactly_six_stable_string_members(self):
        self.assertEqual({name: member.value for name, member in ledger.ReconciliationReason.__members__.items()},
                         {"BINDING_MISMATCH": "BINDING_MISMATCH",
                          "INGESTION_INCOMPLETE": "INGESTION_INCOMPLETE",
                          "ACCOUNTING_MISMATCH": "ACCOUNTING_MISMATCH",
                          "CONFLICT_PRESENT": "CONFLICT_PRESENT",
                          "LEDGER_MISMATCH": "LEDGER_MISMATCH",
                          "EVIDENCE_INSUFFICIENT": "EVIDENCE_INSUFFICIENT"})
        self.assertEqual(str(ledger.ReconciliationReason.CONFLICT_PRESENT), "CONFLICT_PRESENT")
        self.assertIs(ledger.ReconciliationReason("LEDGER_MISMATCH"), ledger.ReconciliationReason.LEDGER_MISMATCH)

    def test_approved_result_preserves_exactly_four_supplied_fields(self):
        result = self.result()
        self.assertEqual(tuple(field.name for field in fields(result)),
                         ("attempt", "ingestion_report", "status", "reason"))
        self.assertIs(result.attempt, self.attempt)
        self.assertIs(result.ingestion_report, self.report)
        self.assertIs(result.status, ledger.ReconciliationStatus.APPROVED)
        self.assertIsNone(result.reason)

    def test_rejected_accepts_each_explicit_reason(self):
        for reason in ledger.ReconciliationReason:
            with self.subTest(reason=reason):
                result = self.result(status=ledger.ReconciliationStatus.REJECTED, reason=reason)
                self.assertIs(result.status, ledger.ReconciliationStatus.REJECTED)
                self.assertIs(result.reason, reason)

    def test_indeterminate_accepts_each_explicit_reason(self):
        for reason in ledger.ReconciliationReason:
            with self.subTest(reason=reason):
                result = self.result(status=ledger.ReconciliationStatus.INDETERMINATE, reason=reason)
                self.assertIs(result.status, ledger.ReconciliationStatus.INDETERMINATE)
                self.assertIs(result.reason, reason)

    def test_approved_rejects_any_reason(self):
        for reason in ledger.ReconciliationReason:
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.result(reason=reason)

    def test_rejected_requires_reason(self):
        with self.assertRaises(ValueError):
            self.result(status=ledger.ReconciliationStatus.REJECTED)

    def test_indeterminate_requires_reason(self):
        with self.assertRaises(ValueError):
            self.result(status=ledger.ReconciliationStatus.INDETERMINATE)

    def test_wrong_attempt_types_are_rejected(self):
        for value in (None, object(), {}, self.attempt.account, self.attempt.planned_range):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.result(attempt=value)

    def test_wrong_report_types_are_rejected(self):
        for value in (None, object(), {}, self.attempt, "SUCCESS"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.result(ingestion_report=value)

    def test_wrong_status_types_are_rejected_without_coercion(self):
        for value in (None, object(), "APPROVED", "approved", False,
                      ledger.RecoveryOutcome.FAILED, ledger.IngestionStatus.SUCCESS):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.result(status=value)

    def test_wrong_reason_types_are_rejected_before_status_reason_validation(self):
        for status in ledger.ReconciliationStatus:
            for value in (object(), "CONFLICT_PRESENT", "conflict_present", False,
                          ledger.ReconciliationStatus.REJECTED):
                with self.subTest(status=status, value=value), self.assertRaises(TypeError):
                    self.result(status=status, reason=value)

    def test_all_fields_are_frozen_and_model_is_slotted(self):
        result = self.result()
        for field in fields(result):
            with self.subTest(field=field.name), self.assertRaises(FrozenInstanceError):
                setattr(result, field.name, getattr(result, field.name))
        self.assertFalse(hasattr(result, "__dict__"))

    def test_equivalent_independent_results_compare_equal(self):
        other_attempt = ledger.RecoveryAttempt(replace(self.attempt.account),
                                               replace(self.attempt.checkpoint_before),
                                               replace(self.attempt.planned_range))
        first = self.result()
        second = self.result(attempt=other_attempt, ingestion_report=replace(self.report))
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_different_status_or_reason_makes_results_unequal(self):
        first = self.result(status=ledger.ReconciliationStatus.REJECTED,
                            reason=ledger.ReconciliationReason.CONFLICT_PRESENT)
        self.assertNotEqual(first, replace(first, status=ledger.ReconciliationStatus.INDETERMINATE))
        self.assertNotEqual(first, replace(first, reason=ledger.ReconciliationReason.EVIDENCE_INSUFFICIENT))

    def test_model_does_not_evaluate_report_or_attempt_semantics(self):
        for ingestion_status in ledger.IngestionStatus:
            for checkpoint in (None, self.attempt.checkpoint_before):
                attempt = replace(self.attempt, checkpoint_before=checkpoint)
                report = replace(self.report, status=ingestion_status, mt5_account_id=UUID(int=1),
                                 requested_from=self.report.requested_to, retrieved=99,
                                 normalized=1, inserted=0, conflicts=7, invalid=3,
                                 errors=(ledger.IngestionError(ledger.IngestionStage.STORAGE,
                                                               "ValueError", "offline fixture"),))
                for status, reason in ((ledger.ReconciliationStatus.APPROVED, None),
                                       (ledger.ReconciliationStatus.REJECTED,
                                        ledger.ReconciliationReason.BINDING_MISMATCH),
                                       (ledger.ReconciliationStatus.INDETERMINATE,
                                        ledger.ReconciliationReason.EVIDENCE_INSUFFICIENT)):
                    with self.subTest(ingestion_status=ingestion_status, checkpoint=checkpoint, status=status):
                        result = self.result(attempt=attempt, ingestion_report=report, status=status, reason=reason)
                        self.assertIs(result.status, status)
                        self.assertIs(result.reason, reason)
                        self.assertIs(result.attempt, attempt)
                        self.assertIs(result.ingestion_report, report)

    def test_public_import_executes_real_ledger_without_loading_mt5(self):
        package_dir = Path(__file__).resolve().parents[2] / "src" / "aiomql"
        script = textwrap.dedent("""
            import sys
            from types import ModuleType

            parent = ModuleType("aiomql")
            parent.__path__ = [sys.argv[1]]
            sys.modules["aiomql"] = parent
            from aiomql.ledger import ReconciliationStatus, ReconciliationReason, ReconciliationResult, __all__
            from aiomql.ledger.reconciliation_models import ReconciliationResult as DirectResult

            assert {"ReconciliationStatus", "ReconciliationReason", "ReconciliationResult"} <= set(__all__)
            assert ReconciliationResult is DirectResult
            assert ReconciliationStatus.APPROVED.value == "APPROVED"
            assert ReconciliationReason.EVIDENCE_INSUFFICIENT.value == "EVIDENCE_INSUFFICIENT"
            assert "MetaTrader5" not in sys.modules
        """)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(package_dir)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
