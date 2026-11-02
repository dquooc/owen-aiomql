"""Explicit one-shot history ingestion. No scheduling or order execution."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from .models import AccountRef, RecoveryRange
from .normalize import normalize_deal
from .reconciliation_evidence import SourceBatchEvidence, SourceHistoryAssurance, SourceRecordFingerprint
from .repository import AccountIdentityError, DealRepository, IngestOutcome
from .source import DealHistoryBatch, DealHistorySource, HistoryFetchError, SessionAccount, utc_range


class IngestionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class IngestionStage(StrEnum):
    ACCOUNT_BEFORE_FETCH = "ACCOUNT_BEFORE_FETCH"
    FETCH = "FETCH"
    ACCOUNT_AFTER_FETCH = "ACCOUNT_AFTER_FETCH"
    NORMALIZE = "NORMALIZE"
    STORAGE = "STORAGE"


@dataclass(frozen=True, slots=True)
class IngestionError:
    stage: IngestionStage
    exception_type: str
    message: str
    record_index: int | None = None
    deal_ticket: int | None = None


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """Counters for one invocation, with requested bounds canonicalized to UTC.

    retrieved counts the fetched batch, normalized counts successful conversions,
    and inserted/duplicates/conflicts count completed repository calls. invalid
    counts normalization failures only. On storage failure later records remain
    unprocessed; the successful counters still describe earlier completed calls.
    """

    ingestion_id: str
    mt5_account_id: UUID
    requested_from: datetime
    requested_to: datetime
    status: IngestionStatus
    retrieved: int = 0
    normalized: int = 0
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    invalid: int = 0
    errors: tuple[IngestionError, ...] = ()

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True, slots=True)
class IngestionExecution:
    """One invocation's report and optional guarded source evidence.

    Evidence is absent when account/fetch guards fail. It describes fetched
    records, not successful persistence or source completeness. A storage error
    stops processing, so fingerprints may cover only the normalized prefix.
    Structural binding validation does not establish reconciliation eligibility.
    """

    report: IngestionReport
    source_evidence: SourceBatchEvidence | None

    def __post_init__(self) -> None:
        if not isinstance(self.report, IngestionReport):
            raise TypeError("report must be an IngestionReport")
        evidence = self.source_evidence
        if evidence is None:
            if self.report.status is not IngestionStatus.FAILED:
                raise ValueError("only a failed guarded fetch may lack source evidence")
            return
        if not isinstance(evidence, SourceBatchEvidence):
            raise TypeError("source_evidence must be SourceBatchEvidence or None")
        if (evidence.ingestion_id != self.report.ingestion_id
                or evidence.account.mt5_account_id != self.report.mt5_account_id
                or evidence.requested_range != RecoveryRange(self.report.requested_from, self.report.requested_to)
                or evidence.retrieved != self.report.retrieved):
            raise ValueError("source evidence must bind to the same ingestion report")


class DealIngestor:
    """Guard a single fetch, normalize SDK records, persist and report outcomes.

    Invalid arguments raise before I/O. Fetch/account failures return FAILED
    without writes. Normalization failures continue: PARTIAL if at least one
    record was persisted/classified, FAILED if all were invalid. A storage error
    stops the batch and returns FAILED even if earlier rows committed; counters
    retain those completed outcomes. There is no whole-batch transaction/retry.
    Conflicts are preserved repository outcomes, not normalization/storage errors.
    Cancellation propagates; earlier per-record commits may already exist.

    Persistence runs synchronously through the existing repository contract,
    before returning, without TaskQueue or a background worker.
    """

    def __init__(self, source: DealHistorySource, repository: DealRepository):
        self.source = source
        self.repository = repository

    async def _guard(self, expected: AccountRef) -> None:
        if self.source.broker_namespace != expected.broker_namespace:
            raise AccountIdentityError("source broker_namespace differs from expected account")
        current = await self.source.current_account()
        if not isinstance(current, SessionAccount):
            raise AccountIdentityError("source did not return a verified session account")
        if (current.login, current.server) != (expected.login, expected.server):
            raise AccountIdentityError("session login/server differs from expected account")

    async def ingest_range(self, account_ref: AccountRef, from_time: datetime,
                           to_time: datetime, *, ingestion_id: str | None = None) -> IngestionReport:
        """Backward-compatible report-only view of the same one-shot execution."""
        execution = await self.ingest_range_with_evidence(account_ref, from_time, to_time,
                                                        ingestion_id=ingestion_id)
        return execution.report

    async def ingest_range_with_evidence(self, account_ref: AccountRef, from_time: datetime,
                                         to_time: datetime, *, ingestion_id: str | None = None) -> IngestionExecution:
        """Collect fingerprints from the actual batch, never fetching it again.

        Both session guards must pass before evidence can be returned, including
        for empty batches. The current source contract only supports unverified
        completion; neither successful persistence nor counts strengthen it.
        Fingerprints use the very NormalizedDeal passed to persistence, retaining
        source order and repeated tickets. Unprocessed/invalid records have no
        fingerprint, while retrieved always counts the full fetched batch.
        """
        if not isinstance(account_ref, AccountRef):
            raise TypeError("account_ref must be an AccountRef")
        start, end = utc_range(from_time, to_time)
        if ingestion_id is None:
            ingestion_id = str(uuid4())
        if not isinstance(ingestion_id, str) or not ingestion_id.strip():
            raise ValueError("ingestion_id must be a non-empty string")
        counts = dict(retrieved=0, normalized=0, inserted=0, duplicates=0, conflicts=0, invalid=0)
        errors: list[IngestionError] = []
        records: list[SourceRecordFingerprint] = []
        guarded = False
        requested_range = RecoveryRange(start, end)

        def report(status: IngestionStatus) -> IngestionExecution:
            result = IngestionReport(ingestion_id=ingestion_id, mt5_account_id=account_ref.mt5_account_id,
                                   requested_from=start, requested_to=end, status=status,
                                   **counts, errors=tuple(errors))
            evidence = SourceBatchEvidence(
                ingestion_id=ingestion_id, account=account_ref, requested_range=requested_range,
                assurance=SourceHistoryAssurance.CALL_COMPLETED_UNVERIFIED,
                retrieved=counts["retrieved"], records=tuple(records)) if guarded else None
            return IngestionExecution(report=result, source_evidence=evidence)

        def error(stage: IngestionStage, exc: Exception, index: int | None = None,
                  ticket: int | None = None) -> None:
            errors.append(IngestionError(stage, type(exc).__name__, str(exc), index, ticket))

        stage = IngestionStage.ACCOUNT_BEFORE_FETCH
        try:
            await self._guard(account_ref)
            stage = IngestionStage.FETCH
            batch = await self.source.fetch(account_ref, start, end)
            if not isinstance(batch, DealHistoryBatch):
                raise HistoryFetchError("source did not return DealHistoryBatch")
            counts["retrieved"] = len(batch.deals)
            stage = IngestionStage.ACCOUNT_AFTER_FETCH
            await self._guard(account_ref)
            guarded = True
        except Exception as exc:
            error(stage, exc)
            return report(IngestionStatus.FAILED)

        if not batch.deals:
            return report(IngestionStatus.SUCCESS_EMPTY)

        observed_at = datetime.now(timezone.utc)
        for index, item in enumerate(batch.deals):
            ticket = None
            try:
                # Extract all SDK fields once; normalize_deal owns the deep raw
                # snapshot and canonicalization before converting any fields.
                raw = item._asdict() if callable(getattr(item, "_asdict", None)) else item
                candidate = raw.get("ticket") if isinstance(raw, Mapping) else None
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    ticket = candidate
                deal = normalize_deal(account_ref, raw, observed_at, ingestion_id)
                records.append(SourceRecordFingerprint(
                    deal_ticket=deal.deal_ticket, payload_hash=deal.payload_hash,
                    executed_at_msc=deal.executed_at_msc, normalizer_version=deal.normalizer_version))
                counts["normalized"] += 1
            except Exception as exc:
                counts["invalid"] += 1
                error(IngestionStage.NORMALIZE, exc, index, ticket)
                continue
            try:
                result = self.repository.ingest(account_ref, deal)
                counter = {IngestOutcome.INSERTED: "inserted", IngestOutcome.DUPLICATE: "duplicates",
                           IngestOutcome.CONFLICT: "conflicts"}[result.outcome]
                counts[counter] += 1
            except Exception as exc:
                error(IngestionStage.STORAGE, exc, index, ticket)
                return report(IngestionStatus.FAILED)

        if errors:
            return report(IngestionStatus.PARTIAL if counts["normalized"] else IngestionStatus.FAILED)
        return report(IngestionStatus.SUCCESS)
