"""Full offline recovery stack with durable SQLite and raw fake history."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal
from test_ledger_recovery_manager import CountingClock
from test_ledger_recovery_manager_failures import ReportRecorder


class RecoveryManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(dir=Path.cwd(), suffix=".sqlite3", delete=False)
        self.path = Path(temp.name)
        temp.close()
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.repo = ledger.SQLiteDealRepository(self.path)
        self.addCleanup(self.repo.close)
        self.account = ledger.AccountRef(UUID(int=1), "broker-a", "server-live", 123456, "Broker A", 2)
        self.other = ledger.AccountRef(UUID(int=2), "broker-b", "server-live", 123456, "Broker B", 2)
        # Actual registration belongs exclusively to disposable fixture setup.
        self.repo.register_account(self.account)
        self.repo.register_account(self.other)
        self.start = datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=timezone.utc)
        self.audit_time = datetime(2026, 11, 1, 9, 59, 0, 987654, tzinfo=timezone.utc)
        self.now = datetime(2026, 11, 1, 10, 20, 0, 654321, tzinfo=timezone.utc)
        self.clock = CountingClock(self.now, [])
        self.config = ledger.RecoveryConfig(overlap=timedelta(minutes=5, microseconds=1),
                                             bootstrap_start=self.start)
        self.checkpoint_rows = {}
        for name in ("advance_checkpoint", "register_account"):
            guard = patch.object(self.repo, name, side_effect=AssertionError(f"recovery called {name}"))
            spy = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(spy.assert_not_called)
        self.addCleanup(self.assert_checkpoints_unchanged)

    def seed_checkpoint(self, account=None, *, completed=None, updated=None):
        account = self.account if account is None else account
        completed = self.start if completed is None else completed
        updated = self.audit_time if updated is None else updated
        # Preserve supplied offset/text precision to detect any rewrite as well
        # as changes to completed_through or updated_at. No advancement API used.
        row = (str(account.mt5_account_id), completed.isoformat(timespec="microseconds"),
               updated.isoformat(timespec="microseconds"))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO ledger_checkpoints VALUES (?, ?, ?)", row)
        self.checkpoint_rows[row[0]] = row

    def assert_checkpoints_unchanged(self):
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute("SELECT mt5_account_id, completed_through, updated_at "
                                "FROM ledger_checkpoints ORDER BY mt5_account_id").fetchall()
        self.assertEqual(rows, sorted(self.checkpoint_rows.values()))

    def raw(self, ticket=9001, *, at=None, **changes):
        instant = self.start + timedelta(seconds=1) if at is None else at
        seconds = int(instant.timestamp())
        return raw_deal(ticket=ticket, time=seconds,
                        time_msc=seconds * 1000 + instant.microsecond // 1000, **changes)

    def count(self, table):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def manager(self, source, *, config=None, clock=None):
        recorder = ReportRecorder(ledger.DealIngestor(source, self.repo))
        manager = ledger.RecoveryManager(account_lookup=self.repo, checkpoints=self.repo,
                                         config=self.config if config is None else config,
                                         clock=self.clock if clock is None else clock, ingestor=recorder)
        return manager, recorder

    async def recover_once(self, manager, recorder, source, *, account=None):
        account = self.account if account is None else account
        checkpoint = self.repo.get_checkpoint(account.mt5_account_id)
        clock_calls, fetch_calls = manager.clock.calls, len(source.fetch_calls)
        ingest_calls, guard_calls = len(recorder.calls), source.account_calls
        result = await manager.recover(account=account)
        self.assertIs(result.account, account)
        self.assertEqual(result.checkpoint_before, checkpoint)
        self.assertIs(result.checkpoint_after, result.checkpoint_before)
        self.assertIs(result.ingestion_report, recorder.report)
        self.assertEqual(result.ingestion_report.mt5_account_id, account.mt5_account_id)
        self.assertEqual(manager.clock.calls, clock_calls + 1)
        self.assertEqual(len(recorder.calls), ingest_calls + 1)
        self.assertEqual(len(source.fetch_calls), fetch_calls + 1)
        self.assertEqual(source.account_calls, guard_calls + 2)
        requested = (account, result.planned_range.from_time, result.planned_range.to_time)
        self.assertEqual(recorder.calls[-1], requested)
        self.assertEqual(source.fetch_calls[-1], requested)
        self.assertIs(source.fetch_calls[-1][1].tzinfo, timezone.utc)
        self.assertIs(source.fetch_calls[-1][2].tzinfo, timezone.utc)
        self.assertEqual((result.ingestion_report.requested_from, result.ingestion_report.requested_to), requested[1:])
        self.assert_checkpoints_unchanged()
        return result

    async def test_bootstrap_with_non_utc_policy_preserves_precision_and_independent_partial_fills(self):
        config = ledger.RecoveryConfig(overlap=timedelta(hours=12),
            bootstrap_start=datetime(2026, 11, 1, 15, 30, 0, 123456,
                                     tzinfo=timezone(timedelta(hours=5, minutes=30))))
        self.clock.value = datetime(2026, 11, 1, 17, 20, 0, 654321, tzinfo=timezone(timedelta(hours=7)))
        source = FakeSource(self.account, [self.raw(volume=0.015), self.raw(9002, volume=0.025)])
        manager, recorder = self.manager(source, config=config)
        result = await self.recover_once(manager, recorder, source)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.SUCCESS)
        self.assertEqual((result.planned_range.from_time, result.planned_range.to_time), (self.start, self.now))
        self.assertIsNone(result.checkpoint_before)
        self.assertIsNone(result.checkpoint_after)
        self.assertEqual((result.ingestion_report.retrieved, result.ingestion_report.inserted), (2, 2))
        deals = self.repo.list_by_order(self.account.mt5_account_id, 8001)
        self.assertEqual({deal.deal_ticket for deal in deals}, {9001, 9002})
        self.assertEqual({deal.volume for deal in deals}, {Decimal("0.015"), Decimal("0.025")})
        self.assertEqual(len({deal.id for deal in deals}), 2)
        self.assertEqual(self.repo.list_by_position(self.account.mt5_account_id, 7001), deals)
        self.assertEqual(self.count("deals"), 2)

    async def test_overlap_replay_and_new_horizons_keep_attempts_independent(self):
        self.seed_checkpoint(completed=self.start.astimezone(timezone(timedelta(hours=7))),
                             updated=self.audit_time.astimezone(timezone(timedelta(hours=5, minutes=30))))
        a = self.raw(at=self.start - timedelta(minutes=2))
        b = self.raw(9002, at=self.start - timedelta(minutes=1))
        originals = [ledger.normalize_deal(self.account, raw, self.audit_time, "fixture") for raw in (a, b)]
        for original in originals:
            self.repo.ingest(self.account, original)
        source = FakeSource(self.account, [a, b, self.raw(9003)])
        manager, recorder = self.manager(source)
        first = await self.recover_once(manager, recorder, source)
        self.assertEqual((first.ingestion_report.retrieved, first.ingestion_report.duplicates,
                          first.ingestion_report.inserted), (3, 2, 1))
        self.assertIs(first.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual(first.planned_range.from_time,
                         datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc))
        self.assertEqual(first.checkpoint_before.completed_through, self.start)
        self.assertEqual(first.checkpoint_before.updated_at, self.audit_time)
        first_report = first.ingestion_report
        next_horizon = self.now + timedelta(minutes=1, microseconds=1)
        self.clock.value = next_horizon
        source.response = ledger.DealHistoryBatch((a, b, self.raw(9003), self.raw(9004)))
        second = await self.recover_once(manager, recorder, source)
        self.assertEqual((second.ingestion_report.retrieved, second.ingestion_report.duplicates,
                          second.ingestion_report.inserted), (4, 3, 1))
        self.assertIs(second.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual(second.planned_range.from_time, first.planned_range.from_time)
        self.assertEqual(second.planned_range.to_time, next_horizon)
        self.assertEqual(first.planned_range.to_time, self.now)
        self.assertIs(first.ingestion_report, first_report)
        self.assertIsNot(first, second)
        self.assertIsNot(first.planned_range, second.planned_range)
        self.assertNotEqual(first_report.ingestion_id, second.ingestion_report.ingestion_id)
        self.assertEqual(self.clock.calls, 2)
        self.assertEqual(self.count("deals"), 4)
        self.assertEqual([self.repo.get(self.account.mt5_account_id, ticket) for ticket in (9001, 9002)], originals)

    async def test_conflict_replay_preserves_original_and_deduplicates_observation(self):
        self.seed_checkpoint()
        original = ledger.normalize_deal(self.account, self.raw(), self.start, "fixture")
        self.repo.ingest(self.account, original)
        correction = self.raw(profit=99.25, extra={"broker_revision": 1})
        source = FakeSource(self.account, [correction])
        manager, recorder = self.manager(source)
        first = await self.recover_once(manager, recorder, source)
        observations = self.repo.list_observations(self.account.mt5_account_id, 9001)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].raw_payload, correction)
        self.assertEqual(observations[0].ingestion_id, first.ingestion_report.ingestion_id)
        second = await self.recover_once(manager, recorder, source)
        for result in (first, second):
            self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.SUCCESS)
            self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
            self.assertEqual((result.ingestion_report.inserted, result.ingestion_report.duplicates,
                              result.ingestion_report.conflicts), (0, 0, 1))
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
        self.assertEqual(self.repo.list_observations(self.account.mt5_account_id, 9001), observations)
        self.assertEqual((self.count("deals"), self.count("deal_observations")), (1, 1))
        self.assertNotEqual(first.ingestion_report.ingestion_id, second.ingestion_report.ingestion_id)

    async def test_empty_history_preserves_entire_database_with_and_without_checkpoint(self):
        for existing in (False, True):
            if existing:
                self.seed_checkpoint()
            source = FakeSource(self.account)
            manager, recorder = self.manager(source)
            with closing(sqlite3.connect(self.path)) as conn:
                before = tuple(conn.iterdump())
            with self.subTest(existing=existing):
                result = await self.recover_once(manager, recorder, source)
                self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.SUCCESS_EMPTY)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
                self.assertEqual(result.checkpoint_before is not None, existing)
                self.assertEqual(self.count("deals"), 0)
                with closing(sqlite3.connect(self.path)) as conn:
                    self.assertEqual(tuple(conn.iterdump()), before)

    async def test_failed_bootstrap_manual_retry_replays_without_creating_checkpoint(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER deny_second BEFORE INSERT ON deals WHEN NEW.deal_ticket = '9002' "
                         "BEGIN SELECT RAISE(ABORT, 'controlled storage failure'); END")
        source = FakeSource(self.account, [self.raw(ticket) for ticket in (9001, 9002, 9003)])
        manager, recorder = self.manager(source)
        failed = await self.recover_once(manager, recorder, source)
        self.assertIs(failed.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(failed.ingestion_report.status, ledger.IngestionStatus.FAILED)
        self.assertEqual((failed.ingestion_report.retrieved, failed.ingestion_report.normalized,
                          failed.ingestion_report.inserted), (3, 2, 1))
        self.assertEqual(failed.ingestion_report.errors[0].stage, ledger.IngestionStage.STORAGE)
        self.assertEqual((failed.checkpoint_before, failed.checkpoint_after), (None, None))
        original = self.repo.get(self.account.mt5_account_id, 9001)
        self.assertIsNotNone(original)
        self.assertEqual(self.count("deals"), 1)
        self.assertEqual(len(source.fetch_calls), 1)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TRIGGER deny_second")  # Remove only the fixture fault.
        self.clock.value = self.now + timedelta(minutes=1)
        replay = await self.recover_once(manager, recorder, source)
        self.assertIs(replay.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual((replay.ingestion_report.duplicates, replay.ingestion_report.inserted), (1, 2))
        self.assertEqual((replay.checkpoint_before, replay.checkpoint_after), (None, None))
        self.assertEqual(failed.planned_range.from_time, replay.planned_range.from_time)
        self.assertEqual(failed.planned_range.from_time, self.start)
        self.assertEqual(failed.planned_range.to_time, self.now)
        self.assertEqual(replay.planned_range.to_time, self.clock.value)
        self.assertIsNot(failed, replay)
        self.assertNotEqual(failed.ingestion_report.ingestion_id, replay.ingestion_report.ingestion_id)
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), original)
        self.assertEqual(self.count("deals"), 3)
        self.assertEqual(len(source.fetch_calls), 2)

    async def test_verified_ledger_identity_does_not_bypass_post_fetch_session_guard(self):
        self.seed_checkpoint()
        self.assertEqual(self.repo.resolve_account(broker_namespace="broker-a", server="server-live", login=123456),
                         self.account)
        source = FakeSource(self.account, [self.raw()])
        source.after = ledger.SessionAccount(999, "wrong-server")
        manager, recorder = self.manager(source)
        result = await self.recover_once(manager, recorder, source)
        self.assertIs(result.outcome, ledger.RecoveryOutcome.FAILED)
        self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.FAILED)
        self.assertEqual((result.ingestion_report.errors[0].stage, result.ingestion_report.errors[0].exception_type),
                         (ledger.IngestionStage.ACCOUNT_AFTER_FETCH, "AccountIdentityError"))
        self.assertEqual(result.ingestion_report.inserted, 0)
        self.assertEqual(self.count("deals"), 0)

    async def test_zero_length_ranges_reach_source_through_both_branches(self):
        config = ledger.RecoveryConfig(overlap=timedelta(0), bootstrap_start=self.now)
        for existing in (False, True):
            if existing:
                self.seed_checkpoint(completed=self.now)
            source = FakeSource(self.account)
            manager, recorder = self.manager(source, config=config)
            with self.subTest(existing=existing):
                result = await self.recover_once(manager, recorder, source)
                self.assertEqual((result.planned_range.from_time, result.planned_range.to_time), (self.now, self.now))
                self.assertIs(result.ingestion_report.status, ledger.IngestionStatus.SUCCESS_EMPTY)
                self.assertIs(result.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)

    async def test_sequential_accounts_share_storage_without_mixing_context(self):
        self.seed_checkpoint()
        other_completed = self.start - timedelta(hours=1)
        self.seed_checkpoint(self.other, completed=other_completed)
        source_a = FakeSource(self.account, [self.raw(profit=1.25)])
        source_b = FakeSource(self.other, [self.raw(profit=9.5, symbol="GBPUSD")])
        manager_a, recorder_a = self.manager(source_a)
        clock_b = CountingClock(self.now + timedelta(minutes=2), [])
        manager_b, recorder_b = self.manager(source_b, clock=clock_b)
        first = await self.recover_once(manager_a, recorder_a, source_a)
        deal_a = self.repo.get(self.account.mt5_account_id, 9001)
        self.assertIsNone(self.repo.get(self.other.mt5_account_id, 9001))
        second = await self.recover_once(manager_b, recorder_b, source_b, account=self.other)
        deal_b = self.repo.get(self.other.mt5_account_id, 9001)
        self.assertEqual(self.repo.get(self.account.mt5_account_id, 9001), deal_a)
        self.assertEqual((deal_a.profit, deal_b.profit), (Decimal("1.25"), Decimal("9.5")))
        self.assertNotEqual(deal_a.id, deal_b.id)
        self.assertEqual(first.checkpoint_before.completed_through, self.start)
        self.assertEqual(second.checkpoint_before.completed_through, other_completed)
        self.assertEqual(first.planned_range.from_time,
                         datetime(2026, 11, 1, 9, 55, 0, 123455, tzinfo=timezone.utc))
        self.assertEqual(second.planned_range.from_time,
                         datetime(2026, 11, 1, 8, 55, 0, 123455, tzinfo=timezone.utc))
        self.assertEqual(first.planned_range.to_time, self.now)
        self.assertEqual(second.planned_range.to_time, clock_b.value)
        self.assertNotEqual(first.ingestion_report.mt5_account_id, second.ingestion_report.mt5_account_id)
        self.assertNotEqual(first.ingestion_report.ingestion_id, second.ingestion_report.ingestion_id)
        self.assertIs(first.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertIs(second.outcome, ledger.RecoveryOutcome.PENDING_RECONCILIATION)
        self.assertEqual(self.count("deals"), 2)


if __name__ == "__main__":
    unittest.main()
