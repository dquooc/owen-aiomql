"""Forward advancement and idempotency against real offline SQLite files."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import UUID

from test_deal_normalizer import ledger


class LedgerCheckpointAdvanceTests(unittest.TestCase):
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
        self.other = replace(self.account,
                             mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"), login=555)
        self.start = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
        self.repo.register_account(self.account)
        self.initial = self.repo.advance_checkpoint(self.account, self.start)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return tuple(conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
                         for table in ("ledger_accounts", "ledger_checkpoints", "deals", "deal_observations"))

    def reopen(self):
        self.repo.close()
        self.repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.repo.close)

    def test_forward_boundary_advances(self):
        requested = datetime(2026, 10, 31, 10, 5, tzinfo=timezone.utc)
        result = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(result.completed_through, requested)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)

    def test_multiple_forward_advances_reach_latest_boundary(self):
        for minute in (5, 10, 30):
            requested = datetime(2026, 10, 31, 10, minute, tzinfo=timezone.utc)
            result = self.repo.advance_checkpoint(self.account, requested)
            self.assertEqual(result.completed_through, requested)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id).completed_through,
                         datetime(2026, 10, 31, 10, 30, tzinfo=timezone.utc))

    def test_returned_value_matches_exact_storage(self):
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT * FROM ledger_checkpoints WHERE mt5_account_id = ?",
                               (str(self.account.mt5_account_id),)).fetchone()
        self.assertEqual(row, (str(self.account.mt5_account_id), "2026-10-31T10:05:00.000000+00:00",
                               result.updated_at.isoformat(timespec="microseconds")))

    def test_forward_advance_generates_current_utc_audit_time(self):
        # Simulate an older persisted checkpoint without sleeping or mocking time.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE ledger_checkpoints SET updated_at = ? WHERE mt5_account_id = ?",
                         ("2020-01-01T00:00:00.123456+00:00", str(self.account.mt5_account_id)))
        previous = self.repo.get_checkpoint(self.account.mt5_account_id)
        before = datetime.now(timezone.utc)
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        after = datetime.now(timezone.utc)
        self.assertGreater(result.updated_at, previous.updated_at)
        self.assertGreaterEqual(result.updated_at, before)
        self.assertLessEqual(result.updated_at, after)
        self.assertIs(result.updated_at.tzinfo, timezone.utc)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)

    def test_same_boundary_preserves_row_and_audit_time_without_any_write(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER deny_{operation.lower()} BEFORE {operation} ON ledger_checkpoints "
                             "BEGIN SELECT RAISE(ABORT, 'same boundary must not write'); END")
        before = self.snapshot()
        result = self.repo.advance_checkpoint(self.account, self.start)
        self.assertEqual(result, self.initial)
        self.assertEqual(self.snapshot(), before)

    def test_equivalent_offset_instant_is_idempotent(self):
        before = self.snapshot()
        requested = datetime(2026, 10, 31, 17, tzinfo=timezone(timedelta(hours=7)))
        result = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(result, self.initial)
        self.assertEqual(self.snapshot(), before)

    def test_one_microsecond_is_forward_progress(self):
        requested = datetime(2026, 10, 31, 10, 0, 0, 1, tzinfo=timezone.utc)
        result = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(result.completed_through, requested)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)

    def test_advancement_does_not_change_other_accounts_checkpoint(self):
        self.repo.register_account(self.other)
        other = self.repo.advance_checkpoint(self.other, self.start + timedelta(hours=1))
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=15))
        self.assertEqual(result.completed_through, datetime(2026, 10, 31, 10, 15, tzinfo=timezone.utc))
        self.assertEqual(self.repo.get_checkpoint(self.other.mt5_account_id), other)

    def test_forward_advance_survives_reopen(self):
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        self.reopen()
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), result)

    def test_same_value_after_reopen_preserves_checkpoint(self):
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        self.reopen()
        before = self.snapshot()
        self.assertEqual(self.repo.advance_checkpoint(self.account, result.completed_through), result)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_account_is_rejected_without_registration(self):
        before = self.snapshot()
        with self.assertRaises(ledger.AccountIdentityError):
            self.repo.advance_checkpoint(self.other, self.start + timedelta(minutes=5))
        self.assertEqual(self.snapshot(), before)

    def test_conflicting_identity_is_rejected_before_same_or_forward_success(self):
        before = self.snapshot()
        for changes in ({"broker_namespace": "other-broker"}, {"server": "other-server"}, {"login": 999}):
            for requested in (self.start, self.start + timedelta(minutes=5)):
                with self.subTest(changes=changes, requested=requested), self.assertRaises(ledger.AccountIdentityError):
                    self.repo.advance_checkpoint(replace(self.account, **changes), requested)
                self.assertEqual(self.snapshot(), before)

    def test_failed_update_rolls_back_and_repository_remains_usable(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_advance AFTER UPDATE ON ledger_checkpoints "
                         "BEGIN SELECT RAISE(FAIL, 'deny advancement'); END")
        before = self.snapshot()
        requested = self.start + timedelta(minutes=5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), self.initial)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TRIGGER deny_advance")
        result = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(result.completed_through, requested)

    def test_advancement_updates_one_existing_row_without_replacement(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            original_rowid = conn.execute("SELECT rowid FROM ledger_checkpoints").fetchone()[0]
            for operation in ("INSERT", "DELETE"):
                conn.execute(f"CREATE TRIGGER deny_{operation.lower()} BEFORE {operation} ON ledger_checkpoints "
                             "BEGIN SELECT RAISE(ABORT, 'must update existing row'); END")
        for minute in (5, 10, 10, 30):
            self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=minute))
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT rowid, completed_through FROM ledger_checkpoints").fetchall()
        self.assertEqual(rows, [(original_rowid, "2026-10-31T10:30:00.000000+00:00")])

    def test_backward_request_keeps_checkpoint_unchanged(self):
        before = self.snapshot()
        with self.assertRaises(NotImplementedError):
            self.repo.advance_checkpoint(self.account, self.start - timedelta(minutes=1))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), self.initial)

    def test_observed_metadata_is_unchanged_after_forward_and_same_calls(self):
        caller = replace(self.account, company_observed="New label", trade_mode=1)
        requested = self.start + timedelta(minutes=5)
        self.repo.advance_checkpoint(caller, requested)
        self.repo.advance_checkpoint(caller, requested)
        self.assertEqual(self.repo.resolve_account(broker_namespace="broker-a", server="server-live",
                                                   login=123456), self.account)

    def test_stored_offset_is_compared_as_instant_for_forward_advance(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE ledger_checkpoints SET completed_through = ? WHERE mt5_account_id = ?",
                         ("2026-10-31T17:00:00+07:00", str(self.account.mt5_account_id)))
        result = self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        self.assertEqual(result.completed_through, datetime(2026, 10, 31, 10, 5, tzinfo=timezone.utc))

    def test_stored_offset_same_instant_is_not_rewritten(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE ledger_checkpoints SET completed_through = ? WHERE mt5_account_id = ?",
                         ("2026-10-31T17:00:00+07:00", str(self.account.mt5_account_id)))
        before = self.snapshot()
        result = self.repo.advance_checkpoint(self.account, self.start)
        self.assertEqual(result, self.initial)
        self.assertEqual(self.snapshot(), before)

    def test_malformed_persisted_checkpoint_is_not_overwritten(self):
        for field in ("completed_through", "updated_at"):
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute("UPDATE ledger_checkpoints SET completed_through = ?, updated_at = ?",
                                 ("not-a-timestamp" if field == "completed_through" else self.start.isoformat(),
                                  "not-a-timestamp" if field == "updated_at" else self.initial.updated_at.isoformat()))
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    self.repo.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
