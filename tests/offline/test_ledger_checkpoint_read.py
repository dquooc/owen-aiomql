"""Checkpoint reads from real temporary SQLite files, without MT5."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import UUID

from test_deal_normalizer import ledger, raw_deal


class LedgerCheckpointReadTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger.SQLiteDealRepository, "get_checkpoint"),
                        "SQLite repository must implement get_checkpoint")
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
        self.completed = "2026-10-31T10:00:00.123456+00:00"
        self.updated = "2026-11-01T09:00:00.654321+00:00"
        self.repo.register_account(self.account)

    def insert_checkpoint(self, account=None, **changes):
        values = dict(completed_through=self.completed, updated_at=self.updated)
        values.update(changes)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT OR REPLACE INTO ledger_checkpoints "
                         "(mt5_account_id, completed_through, updated_at) VALUES (?, ?, ?)",
                         (str((account or self.account).mt5_account_id),
                          values["completed_through"], values["updated_at"]))

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                         for table in ("ledger_accounts", "ledger_checkpoints", "deals", "deal_observations"))

    def test_registered_account_without_checkpoint_returns_none_without_writes(self):
        before = self.snapshot()
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))
        self.assertEqual(self.snapshot(), before)

    def test_existing_checkpoint_returns_typed_model_without_writes(self):
        self.insert_checkpoint()
        before = self.snapshot()
        checkpoint = self.repo.get_checkpoint(self.account.mt5_account_id)
        self.assertIsInstance(checkpoint, ledger.LedgerCheckpoint)
        self.assertEqual(checkpoint.mt5_account_id, self.account.mt5_account_id)
        self.assertEqual(checkpoint.completed_through,
                         datetime(2026, 10, 31, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(checkpoint.updated_at,
                         datetime(2026, 11, 1, 9, 0, 0, 654321, tzinfo=timezone.utc))
        self.assertEqual(self.snapshot(), before)

    def test_utc_timestamps_decode_to_aware_utc(self):
        self.insert_checkpoint()
        checkpoint = self.repo.get_checkpoint(self.account.mt5_account_id)
        self.assertIs(checkpoint.completed_through.tzinfo, timezone.utc)
        self.assertIs(checkpoint.updated_at.tzinfo, timezone.utc)

    def test_offset_timestamps_are_canonicalized_without_rewriting_storage(self):
        self.insert_checkpoint(completed_through="2026-10-31T17:00:00+07:00",
                               updated_at="2026-11-01T04:00:00-05:00")
        before = self.snapshot()
        checkpoint = self.repo.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(checkpoint.completed_through, datetime(2026, 10, 31, 10, tzinfo=timezone.utc))
        self.assertEqual(checkpoint.updated_at, datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
        self.assertIs(checkpoint.completed_through.tzinfo, timezone.utc)
        self.assertIs(checkpoint.updated_at.tzinfo, timezone.utc)
        self.assertEqual(self.snapshot(), before)

    def test_microseconds_are_preserved(self):
        self.insert_checkpoint()
        checkpoint = self.repo.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(checkpoint.completed_through.microsecond, 123456)
        self.assertEqual(checkpoint.updated_at.microsecond, 654321)

    def test_unknown_account_raises_identity_error_without_writes(self):
        self.insert_checkpoint()
        before = self.snapshot()
        with self.assertRaises(ledger.AccountIdentityError):
            self.repo.get_checkpoint(self.other.mt5_account_id)
        self.assertEqual(self.snapshot(), before)

    def test_nil_uuid_is_rejected(self):
        with self.assertRaises(ValueError):
            self.repo.get_checkpoint(UUID(int=0))

    def test_invalid_uuid_types_are_rejected(self):
        self.insert_checkpoint()
        for value in (str(self.account.mt5_account_id), 123, None, False):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.repo.get_checkpoint(value)

    def test_accounts_read_only_their_own_checkpoint(self):
        self.repo.register_account(self.other)
        self.insert_checkpoint()
        self.insert_checkpoint(self.other, completed_through="2026-10-30T08:00:00+00:00")
        first = self.repo.get_checkpoint(self.account.mt5_account_id)
        second = self.repo.get_checkpoint(self.other.mt5_account_id)
        self.assertEqual(first.mt5_account_id, self.account.mt5_account_id)
        self.assertEqual(first.completed_through,
                         datetime(2026, 10, 31, 10, 0, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(second.mt5_account_id, self.other.mt5_account_id)
        self.assertEqual(second.completed_through, datetime(2026, 10, 30, 8, tzinfo=timezone.utc))

    def test_other_accounts_checkpoint_does_not_hide_missing_checkpoint(self):
        self.repo.register_account(self.other)
        self.insert_checkpoint(self.other)
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))

    def test_closed_repository_raises_runtime_error(self):
        self.repo.close()
        with self.assertRaises(RuntimeError):
            self.repo.get_checkpoint(self.account.mt5_account_id)

    def test_malformed_or_naive_persisted_timestamps_fail_without_repair(self):
        for name in ("completed_through", "updated_at"):
            for value in ("not-a-timestamp", "2026-02-30T10:00:00+00:00",
                          "2026-10-31T10:00:00", "2026-10-31", b"2026-10-31T10:00:00+00:00"):
                with self.subTest(field=name, value=value):
                    self.insert_checkpoint(**{name: value})
                    before = self.snapshot()
                    with self.assertRaises((ValueError, TypeError)):
                        self.repo.get_checkpoint(self.account.mt5_account_id)
                    self.assertEqual(self.snapshot(), before)

    def test_checkpoint_reads_do_not_change_existing_account_and_deal_apis(self):
        deal = ledger.normalize_deal(self.account, raw_deal(),
                                     datetime(2026, 11, 1, tzinfo=timezone.utc), "read-test")
        self.assertEqual(self.repo.ingest(self.account, deal).outcome, ledger.IngestOutcome.INSERTED)
        self.insert_checkpoint()
        self.repo.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(self.repo.resolve_account(broker_namespace="broker-a", server="server-live",
                                                   login=123456), self.account)
        self.assertEqual(self.repo.get(self.account.mt5_account_id, deal.deal_ticket), deal)
        self.assertEqual(self.repo.list_by_order(self.account.mt5_account_id, deal.order_ticket), (deal,))
        self.assertEqual(self.repo.list_by_position(self.account.mt5_account_id, deal.position_id), (deal,))

    def test_checkpoint_is_readable_after_reopen(self):
        self.insert_checkpoint()
        self.repo.close()
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        checkpoint = reopened.get_checkpoint(self.account.mt5_account_id)
        self.assertEqual(checkpoint, ledger.LedgerCheckpoint(
            self.account.mt5_account_id,
            datetime(2026, 10, 31, 10, 0, 0, 123456, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 9, 0, 0, 654321, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
