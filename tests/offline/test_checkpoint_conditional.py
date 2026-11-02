"""Atomic expected-boundary advancement using real temporary SQLite files."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta, timezone
import sqlite3
from threading import Barrier
from unittest.mock import patch
import unittest
from uuid import UUID

import test_deal_ingestion as fixtures
from test_deal_normalizer import ledger, raw_deal


class ConditionalCheckpointTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.DealIngestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo, self.account, self.start = fixture.repo, fixture.account, fixture.start
        self.target = self.start + timedelta(minutes=1, microseconds=654321)

    def advance(self, expected=None, target=None, **kwargs):
        return self.repo.advance_checkpoint_if_expected(self.account, target or self.target,
                                                        expected=expected, **kwargs)

    def test_bootstrap_expected_none_creates_exact_target_and_survives_restart(self):
        result = self.advance()
        self.assertEqual(result.completed_through, self.target)
        other = ledger.SQLiteDealRepository(self.repo.path)
        self.addCleanup(other.close)
        self.assertEqual(other.get_checkpoint(self.account.mt5_account_id), result)
        with self.assertRaises(ledger.CheckpointExpectationError):
            self.advance(target=self.target + timedelta(minutes=1))
        self.assertEqual(other.get_checkpoint(self.account.mt5_account_id), result)

    def test_exact_expected_boundary_succeeds_updated_at_is_not_version(self):
        first = self.repo.advance_checkpoint(self.account, self.start)
        expected = replace(first, updated_at=first.updated_at - timedelta(days=1))
        result = self.advance(expected)
        self.assertEqual(result.completed_through, self.target)
        self.assertEqual(result, self.repo.get_checkpoint(self.account.mt5_account_id))

    def test_stale_boundary_and_missing_current_reject_without_any_write(self):
        expected = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.start, self.start)
        with self.assertRaises(ledger.CheckpointExpectationError):
            self.advance(expected)
        current = self.repo.advance_checkpoint(self.account, self.target)
        statements = []
        original = self.repo._connect

        def connect():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(self.repo, "_connect", side_effect=connect), self.assertRaises(ledger.CheckpointExpectationError):
            self.advance(expected, self.target + timedelta(days=1))
        self.assertFalse(any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements))
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), current)

    def test_backward_target_rejects_and_equal_target_retains_audit_time(self):
        current = self.repo.advance_checkpoint(self.account, self.start)
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.advance(current, self.start - timedelta(microseconds=1))
        self.assertEqual(self.advance(current, self.start), current)

    def test_offset_equivalent_expected_and_target_preserve_microseconds(self):
        current = self.repo.advance_checkpoint(self.account, self.start)
        zone = timezone(timedelta(hours=7))
        expected = replace(current, completed_through=current.completed_through.astimezone(zone))
        result = self.advance(expected, self.target.astimezone(zone))
        self.assertEqual(result.completed_through, self.target)
        self.assertIs(result.completed_through.tzinfo, timezone.utc)

    def test_compare_snapshot_and_write_share_single_immediate_transaction(self):
        requested = ledger.RecoveryRange(self.start, self.target)
        snapshot = self.repo.read_snapshot(account=self.account, requested_range=requested)
        statements = []
        original = self.repo._connect

        def connect():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(self.repo, "_connect", side_effect=connect) as connect_spy:
            self.advance(expected_snapshot=snapshot)
        self.assertEqual(connect_spy.call_count, 1)
        begin = statements.index("BEGIN IMMEDIATE")
        read = next(i for i, sql in enumerate(statements) if "SELECT * FROM ledger_checkpoints" in sql)
        snapshot_read = next(i for i, sql in enumerate(statements) if "SELECT * FROM deals" in sql)
        write = next(i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO ledger_checkpoints"))
        self.assertLess(begin, read)
        self.assertLess(read, snapshot_read)
        self.assertLess(snapshot_read, write)
        self.assertLess(write, statements.index("COMMIT"))

    def test_two_repository_instances_racing_same_expectation_have_one_winner(self):
        other = ledger.SQLiteDealRepository(self.repo.path)
        self.addCleanup(other.close)
        gate = Barrier(2)

        def write(repo, target):
            gate.wait(timeout=5)
            try:
                return repo.advance_checkpoint_if_expected(self.account, target, expected=None)
            except ledger.CheckpointExpectationError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, repo, target) for repo, target in
                       ((self.repo, self.target), (other, self.target + timedelta(minutes=1)))]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sum(isinstance(r, ledger.LedgerCheckpoint) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ledger.CheckpointExpectationError) for r in results), 1)
        self.assertIn(other.get_checkpoint(self.account.mt5_account_id), results)

    def test_expected_account_type_and_registered_identity_are_checked(self):
        expected = ledger.LedgerCheckpoint(UUID(int=2), self.start, self.start)
        with self.assertRaises(ledger.CheckpointExpectationError):
            self.advance(expected)
        with self.assertRaises(TypeError):
            self.advance(expected=object())
        for account in (replace(self.account, login=999), replace(self.account, mt5_account_id=UUID(int=3))):
            with self.assertRaises(ledger.AccountIdentityError):
                self.repo.advance_checkpoint_if_expected(account, self.target, expected=None)

    def test_relevant_new_conflict_invalidates_snapshot_without_checkpoint_write(self):
        deal = ledger.normalize_deal(self.account, raw_deal(), self.start, "initial")
        self.repo.ingest(self.account, deal)
        requested = ledger.RecoveryRange(self.start, self.target)
        snapshot = self.repo.read_snapshot(account=self.account, requested_range=requested)
        self.repo.ingest(self.account, ledger.normalize_deal(self.account, raw_deal(profit=99), self.start, "changed"))
        with self.assertRaises(ledger.LedgerSnapshotChangedError):
            self.advance(expected_snapshot=snapshot)
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))

    def test_transaction_failure_rolls_back_and_legacy_monotonic_api_still_works(self):
        with closing(sqlite3.connect(self.repo.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_checkpoint BEFORE INSERT ON ledger_checkpoints "
                         "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.advance()
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))
        with closing(sqlite3.connect(self.repo.path)) as conn, conn:
            conn.execute("DROP TRIGGER deny_checkpoint")
        first = self.repo.advance_checkpoint(self.account, self.start)
        self.assertEqual(self.advance(first).completed_through, self.target)
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.start)


if __name__ == "__main__":
    unittest.main()
