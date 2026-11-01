"""Source-side evidence contracts with real normalization and inert fixtures."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from uuid import UUID

from test_deal_normalizer import ledger, raw_deal
import test_ledger_recovery_result as recovery_fixtures


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        for name in ("SourceHistoryAssurance", "SourceRecordFingerprint", "SourceBatchEvidence"):
            self.assertTrue(hasattr(ledger, name), f"{name} must be publicly exported")
        fixture = recovery_fixtures.RecoveryResultTests()
        fixture.setUp()
        self.account = fixture.account
        self.requested_range = fixture.planned
        self.ingestion_id = fixture.report.ingestion_id
        self.deal = ledger.normalize_deal(self.account, raw_deal(), fixture.start, self.ingestion_id)
        self.record = ledger.SourceRecordFingerprint(
            deal_ticket=self.deal.deal_ticket, payload_hash=self.deal.payload_hash,
            executed_at_msc=self.deal.executed_at_msc, normalizer_version=self.deal.normalizer_version)

    def fingerprint(self, **changes):
        values = dict(deal_ticket=self.deal.deal_ticket, payload_hash=self.deal.payload_hash,
                      executed_at_msc=self.deal.executed_at_msc, normalizer_version=self.deal.normalizer_version)
        values.update(changes)
        return ledger.SourceRecordFingerprint(**values)

    def batch(self, **changes):
        values = dict(ingestion_id=self.ingestion_id, account=self.account, requested_range=self.requested_range,
                      assurance=ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED,
                      retrieved=1, records=(self.record,))
        values.update(changes)
        return ledger.SourceBatchEvidence(**values)

    def test_assurance_has_exactly_two_explicit_levels(self):
        self.assertEqual({name: member.value for name, member in ledger.SourceHistoryAssurance.__members__.items()},
                         {"CALL_COMPLETED_UNVERIFIED": "CALL_COMPLETED_UNVERIFIED",
                          "VERIFIED_COMPLETE": "VERIFIED_COMPLETE"})
        self.assertEqual(str(ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED),
                         "CALL_COMPLETED_UNVERIFIED")

    def test_fingerprint_carries_exact_normalizer_identity_without_rehashing(self):
        self.assertEqual(tuple(field.name for field in fields(self.record)),
                         ("deal_ticket", "payload_hash", "executed_at_msc", "normalizer_version"))
        self.assertEqual(self.record.deal_ticket, 9001)
        self.assertIs(self.record.payload_hash, self.deal.payload_hash)
        self.assertEqual(self.record.executed_at_msc, 1700000000123)
        self.assertEqual(self.record.normalizer_version, ledger.NORMALIZER_VERSION)

    def test_fingerprint_fields_are_frozen_and_slotted(self):
        for field in fields(self.record):
            with self.subTest(field=field.name), self.assertRaises(FrozenInstanceError):
                setattr(self.record, field.name, getattr(self.record, field.name))
        self.assertFalse(hasattr(self.record, "__dict__"))

    def test_large_ticket_and_execution_milliseconds_preserve_integer_precision(self):
        record = self.fingerprint(deal_ticket=2**64 + 55, executed_at_msc=2**63 + 7)
        self.assertEqual(record.deal_ticket, 18446744073709551671)
        self.assertEqual(record.executed_at_msc, 9223372036854775815)
        self.assertEqual(self.fingerprint(executed_at_msc=0).executed_at_msc, 0)

    def test_equivalent_fingerprints_compare_equal(self):
        other = self.fingerprint()
        self.assertIsNot(other, self.record)
        self.assertEqual(other, self.record)
        self.assertEqual(hash(other), hash(self.record))

    def test_integer_fingerprint_fields_reject_wrong_types_including_bools(self):
        for name in ("deal_ticket", "executed_at_msc", "normalizer_version"):
            for value in (None, True, False, "1", 1.0, object()):
                with self.subTest(field=name, value=value), self.assertRaises(TypeError):
                    self.fingerprint(**{name: value})

    def test_fingerprint_integer_domains_are_validated(self):
        for name, values in (("deal_ticket", (0, -1)), ("executed_at_msc", (-1,)),
                             ("normalizer_version", (0, -1))):
            for value in values:
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    self.fingerprint(**{name: value})

    def test_payload_hash_requires_string(self):
        for value in (None, False, 123, self.deal.payload_hash.encode(), object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.fingerprint(payload_hash=value)

    def test_payload_hash_requires_canonical_sha256_hexdigest_without_coercion(self):
        for value in ("", "f" * 63, "f" * 65, "g" * 64, "A" * 64, " " + self.deal.payload_hash):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fingerprint(payload_hash=value)

    def test_version_is_preserved_without_restricting_to_current_normalizer(self):
        other = self.fingerprint(normalizer_version=2)
        self.assertEqual(other.normalizer_version, 2)
        self.assertNotEqual(other, self.record)

    def test_nonempty_batch_preserves_exact_six_supplied_fields(self):
        records = (self.record,)
        result = self.batch(records=records)
        self.assertEqual(tuple(field.name for field in fields(result)),
                         ("ingestion_id", "account", "requested_range", "assurance", "retrieved", "records"))
        self.assertIs(result.account, self.account)
        self.assertIs(result.requested_range, self.requested_range)
        self.assertIs(result.ingestion_id, self.ingestion_id)
        self.assertIs(result.records, records)
        self.assertEqual(result.retrieved, 1)
        self.assertIs(result.assurance, ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)

    def test_empty_batch_preserves_explicit_assurance_without_inference(self):
        for assurance in ledger.SourceHistoryAssurance:
            with self.subTest(assurance=assurance):
                batch = self.batch(retrieved=0, records=(), assurance=assurance)
                self.assertEqual((batch.retrieved, batch.records), (0, ()))
                self.assertIs(batch.assurance, assurance)

    def test_batch_fields_are_frozen_and_slotted(self):
        batch = self.batch()
        for field in fields(batch):
            with self.subTest(field=field.name), self.assertRaises(FrozenInstanceError):
                setattr(batch, field.name, getattr(batch, field.name))
        self.assertFalse(hasattr(batch, "__dict__"))
        with self.assertRaises(TypeError):
            batch.records[0] = self.record

    def test_duplicate_fingerprints_and_conflicting_same_ticket_are_preserved(self):
        changed = replace(self.record, payload_hash="f" * 64)
        records = (self.record, self.record, changed)
        batch = self.batch(retrieved=3, records=records)
        self.assertIs(batch.records, records)
        self.assertEqual(len(batch.records), 3)
        self.assertEqual([record.deal_ticket for record in batch.records], [9001, 9001, 9001])
        self.assertEqual(batch.records[2].payload_hash, "f" * 64)
        self.assertIs(batch.assurance, ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)

    def test_non_utc_range_preserves_canonical_endpoints_and_microseconds(self):
        zone = timezone(timedelta(hours=5, minutes=30))
        requested = ledger.RecoveryRange(datetime(2026, 11, 1, 15, 30, 0, 123456, tzinfo=zone),
                                         datetime(2026, 11, 1, 15, 50, 0, 654321, tzinfo=zone))
        batch = self.batch(requested_range=requested)
        self.assertIs(batch.requested_range, requested)
        self.assertEqual(batch.requested_range.from_time,
                         datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(batch.requested_range.to_time,
                         datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc))
        self.assertIs(batch.requested_range.from_time.tzinfo, timezone.utc)
        self.assertIs(batch.requested_range.to_time.tzinfo, timezone.utc)

    def test_zero_length_range_is_preserved_without_record_membership_evaluation(self):
        instant = self.requested_range.from_time
        requested = ledger.RecoveryRange(instant, instant)
        batch = self.batch(requested_range=requested)
        self.assertIs(batch.requested_range, requested)
        self.assertIs(batch.records[0], self.record)
        self.assertEqual(batch.records[0].executed_at_msc, 1700000000123)

    def test_partial_fingerprint_coverage_is_representable(self):
        for records in ((), (self.record,)):
            for assurance in ledger.SourceHistoryAssurance:
                with self.subTest(records=records, assurance=assurance):
                    batch = self.batch(retrieved=3, records=records, assurance=assurance)
                    self.assertEqual(batch.retrieved, 3)
                    self.assertIs(batch.records, records)
                    self.assertIs(batch.assurance, assurance)

    def test_more_fingerprints_than_retrieved_is_structurally_invalid(self):
        for retrieved, records in ((0, (self.record,)), (1, (self.record, self.record))):
            with self.subTest(retrieved=retrieved), self.assertRaises(ValueError):
                self.batch(retrieved=retrieved, records=records)

    def test_retrieved_rejects_wrong_types_including_bools(self):
        for value in (True, False, None, "1", 1.0, object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.batch(retrieved=value)

    def test_retrieved_rejects_negative_values(self):
        with self.assertRaises(ValueError):
            self.batch(retrieved=-1, records=())

    def test_batch_binding_fields_reject_wrong_types_without_coercion(self):
        invalid = {
            "ingestion_id": (None, False, UUID(int=1), 123),
            "account": (None, {}, object(), str(self.account.mt5_account_id)),
            "requested_range": (None, {}, object(), (self.requested_range.from_time, self.requested_range.to_time)),
            "assurance": (None, False, "CALL_COMPLETED_UNVERIFIED", ledger.ReconciliationStatus.APPROVED),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(field=name, value=value), self.assertRaises(TypeError):
                    self.batch(**{name: value})

    def test_ingestion_id_rejects_blank_but_preserves_non_uuid_string_exactly(self):
        for value in ("", " \t\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.batch(ingestion_id=value)
        value = " caller batch / not-a-uuid "
        self.assertIs(self.batch(ingestion_id=value).ingestion_id, value)

    def test_records_require_tuple_without_silent_conversion(self):
        for value in (None, [self.record], {self.record}, {}, iter((self.record,))):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.batch(records=value)

    def test_records_require_fingerprint_elements(self):
        for value in (None, self.deal, {}, "record", object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.batch(records=(value,))

    def test_equivalent_batches_compare_equal(self):
        first = self.batch()
        second = self.batch(account=replace(self.account), requested_range=replace(self.requested_range),
                            records=(replace(self.record),))
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_public_import_executes_real_ledger_without_loading_mt5(self):
        package_dir = Path(__file__).resolve().parents[2] / "src" / "aiomql"
        script = textwrap.dedent("""
            import sys
            from types import ModuleType

            parent = ModuleType("aiomql")
            parent.__path__ = [sys.argv[1]]
            sys.modules["aiomql"] = parent
            from aiomql.ledger import SourceHistoryAssurance, SourceRecordFingerprint, SourceBatchEvidence, __all__
            from aiomql.ledger.reconciliation_evidence import SourceBatchEvidence as DirectBatch

            assert {"SourceHistoryAssurance", "SourceRecordFingerprint", "SourceBatchEvidence"} <= set(__all__)
            assert SourceBatchEvidence is DirectBatch
            assert SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED.value == "CALL_COMPLETED_UNVERIFIED"
            assert SourceRecordFingerprint.__name__ == "SourceRecordFingerprint"
            assert "MetaTrader5" not in sys.modules
        """)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(package_dir)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
