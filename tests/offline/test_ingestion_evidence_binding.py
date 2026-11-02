"""A4.4: evidence from one guarded ingestion, without a terminal or SDK."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch
import unittest

import test_deal_ingestion as ingestion_fixtures
from test_deal_ingestion import FakeSource
from test_deal_normalizer import ledger, raw_deal, SDKDeal


class IngestionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = ingestion_fixtures.DealIngestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo, self.account = fixture.repo, fixture.account
        self.start = fixture.start.replace(microsecond=123456)
        self.end = fixture.end.replace(microsecond=654321)

    async def execute(self, source, **kwargs):
        return await ledger.DealIngestor(source, self.repo).ingest_range_with_evidence(
            self.account, self.start, self.end, **kwargs)

    def assert_binding(self, execution, source):
        report, evidence = execution.report, execution.source_evidence
        self.assertEqual(evidence.ingestion_id, report.ingestion_id)
        self.assertIs(evidence.account, self.account)
        self.assertEqual(evidence.requested_range, ledger.RecoveryRange(self.start, self.end))
        self.assertEqual(evidence.retrieved, report.retrieved)
        self.assertIs(evidence.assurance, ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)
        self.assertEqual(source.fetch_calls, [(self.account, self.start, self.end)])
        self.assertEqual(source.account_calls, 2)

    async def test_actual_batch_once_canonical_fingerprints_order_and_duplicate_tickets(self):
        records = [raw_deal(ticket=9003), raw_deal(), raw_deal(), raw_deal(comment="changed")]
        source = FakeSource(self.account, [SDKDeal(r) for r in records])
        # A second fetch would return a different batch, rather than accidentally
        # making a refetch implementation pass with the same fixture response.
        original_fetch = source.fetch

        async def fetch(*args):
            result = await original_fetch(*args)
            source.response = ledger.DealHistoryBatch(())
            return result

        source.fetch = fetch
        result = await self.execute(source, ingestion_id=" exact invocation ")
        self.assert_binding(result, source)
        self.assertEqual(result.report.ingestion_id, " exact invocation ")
        self.assertEqual((result.report.inserted, result.report.duplicates, result.report.conflicts), (2, 1, 1))
        expected = [ledger.normalize_deal(self.account, raw, self.start, result.report.ingestion_id)
                    for raw in records]
        self.assertEqual(result.source_evidence.records, tuple(ledger.SourceRecordFingerprint(
            d.deal_ticket, d.payload_hash, d.executed_at_msc, d.normalizer_version) for d in expected))
        self.assertEqual([r.deal_ticket for r in result.source_evidence.records], [9003, 9001, 9001, 9001])

    async def test_fingerprints_reuse_normalized_objects_passed_to_storage(self):
        source = FakeSource(self.account, [raw_deal(), raw_deal(ticket=9002)])
        normalized = []

        def normalize(*args):
            deal = ledger.normalize_deal(*args)
            normalized.append(deal)
            return deal

        ingestor = ledger.DealIngestor(source, self.repo)
        with patch(f"{ingestor.__class__.__module__}.normalize_deal", side_effect=normalize) as convert, \
                patch.object(self.repo, "ingest", wraps=self.repo.ingest) as write:
            result = await ingestor.ingest_range_with_evidence(self.account, self.start, self.end)
        self.assert_binding(result, source)
        self.assertEqual(convert.call_count, 2)
        self.assertEqual(write.call_count, 2)
        for deal, call, fingerprint in zip(normalized, write.call_args_list, result.source_evidence.records):
            self.assertIs(call.args[0], self.account)
            self.assertIs(call.args[1], deal)
            self.assertIs(fingerprint.payload_hash, deal.payload_hash)
            self.assertEqual((fingerprint.deal_ticket, fingerprint.executed_at_msc, fingerprint.normalizer_version),
                             (deal.deal_ticket, deal.executed_at_msc, deal.normalizer_version))

    async def test_source_adapter_with_inert_client_never_claims_verified_complete(self):
        # Exercise the existing adapter with local mocks only: no SDK import,
        # terminal, credentials, network connection, or broker operation.
        from types import SimpleNamespace
        for records in ((), (raw_deal(),)):
            client = SimpleNamespace(
                account_info=AsyncMock(return_value={"login": self.account.login, "server": self.account.server}),
                history_deals_get=AsyncMock(return_value=records))
            source = ledger.MetaTraderDealHistorySource(client, broker_namespace=self.account.broker_namespace)
            result = await self.execute(source)
            self.assertIs(result.source_evidence.assurance, ledger.SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED)
            self.assertEqual(result.source_evidence.retrieved, len(records))
            client.history_deals_get.assert_awaited_once_with(date_from=self.start, date_to=self.end)
            self.assertEqual(client.account_info.await_count, 2)

    async def test_utc_microseconds_and_equal_range(self):
        zone = timezone(timedelta(hours=7))
        for end in (self.end, self.start):
            source = FakeSource(self.account)
            result = await ledger.DealIngestor(source, self.repo).ingest_range_with_evidence(
                self.account, self.start.astimezone(zone), end.astimezone(zone))
            self.assertEqual(result.source_evidence.requested_range, ledger.RecoveryRange(self.start, end))
            self.assertEqual(source.fetch_calls[0][1:], (self.start, end))
            self.assertIs(result.report.requested_from.tzinfo, timezone.utc)
            self.assertEqual(result.report.requested_from.microsecond, 123456)

    async def test_guarded_empty_fetch_is_unverified_with_generated_id(self):
        source = FakeSource(self.account)
        result = await self.execute(source)
        self.assert_binding(result, source)
        self.assertTrue(result.report.ingestion_id)
        self.assertEqual(result.report.status, ledger.IngestionStatus.SUCCESS_EMPTY)
        self.assertEqual((result.source_evidence.retrieved, result.source_evidence.records), (0, ()))

    async def test_normalization_failures_keep_retrieved_and_valid_fingerprints(self):
        for records in ([raw_deal(ticket=0), raw_deal(), raw_deal(volume=-1)], [raw_deal(ticket=0)]):
            source = FakeSource(self.account, records)
            result = await self.execute(source)
            self.assert_binding(result, source)
            self.assertEqual(result.report.retrieved, len(records))
            self.assertEqual(len(result.source_evidence.records), result.report.normalized)
            self.assertEqual(result.report.invalid, len(records) - result.report.normalized)
            self.assertEqual(result.report.status, ledger.IngestionStatus.PARTIAL if len(records) == 3
                             else ledger.IngestionStatus.FAILED)

    async def test_guard_and_fetch_failures_never_produce_evidence_or_retry(self):
        for stage in ("before", "fetch", "after", "invalid_batch"):
            with self.subTest(stage=stage):
                source = FakeSource(self.account, [raw_deal()])
                if stage in ("before", "after"):
                    setattr(source, stage, ledger.SessionAccount(999, "different"))
                elif stage == "fetch":
                    source.failure = ConnectionError("unavailable")
                else:
                    source.response = None
                with patch.object(self.repo, "ingest") as ingest:
                    result = await self.execute(source)
                self.assertIsNone(result.source_evidence)
                self.assertEqual(result.report.status, ledger.IngestionStatus.FAILED)
                self.assertEqual(len(source.fetch_calls), 0 if stage == "before" else 1)
                self.assertEqual(source.account_calls, 2 if stage == "after" else 1)
                self.assertEqual(result.report.retrieved, 1 if stage == "after" else 0)
                ingest.assert_not_called()

    async def test_storage_failure_keeps_commits_and_source_fingerprints_through_failure(self):
        source = FakeSource(self.account, [raw_deal(ticket=n) for n in (9001, 9002, 9003)])
        original_ingest = self.repo.ingest

        def ingest(account, deal):
            if deal.deal_ticket == 9002:
                raise OSError("storage unavailable")
            return original_ingest(account, deal)

        with patch.object(self.repo, "ingest", side_effect=ingest) as write:
            result = await self.execute(source)
        self.assert_binding(result, source)
        self.assertEqual(result.report.status, ledger.IngestionStatus.FAILED)
        self.assertEqual((result.report.retrieved, result.report.normalized, result.report.inserted), (3, 2, 1))
        self.assertEqual([r.deal_ticket for r in result.source_evidence.records], [9001, 9002])
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9003))
        self.assertEqual(write.call_count, 2)

    async def test_cancellation_propagates_at_each_stage_without_retry(self):
        for stage in ("before", "fetch", "after", "normalize", "storage"):
            source = FakeSource(self.account, [raw_deal()])
            ingestor = ledger.DealIngestor(source, self.repo)
            if stage in ("before", "after"):
                target, name = source, "current_account"
                effects = [asyncio.CancelledError()] if stage == "before" else [source.account, asyncio.CancelledError()]
                replacement = AsyncMock(side_effect=effects)
            elif stage == "fetch":
                source.failure = asyncio.CancelledError()
                target, name, replacement = source, "failure", source.failure
            else:
                target, name = ((None, None) if stage == "normalize"
                                else (self.repo, "ingest"))
                replacement = Mock(side_effect=asyncio.CancelledError())
            if stage == "normalize":
                context = patch(f"{ingestor.__class__.__module__}.normalize_deal", replacement)
            else:
                context = patch.object(target, name, replacement)
            with self.subTest(stage=stage), context, self.assertRaises(asyncio.CancelledError):
                await ingestor.ingest_range_with_evidence(self.account, self.start, self.end)
            self.assertLessEqual(len(source.fetch_calls), 1)

    async def test_storage_cancellation_preserves_prior_commit(self):
        source = FakeSource(self.account, [raw_deal(), raw_deal(ticket=9002)])
        original_ingest = self.repo.ingest

        def ingest(account, deal):
            if deal.deal_ticket == 9002:
                raise asyncio.CancelledError()
            return original_ingest(account, deal)

        with patch.object(self.repo, "ingest", side_effect=ingest) as write:
            with self.assertRaises(asyncio.CancelledError):
                await self.execute(source)
        self.assertIsNotNone(self.repo.get(self.account.mt5_account_id, 9001))
        self.assertIsNone(self.repo.get(self.account.mt5_account_id, 9002))
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(write.call_count, 2)

    async def test_repeated_invocations_do_not_share_evidence(self):
        source = FakeSource(self.account, [raw_deal()])
        ingestor = ledger.DealIngestor(source, self.repo)
        first = await ingestor.ingest_range_with_evidence(self.account, self.start, self.end)
        source.response = ledger.DealHistoryBatch((raw_deal(ticket=9002),))
        second = await ingestor.ingest_range_with_evidence(self.account, self.start, self.end)
        self.assertNotEqual(first.report.ingestion_id, second.report.ingestion_id)
        self.assertEqual([r.deal_ticket for r in first.source_evidence.records], [9001])
        self.assertEqual([r.deal_ticket for r in second.source_evidence.records], [9002])
        self.assertEqual(second.report.ingestion_id, second.source_evidence.ingestion_id)
        self.assertEqual(len(source.fetch_calls), 2)

    async def test_legacy_api_still_returns_identical_report_and_one_fetch(self):
        source = FakeSource(self.account, [raw_deal()])
        report = await ledger.DealIngestor(source, self.repo).ingest_range(self.account, self.start, self.end)
        self.assertIsInstance(report, ledger.IngestionReport)
        self.assertEqual(report.status, ledger.IngestionStatus.SUCCESS)
        self.assertEqual(len(source.fetch_calls), 1)

    async def test_legacy_api_forwards_arguments_and_returns_exact_execution_report(self):
        ingestor = ledger.DealIngestor(FakeSource(self.account), self.repo)
        execution = await self.execute(FakeSource(self.account), ingestion_id="caller-id")
        with patch.object(ingestor, "ingest_range_with_evidence", return_value=execution) as execute:
            report = await ingestor.ingest_range(account_ref=self.account, from_time=self.start,
                                                 to_time=self.end, ingestion_id="caller-id")
        self.assertIs(report, execution.report)
        execute.assert_awaited_once_with(self.account, self.start, self.end, ingestion_id="caller-id")

    async def test_execution_is_public_frozen_slotted_and_validates_bindings(self):
        result = await self.execute(FakeSource(self.account))
        self.assertIn("IngestionExecution", ledger.__all__)
        self.assertIsInstance(result, ledger.IngestionExecution)
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.report = result.report
        for field, value in (("report", object()), ("source_evidence", object())):
            with self.subTest(field=field), self.assertRaises(TypeError):
                replace(result, **{field: value})
        for evidence in (replace(result.source_evidence, ingestion_id="another"),
                         replace(result.source_evidence, account=replace(self.account, mt5_account_id="ac647462-f801-44f7-a60f-6b6c86c2b9e1")),
                         replace(result.source_evidence, requested_range=ledger.RecoveryRange(self.start, self.start)),
                         replace(result.source_evidence, retrieved=1)):
            with self.assertRaises(ValueError):
                replace(result, source_evidence=evidence)
        with self.assertRaises(ValueError):
            replace(result, source_evidence=None)


if __name__ == "__main__":
    unittest.main()
