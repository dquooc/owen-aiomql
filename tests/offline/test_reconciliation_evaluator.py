"""A4.6 pure decisions using immutable evidence only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import builtins
import importlib
import socket
from unittest.mock import patch
import unittest
from uuid import UUID

from test_deal_normalizer import ledger


class ReconciliationEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.account = ledger.AccountRef(UUID(int=1), "broker", "server", 123, "Company", 0)
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123000, tzinfo=timezone.utc)
        self.range = ledger.RecoveryRange(self.start, self.start + timedelta(minutes=1, microseconds=456))
        self.attempt = ledger.RecoveryAttempt(self.account, None, self.range)
        self.record = ledger.SourceRecordFingerprint(9001, "a" * 64, 1793527200123, 1)
        self.report = ledger.IngestionReport("run", self.account.mt5_account_id, self.range.from_time,
                                             self.range.to_time, ledger.IngestionStatus.SUCCESS,
                                             retrieved=1, normalized=1, inserted=1)
        self.source = ledger.SourceBatchEvidence("run", self.account, self.range,
                                                 ledger.SourceHistoryAssurance.VERIFIED_COMPLETE, 1, (self.record,))
        self.snapshot = ledger.LedgerSnapshotEvidence(self.account, self.range, (self.persisted(self.record),), ())

    @staticmethod
    def persisted(record):
        return ledger.LedgerRecordFingerprint(record.deal_ticket, record.payload_hash,
                                               record.executed_at_msc, record.normalizer_version)

    def evaluate(self, **changes):
        inputs = dict(attempt=self.attempt, ingestion_report=self.report,
                      source_evidence=self.source, ledger_evidence=self.snapshot)
        inputs.update(changes)
        return ledger.evaluate_reconciliation(**inputs)

    def assert_decision(self, reason=None, status=None, **changes):
        result = self.evaluate(**changes)
        self.assertIs(result.status, status or (ledger.ReconciliationStatus.REJECTED if reason
                                                else ledger.ReconciliationStatus.APPROVED))
        self.assertIs(result.reason, getattr(ledger.ReconciliationReason, reason) if reason else None)
        self.assertIs(result.attempt, changes.get("attempt", self.attempt))
        self.assertIs(result.ingestion_report, changes.get("ingestion_report", self.report))
        return result

    def test_verified_exact_binding_happy_path_preserves_context(self):
        self.assert_decision()

    def test_each_stable_account_field_mismatch_in_source_or_ledger_is_rejected(self):
        for field, value in (("mt5_account_id", UUID(int=2)), ("broker_namespace", "other"),
                             ("server", "other"), ("login", 456)):
            for name, evidence in (("source_evidence", self.source), ("ledger_evidence", self.snapshot)):
                with self.subTest(field=field, evidence=name):
                    self.assert_decision("BINDING_MISMATCH", **{name: replace(evidence, account=replace(self.account, **{field: value}))})
        self.assert_decision("BINDING_MISMATCH", ingestion_report=replace(self.report, mt5_account_id=UUID(int=2)))

    def test_observed_account_metadata_is_not_identity(self):
        account = replace(self.account, company_observed="renamed", trade_mode=2)
        self.assert_decision(source_evidence=replace(self.source, account=account),
                             ledger_evidence=replace(self.snapshot, account=account))

    def test_range_and_microsecond_mismatches_rejected_for_all_evidence(self):
        other = ledger.RecoveryRange(self.start, self.range.to_time + timedelta(microseconds=1))
        for name, evidence in (("source_evidence", self.source), ("ledger_evidence", self.snapshot)):
            self.assert_decision("BINDING_MISMATCH", **{name: replace(evidence, requested_range=other)})
        self.assert_decision("BINDING_MISMATCH", ingestion_report=replace(self.report, requested_to=other.to_time))
        self.assert_decision("BINDING_MISMATCH", ingestion_report=replace(self.report, requested_from=self.start + timedelta(microseconds=1)))

    def test_ingestion_id_mismatch_and_invalid_report_id_rejected(self):
        self.assert_decision("BINDING_MISMATCH", source_evidence=replace(self.source, ingestion_id="other"))
        self.assert_decision("BINDING_MISMATCH", ingestion_report=replace(self.report, ingestion_id=""))

    def test_timezone_equivalent_canonical_bindings_match(self):
        zone = timezone(timedelta(hours=7))
        other = ledger.RecoveryRange(self.start.astimezone(zone), self.range.to_time.astimezone(zone))
        self.assert_decision(source_evidence=replace(self.source, requested_range=other),
                             ledger_evidence=replace(self.snapshot, requested_range=other),
                             ingestion_report=replace(self.report, requested_from=self.start.astimezone(zone),
                                                       requested_to=self.range.to_time.astimezone(zone)))

    def test_failed_and_partial_status_never_approved_from_counters(self):
        for status in (ledger.IngestionStatus.FAILED, ledger.IngestionStatus.PARTIAL):
            self.assert_decision("INGESTION_INCOMPLETE", ingestion_report=replace(self.report, status=status))

    def test_counter_types_and_negative_values_rejected_including_bools(self):
        for field in ("retrieved", "normalized", "inserted", "duplicates", "conflicts", "invalid"):
            for value in (-1, True, False, 1.0, "1", None):
                with self.subTest(field=field, value=value):
                    self.assert_decision("ACCOUNTING_MISMATCH", ingestion_report=replace(self.report, **{field: value}))

    def test_success_arithmetic_and_source_retrieved_mismatch(self):
        for changes in (dict(retrieved=2), dict(normalized=2), dict(inserted=0), dict(duplicates=1)):
            self.assert_decision("ACCOUNTING_MISMATCH", ingestion_report=replace(self.report, **changes))
        self.assert_decision("ACCOUNTING_MISMATCH", source_evidence=replace(self.source, retrieved=2))

    def test_processing_errors_and_invalid_records_mean_incomplete(self):
        error = ledger.IngestionError(ledger.IngestionStage.NORMALIZE, "ValueError", "invalid", 0)
        self.assert_decision("INGESTION_INCOMPLETE", ingestion_report=replace(self.report, errors=(error,)))
        self.assert_decision("INGESTION_INCOMPLETE", ingestion_report=replace(self.report, invalid=1, retrieved=2))

    def test_success_empty_requires_every_counter_zero(self):
        report = replace(self.report, status=ledger.IngestionStatus.SUCCESS_EMPTY,
                         retrieved=0, normalized=0, inserted=0)
        source = replace(self.source, retrieved=0, records=())
        snapshot = replace(self.snapshot, records=())
        self.assert_decision(ingestion_report=report, source_evidence=source, ledger_evidence=snapshot)
        for field in ("retrieved", "normalized", "inserted", "duplicates", "conflicts"):
            with self.subTest(field=field):
                self.assert_decision("ACCOUNTING_MISMATCH", ingestion_report=replace(report, **{field: 1}),
                                     source_evidence=source, ledger_evidence=snapshot)
        self.assert_decision("ACCOUNTING_MISMATCH", ingestion_report=replace(report, status=ledger.IngestionStatus.SUCCESS),
                             source_evidence=source, ledger_evidence=snapshot)

    def test_current_conflict_rejected(self):
        self.assert_decision("CONFLICT_PRESENT", ingestion_report=replace(self.report, inserted=0, conflicts=1))

    def test_old_relevant_conflict_rejected_even_duplicates_only(self):
        conflict = ledger.LedgerConflictEvidence(self.snapshot.records[0],
                                                  replace(self.snapshot.records[0], payload_hash="b" * 64))
        self.assert_decision("CONFLICT_PRESENT", ingestion_report=replace(self.report, inserted=0, duplicates=1),
                             ledger_evidence=replace(self.snapshot, conflicts=(conflict,)))

    def test_missing_source_fingerprint_is_insufficient(self):
        self.assert_decision("EVIDENCE_INSUFFICIENT", status=ledger.ReconciliationStatus.INDETERMINATE,
                             source_evidence=replace(self.source, records=()))

    def test_each_fingerprint_component_mismatch_and_missing_ticket_rejected(self):
        for field, value in (("payload_hash", "b" * 64), ("normalizer_version", 2),
                             ("executed_at_msc", self.record.executed_at_msc + 1), ("deal_ticket", 9002)):
            with self.subTest(field=field):
                self.assert_decision("LEDGER_MISMATCH", ledger_evidence=replace(
                    self.snapshot, records=(replace(self.snapshot.records[0], **{field: value}),)))
        self.assert_decision("LEDGER_MISMATCH", ledger_evidence=replace(self.snapshot, records=()))

    def test_extra_or_duplicate_canonical_ledger_rows_rejected(self):
        for extra in (replace(self.snapshot.records[0], deal_ticket=9002), self.snapshot.records[0]):
            self.assert_decision("LEDGER_MISMATCH", ledger_evidence=replace(self.snapshot, records=self.snapshot.records + (extra,)))

    def test_clean_duplicates_inserts_and_mixed_counts_approve_without_order_assumption(self):
        second = replace(self.record, deal_ticket=9002)
        for inserted, duplicates in ((2, 0), (0, 2), (1, 1)):
            self.assert_decision(ingestion_report=replace(self.report, retrieved=2, normalized=2,
                                                        inserted=inserted, duplicates=duplicates),
                                 source_evidence=replace(self.source, retrieved=2, records=(second, self.record)),
                                 ledger_evidence=replace(self.snapshot, records=self.snapshot.records + (self.persisted(second),)))

    def test_identical_repeated_source_ticket_requires_truthful_idempotent_accounting(self):
        source = replace(self.source, retrieved=2, records=(self.record, self.record))
        report = replace(self.report, retrieved=2, normalized=2, duplicates=1)
        self.assert_decision(ingestion_report=report, source_evidence=source)
        self.assert_decision("ACCOUNTING_MISMATCH", ingestion_report=replace(report, inserted=2, duplicates=0), source_evidence=source)

    def test_differing_repeated_source_ticket_rejected_even_if_report_claims_no_conflicts(self):
        source = replace(self.source, retrieved=2, records=(self.record, replace(self.record, payload_hash="b" * 64)))
        self.assert_decision("CONFLICT_PRESENT", ingestion_report=replace(self.report, retrieved=2, normalized=2, duplicates=1),
                             source_evidence=source)

    def test_unverified_clean_empty_and_nonempty_never_approved(self):
        source = replace(self.source, assurance=ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)
        self.assert_decision("EVIDENCE_INSUFFICIENT", status=ledger.ReconciliationStatus.INDETERMINATE, source_evidence=source)
        self.assert_decision("EVIDENCE_INSUFFICIENT", status=ledger.ReconciliationStatus.INDETERMINATE,
                             ingestion_report=replace(self.report, status=ledger.IngestionStatus.SUCCESS_EMPTY,
                                                       retrieved=0, normalized=0, inserted=0),
                             source_evidence=replace(source, retrieved=0, records=()), ledger_evidence=replace(self.snapshot, records=()))

    def test_source_outside_ledger_projection_does_not_invent_endpoint_semantics(self):
        source = replace(self.source, records=(replace(self.record, executed_at_msc=0),))
        self.assert_decision("EVIDENCE_INSUFFICIENT", status=ledger.ReconciliationStatus.INDETERMINATE, source_evidence=source)

    def test_reason_precedence_with_multiple_failures(self):
        unverified = replace(self.source, assurance=ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)
        missing = replace(self.snapshot, records=())
        self.assert_decision("BINDING_MISMATCH", ingestion_report=replace(self.report, status=ledger.IngestionStatus.FAILED),
                             source_evidence=replace(unverified, ingestion_id="other"))
        self.assert_decision("INGESTION_INCOMPLETE", ingestion_report=replace(self.report, status=ledger.IngestionStatus.FAILED, retrieved=-1))
        self.assert_decision("ACCOUNTING_MISMATCH", ingestion_report=replace(self.report, inserted=1, conflicts=1), source_evidence=unverified)
        self.assert_decision("CONFLICT_PRESENT", ingestion_report=replace(self.report, inserted=0, conflicts=1), source_evidence=unverified)
        self.assert_decision("EVIDENCE_INSUFFICIENT", status=ledger.ReconciliationStatus.INDETERMINATE,
                             source_evidence=unverified, ledger_evidence=missing)

    def test_purity_determinism_no_io_clock_source_normalization_or_mutation(self):
        module = importlib.import_module(f"{ledger.__name__}.normalize")
        inputs = (self.attempt, self.report, self.source, self.snapshot)
        before = repr(inputs), hash(inputs)
        with patch.object(builtins, "open", side_effect=AssertionError("I/O")), \
                patch.object(socket, "socket", side_effect=AssertionError("network")), \
                patch.object(ledger.SQLiteDealRepository, "read_snapshot", side_effect=AssertionError("repository")), \
                patch.object(ledger.MetaTraderDealHistorySource, "fetch", side_effect=AssertionError("source")), \
                patch.object(module, "normalize_deal", side_effect=AssertionError("normalization")), \
                patch.object(ledger, "utc_now", side_effect=AssertionError("clock")):
            first = self.assert_decision()
            self.assertEqual(first, self.assert_decision())
        self.assertEqual((repr(inputs), hash(inputs)), before)

    def test_invalid_public_argument_types_fail_fast(self):
        for name in ("attempt", "ingestion_report", "source_evidence", "ledger_evidence"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                self.evaluate(**{name: None})


if __name__ == "__main__":
    unittest.main()
