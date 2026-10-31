"""Checkpoint durability across fresh instances and a fresh offline process."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from textwrap import dedent
import unittest
from uuid import UUID

from test_deal_normalizer import ledger, raw_deal


class LedgerCheckpointRestartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.account = ledger.AccountRef(
            mt5_account_id=UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"),
            broker_namespace="broker-a", server="server-live", login=123456,
            company_observed="Broker A", trade_mode=2)
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)

    def repository(self):
        repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(repo.close)
        return repo

    def resolve(self, repo, account=None):
        account = account or self.account
        resolved = repo.resolve_account(broker_namespace=account.broker_namespace,
                                        server=account.server, login=account.login)
        self.assertEqual(resolved, account)
        return resolved

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()

    def test_fresh_process_resolves_reads_and_advances_committed_checkpoint(self):
        first = self.repository()
        first.register_account(self.account)
        first.advance_checkpoint(self.account, self.start)
        committed = first.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        first.close()
        # Import only the established isolated ledger harness, never aiomql's
        # terminal-connected top-level package. The child shares no Python state.
        script = dedent("""
            from datetime import datetime, timezone
            import json
            import sys
            from test_deal_normalizer import ledger

            repo = ledger.SQLiteDealRepository(sys.argv[1])
            try:
                account = repo.resolve_account(broker_namespace="broker-a", server="server-live", login=123456)
                before = repo.get_checkpoint(account.mt5_account_id)
                after = repo.advance_checkpoint(account, datetime(2026, 11, 1, 10, 10, tzinfo=timezone.utc))
                print(json.dumps({
                    "account_id": str(account.mt5_account_id),
                    "before_boundary": before.completed_through.isoformat(),
                    "before_updated_at": before.updated_at.isoformat(),
                    "after_boundary": after.completed_through.isoformat(),
                    "after_updated_at": after.updated_at.isoformat(),
                }))
            finally:
                repo.close()
        """)
        child = subprocess.run([sys.executable, "-B", "-c", script, str(self.path)],
                               cwd=Path(__file__).resolve().parent,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result["account_id"], str(self.account.mt5_account_id))
        self.assertEqual(result["before_boundary"], "2026-11-01T10:05:00+00:00")
        self.assertEqual(result["before_updated_at"], committed.updated_at.isoformat())
        self.assertEqual(result["after_boundary"], "2026-11-01T10:10:00+00:00")
        third = self.repository()
        self.assertIsNot(first, third)
        resolved = self.resolve(third)
        persisted = third.get_checkpoint(resolved.mt5_account_id)
        self.assertEqual(persisted.completed_through, datetime(2026, 11, 1, 10, 10, tzinfo=timezone.utc))
        self.assertEqual(persisted.updated_at.isoformat(), result["after_updated_at"])
        self.assertIs(persisted.completed_through.tzinfo, timezone.utc)
        self.assertIs(persisted.updated_at.tzinfo, timezone.utc)

    def test_repeated_restarts_preserve_one_row_identity_and_no_write_replay(self):
        first = self.repository()
        first.register_account(self.account)
        first.advance_checkpoint(self.account, self.start)
        first.close()
        with self.assertRaises(RuntimeError):
            first.get_checkpoint(self.account.mt5_account_id)
        with self.assertRaises(RuntimeError):
            first.advance_checkpoint(self.account, self.start + timedelta(minutes=5))
        second = self.repository()
        self.assertIsNot(second, first)
        account = self.resolve(second)
        committed = second.advance_checkpoint(account, self.start + timedelta(minutes=5))
        second.close()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER deny_{operation.lower()} BEFORE {operation} ON ledger_checkpoints "
                             "BEGIN SELECT RAISE(ABORT, 'restarted same-value replay must not write'); END")
        third = self.repository()
        self.assertIsNot(third, second)
        before = self.rows("ledger_checkpoints")
        self.assertEqual(third.advance_checkpoint(self.resolve(third), committed.completed_through), committed)
        self.assertEqual(self.rows("ledger_checkpoints"), before)
        third.close()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for operation in ("insert", "update", "delete"):
                conn.execute(f"DROP TRIGGER deny_{operation}")
        fourth = self.repository()
        committed = fourth.advance_checkpoint(self.resolve(fourth), self.start + timedelta(minutes=20))
        fourth.close()
        fifth = self.repository()
        self.assertEqual(fifth.get_checkpoint(self.resolve(fifth).mt5_account_id), committed)
        self.assertEqual(committed.completed_through, datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc))
        self.assertEqual(len(self.rows("ledger_accounts")), 1)
        self.assertEqual(len(self.rows("ledger_checkpoints")), 1)

    def test_two_accounts_remain_independent_across_multiple_restarts(self):
        other = replace(self.account, mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"), login=555)
        first = self.repository()
        first.register_account(self.account)
        first.register_account(other)
        a = first.advance_checkpoint(self.account, self.start + timedelta(minutes=10))
        b = first.advance_checkpoint(other, self.start + timedelta(minutes=90))
        first.close()
        second = self.repository()
        self.assertEqual(second.get_checkpoint(self.resolve(second).mt5_account_id), a)
        self.assertEqual(second.get_checkpoint(self.resolve(second, other).mt5_account_id), b)
        before = self.rows("ledger_checkpoints")
        with self.assertRaises(ledger.AccountIdentityError):
            second.advance_checkpoint(replace(self.account, server="wrong-server"), self.start + timedelta(minutes=20))
        self.assertEqual(self.rows("ledger_checkpoints"), before)
        a = second.advance_checkpoint(self.resolve(second), self.start + timedelta(minutes=20))
        second.close()
        third = self.repository()
        self.assertEqual(third.get_checkpoint(self.resolve(third).mt5_account_id), a)
        self.assertEqual(third.get_checkpoint(self.resolve(third, other).mt5_account_id), b)
        self.assertEqual(len(self.rows("ledger_accounts")), 2)
        self.assertEqual(len(self.rows("ledger_checkpoints")), 2)

    def test_failed_update_then_reopen_preserves_last_committed_state(self):
        first = self.repository()
        first.register_account(self.account)
        committed = first.advance_checkpoint(self.account, self.start + timedelta(minutes=10))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_update AFTER UPDATE ON ledger_checkpoints "
                         "BEGIN SELECT RAISE(FAIL, 'deny advancement'); END")
        before = self.rows("ledger_checkpoints")
        with self.assertRaises(sqlite3.IntegrityError):
            first.advance_checkpoint(self.account, self.start + timedelta(minutes=20))
        first.close()
        second = self.repository()
        self.assertEqual(second.get_checkpoint(self.resolve(second).mt5_account_id), committed)
        self.assertEqual(self.rows("ledger_checkpoints"), before)
        second.close()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TRIGGER deny_update")
        third = self.repository()
        latest = third.advance_checkpoint(self.resolve(third), self.start + timedelta(minutes=20))
        third.close()
        fourth = self.repository()
        self.assertEqual(fourth.get_checkpoint(self.account.mt5_account_id), latest)

    def test_legacy_upgrade_checkpoint_write_survives_another_reopen(self):
        legacy = self.repository()
        legacy.register_account(self.account)
        original = ledger.normalize_deal(self.account, raw_deal(), self.start, "legacy-original")
        correction = ledger.normalize_deal(self.account, raw_deal(comment="correction"), self.start, "legacy-change")
        legacy.ingest(self.account, original)
        legacy.ingest(self.account, correction)
        legacy.close()
        # Change only this disposable fixture to simulate the pre-checkpoint DB.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TABLE ledger_checkpoints")
        before = {table: self.rows(table) for table in ("ledger_accounts", "deals", "deal_observations")}
        upgraded = self.repository()
        account = self.resolve(upgraded)
        self.assertIsNone(upgraded.get_checkpoint(account.mt5_account_id))
        committed = upgraded.advance_checkpoint(account, self.start + timedelta(minutes=10))
        upgraded.close()
        reopened = self.repository()
        self.assertEqual(reopened.get_checkpoint(self.resolve(reopened).mt5_account_id), committed)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)
        self.assertEqual(reopened.get(self.account.mt5_account_id, original.deal_ticket), original)
        self.assertEqual(len(reopened.list_observations(self.account.mt5_account_id, original.deal_ticket)), 1)
        self.assertEqual(len(self.rows("ledger_checkpoints")), 1)


if __name__ == "__main__":
    unittest.main()
