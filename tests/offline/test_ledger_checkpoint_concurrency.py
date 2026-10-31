"""Bounded multi-repository checkpoint tests using SQLite writer serialization."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from uuid import UUID

from test_deal_normalizer import ledger


class LedgerCheckpointConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.first = self.repository()
        self.second = self.repository()
        self.account = ledger.AccountRef(
            mt5_account_id=UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"),
            broker_namespace="broker-a", server="server-live", login=123456,
            company_observed="Broker A", trade_mode=2)
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.first.register_account(self.account)
        self.first.advance_checkpoint(self.account, self.start)

    def repository(self):
        repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(repo.close)
        return repo

    def run_writers(self, repositories, boundaries):
        barrier = Barrier(len(repositories))

        def worker(repo, boundary):
            barrier.wait(timeout=5)
            try:
                return repo.advance_checkpoint(self.account, boundary)
            except ledger.CheckpointRegressionError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(repositories)) as pool:
            futures = [pool.submit(worker, repo, boundary) for repo, boundary in zip(repositories, boundaries)]
            return [future.result(timeout=10) for future in futures]

    def assert_persisted(self, boundary):
        checkpoint = self.first.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(checkpoint.completed_through, boundary)
        self.assertEqual(self.second.get_checkpoint(self.account.mt5_account_id), checkpoint)
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT mt5_account_id, completed_through, updated_at FROM ledger_checkpoints").fetchall()
        self.assertEqual(rows, [(str(self.account.mt5_account_id), boundary.isoformat(timespec="microseconds"),
                                 checkpoint.updated_at.isoformat(timespec="microseconds"))])
        return checkpoint

    def test_stale_repository_cannot_overwrite_larger_committed_boundary(self):
        stale = self.first.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(stale.completed_through, self.start)
        larger = self.second.advance_checkpoint(self.account, self.start + timedelta(minutes=20))
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.first.advance_checkpoint(self.account, self.start + timedelta(minutes=10))
        self.assertEqual(self.assert_persisted(self.start + timedelta(minutes=20)), larger)

    def test_concurrent_smaller_and_larger_writers_finish_at_maximum(self):
        smaller, larger = self.start + timedelta(minutes=10), self.start + timedelta(minutes=20)
        outcomes = self.run_writers([self.first, self.second], [smaller, larger])
        if isinstance(outcomes[0], ledger.CheckpointRegressionError):
            self.assertIn(str(self.account.mt5_account_id), str(outcomes[0]))
        else:
            self.assertIsInstance(outcomes[0], ledger.LedgerCheckpoint)
            self.assertEqual(outcomes[0].completed_through, smaller)
        self.assertIsInstance(outcomes[1], ledger.LedgerCheckpoint)
        self.assertEqual(outcomes[1].completed_through, larger)
        self.assertEqual(self.assert_persisted(larger), outcomes[1])

    def test_concurrent_equal_writers_return_same_persisted_checkpoint(self):
        requested = self.start + timedelta(minutes=5)
        outcomes = self.run_writers([self.first, self.second], [requested, requested])
        persisted = self.assert_persisted(requested)
        self.assertEqual(outcomes, [persisted, persisted])

    def test_five_writer_boundaries_finish_at_maximum(self):
        repositories = [self.first, self.second, self.repository(), self.repository(), self.repository()]
        boundaries = [self.start + timedelta(minutes=minute) for minute in (1, 3, 2, 5, 4)]
        outcomes = self.run_writers(repositories, boundaries)
        largest = self.start + timedelta(minutes=5)
        for boundary, outcome in zip(boundaries, outcomes):
            with self.subTest(boundary=boundary):
                if isinstance(outcome, ledger.CheckpointRegressionError):
                    self.assertLess(boundary, largest)
                else:
                    self.assertIsInstance(outcome, ledger.LedgerCheckpoint)
                    self.assertEqual(outcome.completed_through, boundary)
        self.assertIsInstance(outcomes[3], ledger.LedgerCheckpoint)
        self.assertEqual(self.assert_persisted(largest), outcomes[3])


if __name__ == "__main__":
    unittest.main()
