"""Missing checkpoint timezone edge cases; file-backed SQLite and no MT5."""

from contextlib import closing
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import UUID

from test_deal_normalizer import ledger


class RepeatedHourTimezone(tzinfo):
    """Input-only fixture: the repeated hour uses UTC-4, then UTC-5."""

    def utcoffset(self, dt):
        return timedelta(hours=-5 if dt.fold else -4)

    def dst(self, dt):
        return timedelta(hours=0 if dt.fold else 1)


class LedgerCheckpointTimezoneTests(unittest.TestCase):
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
        self.repo.register_account(self.account)

    def stored_rows(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                                "FROM ledger_checkpoints ORDER BY mt5_account_id").fetchall()

    def test_negative_and_fractional_offsets_are_idempotent_without_writes(self):
        initial = self.repo.advance_checkpoint(self.account, datetime(2026, 11, 1, 10, tzinfo=timezone.utc))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER deny_{operation.lower()} BEFORE {operation} ON ledger_checkpoints "
                             "BEGIN SELECT RAISE(ABORT, 'equivalent offsets must not write'); END")
        before = self.stored_rows()
        equivalents = (
            datetime(2026, 11, 1, 5, tzinfo=timezone(timedelta(hours=-5))),
            datetime(2026, 11, 1, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        )
        for requested in equivalents:
            with self.subTest(requested=requested):
                result = self.repo.advance_checkpoint(self.account, requested)
                self.assertEqual(result, initial)
                self.assertIs(result.completed_through.tzinfo, timezone.utc)
                self.assertEqual(self.stored_rows(), before)

    def test_offset_creation_persists_canonical_utc_microseconds_after_reopen(self):
        requested = datetime(2026, 11, 1, 17, 0, 0, 123456, tzinfo=timezone(timedelta(hours=7)))
        created = self.repo.advance_checkpoint(self.account, requested)
        self.assertEqual(self.stored_rows(), [
            (str(self.account.mt5_account_id), "2026-11-01T10:00:00.123456+00:00",
             created.updated_at.isoformat(timespec="microseconds"))])
        before = self.stored_rows()
        self.repo.close()
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        loaded = reopened.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(loaded, created)
        self.assertEqual(loaded.completed_through,
                         datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertIs(loaded.completed_through.tzinfo, timezone.utc)
        self.assertIs(loaded.updated_at.tzinfo, timezone.utc)
        self.assertEqual(self.stored_rows(), before)

    def test_repeated_local_hour_fold_uses_absolute_instants(self):
        zone = RepeatedHourTimezone()
        first_hour = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        second_hour = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        first = self.repo.advance_checkpoint(self.account, first_hour)
        self.assertEqual(first.completed_through, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        second = self.repo.advance_checkpoint(self.account, second_hour)
        self.assertEqual(second.completed_through, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        self.assertIs(second.completed_through.tzinfo, timezone.utc)
        self.assertIs(second.updated_at.tzinfo, timezone.utc)
        before = self.stored_rows()
        self.assertEqual(self.repo.advance_checkpoint(self.account, second_hour), second)
        with self.assertRaises(ledger.CheckpointRegressionError):
            self.repo.advance_checkpoint(self.account, first_hour)
        self.assertEqual(self.stored_rows(), before)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), second)


if __name__ == "__main__":
    unittest.main()
