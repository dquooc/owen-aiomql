"""Immutable recovery observations, without orchestration or completion inference."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .ingestion import IngestionReport
from .models import AccountRef, LedgerCheckpoint, RecoveryRange

if TYPE_CHECKING:
    from .reconciliation_models import ReconciliationResult


class RecoveryOutcome(StrEnum):
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Preserve one attempt's inputs, ingestion report, and explicit outcome.

    checkpoint_after is the state confirmed by this attempt, not a guarantee
    against concurrent writers. COMPLETED requires manager approval and durable
    conditional advancement; this passive DTO does not infer orchestration.
    Type checks do not infer outcomes or validate orchestration decisions.
    """

    account: AccountRef
    checkpoint_before: LedgerCheckpoint | None
    planned_range: RecoveryRange
    ingestion_report: IngestionReport
    outcome: RecoveryOutcome
    checkpoint_after: LedgerCheckpoint | None
    reconciliation_result: "ReconciliationResult | None" = None

    def __post_init__(self) -> None:
        for name, expected in (("account", AccountRef), ("planned_range", RecoveryRange),
                               ("ingestion_report", IngestionReport), ("outcome", RecoveryOutcome)):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        for name in ("checkpoint_before", "checkpoint_after"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, LedgerCheckpoint):
                raise TypeError(f"{name} must be a LedgerCheckpoint or None")
        if self.reconciliation_result is not None:
            from .reconciliation_models import ReconciliationResult
            if not isinstance(self.reconciliation_result, ReconciliationResult):
                raise TypeError("reconciliation_result must be a ReconciliationResult or None")
