"""Read-only account preflight against real SQLite, without history access."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from uuid import UUID

from test_deal_normalizer import ledger, raw_deal


class AccountLookupTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger, "AccountLookupRepository"),
                        "AccountLookupRepository must be publicly exported")
        self.assertTrue(hasattr(ledger, "verify_account_identity"),
                        "verify_account_identity must be publicly exported")
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
                             mt5_account_id=UUID("ac647462-f801-44f7-a60f-6b6c86c2b9e1"),
                             broker_namespace="broker-b", company_observed="Broker B")
        # Registration is fixture setup only; preflight must never register.
        self.repo.register_account(self.account)
        self.repo.register_account(self.other)
        self.before = self.snapshot()
        self.addCleanup(self.assert_unchanged)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                         for table in ("ledger_accounts", "deals", "deal_observations", "ledger_checkpoints"))

    def assert_unchanged(self):
        self.assertEqual(self.snapshot(), self.before)

    def test_matching_registered_account_succeeds_without_deals_or_checkpoint(self):
        self.assertEqual(self.before[1:], ([], [], []))
        verified = ledger.verify_account_identity(account=self.account, repository=self.repo)
        self.assertIs(verified, self.account)

    def test_unknown_identity_raises_without_auto_registration(self):
        unknown = replace(self.account, mt5_account_id=UUID(int=1), broker_namespace="unknown-broker")
        with self.assertRaises(ledger.AccountIdentityError) as raised:
            ledger.verify_account_identity(account=unknown, repository=self.repo)
        message = str(raised.exception)
        for context in (str(unknown.mt5_account_id), unknown.broker_namespace, unknown.server, str(unknown.login)):
            self.assertIn(context, message)
        self.assertNotIn(unknown.company_observed, message)

    def test_tuple_resolving_to_different_uuid_is_rejected_with_no_history(self):
        self.assertEqual(self.before[1:], ([], [], []))
        for account_id in (self.other.mt5_account_id, UUID(int=1)):
            with self.subTest(account_id=account_id), self.assertRaises(ledger.AccountIdentityError) as raised:
                ledger.verify_account_identity(account=replace(self.account, mt5_account_id=account_id),
                                                repository=self.repo)
            self.assertIn(str(account_id), str(raised.exception))
            self.assertIn(str(self.account.mt5_account_id), str(raised.exception))

    def test_same_uuid_with_wrong_server_is_rejected(self):
        with self.assertRaises(ledger.AccountIdentityError):
            ledger.verify_account_identity(account=replace(self.account, server="wrong-server"),
                                            repository=self.repo)

    def test_same_uuid_with_wrong_broker_is_rejected_even_if_tuple_is_registered(self):
        with self.assertRaises(ledger.AccountIdentityError):
            ledger.verify_account_identity(account=replace(self.account, broker_namespace="broker-b"),
                                            repository=self.repo)

    def test_same_uuid_with_wrong_login_is_rejected(self):
        with self.assertRaises(ledger.AccountIdentityError):
            ledger.verify_account_identity(account=replace(self.account, login=654321), repository=self.repo)

    def test_observed_metadata_variation_succeeds_without_updating_storage(self):
        observed = replace(self.account, company_observed="Renamed company", trade_mode=0)
        verified = ledger.verify_account_identity(account=observed, repository=self.repo)
        self.assertIs(verified, observed)
        stored = self.repo.resolve_account(broker_namespace="broker-a", server="server-live", login=123456)
        self.assertEqual(stored, self.account)

    def test_identity_strings_are_not_normalized(self):
        for changes in ({"broker_namespace": "BROKER-A"}, {"broker_namespace": "broker-a "},
                        {"server": "SERVER-LIVE"}, {"server": " server-live"}):
            with self.subTest(changes=changes), self.assertRaises(ledger.AccountIdentityError):
                ledger.verify_account_identity(account=replace(self.account, **changes), repository=self.repo)

    def test_preflight_preserves_all_populated_ledger_tables(self):
        when = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        # Seed only this disposable fixture, without invoking ingestion or advance_checkpoint.
        original = ledger.normalize_deal(self.account, raw_deal(), when, "fixture")
        changed = ledger.normalize_deal(self.account, raw_deal(profit=99.25), when, "fixture-correction")
        self.repo.ingest(self.account, original)
        self.repo.ingest(self.account, changed)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)",
                         (str(self.account.mt5_account_id), when.isoformat(), when.isoformat()))
        self.before = self.snapshot()
        self.assertEqual(tuple(len(rows) for rows in self.before), (2, 1, 1, 1))
        for _ in range(2):
            ledger.verify_account_identity(account=replace(self.account, company_observed="New observation"),
                                            repository=self.repo)
            with self.assertRaises(ledger.AccountIdentityError):
                ledger.verify_account_identity(account=replace(self.account, login=654321), repository=self.repo)
        self.assert_unchanged()

    def test_storage_exception_propagates_unchanged_through_narrow_lookup(self):
        failure = sqlite3.OperationalError("controlled offline storage failure")

        class FailingLookup:
            def resolve_account(self, *, broker_namespace, server, login):
                raise failure

        with self.assertRaises(sqlite3.OperationalError) as raised:
            ledger.verify_account_identity(account=self.account, repository=FailingLookup())
        self.assertIs(raised.exception, failure)

    def test_closed_repository_failure_is_not_treated_as_unknown_account(self):
        self.repo.close()
        with self.assertRaises(RuntimeError):
            ledger.verify_account_identity(account=self.account, repository=self.repo)

    def test_invalid_account_type_is_rejected_before_lookup(self):
        for value in (None, object(), {}, str(self.account.mt5_account_id)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                ledger.verify_account_identity(account=value, repository=None)

    def test_public_import_executes_real_ledger_without_loading_mt5(self):
        package_dir = Path(__file__).resolve().parents[2] / "src" / "aiomql"
        script = textwrap.dedent("""
            import sys
            from types import ModuleType

            package = ModuleType("aiomql")
            package.__path__ = [sys.argv[1]]
            sys.modules["aiomql"] = package
            from aiomql.ledger import AccountLookupRepository, verify_account_identity
            from aiomql.ledger import __all__

            assert "AccountLookupRepository" in __all__
            assert "verify_account_identity" in __all__
            assert AccountLookupRepository.__name__ == "AccountLookupRepository"
            assert callable(verify_account_identity)
            assert "MetaTrader5" not in sys.modules
        """)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(package_dir)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
