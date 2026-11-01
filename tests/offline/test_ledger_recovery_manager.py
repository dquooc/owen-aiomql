"""Preparation-only recovery orchestration with deterministic offline collaborators."""

import asyncio
from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from test_deal_normalizer import ledger


class Lookup:
    def __init__(self, account, events):
        self.account = account
        self.events = events
        self.calls = []
        self.failure = None

    def resolve_account(self, *, broker_namespace, server, login):
        self.events.append("identity")
        self.calls.append((broker_namespace, server, login))
        if self.failure is not None:
            raise self.failure
        return self.account


class Checkpoints:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.result = None
        self.failure = None

    def get_checkpoint(self, mt5_account_id):
        self.events.append("checkpoint")
        self.calls.append(mt5_account_id)
        if self.failure is not None:
            raise self.failure
        return self.result


class CountingClock:
    def __init__(self, value, events):
        self.value = value
        self.events = events
        self.calls = 0
        self.failure = None

    def __call__(self):
        self.events.append("clock")
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.value


class RecoveryManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("RecoveryManager", "RecoveryAttempt"):
            self.assertTrue(hasattr(ledger, name), f"{name} must be publicly exported")
        self.module = importlib.import_module(f"{ledger.__name__}.recovery_manager")
        self.account = ledger.AccountRef(
            mt5_account_id=UUID("cc17f0e2-0784-4d99-81cc-ce44e990767c"),
            broker_namespace="broker-a", server="server-live", login=123456,
            company_observed="Broker A", trade_mode=2)
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        self.events = []
        self.lookup = Lookup(self.account, self.events)
        self.checkpoints = Checkpoints(self.events)
        self.clock = CountingClock(self.now, self.events)
        self.config = ledger.RecoveryConfig(overlap=timedelta(minutes=5, microseconds=1),
                                             bootstrap_start=self.start)
        self.planner_args = []

        def observed_planner(**kwargs):
            self.events.append("planner")
            self.planner_args.append(kwargs)
            return ledger.plan_recovery_range(**kwargs)

        planner_patch = patch.object(self.module, "plan_recovery_range", side_effect=observed_planner)
        planner_patch.start()
        self.addCleanup(planner_patch.stop)

    def manager(self, **changes):
        dependencies = dict(account_lookup=self.lookup, checkpoints=self.checkpoints,
                            config=self.config, clock=self.clock)
        dependencies.update(changes)
        return ledger.RecoveryManager(**dependencies)

    def assert_preflight_stopped(self, events):
        self.assertEqual(self.events, events)
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(self.planner_args, [])

    async def test_bootstrap_preparation_calls_real_planner_in_exact_order(self):
        attempt = await self.manager().prepare(account=self.account)
        self.assertIsInstance(attempt, ledger.RecoveryAttempt)
        self.assertIs(attempt.account, self.account)
        self.assertIsNone(attempt.checkpoint_before)
        self.assertEqual((attempt.planned_range.from_time, attempt.planned_range.to_time),
                         (self.start, self.now))
        self.assertEqual(self.events, ["identity", "checkpoint", "clock", "planner"])
        self.assertEqual(self.lookup.calls, [("broker-a", "server-live", 123456)])
        self.assertEqual(self.checkpoints.calls, [self.account.mt5_account_id])
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.planner_args, [dict(checkpoint=None, current_time=self.now,
                                                overlap=self.config.overlap, bootstrap_start=self.start)])
        self.assertIs(self.planner_args[0]["current_time"], self.now)

    async def test_checkpoint_preparation_ignores_future_bootstrap_and_audit_time(self):
        checkpoint = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.start, self.now + timedelta(days=2))
        self.checkpoints.result = checkpoint
        config = ledger.RecoveryConfig(overlap=self.config.overlap, bootstrap_start=self.now + timedelta(days=1))
        attempt = await self.manager(config=config).prepare(account=self.account)
        self.assertIs(attempt.checkpoint_before, checkpoint)
        self.assertEqual(attempt.planned_range.from_time,
                         datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc))
        self.assertEqual(attempt.planned_range.to_time, self.now)
        self.assertEqual(self.events, ["identity", "checkpoint", "clock", "planner"])
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.planner_args, [dict(checkpoint=checkpoint, current_time=self.now,
                                                overlap=config.overlap, bootstrap_start=config.bootstrap_start)])

    async def test_unknown_account_stops_before_checkpoint_clock_and_planner(self):
        self.lookup.account = None
        with self.assertRaises(ledger.AccountIdentityError):
            await self.manager().prepare(account=self.account)
        self.assertEqual(self.checkpoints.calls, [])
        self.assert_preflight_stopped(["identity"])

    async def test_account_uuid_mismatch_stops_before_checkpoint_clock_and_planner(self):
        self.lookup.account = replace(self.account, mt5_account_id=UUID(int=1))
        with self.assertRaises(ledger.AccountIdentityError):
            await self.manager().prepare(account=self.account)
        self.assertEqual(self.checkpoints.calls, [])
        self.assert_preflight_stopped(["identity"])

    async def test_wrong_account_type_is_rejected_before_any_collaborator(self):
        for account in (None, object(), str(self.account.mt5_account_id), False):
            with self.subTest(account=account), self.assertRaises(TypeError):
                await self.manager().prepare(account=account)
        self.assertEqual(self.lookup.calls, [])
        self.assertEqual(self.checkpoints.calls, [])
        self.assert_preflight_stopped([])

    async def test_account_lookup_failure_propagates_without_checkpoint_query(self):
        failure = sqlite3.OperationalError("offline identity lookup failure")
        self.lookup.failure = failure
        with self.assertRaises(sqlite3.OperationalError) as raised:
            await self.manager().prepare(account=self.account)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.checkpoints.calls, [])
        self.assert_preflight_stopped(["identity"])

    async def test_checkpoint_storage_and_decode_errors_propagate_before_clock(self):
        for failure in (sqlite3.OperationalError("offline checkpoint failure"), ValueError("malformed timestamp")):
            self.events.clear()
            self.checkpoints.calls.clear()
            self.checkpoints.failure = failure
            with self.subTest(failure=failure), self.assertRaises(type(failure)) as raised:
                await self.manager().prepare(account=self.account)
            self.assertIs(raised.exception, failure)
            self.assertEqual(self.checkpoints.calls, [self.account.mt5_account_id])
            self.assert_preflight_stopped(["identity", "checkpoint"])

    async def test_wrong_checkpoint_types_are_not_treated_as_missing(self):
        for value in ({}, False, "checkpoint", SimpleNamespace(mt5_account_id=self.account.mt5_account_id)):
            self.events.clear()
            self.checkpoints.result = value
            with self.subTest(value=value), self.assertRaises(TypeError):
                await self.manager().prepare(account=self.account)
            self.assert_preflight_stopped(["identity", "checkpoint"])

    async def test_checkpoint_owner_mismatch_is_rejected_before_clock(self):
        self.checkpoints.result = ledger.LedgerCheckpoint(UUID(int=1), self.start, self.start)
        with self.assertRaises(ledger.AccountIdentityError):
            await self.manager().prepare(account=self.account)
        self.assert_preflight_stopped(["identity", "checkpoint"])

    async def test_clock_exception_propagates_once_without_planning_or_retry(self):
        failure = RuntimeError("offline clock failure")
        self.clock.failure = failure
        with self.assertRaises(RuntimeError) as raised:
            await self.manager().prepare(account=self.account)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.events, ["identity", "checkpoint", "clock"])
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.planner_args, [])

    async def test_cancellation_from_clock_propagates_without_planning(self):
        self.clock.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.manager().prepare(account=self.account)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.planner_args, [])

    async def test_invalid_clock_result_is_rejected_by_real_planner_without_retry(self):
        for value in (self.now.replace(tzinfo=None), None, "now", False):
            self.events.clear()
            self.planner_args.clear()
            self.clock.calls = 0
            self.clock.value = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.manager().prepare(account=self.account)
            self.assertEqual(self.clock.calls, 1)
            self.assertEqual(self.events, ["identity", "checkpoint", "clock", "planner"])
            self.assertIs(self.planner_args[0]["current_time"], value)

    async def test_missing_bootstrap_is_planner_error_after_one_clock_call(self):
        with self.assertRaises(ValueError):
            await self.manager(config=ledger.RecoveryConfig(overlap=timedelta(0))).prepare(account=self.account)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.events, ["identity", "checkpoint", "clock", "planner"])
        self.assertIsNone(self.planner_args[0]["bootstrap_start"])

    async def test_future_checkpoint_is_not_hidden_by_overlap(self):
        self.checkpoints.result = ledger.LedgerCheckpoint(
            self.account.mt5_account_id, self.now + timedelta(microseconds=1), self.start)
        with self.assertRaises(ValueError):
            await self.manager().prepare(account=self.account)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(len(self.planner_args), 1)

    async def test_overlap_underflow_propagates_from_planner(self):
        self.checkpoints.result = ledger.LedgerCheckpoint(
            self.account.mt5_account_id, datetime(1, 1, 1, tzinfo=timezone.utc), self.start)
        with self.assertRaises(OverflowError):
            await self.manager().prepare(account=self.account)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(len(self.planner_args), 1)

    async def test_zero_length_ranges_are_valid_for_both_branches(self):
        config = ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=self.now)
        for checkpoint in (None, ledger.LedgerCheckpoint(self.account.mt5_account_id, self.now, self.start)):
            self.clock.calls = 0
            self.checkpoints.result = checkpoint
            with self.subTest(checkpoint=checkpoint):
                attempt = await self.manager(config=config).prepare(account=self.account)
                self.assertEqual((attempt.planned_range.from_time, attempt.planned_range.to_time),
                                 (self.now, self.now))
                self.assertEqual(self.clock.calls, 1)

    async def test_non_utc_clock_is_passed_unchanged_then_canonicalized_by_planner(self):
        value = datetime(2026, 11, 1, 17, 20, 0, 654321, tzinfo=timezone(timedelta(hours=7)))
        self.clock.value = value
        attempt = await self.manager().prepare(account=self.account)
        self.assertIs(self.planner_args[0]["current_time"], value)
        self.assertEqual(attempt.planned_range.to_time, self.now)
        self.assertIs(attempt.planned_range.to_time.tzinfo, timezone.utc)
        self.assertEqual(self.clock.calls, 1)

    async def test_prepared_state_has_only_three_immutable_fields(self):
        attempt = await self.manager().prepare(account=self.account)
        self.assertEqual(tuple(field.name for field in fields(attempt)),
                         ("account", "checkpoint_before", "planned_range"))
        for field in fields(attempt):
            with self.subTest(field=field.name), self.assertRaises(FrozenInstanceError):
                setattr(attempt, field.name, getattr(attempt, field.name))
        self.assertFalse(hasattr(attempt, "__dict__"))

    def test_constructor_does_not_perform_io_or_capture_clock(self):
        self.manager()
        self.assert_preflight_stopped([])

    def test_invalid_config_or_clock_is_rejected_at_construction(self):
        for changes in ({"config": None}, {"config": {}}, {"clock": None}, {"clock": self.now}):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                self.manager(**changes)
        self.assert_preflight_stopped([])

    def test_public_import_executes_real_ledger_without_loading_mt5(self):
        package_dir = Path(__file__).resolve().parents[2] / "src" / "aiomql"
        script = textwrap.dedent("""
            import sys
            from types import ModuleType

            package = ModuleType("aiomql")
            package.__path__ = [sys.argv[1]]
            sys.modules["aiomql"] = package
            from aiomql.ledger import RecoveryManager, RecoveryAttempt, __all__

            assert {"RecoveryManager", "RecoveryAttempt"} <= set(__all__)
            assert callable(RecoveryManager.prepare)
            assert "MetaTrader5" not in sys.modules
        """)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(package_dir)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class RecoveryManagerSQLiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_preflight_and_checkpoint_reads_leave_database_unchanged(self):
        self.assertTrue(hasattr(ledger, "RecoveryManager"), "RecoveryManager must be publicly exported")
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        path = Path(temp.name)
        temp.close()
        self.addCleanup(path.unlink, missing_ok=True)
        repo = ledger.SQLiteDealRepository(path)
        self.addCleanup(repo.close)
        account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        repo.register_account(account)  # Fixture setup only.
        start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        clock = CountingClock(now, [])
        manager = ledger.RecoveryManager(account_lookup=repo, checkpoints=repo,
                                         config=ledger.RecoveryConfig(overlap=timedelta(minutes=5),
                                                                      bootstrap_start=start), clock=clock)

        def snapshot():
            with closing(sqlite3.connect(path)) as conn:
                return tuple(conn.iterdump())

        for with_checkpoint in (False, True):
            if with_checkpoint:
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)",
                                 (str(account.mt5_account_id), start.isoformat(), start.isoformat()))
            before = snapshot()
            clock.calls = 0
            attempt = await manager.prepare(account=account)
            self.assertEqual(attempt.planned_range.from_time,
                             datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc) if with_checkpoint else start)
            self.assertEqual(attempt.planned_range.to_time, now)
            self.assertEqual(attempt.checkpoint_before is not None, with_checkpoint)
            self.assertEqual(clock.calls, 1)
            self.assertEqual(snapshot(), before)
            with self.assertRaises(ledger.AccountIdentityError):
                await manager.prepare(account=replace(account, server="wrong-server"))
            self.assertEqual(clock.calls, 1)
            self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
