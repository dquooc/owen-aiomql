"""Checkpoint schema integrity using temporary files, without MT5."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import UUID

from test_deal_normalizer import ledger, raw_deal


class LedgerCheckpointSchemaTests(unittest.TestCase):
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
        self.completed = "2026-10-31T10:00:00.123456+00:00"
        self.updated = "2026-11-01T09:00:00.654321+00:00"

    def assert_checkpoint_table(self):
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                               "AND name = 'ledger_checkpoints'").fetchone()
        self.assertEqual(row, ("ledger_checkpoints",))

    def insert_checkpoint(self, conn, account_id, completed=None, updated=None):
        conn.execute("INSERT INTO ledger_checkpoints "
                     "(mt5_account_id, completed_through, updated_at) VALUES (?, ?, ?)",
                     (str(account_id), completed or self.completed, updated or self.updated))

    def test_new_database_has_only_required_checkpoint_columns(self):
        self.assert_checkpoint_table()
        with closing(sqlite3.connect(self.path)) as conn:
            columns = conn.execute("PRAGMA table_info(ledger_checkpoints)").fetchall()
        self.assertEqual([(row[1], row[2], row[3], row[5]) for row in columns],
                         [("mt5_account_id", "TEXT", 1, 1),
                          ("completed_through", "TEXT", 1, 0),
                          ("updated_at", "TEXT", 1, 0)])

    def test_repeated_initialization_preserves_checkpoint_rows(self):
        self.assert_checkpoint_table()
        self.repo.register_account(self.account)
        with closing(self.repo._connect()) as conn:
            self.insert_checkpoint(conn, self.account.mt5_account_id)
        self.repo.close()
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        self.assert_checkpoint_table()
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT * FROM ledger_checkpoints").fetchall()
        self.assertEqual(rows, [(str(self.account.mt5_account_id), self.completed, self.updated)])

    def test_account_and_deal_operations_remain_usable(self):
        self.assert_checkpoint_table()
        self.repo.register_account(self.account)
        self.assertEqual(self.repo.resolve_account(broker_namespace="broker-a", server="server-live",
                                                   login=123456), self.account)
        deal = ledger.normalize_deal(self.account, raw_deal(),
                                     datetime(2026, 11, 1, tzinfo=timezone.utc), "schema-test")
        self.assertEqual(self.repo.ingest(self.account, deal).outcome, ledger.IngestOutcome.INSERTED)
        self.assertEqual(self.repo.get(self.account.mt5_account_id, deal.deal_ticket), deal)

    def test_pre_checkpoint_database_gains_empty_table_without_data_changes(self):
        self.repo.register_account(self.account)
        when = datetime(2026, 11, 1, tzinfo=timezone.utc)
        original = ledger.normalize_deal(self.account, raw_deal(), when, "original")
        changed = ledger.normalize_deal(self.account, raw_deal(comment="correction"), when, "changed")
        self.repo.ingest(self.account, original)
        self.repo.ingest(self.account, changed)
        self.repo.close()
        tables = ("ledger_accounts", "deals", "deal_observations")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            # Only this disposable fixture is changed: removing the new empty
            # table recreates the previous schema without duplicating its DDL.
            conn.execute("DROP TABLE IF EXISTS ledger_checkpoints")
            before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                      for table in tables}
            schema_before = conn.execute("SELECT type, name, sql FROM sqlite_master "
                                         "ORDER BY type, name").fetchall()
        self.assertTrue(all(before.values()))
        reopened = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(reopened.close)
        self.assert_checkpoint_table()
        with closing(sqlite3.connect(self.path)) as conn:
            after = {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                     for table in tables}
            schema_after = conn.execute("SELECT type, name, sql FROM sqlite_master "
                                        "WHERE name NOT LIKE '%ledger_checkpoints%' "
                                        "ORDER BY type, name").fetchall()
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger_checkpoints").fetchone()[0], 0)
        self.assertEqual(after, before)
        self.assertEqual(schema_after, schema_before)
        self.assertEqual(reopened.get(self.account.mt5_account_id, original.deal_ticket), original)
        self.assertEqual(len(reopened.list_observations(self.account.mt5_account_id, original.deal_ticket)), 1)

    def test_database_rejects_second_checkpoint_for_same_account(self):
        self.assert_checkpoint_table()
        self.repo.register_account(self.account)
        with closing(self.repo._connect()) as conn:
            self.insert_checkpoint(conn, self.account.mt5_account_id)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_checkpoint(conn, self.account.mt5_account_id, completed=self.updated)
            self.assertEqual(tuple(conn.execute("SELECT * FROM ledger_checkpoints").fetchone()),
                             (str(self.account.mt5_account_id), self.completed, self.updated))
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger_checkpoints").fetchone()[0], 1)

    def test_repository_connection_rejects_foreign_key_to_unknown_account(self):
        self.assert_checkpoint_table()
        with closing(self.repo._connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            foreign_keys = conn.execute("PRAGMA foreign_key_list(ledger_checkpoints)").fetchall()
            self.assertEqual([(row[2], row[3], row[4]) for row in foreign_keys],
                             [("ledger_accounts", "mt5_account_id", "mt5_account_id")])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                self.insert_checkpoint(conn, self.account.mt5_account_id)
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger_checkpoints").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger_accounts").fetchone()[0], 0)

    def test_different_accounts_have_independent_checkpoint_rows(self):
        self.assert_checkpoint_table()
        other = replace(self.account, mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"), login=555)
        self.repo.register_account(self.account)
        self.repo.register_account(other)
        with closing(self.repo._connect()) as conn:
            self.insert_checkpoint(conn, self.account.mt5_account_id)
            self.insert_checkpoint(conn, other.mt5_account_id, completed=self.updated)
            rows = conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                                "FROM ledger_checkpoints").fetchall()
        self.assertCountEqual([tuple(row) for row in rows],
                              [(str(self.account.mt5_account_id), self.completed, self.updated),
                               (str(other.mt5_account_id), self.updated, self.updated)])

    def test_database_rejects_null_required_columns(self):
        self.assert_checkpoint_table()
        self.repo.register_account(self.account)
        with closing(self.repo._connect()) as conn:
            for values in ((None, self.completed, self.updated),
                           (str(self.account.mt5_account_id), None, self.updated),
                           (str(self.account.mt5_account_id), self.completed, None)):
                with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO ledger_checkpoints "
                                 "(mt5_account_id, completed_through, updated_at) VALUES (?, ?, ?)", values)
            self.assertEqual(conn.execute("SELECT count(*) FROM ledger_checkpoints").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
