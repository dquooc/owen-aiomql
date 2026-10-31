"""First checkpoint creation using real SQLite files, without MT5."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import UUID

from test_deal_normalizer import ledger, raw_deal
from test_ledger_checkpoint import OffsetlessTimezone, UnusableTimezone


class LedgerCheckpointCreateTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger.SQLiteDealRepository, "advance_checkpoint"),
                        "SQLite repository must implement initial checkpoint creation")
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
        self.other = replace(self.account,
                             mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"), login=555)
        self.completed = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                         for table in ("ledger_accounts", "ledger_checkpoints", "deals", "deal_observations"))

    def test_create_first_checkpoint_with_current_utc_audit_time(self):
        self.repo.register_account(self.account)
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))
        before = datetime.now(timezone.utc)
        checkpoint = self.repo.advance_checkpoint(self.account, self.completed)
        after = datetime.now(timezone.utc)
        self.assertIsInstance(checkpoint, ledger.LedgerCheckpoint)
        self.assertEqual(checkpoint.mt5_account_id, self.account.mt5_account_id)
        self.assertEqual(checkpoint.completed_through, self.completed)
        self.assertIs(checkpoint.updated_at.tzinfo, timezone.utc)
        self.assertGreaterEqual(checkpoint.updated_at, before)
        self.assertLessEqual(checkpoint.updated_at, after)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), checkpoint)

    def test_returned_value_matches_exact_persisted_timestamps(self):
        self.repo.register_account(self.account)
        checkpoint = self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), checkpoint)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                               "FROM ledger_checkpoints WHERE mt5_account_id = ?",
                               (str(self.account.mt5_account_id),)).fetchone()
        self.assertEqual(row, (str(self.account.mt5_account_id), "2026-10-31T10:00:00.000000+00:00",
                               checkpoint.updated_at.isoformat(timespec="microseconds")))

    def test_creation_normalizes_offset_to_utc(self):
        self.repo.register_account(self.account)
        requested = datetime(2026, 10, 31, 17, tzinfo=timezone(timedelta(hours=7)))
        checkpoint = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(checkpoint.completed_through, self.completed)
        self.assertIs(checkpoint.completed_through.tzinfo, timezone.utc)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), checkpoint)

    def test_creation_preserves_microseconds(self):
        self.repo.register_account(self.account)
        requested = datetime(2026, 10, 31, 17, 0, 0, 123456, tzinfo=timezone(timedelta(hours=7)))
        checkpoint = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(checkpoint.completed_through,
                         datetime(2026, 10, 31, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), checkpoint)

    def test_naive_boundary_is_rejected_without_changes(self):
        self.repo.register_account(self.account)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.repo.advance_checkpoint(self.account, datetime(2026, 10, 31, 10))
        self.assertEqual(self.snapshot(), before)

    def test_non_datetime_boundary_is_rejected_without_changes(self):
        self.repo.register_account(self.account)
        before = self.snapshot()
        for value in ("2026-10-31T10:00:00+00:00", 123, None, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.repo.advance_checkpoint(self.account, value)
            self.assertEqual(self.snapshot(), before)

    def test_unknown_account_is_not_implicitly_registered(self):
        before = self.snapshot()
        with self.assertRaises(ledger.AccountIdentityError):
            self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.repo.resolve_account(broker_namespace="broker-a", server="server-live", login=123456))

    def test_immutable_identity_mismatches_are_rejected_without_changes(self):
        self.repo.register_account(self.account)
        before = self.snapshot()
        for changes in ({"server": "other-server"}, {"login": 999},
                        {"broker_namespace": "other-broker"}, {"mt5_account_id": self.other.mt5_account_id}):
            with self.subTest(changes=changes), self.assertRaises(ledger.AccountIdentityError):
                self.repo.advance_checkpoint(replace(self.account, **changes), self.completed)
            self.assertEqual(self.snapshot(), before)

    def test_creation_is_scoped_to_supplied_account(self):
        self.repo.register_account(self.account)
        self.repo.register_account(self.other)
        checkpoint = self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), checkpoint)
        self.assertIsNone(self.repo.get_checkpoint(self.other.mt5_account_id))

    def test_created_checkpoint_survives_reopen(self):
        self.repo.register_account(self.account)
        checkpoint = self.repo.advance_checkpoint(self.account, self.completed)
        self.repo.close()
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_checkpoint(self.account.mt5_account_id), checkpoint)

    def test_creation_leaves_one_row_for_account(self):
        self.repo.register_account(self.account)
        self.repo.advance_checkpoint(self.account, self.completed)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger_checkpoints WHERE mt5_account_id = ?",
                                          (str(self.account.mt5_account_id),)).fetchone()[0], 1)

    def test_existing_checkpoint_rejects_backward_request_without_overwrite(self):
        self.repo.register_account(self.account)
        original = self.repo.advance_checkpoint(self.account, self.completed)
        before = self.snapshot()
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, self.completed - timedelta(days=1))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), original)

    def test_failed_creation_rolls_back_insert_and_repository_remains_usable(self):
        self.repo.register_account(self.account)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            # FAIL after INSERT leaves statement changes for the enclosing
            # transaction to roll back, rather than aborting before insertion.
            conn.execute("CREATE TRIGGER deny_checkpoint AFTER INSERT ON ledger_checkpoints "
                         "BEGIN SELECT RAISE(FAIL, 'deny checkpoint'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TRIGGER deny_checkpoint")
        checkpoint = self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), checkpoint)

    def test_creation_preserves_existing_deal_functionality(self):
        self.repo.register_account(self.account)
        deal = ledger.normalize_deal(self.account, raw_deal(), self.completed, "create-test")
        self.assertEqual(self.repo.ingest(self.account, deal).outcome, ledger.IngestOutcome.INSERTED)
        self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.repo.get(self.account.mt5_account_id, deal.deal_ticket), deal)
        self.assertEqual(self.repo.ingest(self.account, deal).outcome, ledger.IngestOutcome.DUPLICATE)
        self.assertEqual(self.repo.list_by_order(self.account.mt5_account_id, deal.order_ticket), (deal,))

    def test_creation_does_not_reregister_or_change_observed_metadata(self):
        self.repo.register_account(self.account)
        caller = replace(self.account, company_observed="New label", trade_mode=1)
        self.repo.advance_checkpoint(caller, self.completed)
        self.assertEqual(self.repo.resolve_account(broker_namespace="broker-a", server="server-live",
                                                   login=123456), self.account)

    def test_invalid_account_argument_is_rejected_without_changes(self):
        before = self.snapshot()
        for value in (None, str(self.account.mt5_account_id), self.account.mt5_account_id, {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.repo.advance_checkpoint(value, self.completed)
            self.assertEqual(self.snapshot(), before)

    def test_offsetless_or_unusable_timezone_is_rejected_without_changes(self):
        self.repo.register_account(self.account)
        before = self.snapshot()
        for zone in (OffsetlessTimezone(), UnusableTimezone()):
            with self.subTest(zone=type(zone).__name__), self.assertRaises(ValueError):
                self.repo.advance_checkpoint(self.account, datetime(2026, 10, 31, 10, tzinfo=zone))
            self.assertEqual(self.snapshot(), before)

    def test_closed_repository_rejects_creation(self):
        self.repo.register_account(self.account)
        self.repo.close()
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.repo.advance_checkpoint(self.account, self.completed)
        self.assertEqual(self.snapshot(), before)

    def test_completed_boundary_may_follow_audit_time(self):
        self.repo.register_account(self.account)
        future = datetime.now(timezone.utc) + timedelta(days=365)
        checkpoint = self.repo.advance_checkpoint(self.account, future)
        self.assertEqual(checkpoint.completed_through, future)
        self.assertLess(checkpoint.updated_at, checkpoint.completed_through)


if __name__ == "__main__":
    unittest.main()
