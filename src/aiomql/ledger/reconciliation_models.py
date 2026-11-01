"""Immutable reconciliation decisions without evidence evaluation or I/O."""

from dataclasses import dataclass
from enum import StrEnum

from .ingestion import IngestionReport
from .recovery_manager import RecoveryAttempt


class ReconciliationStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"


class ReconciliationReason(StrEnum):
    BINDING_MISMATCH = "BINDING_MISMATCH"
    INGESTION_INCOMPLETE = "INGESTION_INCOMPLETE"
    ACCOUNTING_MISMATCH = "ACCOUNTING_MISMATCH"
    CONFLICT_PRESENT = "CONFLICT_PRESENT"
    LEDGER_MISMATCH = "LEDGER_MISMATCH"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Preserve an explicit decision and its supplied preparation/report context.

    Validation covers types and status/reason structure only. It neither checks
    report/attempt semantics nor establishes checkpoint advancement eligibility.
    """

    attempt: RecoveryAttempt
    ingestion_report: IngestionReport
    status: ReconciliationStatus
    reason: ReconciliationReason | None

    def __post_init__(self) -> None:
        for name, expected in (("attempt", RecoveryAttempt), ("ingestion_report", IngestionReport),
                               ("status", ReconciliationStatus)):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        if self.reason is not None and not isinstance(self.reason, ReconciliationReason):
            raise TypeError("reason must be a ReconciliationReason or None")
        if self.status is ReconciliationStatus.APPROVED:
            if self.reason is not None:
                raise ValueError("APPROVED requires reason to be None")
        elif self.reason is None:
            raise ValueError("REJECTED and INDETERMINATE require a reason")
