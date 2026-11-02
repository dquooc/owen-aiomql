"""One-shot manager ingestion with fake sources and temporary SQLite only."""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal
from test_ledger_recovery_manager import Checkpoints, CountingClock, Lookup


class ReportIngestor:
    def __init__(self, report, events):
        self.report = report
        self.events = events
        self.calls = []
        self.failure = None
        self.on_ingest = None

    async def ingest_range(self, account_ref, from_time, to_time, *, ingestion_id=None):
        self.events.append("ingestion")
        self.calls.append((account_ref, from_time, to_time))
        if self.on_ingest is not None:
            self.on_ingest()
        if self.failure is not None:
            raise self.failure
        return self.report

    async def ingest_range_with_evidence(self, account_ref, from_time, to_time, *, ingestion_id=None):
        report = await self.ingest_range(account_ref, from_time, to_time, ingestion_id=ingestion_id)
        evidence = None if report.status is ledger.IngestionStatus.FAILED else ledger.SourceBatchEvidence(
            report.ingestion_id, account_ref, ledger.RecoveryRange(report.requested_from, report.requested_to),
            ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED, report.retrieved, ())
        return ledger.IngestionExecution(report, evidence)


class RecoveryManagerIngestionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger.RecoveryManager, "recover"), "RecoveryManager must expose recover")
        self.account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        self.checkpoint = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.start, self.start)
        self.config = ledger.RecoveryConfig(overlap=timedelta(minutes=5, microseconds=1),
                                             bootstrap_start=self.start)
        self.events = []
        self.lookup = Lookup(self.account, self.events)
        self.checkpoints = Checkpoints(self.events)
        self.checkpoints.result = self.checkpoint
        self.clock = CountingClock(self.now, self.events)
        self.report = ledger.IngestionReport(
            ingestion_id="recovery-1", mt5_account_id=self.account.mt5_account_id,
            requested_from=datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc),
            requested_to=self.now, status=ledger.IngestionStatus.SUCCESS,
            retrieved=1, normalized=1, inserted=1)
        self.ingestor = ReportIngestor(self.report, self.events)

    def manager(self, **changes):
        dependencies = dict(account_lookup=self.lookup, checkpoints=self.checkpoints,
                            config=self.config, clock=self.clock, ingestor=self.ingestor)
        dependencies.update(changes)
        return ledger.RecoveryManager(**dependencies)

    async def test_success_reuses_prepare_and_preserves_exact_attempt_report_and_snapshot(self):
        manager = self.manager()
        original_prepare = manager.prepare
        attempts = []

        async def observed_prepare(*, account):
            attempt = await original_prepare(account=account)
            attempts.append(attempt)
            return attempt

        def external_change():
            self.checkpoints.result = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.now, self.now)
            self.clock.value = self.now + timedelta(days=1)

        self.ingestor.on_ingest = external_change
        module = importlib.import_module(f"{ledger.__name__}.recovery_manager")
        with patch.object(manager, "prepare", side_effect=observed_prepare), \
                patch.object(module, "plan_recovery_range", wraps=ledger.plan_recovery_range) as planner:
            result = await manager.recover(account=self.account)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertIsInstance(result, ledger.RecoveryResult)
        self.assertIs(result.account, attempt.account)
        self.assertIs(result.checkpoint_before, attempt.checkpoint_before)
        self.assertIs(result.checkpoint_after, attempt.checkpoint_before)
        self.assertIs(result.planned_range, attempt.planned_range)
        self.assertIs(result.ingestion_report, self.report)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual(self.ingestor.calls, [(attempt.account, attempt.planned_range.from_time,
                                              attempt.planned_range.to_time)])
        self.assertIs(self.ingestor.calls[0][0], attempt.account)
        self.assertIs(self.ingestor.calls[0][1], attempt.planned_range.from_time)
        self.assertIs(self.ingestor.calls[0][2], attempt.planned_range.to_time)
        self.assertEqual(result.planned_range.to_time, self.now)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(self.checkpoints.calls, [self.account.mt5_account_id])
        self.assertEqual(self.events, ["identity", "checkpoint", "clock", "ingestion"])

    async def test_empty_success_remains_pending_with_no_checkpoint(self):
        self.checkpoints.result = None
        report = replace(self.report, status=ledger.IngestionStatus.SUCCESS_EMPTY,
                         requested_from=self.start, retrieved=0, normalized=0, inserted=0)
        self.ingestor.report = report
        result = await self.manager().recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIs(result.ingestion_report, report)
        self.assertIsNone(result.checkpoint_before)
        self.assertIsNone(result.checkpoint_after)
        self.assertEqual(len(self.ingestor.calls), 1)

    async def test_success_with_conflicts_keeps_original_report_and_rejects_reconciliation(self):
        report = replace(self.report, retrieved=3, normalized=3, inserted=1, duplicates=1, conflicts=1)
        self.ingestor.report = report
        result = await self.manager().recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(result.reconciliation_result.reason, ledger.ReconciliationReason.CONFLICT_PRESENT)
        self.assertIs(result.ingestion_report, report)
        self.assertEqual((report.inserted, report.duplicates, report.conflicts), (1, 1, 1))
        self.assertIs(result.checkpoint_after, self.checkpoint)

    async def test_failed_report_preserves_errors_without_retry(self):
        report = replace(self.report, status=ledger.IngestionStatus.FAILED, retrieved=0, normalized=0, inserted=0,
                         errors=(ledger.IngestionError(ledger.IngestionStage.FETCH, "ConnectionError", "offline"),))
        self.ingestor.report = report
        result = await self.manager().recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(result.ingestion_report, report)
        self.assertIs(result.checkpoint_before, self.checkpoint)
        self.assertIs(result.checkpoint_after, self.checkpoint)
        self.assertEqual(len(self.ingestor.calls), 1)

    async def test_partial_report_maps_to_failed_without_altering_counters(self):
        report = replace(self.report, status=ledger.IngestionStatus.PARTIAL, retrieved=2, invalid=1,
                         errors=(ledger.IngestionError(ledger.IngestionStage.NORMALIZE, "ValueError", "bad row", 1),))
        self.ingestor.report = report
        result = await self.manager().recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(result.ingestion_report, report)
        self.assertEqual((report.inserted, report.invalid), (1, 1))
        self.assertIs(result.checkpoint_after, self.checkpoint)
        self.assertEqual(len(self.ingestor.calls), 1)

    async def test_preparation_failures_never_invoke_ingestion(self):
        for stage in ("identity", "checkpoint", "clock", "bootstrap", "future", "underflow"):
            events = []
            lookup = Lookup(self.account, events)
            checkpoints = Checkpoints(events)
            checkpoints.result = self.checkpoint
            clock = CountingClock(self.now, events)
            config = self.config
            failure = None
            error_type = ValueError
            if stage == "identity":
                lookup.account = None
                error_type = ledger.AccountIdentityError
            elif stage == "checkpoint":
                failure = sqlite3.OperationalError("offline checkpoint load")
                checkpoints.failure = failure
                error_type = sqlite3.OperationalError
            elif stage == "clock":
                failure = RuntimeError("offline clock")
                clock.failure = failure
                error_type = RuntimeError
            elif stage == "bootstrap":
                checkpoints.result = None
                config = ledger.RecoveryConfig(overlap=timedelta(0))
            elif stage == "future":
                checkpoints.result = ledger.LedgerCheckpoint(
                    self.account.mt5_account_id, self.now + timedelta(microseconds=1), self.start)
            else:
                checkpoints.result = ledger.LedgerCheckpoint(
                    self.account.mt5_account_id, datetime(1, 1, 1, tzinfo=timezone.utc), self.start)
                error_type = OverflowError
            manager = self.manager(account_lookup=lookup, checkpoints=checkpoints, clock=clock, config=config)
            with self.subTest(stage=stage), self.assertRaises(error_type) as raised:
                await manager.recover(account=self.account)
            if failure is not None:
                self.assertIs(raised.exception, failure)
            self.assertEqual(self.ingestor.calls, [])
            self.assertEqual(clock.calls, 0 if stage in ("identity", "checkpoint") else 1)

    async def test_escaping_ingestion_exception_propagates_without_fabricated_result(self):
        failure = RuntimeError("unexpected ingestor failure")
        self.ingestor.failure = failure
        with self.assertRaises(RuntimeError) as raised:
            await self.manager().recover(account=self.account)
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(self.ingestor.calls), 1)
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.checkpoints.calls, [self.account.mt5_account_id])

    async def test_ingestion_cancellation_propagates_without_result_or_retry(self):
        failure = asyncio.CancelledError()
        self.ingestor.failure = failure
        with self.assertRaises(asyncio.CancelledError) as raised:
            await self.manager().recover(account=self.account)
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(self.ingestor.calls), 1)
        self.assertEqual(self.clock.calls, 1)

    async def test_non_utc_horizon_reaches_ingestion_as_exact_canonical_range(self):
        self.clock.value = datetime(2026, 11, 1, 17, 20, 0, 654321, tzinfo=timezone(timedelta(hours=7)))
        result = await self.manager().recover(account=self.account)
        self.assertEqual(self.ingestor.calls,
                         [(self.account, datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc), self.now)])
        self.assertIs(self.ingestor.calls[0][1], result.planned_range.from_time)
        self.assertIs(self.ingestor.calls[0][2], result.planned_range.to_time)
        self.assertIs(result.planned_range.to_time.tzinfo, timezone.utc)
        self.assertEqual(self.clock.calls, 1)

    async def test_prepare_remains_available_without_running_injected_ingestor(self):
        await self.manager().prepare(account=self.account)
        self.assertEqual(self.ingestor.calls, [])

    async def test_recover_without_ingestor_fails_before_preparation(self):
        with self.assertRaisesRegex(ValueError, "ingestor"):
            await self.manager(ingestor=None).recover(account=self.account)
        self.assertEqual(self.events, [])


class RecoveryManagerIngestionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue(hasattr(ledger.RecoveryManager, "recover"), "RecoveryManager must expose recover")
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.repo.close)
        self.account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        self.repo.register_account(self.account)  # Disposable fixture setup only.
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.clock = CountingClock(self.now, [])
        self.checkpoint_rows = []
        self.addCleanup(self.assert_checkpoint_unchanged)

    def raw(self, ticket=9001, **changes):
        seconds = int(self.start.timestamp())
        return raw_deal(ticket=ticket, time=seconds, time_msc=seconds * 1000, **changes)

    def seed_checkpoint(self):
        row = (str(self.account.mt5_account_id), self.start.isoformat(), self.start.isoformat())
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)", row)
        self.checkpoint_rows = [row]

    def assert_checkpoint_unchanged(self):
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM ledger_checkpoints ORDER BY mt5_account_id").fetchall(),
                             self.checkpoint_rows)

    def manager(self, source):
        return ledger.RecoveryManager(
            account_lookup=self.repo, checkpoints=self.repo, clock=self.clock,
            config=ledger.RecoveryConfig(overlap=timedelta(minutes=5), bootstrap_start=self.start),
            ingestor=ledger.DealIngestor(source, self.repo))

    async def test_bootstrap_ingests_deal_but_does_not_create_checkpoint(self):
        source = FakeSource(self.account, [self.raw()])
        result = await self.manager(source).recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.SUCCESS)
        self.assertEqual(result.ingestion_report.inserted, 1)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertIsNone(result.checkpoint_before)
        self.assertIsNone(result.checkpoint_after)
        self.assertEqual(source.fetch_calls, [(self.account, self.start, self.now)])
        self.assertEqual(self.clock.calls, 1)

    async def test_checkpoint_overlap_replays_duplicate_and_inserts_new_deal(self):
        self.seed_checkpoint()
        original = ledger.normalize_deal(self.account, self.raw(), self.start, "seed")
        self.repo.ingest(self.account, original)
        source = FakeSource(self.account, [self.raw(), self.raw(9002)])
        result = await self.manager(source).recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual((result.ingestion_report.duplicates, result.ingestion_report.inserted), (1, 1))
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9002))
        self.assertEqual(source.fetch_calls,
                         [(self.account, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now)])
        self.assertIs(result.checkpoint_after, result.checkpoint_before)

    async def test_empty_history_is_pending_and_leaves_all_storage_unchanged(self):
        self.seed_checkpoint()
        with closing(sqlite3.connect(self.path)) as conn:
            before = tuple(conn.iterdump())
        source = FakeSource(self.account)
        result = await self.manager(source).recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.SUCCESS_EMPTY)
        self.assertEqual(source.account_calls, 2)
        self.assertIs(result.checkpoint_after, result.checkpoint_before)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(tuple(conn.iterdump()), before)

    async def test_partial_normalization_preserves_committed_deal_and_reports_failed(self):
        source = FakeSource(self.account, [self.raw(), self.raw(9002, volume="bad")])
        result = await self.manager(source).recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.PARTIAL)
        self.assertEqual((result.ingestion_report.inserted, result.ingestion_report.invalid), (1, 1))
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertEqual(len(source.fetch_calls), 1)

    async def test_storage_failure_keeps_earlier_commit_without_checkpoint_progress(self):
        self.seed_checkpoint()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_second BEFORE INSERT ON deals "
                         "WHEN NEW.deal_ticket = '9002' BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
        source = FakeSource(self.account, [self.raw(ticket) for ticket in (9001, 9002, 9003)])
        result = await self.manager(source).recover(account=self.account)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        report = result.ingestion_report
        self.assertIs(report.status, ledger.IngestionStatus.FAILED)
        self.assertEqual((report.retrieved, report.normalized, report.inserted), (3, 2, 1))
        self.assertEqual(report.errors[0].stage, ledger.IngestionStage.STORAGE)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9003))
        self.assertIs(result.checkpoint_after, result.checkpoint_before)
        self.assertEqual(len(source.fetch_calls), 1)

    async def test_ingestion_account_guards_return_failed_without_alternate_identity_retry(self):
        for guard in ("before", "after"):
            self.clock.calls = 0
            source = FakeSource(self.account, [self.raw()])
            setattr(source, guard, ledger.SessionAccount(999, "wrong-server"))
            with self.subTest(guard=guard):
                result = await self.manager(source).recover(account=self.account)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
                self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.FAILED)
                self.assertEqual(result.ingestion_report.errors[0].exception_type, "AccountIdentityError")
                self.assertEqual(len(source.fetch_calls), 0 if guard == "before" else 1)
                self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9001))
                self.assertEqual(self.clock.calls, 1)

    async def test_cancellation_after_first_deal_keeps_its_commit(self):
        self.seed_checkpoint()
        original_ingest = self.repo.ingest

        def cancel_second(account, deal):
            if deal.deal_ticket == 9002:
                raise asyncio.CancelledError()
            return original_ingest(account, deal)

        source = FakeSource(self.account, [self.raw(), self.raw(9002)])
        with patch.object(self.repo, "ingest", side_effect=cancel_second):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager(source).recover(account=self.account)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9002))
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(self.clock.calls, 1)


if __name__ == "__main__":
    unittest.main()
