"""Adversarial no-advance/result-boundary guarantees; no production changes."""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal
from test_ledger_recovery_manager import Checkpoints, CountingClock, Lookup
from test_ledger_recovery_manager_ingestion import ReportIngestor


class RecoveryFailureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = importlib.import_module(f"{ledger.__name__}.recovery_manager")
        self.account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        self.start = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.checkpoint = ledger.LedgerCheckpoint(self.account.mt5_account_id, self.start, self.start)

    def collaborators(self):
        events = []
        lookup = Lookup(self.account, events)
        checkpoints = Checkpoints(events)
        checkpoints.result = self.checkpoint
        checkpoints.advance_checkpoint = Mock(side_effect=AssertionError("manager attempted advancement"))
        clock = CountingClock(self.now, events)
        report = ledger.IngestionReport("failure-audit", self.account.mt5_account_id,
                                       datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc), self.now,
                                       ledger.IngestionStatus.SUCCESS_EMPTY)
        ingestor = ReportIngestor(report, events)
        return lookup, checkpoints, clock, ingestor

    async def test_preparation_failure_matrix_never_advances_or_constructs_result(self):
        cases = (
            ("unknown", ledger.AccountIdentityError, 0),
            ("identity_uuid", ledger.AccountIdentityError, 0),
            ("account_type", TypeError, 0),
            ("checkpoint_load", sqlite3.OperationalError, 0),
            ("checkpoint_decode", ValueError, 0),
            ("checkpoint_type", TypeError, 0),
            ("checkpoint_owner", ledger.AccountIdentityError, 0),
            ("clock", RuntimeError, 1),
            ("bootstrap", ValueError, 1),
            ("future", ValueError, 1),
            ("underflow", OverflowError, 1),
            ("invalid_horizon", ValueError, 1),
        )
        for stage, error_type, clock_calls in cases:
            lookup, checkpoints, clock, ingestor = self.collaborators()
            config = ledger.RecoveryConfig(overlap=timedelta(minutes=5), bootstrap_start=self.start)
            account = self.account
            failure = None
            if stage == "unknown":
                lookup.account = None
            elif stage == "identity_uuid":
                lookup.account = replace(self.account, mt5_account_id=UUID(int=2))
            elif stage == "account_type":
                account = None
            elif stage in ("checkpoint_load", "checkpoint_decode"):
                failure = error_type("controlled checkpoint failure")
                checkpoints.failure = failure
            elif stage == "checkpoint_type":
                checkpoints.result = {"mt5_account_id": self.account.mt5_account_id}
            elif stage == "checkpoint_owner":
                checkpoints.result = ledger.LedgerCheckpoint(UUID(int=2), self.start, self.start)
            elif stage == "clock":
                failure = RuntimeError("controlled clock failure")
                clock.failure = failure
            elif stage == "bootstrap":
                checkpoints.result = None
                config = ledger.RecoveryConfig(overlap=timedelta(0))
            elif stage == "future":
                checkpoints.result = ledger.LedgerCheckpoint(
                    self.account.mt5_account_id, self.now + timedelta(microseconds=1), self.start)
            elif stage == "underflow":
                checkpoints.result = ledger.LedgerCheckpoint(
                    self.account.mt5_account_id, datetime(1, 1, 1, tzinfo=timezone.utc), self.start)
            else:
                clock.value = self.now.replace(tzinfo=None)
            manager = ledger.RecoveryManager(account_lookup=lookup, checkpoints=checkpoints,
                                             config=config, clock=clock, ingestor=ingestor)
            with self.subTest(stage=stage), patch.object(self.module, "RecoveryResult", wraps=ledger.RecoveryResult) as factory:
                with self.assertRaises(error_type) as raised:
                    await manager.recover(account=account)
                if failure is not None:
                    self.assertIs(raised.exception, failure)
                self.assertEqual(ingestor.calls, [])
                self.assertEqual(clock.calls, clock_calls)
                checkpoints.advance_checkpoint.assert_not_called()
                factory.assert_not_called()

    async def test_status_and_conflict_matrix_preserves_failure_objects_without_advance(self):
        cases = (
            (ledger.IngestionStatus.SUCCESS, ledger.RecoveryOutcome.FAILED,
             dict(retrieved=1, normalized=1, conflicts=1)),
            (ledger.IngestionStatus.SUCCESS_EMPTY, ledger.RecoveryOutcome.PENDING_RECONCILIATION, {}),
            (ledger.IngestionStatus.FAILED, ledger.RecoveryOutcome.FAILED,
             dict(retrieved=3, normalized=3, inserted=1, conflicts=1,
                  errors=(ledger.IngestionError(ledger.IngestionStage.STORAGE, "IntegrityError", "offline", 2, 9003),))),
            (ledger.IngestionStatus.PARTIAL, ledger.RecoveryOutcome.FAILED,
             dict(retrieved=3, normalized=2, inserted=1, conflicts=1, invalid=1,
                  errors=(ledger.IngestionError(ledger.IngestionStage.NORMALIZE, "ValueError", "bad row", 2, 9003),))),
        )
        for status, outcome, counts in cases:
            lookup, checkpoints, clock, ingestor = self.collaborators()
            report = replace(ingestor.report, status=status, **counts)
            ingestor.report = report
            manager = ledger.RecoveryManager(account_lookup=lookup, checkpoints=checkpoints, clock=clock,
                                             config=ledger.RecoveryConfig(overlap=timedelta(minutes=5)), ingestor=ingestor)
            original_prepare = manager.prepare
            attempts = []

            async def record_attempt(*, account):
                attempt = await original_prepare(account=account)
                attempts.append(attempt)
                return attempt

            with self.subTest(status=status), patch.object(manager, "prepare", side_effect=record_attempt):
                result = await manager.recover(account=self.account)
                self.assertIs(result.outcome, outcome)
                self.assertIs(result.ingestion_report, report)
                self.assertIs(result.account, attempts[0].account)
                self.assertIs(result.checkpoint_before, attempts[0].checkpoint_before)
                self.assertIs(result.planned_range, attempts[0].planned_range)
                self.assertIs(result.checkpoint_after, attempts[0].checkpoint_before)
                self.assertEqual(len(attempts), 1)
                self.assertEqual(len(ingestor.calls), 1)
                self.assertEqual(clock.calls, 1)
                checkpoints.advance_checkpoint.assert_not_called()

    async def test_escaping_exception_and_cancellation_never_construct_result_or_advance(self):
        for failure in (RuntimeError("controlled unexpected ingestion failure"), asyncio.CancelledError("controlled cancel")):
            lookup, checkpoints, clock, ingestor = self.collaborators()
            ingestor.failure = failure
            manager = ledger.RecoveryManager(account_lookup=lookup, checkpoints=checkpoints, clock=clock,
                                             config=ledger.RecoveryConfig(overlap=timedelta(0)), ingestor=ingestor)
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(self.module, "RecoveryResult", wraps=ledger.RecoveryResult) as factory:
                with self.assertRaises(type(failure)) as raised:
                    await manager.recover(account=self.account)
                self.assertIs(raised.exception, failure)
                self.assertEqual(len(ingestor.calls), 1)
                self.assertEqual(clock.calls, 1)
                self.assertEqual(checkpoints.calls, [self.account.mt5_account_id])
                checkpoints.advance_checkpoint.assert_not_called()
                factory.assert_not_called()


