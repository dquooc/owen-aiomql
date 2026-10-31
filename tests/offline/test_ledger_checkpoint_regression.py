"""Final checkpoint regression semantics against temporary SQLite files."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from test_deal_normalizer import ledger


class LedgerCheckpointRegressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.repo.close)
        self.account = ledger.AccountRef(
            mt5_account_id=UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"),
            broker_namespace="broker-a", server="server-live", login=123456,
            company_observed="Broker A", trade_mode=2)
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.repo.register_account(self.account)
        self.repo.advance_checkpoint(self.account, self.start)
        self.current = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=10))
        self.earlier = self.start + timedelta(minutes=5)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return tuple(conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
                         for table in ("ledger_accounts", "ledger_checkpoints", "deals", "deal_observations"))

    def test_earlier_boundary_raises_regression(self):
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.earlier)

    def test_regression_performs_no_writes_and_preserves_exact_checkpoint(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER deny_{operation.lower()} BEFORE {operation} ON ledger_checkpoints "
                             "BEGIN SELECT RAISE(ABORT, 'regression must not write'); END")
        before = self.snapshot()
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.earlier)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), self.current)

    def test_regression_error_remains_a_value_error(self):
        with self.assertRaises(ledger.CheckpointRegressionError) as raised:
            self.repo.advance_checkpoint(self.account, self.earlier)
        self.assertIsInstance(raised.exception, ValueError)

    def test_regression_message_identifies_account_and_both_boundaries(self):
        with self.assertRaises(ledger.CheckpointRegressionError) as raised:
            self.repo.advance_checkpoint(self.account, self.earlier)
        message = str(raised.exception)
        self.assertIn(str(self.account.mt5_account_id), message)
        self.assertIn("2026-11-01T10:10:00", message)
        self.assertIn("2026-11-01T10:05:00", message)

    def test_one_microsecond_regression_is_rejected(self):
        current = self.repo.advance_checkpoint(self.account, self.current.completed_through + timedelta(microseconds=1))
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.current.completed_through)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), current)

    def test_different_offset_earlier_instant_is_regression(self):
        requested = datetime(2026, 11, 1, 17, 5, tzinfo=timezone(timedelta(hours=7)))
        before = self.snapshot()
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(self.snapshot(), before)

    def test_equivalent_offset_instant_is_idempotent(self):
        requested = datetime(2026, 11, 1, 17, 10, tzinfo=timezone(timedelta(hours=7)))
        before = self.snapshot()
        self.assertEqual(self.repo.advance_checkpoint(self.account, requested), self.current)
        self.assertEqual(self.snapshot(), before)

    def test_regression_after_reopen_preserves_persisted_state(self):
        self.repo.close()
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        before = self.snapshot()
        with self.assertRaises(ledger.CheckpointRegressionError):
            reopened.advance_checkpoint(self.account, self.earlier)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(reopened.get_checkpoint(self.account.mt5_account_id), self.current)

    def test_regression_does_not_change_other_account(self):
        other = replace(self.account, mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"), login=555)
        self.repo.register_account(other)
        checkpoint = self.repo.advance_checkpoint(other, self.start)
        before = self.snapshot()
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.earlier)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.repo.get_checkpoint(other.mt5_account_id), checkpoint)

    def test_valid_forward_advance_succeeds_after_regression(self):
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.earlier)
        requested = self.start + timedelta(minutes=20)
        result = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(result.completed_through, requested)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)

    def test_offset_one_microsecond_later_is_forward(self):
        requested = datetime(2026, 11, 1, 17, 10, 0, 1, tzinfo=timezone(timedelta(hours=7)))
        result = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(result.completed_through, datetime(2026, 11, 1, 10, 10, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)

    def test_storage_failure_is_not_classified_as_regression(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_advance AFTER UPDATE ON ledger_checkpoints "
                         "BEGIN SELECT RAISE(FAIL, 'storage failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError) as raised:
            self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=20))
        self.assertNotIsInstance(raised.exception, ledger.CheckpointRegressionError)
        self.assertEqual(self.snapshot(), before)

    def test_sqlite_lock_error_is_not_classified_as_regression(self):
        before = self.snapshot()
        # Use real repository connections; shorten only this test connection's
        # lock timeout so lock-error propagation is deterministic and immediate.
        with closing(self.repo._connect()) as holder, closing(self.repo._connect()) as blocked:
            blocked.execute("PRAGMA busy_timeout = 0")
            holder.execute("BEGIN IMMEDIATE")
            try:
                with patch.object(self.repo, "_connect", return_value=blocked):
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=20))
                self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                self.assertNotIsInstance(raised.exception, ledger.CheckpointRegressionError)
            finally:
                holder.rollback()
        self.assertEqual(self.snapshot(), before)
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=20))
        self.assertEqual(result.completed_through, self.start + timedelta(minutes=20))


if __name__ == "__main__":
    unittest.main()
