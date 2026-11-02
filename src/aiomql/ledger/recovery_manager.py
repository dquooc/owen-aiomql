"""Coordinated recovery with approved-only durable conditional advancement."""

from dataclasses import dataclass
from typing import cast

from .account_lookup import verify_account_identity
from .ingestion import DealIngestor, IngestionExecution, IngestionStatus
from .models import AccountRef, LedgerCheckpoint, RecoveryRange
from .recovery import plan_recovery_range
from .recovery_config import Clock, RecoveryConfig
from .recovery_models import RecoveryOutcome, RecoveryResult
from .repository import AccountIdentityError, AccountLookupRepository, CheckpointRepository, LedgerSnapshotReader
from .repository import ConditionalCheckpointRepository
from .recovery_coordination import AccountRecoveryCoordinator, _DEFAULT_COORDINATOR


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
    """Recover one account range under process-local lifecycle ownership.

    An ingestor is required for recover(), but remains optional for prepare-only
    callers. The snapshot reader defaults to the account lookup repository, which
    must also implement LedgerSnapshotReader for recover(); a separate reader can
    be supplied. Approved reconciliation completes only after durable checkpoint
    and ledger-evidence CAS. No distributed lease or background work is created.
    """

    def __init__(self, *, account_lookup: AccountLookupRepository, checkpoints: CheckpointRepository,
                 config: RecoveryConfig, clock: Clock, ingestor: DealIngestor | None = None,
                 snapshot_reader: LedgerSnapshotReader | None = None,
                 coordinator: AccountRecoveryCoordinator | None = None):
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
        self.coordinator = coordinator if coordinator is not None else _DEFAULT_COORDINATOR

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
        """Own the account lifecycle from verification through result construction.

        The supplied non-nil UUID is only trusted after prepare() verifies its
        registration under ownership, before reading checkpoint or horizon. This
        avoids a pre-lock checkpoint read or duplicate identity/clock capture.
        Cancellation/exception always releases ownership. Different accounts do
        not share a lifecycle lock.
        """
        if not isinstance(account, AccountRef):
            raise TypeError("account must be an AccountRef")
        async with self.coordinator.hold(account.mt5_account_id):
            return await self._recover_owned(account=account)

    async def _recover_owned(self, *, account: AccountRef) -> RecoveryResult:
        """Prepare, ingest, reconcile; only approved durable guarded CAS completes.

        Failed/partial ingestion short-circuits reconciliation. Rejected evidence
        maps to FAILED; indeterminate evidence stays pending. Snapshot
        and evaluator exceptions and cancellation propagate unchanged.
        Earlier deal commits remain persisted. Approval alone is not completion.
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
        checkpoint_after = attempt.checkpoint_before
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
            elif reconciliation_result.status is ReconciliationStatus.APPROVED:
                advancement = cast(ConditionalCheckpointRepository, self.checkpoints)
                checkpoint_after = advancement.advance_checkpoint_if_expected(
                    attempt.account, attempt.planned_range.to_time,
                    expected=attempt.checkpoint_before, expected_snapshot=snapshot)
                if not isinstance(checkpoint_after, LedgerCheckpoint):
                    raise TypeError("conditional advancement must return a durable LedgerCheckpoint")
                if (checkpoint_after.mt5_account_id != attempt.account.mt5_account_id
                        or checkpoint_after.completed_through != attempt.planned_range.to_time):
                    raise ValueError("conditional advancement returned the wrong account or boundary")
                outcome = RecoveryOutcome.COMPLETED
        return RecoveryResult(account=attempt.account, checkpoint_before=attempt.checkpoint_before,
                              planned_range=attempt.planned_range, ingestion_report=report, outcome=outcome,
                              checkpoint_after=checkpoint_after, reconciliation_result=reconciliation_result)