class ReportRecorder:
    """Observe the real ingestor's report, optionally pausing its return to manager."""

    def __init__(self, ingestor):
        self.ingestor = ingestor
        self.calls = []
        self.report = None
        self.persisted = None
        self.release = None
        self.continued = False

    async def ingest_range_with_evidence(self, account_ref, from_time, to_time):
        self.calls.append((account_ref, from_time, to_time))
        execution = await self.ingestor.ingest_range_with_evidence(account_ref, from_time, to_time)
        self.report = execution.report
        if self.persisted is not None:
            self.persisted.set()
            await self.release.wait()
            self.continued = True
        return execution


class RecoveryDurableFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = importlib.import_module(f"{ledger.__name__}.recovery_manager")
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.repo.close)
        self.account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        self.repo.register_account(self.account)  # Fixture setup only.
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)
        self.audit_time = datetime(2026, 11, 1, 9, 59, 0, 987654, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        self.clock = CountingClock(self.now, [])
        self.checkpoint_rows = []
        for name in ("advance_checkpoint", "register_account"):
            guard = patch.object(self.repo, name, side_effect=AssertionError(f"manager called {name}"))
            spy = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(spy.assert_not_called)
        self.addCleanup(self.assert_checkpoint_unchanged)

    def seed_checkpoint(self):
        row = (str(self.account.mt5_account_id), self.start.isoformat(timespec="microseconds"),
               self.audit_time.isoformat(timespec="microseconds"))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)", row)
        self.checkpoint_rows = [row]

    def assert_checkpoint_unchanged(self):
        # A fresh connection verifies durable completed_through AND updated_at,
        # including original text precision, rather than only cached models.
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                                "FROM ledger_checkpoints ORDER BY mt5_account_id").fetchall()
        self.assertEqual(rows, self.checkpoint_rows)

    def raw(self, ticket=9001):
        seconds = int(self.start.timestamp())
        return raw_deal(ticket=ticket, time=seconds, time_msc=seconds * 1000 + 123)

    def manager(self, recorder):
        return ledger.RecoveryManager(
            account_lookup=self.repo, checkpoints=self.repo, clock=self.clock,
            config=ledger.RecoveryConfig(overlap=timedelta(minutes=5), bootstrap_start=self.start), ingestor=recorder)

    async def test_returned_failed_report_preserves_exact_checkpoint_or_absence(self):
        for existing in (False, True):
            if existing:
                self.seed_checkpoint()
            self.clock.calls = 0
            before = self.repo.get_checkpoint(self.account.mt5_account_id)
            source = FakeSource(self.account)
            source.failure = ConnectionError("controlled offline fetch failure")
            recorder = ReportRecorder(ledger.DealIngestor(source, self.repo))
            with self.subTest(existing=existing):
                result = await self.manager(recorder).recover(account=self.account)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
                self.assertIs(result.ingestion_report, recorder.report)
                self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.FAILED)
                self.assertEqual(result.checkpoint_before, before)
                self.assertIs(result.checkpoint_after, result.checkpoint_before)
                if before is None:
                    self.assertIsNone(result.checkpoint_after)
                else:
                    self.assertEqual((result.checkpoint_after.completed_through, result.checkpoint_after.updated_at),
                                     (self.start, self.audit_time))
                self.assertEqual(len(source.fetch_calls), 1)
                self.assertEqual(len(recorder.calls), 1)
                self.assertEqual(self.clock.calls, 1)
                self.assert_checkpoint_unchanged()

    async def test_partial_storage_failure_then_manual_replay_retains_commit_and_checkpoint(self):
        self.seed_checkpoint()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_second BEFORE INSERT ON deals WHEN NEW.deal_ticket = '9002' "
                         "BEGIN SELECT RAISE(ABORT, 'controlled storage failure'); END")
        source = FakeSource(self.account, [self.raw(), self.raw(9002)])
        recorder = ReportRecorder(ledger.DealIngestor(source, self.repo))
        manager = self.manager(recorder)
        failed = await manager.recover(account=self.account)
        self.assertIs(failed.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(failed.ingestion_report, recorder.report)
        self.assertEqual((failed.ingestion_report.normalized, failed.ingestion_report.inserted), (2, 1))
        self.assertEqual(failed.ingestion_report.errors[0].stage, ledger.IngestionStage.STORAGE)
        original = self.repo.get(self.account.mt5_account_id, 9001)
        self.assertIsNotNone(original)
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9002))
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(self.clock.calls, 1)
        self.assert_checkpoint_unchanged()
        # Remove only the disposable fixture's fault; caller explicitly starts
        # attempt two. RecoveryManager itself must never retry automatically.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TRIGGER deny_second")
        replay = await manager.recover(account=self.account)
        self.assertIs(replay.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual((replay.ingestion_report.duplicates, replay.ingestion_report.inserted), (1, 1))
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9002))
        self.assertEqual(source.fetch_calls[0], source.fetch_calls[1])
        self.assertEqual(len(source.fetch_calls), 2)
        self.assertEqual(self.clock.calls, 2)
        self.assert_checkpoint_unchanged()

    async def test_external_task_cancellation_after_commit_has_no_result_or_continuation(self):
        self.seed_checkpoint()
        source = FakeSource(self.account, [self.raw()])
        recorder = ReportRecorder(ledger.DealIngestor(source, self.repo))
        recorder.persisted = asyncio.Event()
        recorder.release = asyncio.Event()
        manager = self.manager(recorder)
        with patch.object(self.module, "RecoveryResult", wraps=ledger.RecoveryResult) as factory:
            task = asyncio.create_task(manager.recover(account=self.account))

            async def cleanup_task():
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            self.addAsyncCleanup(cleanup_task)
            await asyncio.wait_for(recorder.persisted.wait(), timeout=5)
            original = self.repo.get(self.account.mt5_account_id, 9001)
            self.assertIsNotNone(original)
            factory.assert_not_called()
            self.assert_checkpoint_unchanged()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            recorder.release.set()
            self.assertTrue(task.done())
            self.assertTrue(task.cancelled())
            self.assertFalse(recorder.continued)
            self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
            self.assertEqual(len(recorder.calls), 1)
            self.assertEqual(len(source.fetch_calls), 1)
            self.assertEqual(self.clock.calls, 1)
            factory.assert_not_called()
            self.assert_checkpoint_unchanged()

    async def test_session_mismatch_preserves_real_failure_report_and_all_identity_metadata(self):
        self.seed_checkpoint()
        with closing(sqlite3.connect(self.path)) as conn:
            accounts_before = conn.execute("SELECT * FROM ledger_accounts").fetchall()
        for guard, stage in (("before", ledger.IngestionStage.ACCOUNT_BEFORE_FETCH),
                             ("after", ledger.IngestionStage.ACCOUNT_AFTER_FETCH)):
            self.clock.calls = 0
            source = FakeSource(self.account, [self.raw()])
            setattr(source, guard, ledger.SessionAccount(999, "wrong-server"))
            recorder = ReportRecorder(ledger.DealIngestor(source, self.repo))
            with self.subTest(guard=guard):
                result = await self.manager(recorder).recover(account=self.account)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
                self.assertIs(result.ingestion_report, recorder.report)
                self.assertEqual((result.ingestion_report.errors[0].stage,
                                  result.ingestion_report.errors[0].exception_type), (stage, "AccountIdentityError"))
                self.assertIs(result.account, self.account)
                self.assertEqual(len(recorder.calls), 1)
                self.assertEqual(len(source.fetch_calls), 0 if guard == "before" else 1)
                self.assertEqual(self.clock.calls, 1)
                self.assert_checkpoint_unchanged()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM ledger_accounts").fetchall(), accounts_before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
