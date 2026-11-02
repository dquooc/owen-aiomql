"""Approved-only advancement and same-account lifecycle ownership."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
import importlib
from unittest.mock import patch
import unittest
from uuid import UUID

import test_deal_ingestion as fixtures
from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal
from test_ledger_recovery_manager import CountingClock


class VerifiedIngestor(ledger.DealIngestor):
    """Offline stronger-contract double; production assurance is unchanged."""

    async def ingest_range_with_evidence(self, *args, **kwargs):
        execution = await super().ingest_range_with_evidence(*args, **kwargs)
        if execution.source_evidence is not None:
            execution = replace(execution, source_evidence=replace(execution.source_evidence,
                                assurance=ledger.SourceHistoryAssurance.VERIFIED_COMPLETE))
        return execution


class ConditionalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.DealIngestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo, self.account = fixture.repo, fixture.account
        self.start = fixture.start.replace(microsecond=123000)
        self.end = self.start + timedelta(minutes=1, microseconds=456)
        self.clock = CountingClock(self.end, [])
        self.coordinator = ledger.AccountRecoveryCoordinator()

    def manager(self, records=(), verified=True, account=None, **changes):
        source = FakeSource(account or self.account, records)
        ingestor = (VerifiedIngestor if verified else ledger.DealIngestor)(source, self.repo)
        dependencies = dict(account_lookup=self.repo, checkpoints=self.repo,
                            config=ledger.RecoveryConfig(overlap=timedelta(seconds=1), bootstrap_start=self.start),
                            clock=self.clock, ingestor=ingestor, coordinator=self.coordinator)
        dependencies.update(changes)
        return ledger.RecoveryManager(**dependencies), source

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    async def finish(self, task):
        return await asyncio.wait_for(task, timeout=4)

    async def test_approved_completion_uses_exact_planned_end_and_durable_return(self):
        manager, source = self.manager((raw_deal(),))
        advance = self.repo.advance_checkpoint_if_expected
        with patch.object(self.repo, "advance_checkpoint_if_expected", wraps=advance) as write:
            result = await manager.recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)
        self.assertIs(result.reconciliation_result.status, ledger.ReconciliationStatus.APPROVED)
        write.assert_called_once_with(self.account, result.planned_range.to_time, expected=None,
                                      expected_snapshot=write.call_args.kwargs["expected_snapshot"])
        self.assertEqual(result.checkpoint_after, self.repo.get_checkpoint(self.account.mt5_account_id))
        self.assertEqual(result.checkpoint_after.completed_through, self.end)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(self.coordinator._entries, {})

    async def test_unverified_rejected_failed_partial_never_advance(self):
        for case in ("unverified", "conflict", "fetch", "normalize"):
            manager, source = self.manager((raw_deal(),), verified=case != "unverified")
            if case == "conflict":
                source.response = ledger.DealHistoryBatch((raw_deal(), raw_deal(profit=99)))
            elif case == "fetch":
                source.failure = ConnectionError("offline")
            elif case == "normalize":
                source.response = ledger.DealHistoryBatch((raw_deal(), raw_deal(ticket=0)))
            with self.subTest(case=case), patch.object(self.repo, "advance_checkpoint_if_expected",
                                                       side_effect=AssertionError("unauthorized advancement")):
                result = await manager.recover(account=self.account)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION if case == "unverified"
                          else ledger.RecoveryOutcome.FAILED)
            self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))
            self.assertIs(result.checkpoint_after, result.checkpoint_before)
            self.assertEqual(self.coordinator._entries, {})

    async def test_same_account_waits_before_prepare_and_observes_new_checkpoint(self):
        manager, source = self.manager()
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        fetch = source.fetch

        async def gated(*args):
            if not source.fetch_calls:
                entered.set()
                await release.wait()
            return await fetch(*args)

        hold = self.coordinator.hold

        @asynccontextmanager
        async def observed(account_id):
            if entered.is_set():
                queued.set()
            async with hold(account_id):
                yield

        with patch.object(source, "fetch", side_effect=gated), patch.object(self.coordinator, "hold", side_effect=observed), \
                patch.object(manager, "prepare", wraps=manager.prepare) as prepare:
            first = self.task(manager.recover(account=self.account))
            await asyncio.wait_for(entered.wait(), 3)
            self.clock.value = self.end + timedelta(minutes=1)
            second = self.task(manager.recover(account=self.account))
            await asyncio.wait_for(queued.wait(), 3)
            self.assertEqual(prepare.await_count, 1)
            release.set()
            left, right = await self.finish(first), await self.finish(second)
        self.assertIs(left.outcome, ledger.RecoveryOutcome.COMPLETED)
        self.assertIs(right.outcome, ledger.RecoveryOutcome.COMPLETED)
        self.assertEqual(right.checkpoint_before, left.checkpoint_after)
        self.assertEqual(right.planned_range.from_time, left.planned_range.to_time - timedelta(seconds=1))
        self.assertEqual(len(source.fetch_calls), 2)
        self.assertEqual(self.clock.calls, 2)
        self.assertEqual(self.coordinator._entries, {})

    async def test_different_accounts_progress_while_owner_is_blocked(self):
        other = replace(self.account, mt5_account_id=UUID(int=2), login=456)
        self.repo.register_account(other)
        first, source = self.manager()
        second, _ = self.manager(account=other)
        entered, release = asyncio.Event(), asyncio.Event()
        fetch = source.fetch

        async def gated(*args):
            entered.set()
            await release.wait()
            return await fetch(*args)

        with patch.object(source, "fetch", side_effect=gated):
            owner = self.task(first.recover(account=self.account))
            await asyncio.wait_for(entered.wait(), 3)
            result = await asyncio.wait_for(second.recover(account=other), 3)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)
            self.assertFalse(owner.done())
            release.set()
            await self.finish(owner)
        self.assertEqual(self.coordinator._entries, {})

    async def test_waiter_cancellation_does_not_release_owner_and_owner_cancellation_cleans_up(self):
        acquired, queued = asyncio.Event(), asyncio.Event()

        async def owner():
            async with self.coordinator.hold(self.account.mt5_account_id):
                acquired.set()
                await asyncio.Event().wait()

        async def waiting():
            queued.set()
            async with self.coordinator.hold(self.account.mt5_account_id):
                self.fail("waiter acquired before owner released")

        task = self.task(owner())
        await asyncio.wait_for(acquired.wait(), 3)
        waiter = self.task(waiting())
        await asyncio.wait_for(queued.wait(), 3)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.coordinator._entries, {})
        async with self.coordinator.hold(self.account.mt5_account_id):
            pass
        self.assertEqual(self.coordinator._entries, {})

    async def test_each_collaborator_exception_releases_ownership_and_can_run_again(self):
        module = importlib.import_module(f"{ledger.__name__}.reconciliation")
        for stage in ("ingestion", "snapshot", "evaluation", "advance"):
            manager, _ = self.manager()
            target, name = {"ingestion": (manager.ingestor, "ingest_range_with_evidence"),
                            "snapshot": (self.repo, "read_snapshot"),
                            "evaluation": (module, "evaluate_reconciliation"),
                            "advance": (self.repo, "advance_checkpoint_if_expected")}[stage]
            with self.subTest(stage=stage), patch.object(target, name, side_effect=RuntimeError(stage)) as fail:
                with self.assertRaises(RuntimeError):
                    await manager.recover(account=self.account)
                self.assertEqual(fail.call_count, 1)
            self.assertEqual(self.coordinator._entries, {})
            result = await manager.recover(account=self.account)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)

    async def test_stale_cas_is_not_retried_or_converted_to_reconciliation_reason(self):
        manager, _ = self.manager()
        with patch.object(self.repo, "advance_checkpoint_if_expected", side_effect=ledger.CheckpointExpectationError("stale")) as write:
            with self.assertRaises(ledger.CheckpointExpectationError):
                await manager.recover(account=self.account)
        write.assert_called_once()
        self.assertIsNone(self.repo.get_checkpoint(self.account.mt5_account_id))
        self.assertNotIn("STALE_CHECKPOINT", ledger.ReconciliationReason.__members__)
        self.assertEqual(self.coordinator._entries, {})


if __name__ == "__main__":
    unittest.main()
