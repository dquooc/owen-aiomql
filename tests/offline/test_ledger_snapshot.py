"""A4.5: file-backed SQLite snapshot evidence, without SDK imports."""

from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import patch
import unittest
from uuid import UUID

import test_deal_ingestion as fixtures
from test_deal_normalizer import ledger, raw_deal


class LedgerSnapshotTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.DealIngestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo, self.account, self.path = fixture.repo, fixture.account, fixture.path
        self.start = fixture.start
        self.msc = 1700000000123
        self.instant = self.start + timedelta(milliseconds=123)
        self.range = ledger.RecoveryRange(self.start, fixture.end)

    def store(self, account=None, **changes):
        account = account or self.account
        deal = ledger.normalize_deal(account, raw_deal(**changes), self.start, "snapshot-fixture")
        self.repo.ingest(account, deal)
        return deal

    def snapshot(self, account=None, requested=None):
        return self.repo.read_snapshot(account=account or self.account, requested_range=requested or self.range)

    def test_canonical_fingerprints_sorted_numeric_tickets_and_partial_fills(self):
        deals = [self.store(ticket=n) for n in (10, 2, 3)]
        self.repo.ingest(self.account, deals[0])
        result = self.snapshot()
        self.assertIs(result.account, self.account)
        self.assertIs(result.requested_range, self.range)
        self.assertEqual([r.deal_ticket for r in result.records], [2, 3, 10])
        for fingerprint, deal in zip(result.records, sorted(deals, key=lambda d: d.deal_ticket)):
            self.assertEqual(fingerprint, ledger.LedgerRecordFingerprint(
                deal.deal_ticket, deal.payload_hash, deal.executed_at_msc, deal.normalizer_version))
        self.assertEqual(result.conflicts, ())

    def test_account_isolation_same_ticket_and_registered_identity(self):
        other = replace(self.account, mt5_account_id=UUID(int=2), login=999)
        self.repo.register_account(other)
        self.store()
        other_deal = self.store(other, profit=99)
        self.store(other, profit=100)
        self.assertEqual(self.snapshot().conflicts, ())
        self.assertEqual(self.snapshot(other).records[0].payload_hash, other_deal.payload_hash)
        self.assertEqual(len(self.snapshot(other).conflicts), 1)
        for account in (replace(self.account, login=111), replace(self.account, server="other"),
                        replace(self.account, broker_namespace="other"), replace(self.account, mt5_account_id=UUID(int=3))):
            with self.subTest(account=account), self.assertRaises(ledger.AccountIdentityError):
                self.snapshot(account)
        changed_metadata = replace(self.account, company_observed="new", trade_mode=0)
        self.assertIs(self.snapshot(changed_metadata).account, changed_metadata)

    def test_exact_utc_microsecond_bounds_and_timezone_equivalence(self):
        self.store()
        requested = ledger.RecoveryRange(self.instant - timedelta(microseconds=1),
                                         self.instant + timedelta(microseconds=1))
        result = self.snapshot(requested=requested)
        self.assertIs(result.requested_range, requested)
        self.assertEqual(len(result.records), 1)
        zone = timezone(timedelta(hours=7))
        equivalent = ledger.RecoveryRange(requested.from_time.astimezone(zone), requested.to_time.astimezone(zone))
        self.assertEqual(self.snapshot(requested=equivalent), result)

    def test_closed_millisecond_projection_equal_endpoints_and_empty_views(self):
        self.store()
        for start, end, count in ((self.instant, self.instant, 1),
                                   (self.instant + timedelta(microseconds=1), self.range.to_time, 0),
                                   (self.start, self.instant - timedelta(microseconds=1), 0),
                                   (self.instant + timedelta(microseconds=1), self.instant + timedelta(microseconds=1), 0)):
            with self.subTest(start=start, end=end):
                result = self.snapshot(requested=ledger.RecoveryRange(start, end))
                self.assertEqual(len(result.records), count)
                self.assertEqual(result.conflicts, ())

    def test_pre_epoch_and_far_future_integer_precision(self):
        self.store(time_msc=253402300799999)
        point = datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
        self.assertEqual(len(self.snapshot(requested=ledger.RecoveryRange(point, point)).records), 1)
        before = datetime(1960, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(self.snapshot(requested=ledger.RecoveryRange(before, before)).records, ())

    def test_old_conflict_persists_after_current_duplicate_replay(self):
        original = self.store()
        alternative = self.store(profit=99)
        self.repo.ingest(self.account, original)
        result = self.snapshot()
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.original.payload_hash, original.payload_hash)
        self.assertEqual(conflict.alternative.payload_hash, alternative.payload_hash)

    def test_conflict_relevant_when_either_original_or_alternative_time_is_in_range(self):
        outside = self.msc + 86400000 * 2
        self.store(time_msc=outside)
        self.store(time_msc=self.msc)
        result = self.snapshot()
        self.assertEqual(result.records, ())
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].alternative.executed_at_msc, self.msc)
        later = ledger.RecoveryRange(self.start + timedelta(days=2), self.range.to_time + timedelta(days=2))
        self.assertEqual(len(self.snapshot(requested=later).conflicts), 1)
        unrelated = ledger.RecoveryRange(self.start + timedelta(days=4), self.range.to_time + timedelta(days=4))
        self.assertEqual(self.snapshot(requested=unrelated).conflicts, ())

    def test_reads_do_not_mutate_deals_observations_accounts_or_checkpoints(self):
        self.store()
        self.store(profit=99)
        with closing(sqlite3.connect(self.path)) as conn:
            before = tuple(conn.iterdump())
        with patch.object(self.repo, "advance_checkpoint", side_effect=AssertionError("checkpoint write")), \
                patch.object(self.repo, "register_account", side_effect=AssertionError("registration")):
            self.snapshot()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(tuple(conn.iterdump()), before)

    def test_one_read_transaction_sees_consistent_deals_and_conflicts_during_wal_write(self):
        self.store()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        writer = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(writer.close)
        real_connect = self.repo._connect
        statements = []
        fired = []

        class Connection:
            def __init__(inner):
                inner.conn = real_connect()
                inner.conn.set_trace_callback(statements.append)

            def execute(inner, sql, *args):
                if "FROM deal_observations" in sql and not fired:
                    fired.append(True)
                    writer.ingest(self.account, ledger.normalize_deal(
                        self.account, raw_deal(profit=88), self.start, "concurrent"))
                    writer.ingest(self.account, ledger.normalize_deal(
                        self.account, raw_deal(ticket=9002), self.start, "concurrent"))
                return inner.conn.execute(sql, *args)

            def __getattr__(inner, name):
                return getattr(inner.conn, name)

        with patch.object(self.repo, "_connect", side_effect=Connection):
            result = self.snapshot()
        self.assertEqual(fired, [True])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.conflicts, ())
        self.assertEqual(sum(sql == "BEGIN" for sql in statements), 1)
        self.assertFalse(any("IMMEDIATE" in sql for sql in statements))
        later = self.snapshot()
        self.assertEqual((len(later.records), len(later.conflicts)), (2, 1))

    def test_restart_and_separate_instances_read_same_committed_evidence(self):
        self.store()
        self.store(profit=99)
        expected = self.snapshot()
        other = ledger.SQLiteDealRepository(self.path)
        self.assertEqual(other.read_snapshot(account=self.account, requested_range=self.range), expected)
        other.close()
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.read_snapshot(account=self.account, requested_range=self.range), expected)

    def test_corrupt_canonical_and_conflict_rows_raise_instead_of_becoming_empty(self):
        self.store()
        self.store(profit=99)
        for table, column, value in (("deals", "payload_hash", "bad"),
                                      ("deals", "executed_at_msc", "bad"),
                                      ("deal_observations", "raw_payload", "[]"),
                                      ("deal_observations", "raw_payload", "{"),
                                      ("deal_observations", "normalizer_version", 0)):
            with closing(sqlite3.connect(self.path)) as conn:
                original = conn.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                with conn:
                    conn.execute(f"UPDATE {table} SET {column} = ?", (value,))
            with self.subTest(table=table, column=column), self.assertRaises((TypeError, ValueError)):
                self.snapshot()
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute(f"UPDATE {table} SET {column} = ?", (original,))

    def test_storage_failure_and_closed_repository_propagate(self):
        with patch.object(self.repo, "_connect", side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(sqlite3.OperationalError):
                self.snapshot()
        self.repo.close()
        with self.assertRaises(RuntimeError):
            self.snapshot()

    def test_public_evidence_is_strict_frozen_slotted_and_immutable(self):
        self.store()
        self.store(profit=99)
        result = self.snapshot()
        for model in (result, result.records[0], result.conflicts[0]):
            self.assertFalse(hasattr(model, "__dict__"))
            with self.assertRaises(FrozenInstanceError):
                setattr(model, next(iter(model.__dataclass_fields__)), None)
        for field, value in (("account", None), ("requested_range", None),
                             ("records", []), ("records", (object(),)), ("conflicts", []),
                             ("conflicts", (object(),))):
            with self.subTest(field=field), self.assertRaises(TypeError):
                replace(result, **{field: value})
        record = result.records[0]
        for field, value in (("deal_ticket", True), ("payload_hash", "bad"),
                             ("executed_at_msc", -1), ("normalizer_version", 0)):
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                replace(record, **{field: value})
        with self.assertRaises(ValueError):
            replace(result.conflicts[0], alternative=record)
        with self.assertRaises(TypeError):
            replace(result.conflicts[0], original=None)


if __name__ == "__main__":
    unittest.main()
