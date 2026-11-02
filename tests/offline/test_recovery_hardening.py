"""A4.9 adversarial boundaries, stale evidence and durable restart behavior."""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import importlib
import sqlite3
from unittest.mock import patch
import unittest
from uuid import UUID

import test_conditional_recovery as fixtures
from test_deal_normalizer import ledger, raw_deal


class RecoveryHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ConditionalRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.repo, self.account = fixture.repo, fixture.account
        self.start, self.end, self.clock = fixture.start, fixture.end, fixture.clock
        self.coordinator = fixture.coordinator
        self.manager = fixture.manager
        self.evaluator = importlib.import_module(f"{ledger.__name__}.reconciliation")
        self.manager_module = importlib.import_module(f"{ledger.__name__}.recovery_manager")
        self.ingestion_module = importlib.import_module(f"{ledger.__name__}.ingestion")

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    def reopened_checkpoint(self, account=None):
        repo = ledger.SQLiteDealRepository(self.repo.path)
        try:
            return repo.get_checkpoint((account or self.account).mt5_account_id)
        finally:
            repo.close()

    async def test_each_precommit_stage_failure_has_no_false_completion_or_advancement(self):
        for stage in ("identity", "checkpoint", "clock", "planner", "before", "fetch", "after",
                      "normalize", "storage", "snapshot", "evaluate", "advance"):
            manager, source = self.manager((raw_deal(),))
            target, name = {"identity": (self.repo, "resolve_account"),
                            "checkpoint": (self.repo, "get_checkpoint"),
                            "clock": (manager, "clock"),
                            "planner": (self.manager_module, "plan_recovery_range"),
                            "before": (source, "current_account"), "fetch": (source, "fetch"),
                            "after": (source, "current_account"),
                            "normalize": (self.ingestion_module, "normalize_deal"),
                            "storage": (self.repo, "ingest"), "snapshot": (self.repo, "read_snapshot"),
                            "evaluate": (self.evaluator, "evaluate_reconciliation"),
                            "advance": (self.repo, "advance_checkpoint_if_expected")}[stage]
            failure = RuntimeError(stage)
            side_effect = [source.account, failure] if stage == "after" else failure
            with self.subTest(stage=stage), patch.object(target, name, side_effect=side_effect) as fail:
                if stage in ("before", "fetch", "after", "normalize", "storage"):
                    result = await manager.recover(account=self.account)
                    self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
                else:
                    with self.assertRaises(RuntimeError) as raised:
                        await manager.recover(account=self.account)
                    self.assertIs(raised.exception, failure)
                self.assertEqual(fail.call_count, 2 if stage == "after" else 1)
            self.assertIsNone(self.reopened_checkpoint())
            self.assertEqual(self.coordinator._entries, {})

    async def test_cancellation_at_each_stage_releases_owner_and_preserves_durable_boundary(self):
        for stage in ("identity", "checkpoint", "clock", "planner", "before", "fetch", "after",
                      "normalize", "storage", "snapshot", "evaluate", "advance"):
            manager, source = self.manager((raw_deal(),))
            target, name = {"identity": (self.repo, "resolve_account"),
                            "checkpoint": (self.repo, "get_checkpoint"), "clock": (manager, "clock"),
                            "planner": (self.manager_module, "plan_recovery_range"),
                            "before": (source, "current_account"), "fetch": (source, "fetch"),
                            "after": (source, "current_account"),
                            "normalize": (self.ingestion_module, "normalize_deal"),
                            "storage": (self.repo, "ingest"), "snapshot": (self.repo, "read_snapshot"),
                            "evaluate": (self.evaluator, "evaluate_reconciliation"),
                            "advance": (self.repo, "advance_checkpoint_if_expected")}[stage]
            failure = asyncio.CancelledError(stage)
            with self.subTest(stage=stage), patch.object(target, name,
                    side_effect=[source.account, failure] if stage == "after" else failure) as fail:
                with self.assertRaises(asyncio.CancelledError) as raised:
                    await manager.recover(account=self.account)
                self.assertIs(raised.exception, failure)
                self.assertEqual(fail.call_count, 2 if stage == "after" else 1)
            self.assertIsNone(self.reopened_checkpoint())
            self.assertEqual(self.coordinator._entries, {})
        result = await self.manager((raw_deal(),))[0].recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)

    async def test_cancelled_before_start_never_acquires_or_reads(self):
        manager, source = self.manager()
        with patch.object(manager, "prepare", wraps=manager.prepare) as prepare:
            task = self.task(manager.recover(account=self.account))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        prepare.assert_not_called()
        self.assertEqual(source.fetch_calls, [])
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(self.coordinator._entries, {})

    async def test_cancellation_racing_with_handoff_releases_grant_for_next_waiter(self):
        held, release, queued_one, queued_two, survived = (asyncio.Event() for _ in range(5))

        async def owner():
            async with self.coordinator.hold(self.account.mt5_account_id):
                held.set()
                await release.wait()

        async def waiter(queued, success):
            queued.set()
            async with self.coordinator.hold(self.account.mt5_account_id):
                if success:
                    survived.set()
                else:
                    self.fail("cancelled grant entered critical section")

        first = self.task(owner())
        await asyncio.wait_for(held.wait(), 3)
        cancelled = self.task(waiter(queued_one, False))
        await asyncio.wait_for(queued_one.wait(), 3)
        following = self.task(waiter(queued_two, True))
        await asyncio.wait_for(queued_two.wait(), 3)
        wake = self.coordinator._wake

        def cancel_grant(waiting):
            if waiting.task is cancelled:
                cancelled.cancel()
            else:
                wake(waiting)

        with patch.object(self.coordinator, "_wake", side_effect=cancel_grant):
            release.set()
            await asyncio.wait_for(first, 3)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(cancelled, 3)
            await asyncio.wait_for(following, 3)
        self.assertTrue(survived.is_set())
        self.assertEqual(self.coordinator._entries, {})

    async def test_two_independent_manager_domains_prepare_same_state_only_one_cas_completes(self):
        other = ledger.SQLiteDealRepository(self.repo.path)
        self.addCleanup(other.close)
        left, source_left = self.manager()
        right, source_right = self.manager(account_lookup=other, checkpoints=other, snapshot_reader=other,
                                           coordinator=ledger.AccountRecoveryCoordinator())
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]

        def gated_fetch(source, index):
            fetch = source.fetch

            async def gated(*args):
                entered[index].set()
                await release[index].wait()
                return await fetch(*args)
            return gated

        with patch.object(source_left, "fetch", side_effect=gated_fetch(source_left, 0)), \
                patch.object(source_right, "fetch", side_effect=gated_fetch(source_right, 1)), \
                patch.object(other, "advance_checkpoint_if_expected", wraps=other.advance_checkpoint_if_expected) as lose:
            first, second = self.task(left.recover(account=self.account)), self.task(right.recover(account=self.account))
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 3)
            release[0].set()
            winner = await asyncio.wait_for(first, 3)
            release[1].set()
            with self.assertRaises(ledger.CheckpointExpectationError):
                await asyncio.wait_for(second, 3)
        self.assertIs(winner.outcome, ledger.RecoveryOutcome.COMPLETED)
        lose.assert_called_once()
        self.assertEqual(self.reopened_checkpoint(), winner.checkpoint_after)
        self.assertEqual((len(source_left.fetch_calls), len(source_right.fetch_calls)), (1, 1))

    async def test_contradictory_ledger_change_after_approval_prevents_completion(self):
        manager, source = self.manager((raw_deal(),))
        evaluate = ledger.evaluate_reconciliation

        def changed(**kwargs):
            result = evaluate(**kwargs)
            self.assertIs(result.status, ledger.ReconciliationStatus.APPROVED)
            self.repo.ingest(self.account, ledger.normalize_deal(self.account, raw_deal(profit=99), self.start, "new-conflict"))
            return result

        with patch.object(self.evaluator, "evaluate_reconciliation", side_effect=changed), \
                patch.object(self.repo, "advance_checkpoint_if_expected", wraps=self.repo.advance_checkpoint_if_expected) as write:
            with self.assertRaises(ledger.LedgerSnapshotChangedError):
                await manager.recover(account=self.account)
        write.assert_called_once()
        self.assertIsNone(self.reopened_checkpoint())
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.coordinator._entries, {})

    async def test_new_relevant_canonical_row_invalidates_but_unrelated_mutations_do_not(self):
        other = replace(self.account, mt5_account_id=UUID(int=2), login=456)
        self.repo.register_account(other)
        evaluate = ledger.evaluate_reconciliation
        for kind in ("relevant", "other_account", "outside_range"):
            manager, _ = self.manager((raw_deal(),))
            account = other if kind == "other_account" else self.account
            stamp = 1700000000123 if kind != "outside_range" else 1700000000123 + 86400000
            ticket = {"relevant": 9002, "other_account": 9003, "outside_range": 9004}[kind]

            def changed(**kwargs):
                result = evaluate(**kwargs)
                self.repo.ingest(account, ledger.normalize_deal(account, raw_deal(ticket=ticket, time_msc=stamp), self.start, kind))
                return result

            with self.subTest(kind=kind), patch.object(self.evaluator, "evaluate_reconciliation", side_effect=changed):
                if kind == "relevant":
                    with self.assertRaises(ledger.LedgerSnapshotChangedError):
                        await manager.recover(account=self.account)
                    # Remove the disposable fault so later cases have clean evidence.
                    with closing(sqlite3.connect(self.repo.path)) as conn, conn:
                        conn.execute("DELETE FROM deals WHERE deal_ticket = '9002'")
                else:
                    result = await manager.recover(account=self.account)
                    self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)
            # Independent fixture cases; later attempts must use the same range.
            with closing(sqlite3.connect(self.repo.path)) as conn, conn:
                conn.execute("DELETE FROM ledger_checkpoints")

    async def test_execution_time_correction_from_outside_into_range_invalidates_approval(self):
        outside = raw_deal(time_msc=1700000000123 + 86400000)
        self.repo.ingest(self.account, ledger.normalize_deal(self.account, outside, self.start, "outside"))
        manager, _ = self.manager()
        evaluate = ledger.evaluate_reconciliation

        def changed(**kwargs):
            result = evaluate(**kwargs)
            self.assertIs(result.status, ledger.ReconciliationStatus.APPROVED)
            self.repo.ingest(self.account, ledger.normalize_deal(self.account, raw_deal(), self.start, "corrected-time"))
            return result

        with patch.object(self.evaluator, "evaluate_reconciliation", side_effect=changed):
            with self.assertRaises(ledger.LedgerSnapshotChangedError):
                await manager.recover(account=self.account)
        self.assertIsNone(self.reopened_checkpoint())

    async def test_crash_before_advance_retains_ledger_then_replays_on_restart(self):
        manager, _ = self.manager((raw_deal(),))
        with patch.object(self.evaluator, "evaluate_reconciliation", side_effect=RuntimeError("process lost before advance")):
            with self.assertRaises(RuntimeError):
                await manager.recover(account=self.account)
        self.assertIsNone(self.reopened_checkpoint())
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        repo = ledger.SQLiteDealRepository(self.repo.path)
        self.addCleanup(repo.close)
        restarted, _ = self.manager((raw_deal(),), account_lookup=repo, checkpoints=repo, snapshot_reader=repo)
        result = await restarted.recover(account=self.account)
        self.assertEqual(result.ingestion_report.duplicates, 1)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)
        self.assertEqual(self.reopened_checkpoint(), result.checkpoint_after)

    async def test_cancellation_after_per_record_commit_releases_lock_and_replay_completes(self):
        manager, _ = self.manager((raw_deal(), raw_deal(ticket=9002)))
        ingest = self.repo.ingest

        def interrupted(account, deal):
            if deal.deal_ticket == 9002:
                raise asyncio.CancelledError("second record")
            return ingest(account, deal)

        with patch.object(self.repo, "ingest", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await manager.recover(account=self.account)
        self.assertIsNone(self.reopened_checkpoint())
        self.assertEqual(self.coordinator._entries, {})
        result = await manager.recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)
        self.assertEqual((result.ingestion_report.duplicates, result.ingestion_report.inserted), (1, 1))

    async def test_result_failure_after_commit_preserves_new_boundary_and_restart_uses_it(self):
        manager, _ = self.manager()
        with patch.object(self.manager_module, "RecoveryResult", side_effect=RuntimeError("lost result")):
            with self.assertRaises(RuntimeError):
                await manager.recover(account=self.account)
        durable = self.reopened_checkpoint()
        self.assertEqual(durable.completed_through, self.end)
        self.assertEqual(self.coordinator._entries, {})
        self.clock.value = self.end + timedelta(minutes=1)
        restarted, _ = self.manager()
        result = await restarted.recover(account=self.account)
        self.assertEqual(result.checkpoint_before, durable)
        self.assertEqual(result.planned_range.from_time, durable.completed_through - timedelta(seconds=1))
        self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)

    async def test_cancellation_after_commit_before_return_does_not_undo_durable_progress(self):
        manager, _ = self.manager()
        with patch.object(self.manager_module, "RecoveryResult", side_effect=asyncio.CancelledError("after commit")):
            with self.assertRaises(asyncio.CancelledError):
                await manager.recover(account=self.account)
        self.assertEqual(self.reopened_checkpoint().completed_through, self.end)
        self.assertEqual(self.coordinator._entries, {})

    async def test_invalid_advancement_return_cannot_produce_false_completed_result(self):
        manager, _ = self.manager()
        for checkpoint in (None, ledger.LedgerCheckpoint(self.account.mt5_account_id, self.start, self.start),
                           ledger.LedgerCheckpoint(UUID(int=2), self.end, self.start)):
            with self.subTest(checkpoint=checkpoint), patch.object(self.repo, "advance_checkpoint_if_expected", return_value=checkpoint):
                with self.assertRaises((TypeError, ValueError)):
                    await manager.recover(account=self.account)
        self.assertIsNone(self.reopened_checkpoint())


if __name__ == "__main__":
    unittest.main()
