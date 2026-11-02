"""A4.7 orchestration: actual ingestion, consistent snapshots, no advancement."""

import asyncio
from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
from datetime import timedelta
import importlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from uuid import UUID

import test_deal_ingestion as fixtures
from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal
from test_ledger_recovery_manager import CountingClock


class RecoveryReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.DealIngestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo, self.account = fixture.repo, fixture.account
        self.start = fixture.start.replace(microsecond=123000)
        self.end = self.start + timedelta(minutes=1, microseconds=456)
        self.clock = CountingClock(self.end, [])
        self.reader = Mock(spec=ledger.LedgerSnapshotReader)
        self.reader.read_snapshot.side_effect = self.repo.read_snapshot
        self.module = importlib.import_module(f"{ledger.__name__}.reconciliation")
        self.advance = self.enterContext(patch.object(self.repo, "advance_checkpoint", side_effect=AssertionError("advance")))
        self.register = self.enterContext(patch.object(self.repo, "register_account", side_effect=AssertionError("register")))
        self.addCleanup(self.advance.assert_not_called)
        self.addCleanup(self.register.assert_not_called)
        self.addCleanup(self.assert_checkpoints_unchanged)
        self.before_rows = ()

    def assert_checkpoints_unchanged(self):
        with closing(sqlite3.connect(self.repo.path)) as conn:
            self.assertEqual(tuple(conn.execute("SELECT * FROM ledger_checkpoints")), self.before_rows)

    def manager(self, records=(), **changes):
        source = FakeSource(self.account, records)
        ingestor = ledger.DealIngestor(source, self.repo)
        dependencies = dict(account_lookup=self.repo, checkpoints=self.repo,
                            config=ledger.RecoveryConfig(overlap=timedelta(seconds=1), bootstrap_start=self.start),
                            clock=self.clock, ingestor=ingestor, snapshot_reader=self.reader)
        dependencies.update(changes)
        return ledger.RecoveryManager(**dependencies), source, ingestor

    async def test_success_and_empty_reuse_exact_attempt_report_evidence_and_one_fetch_clock(self):
        for records in ((raw_deal(),), ()):
            self.reader.reset_mock()
            self.clock.calls = 0
            manager, source, ingestor = self.manager(records)
            attempts, executions, snapshots, events = [], [], [], []
            prepare = manager.prepare
            ingest = ingestor.ingest_range_with_evidence

            async def preparation(**kwargs):
                attempt = await prepare(**kwargs)
                attempts.append(attempt)
                events.append("prepare")
                return attempt

            async def ingestion(**kwargs):
                execution = await ingest(**kwargs)
                executions.append(execution)
                events.append("ingest")
                return execution

            def snapshot(**kwargs):
                result = self.repo.read_snapshot(**kwargs)
                snapshots.append(result)
                events.append("snapshot")
                return result

            def evaluation(**kwargs):
                events.append("evaluate")
                return ledger.evaluate_reconciliation(**kwargs)

            self.reader.read_snapshot.side_effect = snapshot

            with patch.object(manager, "prepare", side_effect=preparation) as prep, \
                    patch.object(ingestor, "ingest_range_with_evidence", side_effect=ingestion) as execute, \
                    patch.object(ingestor, "ingest_range", side_effect=AssertionError("legacy ingestion")), \
                    patch.object(self.module, "evaluate_reconciliation", side_effect=evaluation) as evaluate:
                result = await manager.recover(account=self.account)
            attempt, execution = attempts[0], executions[0]
            prep.assert_awaited_once_with(account=self.account)
            execute.assert_awaited_once_with(account_ref=attempt.account, from_time=attempt.planned_range.from_time,
                                             to_time=attempt.planned_range.to_time)
            self.reader.read_snapshot.assert_called_once_with(account=attempt.account, requested_range=attempt.planned_range)
            args = evaluate.call_args.kwargs
            evaluate.assert_called_once()
            self.assertIs(args["attempt"], attempt)
            self.assertIs(args["ingestion_report"], execution.report)
            self.assertIs(args["source_evidence"], execution.source_evidence)
            self.assertIs(args["ledger_evidence"], snapshots[0])
            self.assertEqual(events, ["prepare", "ingest", "snapshot", "evaluate"])
            self.assertIs(result.reconciliation_result.attempt, attempt)
            self.assertIs(result.reconciliation_result.ingestion_report, execution.report)
            self.assertIs(result.account, attempt.account)
            self.assertIs(result.planned_range, attempt.planned_range)
            self.assertIs(result.ingestion_report, execution.report)
            self.assertIs(result.reconciliation_result.status, ledger.ReconciliationStatus.INDETERMINATE)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
            self.assertIs(result.checkpoint_after, result.checkpoint_before)
            self.assertEqual(len(source.fetch_calls), 1)
            self.assertEqual(self.clock.calls, 1)

    async def test_approved_completes_only_after_durable_conditional_advancement(self):
        manager, source, ingestor = self.manager((raw_deal(),))
        ingest = ingestor.ingest_range_with_evidence

        async def stronger_contract(**kwargs):
            execution = await ingest(**kwargs)
            return replace(execution, source_evidence=replace(execution.source_evidence,
                           assurance=ledger.SourceHistoryAssurance.VERIFIED_COMPLETE))

        with patch.object(ingestor, "ingest_range_with_evidence", side_effect=stronger_contract):
            result = await manager.recover(account=self.account)
        self.assertIs(result.reconciliation_result.status, ledger.ReconciliationStatus.APPROVED)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.COMPLETED)
        self.assertIsNone(result.checkpoint_before)
        self.assertEqual(result.checkpoint_after, self.repo.get_checkpoint(self.account.mt5_account_id))
        checkpoint = result.checkpoint_after
        self.before_rows = ((str(self.account.mt5_account_id), checkpoint.completed_through.isoformat(timespec="microseconds"),
                             checkpoint.updated_at.isoformat(timespec="microseconds")),)
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertIn("COMPLETED", ledger.RecoveryOutcome.__members__)

    async def test_rejected_conflict_and_later_duplicates_only_both_fail(self):
        manager, source, _ = self.manager((raw_deal(), raw_deal(profit=99)))
        result = await manager.recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(result.reconciliation_result.reason, ledger.ReconciliationReason.CONFLICT_PRESENT)
        source.response = ledger.DealHistoryBatch((raw_deal(),))
        later = await manager.recover(account=self.account)
        self.assertEqual(later.ingestion_report.duplicates, 1)
        self.assertEqual(later.ingestion_report.conflicts, 0)
        self.assertIs(later.reconciliation_result.reason, ledger.ReconciliationReason.CONFLICT_PRESENT)
        self.assertIs(later.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertEqual(len(source.fetch_calls), 2)

    async def test_failed_and_partial_ingestion_skip_snapshot_and_evaluator(self):
        for failure in ("fetch", "normalize", "storage", "before", "after"):
            self.reader.reset_mock()
            manager, source, _ = self.manager((raw_deal(), raw_deal(ticket=9002)))
            context = patch.object(self.module, "evaluate_reconciliation", side_effect=AssertionError("evaluate"))
            if failure == "fetch":
                source.failure = ConnectionError("offline")
            elif failure == "normalize":
                source.response = ledger.DealHistoryBatch((raw_deal(), raw_deal(ticket=0)))
            elif failure in ("before", "after"):
                setattr(source, failure, ledger.SessionAccount(999, "other"))
            original = self.repo.ingest

            def write(account, deal):
                if failure == "storage" and deal.deal_ticket == 9002:
                    raise OSError("storage")
                return original(account, deal)

            with self.subTest(failure=failure), context, patch.object(self.repo, "ingest", side_effect=write):
                result = await manager.recover(account=self.account)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
            self.assertIsNone(result.reconciliation_result)
            self.assertIs(result.checkpoint_after, result.checkpoint_before)
            self.reader.read_snapshot.assert_not_called()
            self.assertEqual(len(source.fetch_calls), 0 if failure == "before" else 1)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))

    async def test_snapshot_failure_or_cancellation_propagates_without_retry_or_compensation(self):
        for failure in (sqlite3.OperationalError("snapshot"), asyncio.CancelledError("snapshot")):
            self.reader.reset_mock()
            self.reader.read_snapshot.side_effect = failure
            manager, source, _ = self.manager((raw_deal(),))
            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)) as raised:
                await manager.recover(account=self.account)
            self.assertIs(raised.exception, failure)
            self.assertEqual(self.reader.read_snapshot.call_count, 1)
            self.assertEqual(len(source.fetch_calls), 1)
            self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))

    async def test_evaluator_failure_or_cancellation_propagates_without_retry(self):
        for failure in (RuntimeError("evaluator"), asyncio.CancelledError("evaluator")):
            manager, source, _ = self.manager((raw_deal(),))
            with patch.object(self.module, "evaluate_reconciliation", side_effect=failure) as evaluate:
                with self.assertRaises(type(failure)) as raised:
                    await manager.recover(account=self.account)
            self.assertIs(raised.exception, failure)
            evaluate.assert_called_once()
            self.assertEqual(len(source.fetch_calls), 1)
            self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))

    async def test_external_cancellation_after_ingestion_leaves_commit_and_no_snapshot(self):
        manager, source, ingestor = self.manager((raw_deal(),))
        ingest = ingestor.ingest_range_with_evidence
        persisted, release = asyncio.Event(), asyncio.Event()

        async def paused(**kwargs):
            execution = await ingest(**kwargs)
            persisted.set()
            await release.wait()
            return execution

        with patch.object(ingestor, "ingest_range_with_evidence", side_effect=paused):
            task = asyncio.create_task(manager.recover(account=self.account))
            try:
                await asyncio.wait_for(persisted.wait(), timeout=3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.reader.read_snapshot.assert_not_called()
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertEqual(len(source.fetch_calls), 1)

    async def test_overlap_uses_original_checkpoint_snapshot_and_one_horizon(self):
        with closing(sqlite3.connect(self.repo.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)",
                         (str(self.account.mt5_account_id), self.start.isoformat(), self.start.isoformat()))
            self.before_rows = tuple(conn.execute("SELECT * FROM ledger_checkpoints"))
        manager, source, _ = self.manager((raw_deal(),))
        result = await manager.recover(account=self.account)
        self.assertEqual(result.planned_range.from_time, self.start - timedelta(seconds=1))
        self.assertIs(result.checkpoint_before, result.checkpoint_after)
        self.assertEqual(result.checkpoint_before.completed_through, self.start)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(source.fetch_calls[0][1:], (result.planned_range.from_time, self.end))

    async def test_different_account_isolation_and_default_shared_repository_reader(self):
        other = replace(self.account, mt5_account_id=UUID(int=2), login=456)
        # Explicit fixture registration, before exercising either recovery.
        with closing(sqlite3.connect(self.repo.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_accounts VALUES (?, ?, ?, ?, ?, ?)",
                         (str(other.mt5_account_id), other.broker_namespace, other.server, str(other.login),
                          other.company_observed, other.trade_mode))
        manager, _, _ = self.manager((raw_deal(),), snapshot_reader=None)
        first = await manager.recover(account=self.account)
        other_source = FakeSource(other, (raw_deal(profit=99),))
        other_manager, _, _ = self.manager(ingestor=ledger.DealIngestor(other_source, self.repo), snapshot_reader=None)
        second = await other_manager.recover(account=other)
        self.assertNotEqual(first.account.mt5_account_id, second.account.mt5_account_id)
        for result in (first, second):
            self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
            self.assertIs(result.reconciliation_result.attempt.account, result.account)

    async def test_unknown_account_stops_before_clock_ingestion_or_snapshot(self):
        manager, source, _ = self.manager()
        with self.assertRaises(ledger.AccountIdentityError):
            await manager.recover(account=replace(self.account, login=999))
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(source.fetch_calls, [])
        self.reader.read_snapshot.assert_not_called()

    async def test_missing_reader_contract_fails_before_side_effects(self):
        manager, source, _ = self.manager(snapshot_reader=object())
        with self.assertRaises(TypeError):
            await manager.recover(account=self.account)
        self.assertEqual(self.clock.calls, 0)
        self.assertEqual(source.fetch_calls, [])

    async def test_invalid_success_execution_is_not_replaced_with_empty_evidence(self):
        manager, _, ingestor = self.manager()
        report = ledger.IngestionReport("bad", self.account.mt5_account_id, self.start, self.end,
                                         ledger.IngestionStatus.SUCCESS_EMPTY)
        with self.assertRaises(ValueError):
            ledger.IngestionExecution(report, None)
        with patch.object(ingestor, "ingest_range_with_evidence", return_value=SimpleNamespace(report=report, source_evidence=None)):
            with self.assertRaises(TypeError):
                await manager.recover(account=self.account)
        self.reader.read_snapshot.assert_not_called()

    async def test_recovery_result_optional_field_preserves_legacy_constructor_and_validation(self):
        manager, _, _ = self.manager()
        result = await manager.recover(account=self.account)
        legacy = ledger.RecoveryResult(result.account, result.checkpoint_before, result.planned_range,
                                       result.ingestion_report, result.outcome, result.checkpoint_after)
        self.assertIsNone(legacy.reconciliation_result)
        self.assertEqual(tuple(f.name for f in fields(result))[-1], "reconciliation_result")
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.reconciliation_result = None
        with self.assertRaises(TypeError):
            replace(result, reconciliation_result=object())


if __name__ == "__main__":
    unittest.main()
