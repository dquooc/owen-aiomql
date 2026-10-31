"""Manual planner/ingestor composition with fake history and temporary SQLite."""

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import UUID

from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal


class RecoveryIngestionIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.repo.register_account(self.account)
        self.completed = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, tzinfo=timezone.utc)
        self.checkpoint = None
        self.checkpoint_fields = None
        self.checkpoint_rows = []
        # Run before closing the repository, including after failures/cancellation.
        self.addCleanup(self.assert_checkpoint_unchanged)

    def seed_checkpoint(self, completed=None):
        completed = self.completed if completed is None else completed
        checkpoint = ledger.LedgerCheckpoint(self.account.mt5_account_id, completed,
            datetime(2026, 11, 1, 10, 2, tzinfo=timezone.utc))
        row = (str(checkpoint.mt5_account_id), checkpoint.completed_through.isoformat(timespec="microseconds"),
               checkpoint.updated_at.isoformat(timespec="microseconds"))
        # Seed only this disposable fixture; no checkpoint advancement API is used.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("INSERT INTO ledger_checkpoints "
                         "(mt5_account_id, completed_through, updated_at) VALUES (?, ?, ?)", row)
        self.checkpoint = self.repo.get_checkpoint(self.account.mt5_account_id)
        self.checkpoint_fields = (checkpoint.mt5_account_id, checkpoint.completed_through, checkpoint.updated_at)
        self.checkpoint_rows = [row]

    def assert_checkpoint_unchanged(self):
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                                "FROM ledger_checkpoints ORDER BY mt5_account_id").fetchall()
        self.assertEqual(rows, self.checkpoint_rows)
        self.assertEqual(self.repo.get_checkpoint(self.account.mt5_account_id), self.checkpoint)
        if self.checkpoint is not None:
            self.assertEqual((self.checkpoint.mt5_account_id, self.checkpoint.completed_through,
                              self.checkpoint.updated_at), self.checkpoint_fields)

    def raw(self, ticket=9001, **changes):
        # All fixture deals execute at 10:00, inside the planned intervals used here.
        seconds = int(self.completed.timestamp())
        return raw_deal(ticket=ticket, time=seconds, time_msc=seconds * 1000, **changes)

    def count_deals(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT count(*) FROM deals").fetchone()[0]

    def assert_request(self, source, planned, report=None):
        self.assertEqual(source.fetch_calls, [(self.account, planned.from_time, planned.to_time)])
        self.assertIs(source.fetch_calls[0][1].tzinfo, timezone.utc)
        self.assertIs(source.fetch_calls[0][2].tzinfo, timezone.utc)
        if report is not None:
            self.assertEqual((report.requested_from, report.requested_to), (planned.from_time, planned.to_time))

    async def test_checkpoint_overlap_reaches_source_and_preserves_partial_fills(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        self.assertEqual(planned.from_time, datetime(2026, 11, 1, 9, 55, tzinfo=timezone.utc))
        self.assertEqual(planned.to_time, self.now)
        source = FakeSource(self.account, [self.raw(volume=0.015), self.raw(9002, volume=0.025)])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual(report.status, ledger.IngestionStatus.SUCCESS)
        self.assertEqual((report.retrieved, report.inserted, self.count_deals()), (2, 2, 2))
        self.assertEqual({deal.deal_ticket for deal in self.repo.list_by_order(self.account.mt5_account_id, 8001)},
                         {9001, 9002})
        self.assertEqual(len(self.repo.list_by_position(self.account.mt5_account_id, 7001)), 2)

    async def test_bootstrap_start_reaches_source_without_overlap_or_implicit_checkpoint(self):
        planned = ledger.plan_recovery_range(checkpoint=None, current_time=self.now,
            overlap=timedelta(hours=12), bootstrap_start=datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
        self.assertEqual(planned.from_time, datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
        self.assertEqual(planned.to_time, self.now)
        source = FakeSource(self.account, [self.raw()])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual((report.status, report.inserted), (ledger.IngestionStatus.SUCCESS, 1))

    async def test_zero_length_plan_is_fetched_and_ingested_normally(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.completed,
                                             overlap=timedelta())
        self.assertEqual((planned.from_time, planned.to_time), (self.completed, self.completed))
        source = FakeSource(self.account, [self.raw()])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual((report.status, report.inserted), (ledger.IngestionStatus.SUCCESS, 1))

    async def test_empty_history_returns_existing_success_empty_without_progress_write(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        source = FakeSource(self.account)
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual(report.status, ledger.IngestionStatus.SUCCESS_EMPTY)
        self.assertEqual((report.retrieved, report.inserted, self.count_deals()), (0, 0, 0))
        self.assertEqual(source.account_calls, 2)

    async def test_overlap_replay_duplicates_then_adds_one_new_deal(self):
        initial_range = ledger.plan_recovery_range(checkpoint=None, current_time=self.completed,
            overlap=timedelta(), bootstrap_start=datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
        initial_source = FakeSource(self.account, [self.raw(), self.raw(9002)])
        first = await ledger.DealIngestor(initial_source, self.repo).ingest_range(
            self.account, from_time=initial_range.from_time, to_time=initial_range.to_time)
        self.assertEqual(first.inserted, 2)
        original = [self.repo.get(self.account.mt5_account_id, ticket) for ticket in (9001, 9002)]
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        for tickets, inserted, count in (((9001, 9002), 0, 2), ((9001, 9002, 9003), 1, 3)):
            with self.subTest(tickets=tickets):
                source = FakeSource(self.account, [self.raw(ticket) for ticket in tickets])
                report = await ledger.DealIngestor(source, self.repo).ingest_range(
                    self.account, from_time=planned.from_time, to_time=planned.to_time)
                self.assert_request(source, planned, report)
                self.assertEqual(report.status, ledger.IngestionStatus.SUCCESS)
                self.assertEqual((report.inserted, report.duplicates, self.count_deals()), (inserted, 2, count))
                self.assertEqual([self.repo.get(self.account.mt5_account_id, ticket) for ticket in (9001, 9002)],
                                 original)
                self.assert_checkpoint_unchanged()

    async def test_overlap_conflict_preserves_original_and_deduplicates_observation(self):
        initial_range = ledger.plan_recovery_range(checkpoint=None, current_time=self.completed,
            overlap=timedelta(), bootstrap_start=datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
        initial = await ledger.DealIngestor(FakeSource(self.account, [self.raw()]), self.repo).ingest_range(
            self.account, from_time=initial_range.from_time, to_time=initial_range.to_time)
        self.assertEqual(initial.inserted, 1)
        original = self.repo.get(self.account.mt5_account_id, 9001)
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        for run in (1, 2):
            source = FakeSource(self.account, [self.raw(profit=99.25)])
            report = await ledger.DealIngestor(source, self.repo).ingest_range(
                self.account, from_time=planned.from_time, to_time=planned.to_time, ingestion_id=f"conflict-{run}")
            self.assert_request(source, planned, report)
            self.assertEqual((report.status, report.conflicts, self.count_deals()),
                             (ledger.IngestionStatus.SUCCESS, 1, 1))
            self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
            observations = self.repo.list_observations(self.account.mt5_account_id, 9001)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].raw_payload["profit"], 99.25)
            self.assertEqual(observations[0].ingestion_id, "conflict-1")
            self.assert_checkpoint_unchanged()

    async def test_source_failure_reports_failed_without_retry_or_checkpoint_write(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        source = FakeSource(self.account)
        source.failure = ConnectionError("controlled offline fetch failure")
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual(report.status, ledger.IngestionStatus.FAILED)
        self.assertEqual((report.errors[0].stage, report.errors[0].exception_type),
                         (ledger.IngestionStage.FETCH, "ConnectionError"))
        self.assertEqual(self.count_deals(), 0)

    async def test_storage_failure_preserves_prior_deal_commit_but_not_checkpoint_progress(self):
        self.seed_checkpoint()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_second BEFORE INSERT ON deals "
                         "WHEN NEW.deal_ticket = '9002' BEGIN SELECT RAISE(ABORT, 'storage unavailable'); END")
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        source = FakeSource(self.account, [self.raw(ticket) for ticket in (9001, 9002, 9003)])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual(report.status, ledger.IngestionStatus.FAILED)
        self.assertEqual((report.retrieved, report.normalized, report.inserted, self.count_deals()), (3, 2, 1, 1))
        self.assertEqual((report.errors[0].stage, report.errors[0].exception_type),
                         (ledger.IngestionStage.STORAGE, "IntegrityError"))
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9003))

    async def test_account_guards_still_block_mismatch_before_and_after_fetch(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        for guard, stage in (("before", ledger.IngestionStage.ACCOUNT_BEFORE_FETCH),
                              ("after", ledger.IngestionStage.ACCOUNT_AFTER_FETCH)):
            with self.subTest(guard=guard):
                source = FakeSource(self.account, [self.raw()])
                setattr(source, guard, ledger.SessionAccount(999, "wrong-server"))
                report = await ledger.DealIngestor(source, self.repo).ingest_range(
                    self.account, from_time=planned.from_time, to_time=planned.to_time)
                self.assertEqual(report.status, ledger.IngestionStatus.FAILED)
                self.assertEqual((report.errors[0].stage, report.errors[0].exception_type), (stage, "AccountIdentityError"))
                self.assertEqual((report.requested_from, report.requested_to), (planned.from_time, planned.to_time))
                if guard == "before":
                    self.assertEqual(source.fetch_calls, [])
                else:
                    self.assert_request(source, planned, report)
                self.assertEqual(self.count_deals(), 0)
                self.assert_checkpoint_unchanged()

    async def test_cancellation_propagates_through_manual_composition(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        source = FakeSource(self.account)
        source.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await ledger.DealIngestor(source, self.repo).ingest_range(
                self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned)
        self.assertEqual(self.count_deals(), 0)

    async def test_partial_ingestion_keeps_existing_report_semantics_without_checkpoint_write(self):
        self.seed_checkpoint()
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint, current_time=self.now,
                                             overlap=timedelta(minutes=5))
        source = FakeSource(self.account, [self.raw(), self.raw(9002, volume="bad")])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual(report.status, ledger.IngestionStatus.PARTIAL)
        self.assertEqual((report.inserted, report.invalid, self.count_deals()), (1, 1, 1))
        self.assertEqual(report.errors[0].stage, ledger.IngestionStage.NORMALIZE)

    async def test_non_utc_microsecond_boundaries_reach_source_without_rounding(self):
        zone = timezone(timedelta(hours=7))
        self.seed_checkpoint(datetime(2026, 11, 1, 17, 0, 0, 123456, tzinfo=zone))
        planned = ledger.plan_recovery_range(checkpoint=self.checkpoint,
            current_time=datetime(2026, 11, 1, 17, 20, 0, 654321, tzinfo=zone), overlap=timedelta(minutes=5))
        self.assertEqual(planned.from_time, datetime(2026, 11, 1, 9, 55, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(planned.to_time, datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc))
        source = FakeSource(self.account, [self.raw()])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(
            self.account, from_time=planned.from_time, to_time=planned.to_time)
        self.assert_request(source, planned, report)
        self.assertEqual((report.status, report.inserted), (ledger.IngestionStatus.SUCCESS, 1))


if __name__ == "__main__":
    unittest.main()
