"""One-shot recovery ingestion and observational reconciliation; no advancement."""

from dataclasses import dataclass
from typing import cast

from .account_lookup import verify_account_identity
from .ingestion import DealIngestor, IngestionExecution, IngestionStatus
from .models import AccountRef, LedgerCheckpoint, RecoveryRange
from .recovery import plan_recovery_range
from .recovery_config import Clock, RecoveryConfig
from .recovery_models import RecoveryOutcome, RecoveryResult
from .repository import AccountIdentityError, AccountLookupRepository, CheckpointRepository, LedgerSnapshotReader


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    """Verified preparation state; planned_range.to_time is the sole horizon.

    Produced by prepare() before ingestion. This is neither a recovery outcome
    nor proof of completed recovery, and it does not reserve a checkpoint.
    """

    account: AccountRef
    checkpoint_before: LedgerCheckpoint | None
    planned_range: RecoveryRange


class RecoveryManager:
    """Prepare and ingest one account-scoped recovery range without advancing it.

    An ingestor is required for recover(), but remains optional for prepare-only
    callers. The snapshot reader defaults to the account lookup repository, which
    must also implement LedgerSnapshotReader for recover(); a separate reader can
    be supplied. Even approved reconciliation remains pending until a future
    advancement phase. No ownership/lease or background work is created.
    """

    def __init__(self, *, account_lookup: AccountLookupRepository, checkpoints: CheckpointRepository,
                 config: RecoveryConfig, clock: Clock, ingestor: DealIngestor | None = None,
                 snapshot_reader: LedgerSnapshotReader | None = None):
        if not isinstance(config, RecoveryConfig):
            raise TypeError("config must be a RecoveryConfig")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.account_lookup = account_lookup
        self.checkpoints = checkpoints
        self.config = config
        self.clock = clock
        self.ingestor = ingestor
        self.snapshot_reader = snapshot_reader if snapshot_reader is not None else cast(LedgerSnapshotReader, account_lookup)

    async def prepare(self, *, account: AccountRef) -> RecoveryAttempt:
        """Verify identity, read checkpoint, capture one horizon, and plan.

        Failures propagate without retry. Identity/checkpoint failures occur
        before any clock read; clock results are validated by the pure planner.
        """
        account = verify_account_identity(account=account, repository=self.account_lookup)
        checkpoint = self.checkpoints.get_checkpoint(account.mt5_account_id)
        if checkpoint is not None:
            if not isinstance(checkpoint, LedgerCheckpoint):
                raise TypeError("checkpoint must be a LedgerCheckpoint or None")
            if checkpoint.mt5_account_id != account.mt5_account_id:
                raise AccountIdentityError(
                    f"checkpoint account UUID {checkpoint.mt5_account_id} differs from verified account "
                    f"UUID {account.mt5_account_id}")
        current_time = self.clock()
        planned_range = plan_recovery_range(checkpoint=checkpoint, current_time=current_time,
                                            overlap=self.config.overlap, bootstrap_start=self.config.bootstrap_start)
        return RecoveryAttempt(account=account, checkpoint_before=checkpoint, planned_range=planned_range)

    async def recover(self, *, account: AccountRef) -> RecoveryResult:
        """Prepare once, ingest once, snapshot once and reconcile without advancing.

        Failed/partial ingestion short-circuits reconciliation. Rejected evidence
        maps to FAILED; indeterminate/approved evidence stays pending. Snapshot
        and evaluator exceptions and cancellation propagate unchanged.
        Earlier deal commits remain persisted, and checkpoints are never written.
        """
        ingestor = self.ingestor
        if ingestor is None:
            raise ValueError("ingestor is required for recover")
        reader = self.snapshot_reader
        if not callable(getattr(reader, "read_snapshot", None)):
            raise TypeError("recover requires a LedgerSnapshotReader")
        attempt = await self.prepare(account=account)
        execution = await ingestor.ingest_range_with_evidence(account_ref=attempt.account,
                                                             from_time=attempt.planned_range.from_time,
                                                             to_time=attempt.planned_range.to_time)
        if not isinstance(execution, IngestionExecution):
            raise TypeError("ingestor must return an IngestionExecution")
        report = execution.report
        reconciliation_result = None
        outcome = {
            IngestionStatus.SUCCESS: RecoveryOutcome.PENDING_RECONCILIATION,
            IngestionStatus.SUCCESS_EMPTY: RecoveryOutcome.PENDING_RECONCILIATION,
            IngestionStatus.FAILED: RecoveryOutcome.FAILED,
            IngestionStatus.PARTIAL: RecoveryOutcome.FAILED,
        }[report.status]
        if report.status in (IngestionStatus.SUCCESS, IngestionStatus.SUCCESS_EMPTY):
            from .reconciliation import evaluate_reconciliation
            from .reconciliation_models import ReconciliationStatus

            if execution.source_evidence is None:
                raise ValueError("successful ingestion requires source evidence")
            snapshot = reader.read_snapshot(account=attempt.account, requested_range=attempt.planned_range)
            reconciliation_result = evaluate_reconciliation(
                attempt=attempt, ingestion_report=report, source_evidence=execution.source_evidence,
                ledger_evidence=snapshot)
            if reconciliation_result.status is ReconciliationStatus.REJECTED:
                outcome = RecoveryOutcome.FAILED
        return RecoveryResult(account=attempt.account, checkpoint_before=attempt.checkpoint_before,
                              planned_range=attempt.planned_range, ingestion_report=report, outcome=outcome,
                              checkpoint_after=attempt.checkpoint_before, reconciliation_result=reconciliation_result)
