"""Deterministic overlapping recovery attempts; no live sessions or production locks."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID

from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal
from test_ledger_recovery_manager import CountingClock


class GatedSource:
    """Account-fixed source with independent entry/release events per horizon."""

    def __init__(self, account, responses):
        self.account = account
        self.broker_namespace = account.broker_namespace
        self.responses = responses
        self.entered = {horizon: asyncio.Event() for horizon in responses}
        self.release = {horizon: asyncio.Event() for horizon in responses}
        self.failures = {}
        self.fetch_calls = []
        self.returned = []

    async def current_account(self):
        return ledger.SessionAccount(self.account.login, self.account.server)

    async def fetch(self, account_ref, from_time, to_time):
        if account_ref != self.account:
            raise AssertionError("cross-account source request")
        self.fetch_calls.append((account_ref, from_time, to_time))
        self.entered[to_time].set()
        await self.release[to_time].wait()
        if to_time in self.failures:
            raise self.failures[to_time]
        self.returned.append(to_time)
        return ledger.DealHistoryBatch(tuple(self.responses[to_time]))

    async def wait_for_entry(self, horizon):
        await asyncio.wait_for(self.entered[horizon].wait(), timeout=5)


class HorizonClock:
    def __init__(self, horizons):
        self.horizons = iter(horizons)
        self.calls = []

    def __call__(self):
        horizon = next(self.horizons)
        self.calls.append(horizon)
        return horizon


class ThreadBarrierSource(FakeSource):
    """Used only in test worker threads, each running its own asyncio loop."""

    def __init__(self, account, records, barrier):
        super().__init__(account, records)
        self.barrier = barrier

    async def fetch(self, account_ref, from_time, to_time):
        self.barrier.wait(timeout=5)
        return await super().fetch(account_ref, from_time, to_time)


class RecoveryManagerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.repo.close)
        self.other_repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.other_repo.close)
        self.account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        self.other = ledger.AccountRef(UUID(int=2), "broker-b", "server-live", 123456, "Broker B", 2)
        self.repo.register_account(self.account)  # Disposable fixture setup only.
        self.repo.register_account(self.other)
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)
        self.audit_time = datetime(2026, 11, 1, 9, 59, 0, 987654, tzinfo=timezone.utc)
        self.h1 = datetime(2026, 11, 1, 10, 20, 0, 111111, tzinfo=timezone.utc)
        self.h2 = datetime(2026, 11, 1, 10, 30, 0, 222222, tzinfo=timezone.utc)
        self.config = ledger.RecoveryConfig(overlap=timedelta(minutes=5), bootstrap_start=self.start)
        self.checkpoint_rows = {}
        for repo in (self.repo, self.other_repo):
            for name in ("advance_checkpoint", "register_account"):
                guard = patch.object(repo, name, side_effect=AssertionError(f"recovery called {name}"))
                spy = guard.start()
                self.addCleanup(guard.stop)
                self.addCleanup(spy.assert_not_called)
        self.addCleanup(self.assert_durable_invariants)

    def seed_checkpoint(self, account, *, completed=None):
        completed = self.start if completed is None else completed
        row = (str(account.mt5_account_id), completed.isoformat(timespec="microseconds"),
               self.audit_time.isoformat(timespec="microseconds"))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)", row)
        self.checkpoint_rows[row[0]] = row

    def assert_durable_invariants(self):
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                                "FROM ledger_checkpoints ORDER BY mt5_account_id").fetchall()
            self.assertEqual(rows, sorted(self.checkpoint_rows.values()))
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def count(self, table):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def raw(self, **changes):
        seconds = int(self.start.timestamp()) + 1
        return raw_deal(time=seconds, time_msc=seconds * 1000 + 123, **changes)

    def manager(self, repo, source, clock):
        ingestor = ledger.DealIngestor(source, repo)
        return ledger.RecoveryManager(account_lookup=repo, checkpoints=repo, config=self.config,
                                       clock=clock, ingestor=ingestor)

    def launch(self, manager, account):
        task = asyncio.create_task(manager.recover(account=account))

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)

        self.addAsyncCleanup(cleanup)
        return task

    async def finish(self, task):
        return await asyncio.wait_for(task, timeout=5)

    def assert_result_scope(self, result, account, horizon, source):
        self.assertIs(result.account, account)
        self.assertIs(result.checkpoint_after, result.checkpoint_before)
        self.assertEqual(result.planned_range.to_time, horizon)
        self.assertEqual(result.ingestion_report.mt5_account_id, account.mt5_account_id)
        self.assertEqual((result.ingestion_report.requested_from, result.ingestion_report.requested_to),
                         (result.planned_range.from_time, horizon))
        self.assertIn((account, result.planned_range.from_time, horizon), source.fetch_calls)
        if result.checkpoint_before is not None:
            self.assertEqual(result.checkpoint_before.mt5_account_id, account.mt5_account_id)
            self.assertEqual(result.checkpoint_before.updated_at, self.audit_time)

    async def test_different_accounts_same_ticket_and_conflict_observations_stay_isolated(self):
        self.seed_checkpoint(self.account)
        self.seed_checkpoint(self.other, completed=self.start - timedelta(hours=1))
        originals = {}
        for correction in (False, True):
            a_raw = self.raw(profit=10.0 if correction else 1.0)
            b_raw = self.raw(profit=20.0 if correction else 2.0, symbol="GBPUSD")
            source_a = GatedSource(self.account, {self.h1: [a_raw]})
            source_b = GatedSource(self.other, {self.h2: [b_raw]})
            clock_a, clock_b = CountingClock(self.h1, []), CountingClock(self.h2, [])
            # Both accounts intentionally share one repository instance.
            manager_a = self.manager(self.repo, source_a, clock_a)
            manager_b = self.manager(self.repo, source_b, clock_b)
            a = self.launch(manager_a, self.account)
            b = self.launch(manager_b, self.other)
            await source_a.wait_for_entry(self.h1)
            await source_b.wait_for_entry(self.h2)
            self.assertFalse(a.done())
            self.assertFalse(b.done())
            self.assert_durable_invariants()
            source_a.release[self.h1].set()
            source_b.release[self.h2].set()
            first, second = await asyncio.gather(self.finish(a), self.finish(b))
            for result, account, horizon, source in (
                    (first, self.account, self.h1, source_a), (second, self.other, self.h2, source_b)):
                self.assert_result_scope(result, account, horizon, source)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
            self.assertEqual(first.planned_range.from_time,
                             datetime(2026, 11, 1, 9, 55, 0, 123456, tzinfo=timezone.utc))
            self.assertEqual(second.planned_range.from_time,
                             datetime(2026, 11, 1, 8, 55, 0, 123456, tzinfo=timezone.utc))
            self.assertNotEqual(first.ingestion_report.ingestion_id, second.ingestion_report.ingestion_id)
            self.assertEqual((clock_a.calls, clock_b.calls), (1, 1))
            if not correction:
                self.assertEqual((first.ingestion_report.inserted, second.ingestion_report.inserted), (1, 1))
                originals = {account.mt5_account_id: self.repo.get(account.mt5_account_id, 9001)
                             for account in (self.account, self.other)}
                self.assertNotEqual(originals[self.account.mt5_account_id].id, originals[self.other.mt5_account_id].id)
            else:
                self.assertEqual((first.ingestion_report.conflicts, second.ingestion_report.conflicts), (1, 1))
                for account, payload in ((self.account, a_raw), (self.other, b_raw)):
                    original = originals[account.mt5_account_id]
                    self.assertEqual(self.repo.get(account.mt5_account_id, 9001), original)
                    observations = self.repo.list_observations(account.mt5_account_id, 9001)
                    self.assertEqual(len(observations), 1)
                    self.assertEqual(observations[0].deal_id, original.id)
                    self.assertEqual(observations[0].raw_payload, payload)
        self.assertEqual((self.count("deals"), self.count("deal_observations")), (2, 2))

    async def test_shared_manager_and_ingestor_keep_same_account_attempts_local(self):
        self.seed_checkpoint(self.account)
        h3, h4 = self.h1 + timedelta(hours=1), self.h2 + timedelta(hours=1)
        source = GatedSource(self.account, {self.h1: [self.raw()], self.h2: [self.raw()],
                                           h3: [self.raw(profit=99.25)], h4: [self.raw(profit=99.25)]})
        clock = HorizonClock((self.h1, self.h2, h3, h4))
        manager = self.manager(self.repo, source, clock)
        manager_fields, ingestor_fields = vars(manager).copy(), vars(manager.ingestor).copy()
        results = []
        original = None
        for left_horizon, right_horizon, reverse in ((self.h1, self.h2, True), (h3, h4, False)):
            left = self.launch(manager, self.account)
            await source.wait_for_entry(left_horizon)
            right = self.launch(manager, self.account)
            await source.wait_for_entry(right_horizon)
            self.assertFalse(left.done())
            self.assertFalse(right.done())
            early, late = (right, left) if reverse else (left, right)
            early_horizon, late_horizon = (right_horizon, left_horizon) if reverse else (left_horizon, right_horizon)
            source.release[early_horizon].set()
            early_result = await self.finish(early)
            self.assertFalse(late.done())
            source.release[late_horizon].set()
            late_result = await self.finish(late)
            pair = (late_result, early_result) if reverse else (early_result, late_result)
            for result, horizon in zip(pair, (left_horizon, right_horizon)):
                self.assert_result_scope(result, self.account, horizon, source)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
                self.assertEqual(result.checkpoint_before.completed_through, self.start)
            self.assertIsNot(pair[0], pair[1])
            self.assertIsNot(pair[0].planned_range, pair[1].planned_range)
            self.assertIsNot(pair[0].ingestion_report, pair[1].ingestion_report)
            if reverse:
                self.assertEqual(sum(result.ingestion_report.inserted for result in pair), 1)
                self.assertEqual(sum(result.ingestion_report.duplicates for result in pair), 1)
                original = self.repo.get(self.account.mt5_account_id, 9001)
            else:
                self.assertEqual(sum(result.ingestion_report.conflicts for result in pair), 2)
            results.extend(pair)
            self.assertEqual(self.count("deals"), 1)
            self.assert_durable_invariants()
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
        observations = self.repo.list_observations(self.account.mt5_account_id, 9001)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].raw_payload["profit"], 99.25)
        self.assertEqual(len({result.ingestion_report.ingestion_id for result in results}), 4)
        self.assertEqual(clock.calls, [self.h1, self.h2, h3, h4])
        self.assertEqual(vars(manager), manager_fields)
        self.assertEqual(vars(manager.ingestor), ingestor_fields)

    async def test_two_repository_instances_replay_bootstrap_in_parallel_worker_threads(self):
        barrier = Barrier(2)
        sources = [ThreadBarrierSource(self.account, [self.raw()], barrier) for _ in range(2)]
        clocks = [CountingClock(self.h1, []), CountingClock(self.h2, [])]
        managers = [self.manager(repo, source, clock)
                    for repo, source, clock in zip((self.repo, self.other_repo), sources, clocks)]

        def worker(manager):
            return asyncio.run(manager.recover(account=self.account))

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = await asyncio.wait_for(asyncio.gather(
                *(loop.run_in_executor(pool, worker, manager) for manager in managers)), timeout=15)
        for result, source, clock in zip(results, sources, clocks):
            self.assert_result_scope(result, self.account, clock.value, source)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
            self.assertIsNone(result.checkpoint_before)
            self.assertIsNone(result.checkpoint_after)
            self.assertEqual(clock.calls, 1)
            self.assertEqual(len(source.fetch_calls), 1)
        # Scheduling decides which attempt inserts; only the combined invariant is stable.
        self.assertEqual(sum(result.ingestion_report.inserted for result in results), 1)
        self.assertEqual(sum(result.ingestion_report.duplicates for result in results), 1)
        self.assertEqual(self.count("deals"), 1)
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001),
                         self.other_repo.get(self.account.mt5_account_id, 9001))
        self.assertEqual(self.count("deal_observations"), 0)

    async def test_failed_account_does_not_poison_other_inflight_recovery(self):
        self.seed_checkpoint(self.account)
        self.seed_checkpoint(self.other)
        source_a = GatedSource(self.account, {self.h1: [self.raw()]})
        source_b = GatedSource(self.other, {self.h2: [self.raw(profit=9.5)]})
        source_a.failures[self.h1] = ConnectionError("controlled account A fetch failure")
        a = self.launch(self.manager(self.repo, source_a, CountingClock(self.h1, [])), self.account)
        b = self.launch(self.manager(self.other_repo, source_b, CountingClock(self.h2, [])), self.other)
        await source_a.wait_for_entry(self.h1)
        await source_b.wait_for_entry(self.h2)
        source_a.release[self.h1].set()
        failed = await self.finish(a)
        self.assertIs(failed.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertEqual(failed.ingestion_report.errors[0].stage, ledger.IngestionStage.FETCH)
        self.assertFalse(b.done())
        self.assertFalse(b.cancelled())
        source_b.release[self.h2].set()
        success = await self.finish(b)
        self.assert_result_scope(failed, self.account, self.h1, source_a)
        self.assert_result_scope(success, self.other, self.h2, source_b)
        self.assertIs(success.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertEqual(self.repo.get(self.other.mt5_account_id, 9001).profit, Decimal("9.5"))
        self.assertEqual((len(source_a.fetch_calls), len(source_b.fetch_calls)), (1, 1))

    async def test_cancelling_one_shared_ingestor_attempt_leaves_other_independent(self):
        self.seed_checkpoint(self.account)
        source = GatedSource(self.account, {self.h1: [self.raw()], self.h2: [self.raw()]})
        clock = HorizonClock((self.h1, self.h2))
        manager = self.manager(self.repo, source, clock)
        cancelled = self.launch(manager, self.account)
        await source.wait_for_entry(self.h1)
        survivor = self.launch(manager, self.account)
        await source.wait_for_entry(self.h2)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.finish(cancelled)
        self.assertTrue(cancelled.cancelled())
        self.assertFalse(survivor.done())
        self.assertFalse(survivor.cancelled())
        self.assertEqual(self.count("deals"), 0)
        source.release[self.h2].set()
        result = await self.finish(survivor)
        self.assert_result_scope(result, self.account, self.h2, source)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual(result.ingestion_report.inserted, 1)
        self.assertEqual(source.returned, [self.h2])
        self.assertEqual(clock.calls, [self.h1, self.h2])
        self.assertEqual(len(source.fetch_calls), 2)
        self.assertEqual(self.count("deals"), 1)


if __name__ == "__main__":
    unittest.main()
