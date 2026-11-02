"""Pure conservative reconciliation of bound source and ledger evidence."""

from typing import TYPE_CHECKING

from .ingestion import IngestionReport, IngestionStatus
from .ledger_snapshot import LedgerSnapshotEvidence, execution_in_range
from .models import AccountRef
from .reconciliation_evidence import SourceBatchEvidence, SourceHistoryAssurance
from .reconciliation_models import ReconciliationReason, ReconciliationResult, ReconciliationStatus

if TYPE_CHECKING:
    from .recovery_manager import RecoveryAttempt


def _identity(account: AccountRef) -> tuple:
    return account.mt5_account_id, account.broker_namespace, account.server, account.login


def _fingerprint(record) -> tuple:
    return record.deal_ticket, record.payload_hash, record.executed_at_msc, record.normalizer_version


def evaluate_reconciliation(*, attempt: "RecoveryAttempt", ingestion_report: IngestionReport,
                            source_evidence: SourceBatchEvidence,
                            ledger_evidence: LedgerSnapshotEvidence) -> ReconciliationResult:
    """Evaluate immutable facts only, without I/O, hashing, normalization or clocks.

    Precedence: binding; failed/partial status; counter domains; processing errors;
    arithmetic; conflicts; insufficient coverage/assurance/range interpretation;
    ledger mismatch; approval. A positive invalid/error state means incomplete
    processing, even if arithmetic is also inconsistent. Malformed scalar counters
    cannot participate in that decision. Approval never means recovery completion.

    Identical repeated source tickets require duplicate accounting and compare to
    one canonical ledger row. Differing variants disqualify reconciliation. Source
    assurance must independently support completeness; current guarded calls do
    not. A source timestamp outside the ledger's numeric projection cannot be
    silently discarded or assigned invented source endpoint semantics.
    """
    from .recovery_manager import RecoveryAttempt

    for name, value, expected in (("attempt", attempt, RecoveryAttempt),
                                   ("ingestion_report", ingestion_report, IngestionReport),
                                   ("source_evidence", source_evidence, SourceBatchEvidence),
                                   ("ledger_evidence", ledger_evidence, LedgerSnapshotEvidence)):
        if not isinstance(value, expected):
            raise TypeError(f"{name} must be a {expected.__name__}")
    report, source, snapshot = ingestion_report, source_evidence, ledger_evidence

    def reject(reason: ReconciliationReason) -> ReconciliationResult:
        return ReconciliationResult(attempt, report, ReconciliationStatus.REJECTED, reason)

    def insufficient() -> ReconciliationResult:
        return ReconciliationResult(attempt, report, ReconciliationStatus.INDETERMINATE,
                                    ReconciliationReason.EVIDENCE_INSUFFICIENT)

    if (report.mt5_account_id != attempt.account.mt5_account_id
            or _identity(source.account) != _identity(attempt.account)
            or _identity(snapshot.account) != _identity(attempt.account)
            or source.ingestion_id != report.ingestion_id
            or source.requested_range != attempt.planned_range
            or snapshot.requested_range != attempt.planned_range
            or report.requested_from != attempt.planned_range.from_time
            or report.requested_to != attempt.planned_range.to_time):
        return reject(ReconciliationReason.BINDING_MISMATCH)
    if not isinstance(report.status, IngestionStatus):
        raise TypeError("report status must be an IngestionStatus")
    if report.status in (IngestionStatus.FAILED, IngestionStatus.PARTIAL):
        return reject(ReconciliationReason.INGESTION_INCOMPLETE)
    counters = (report.retrieved, report.normalized, report.inserted, report.duplicates, report.conflicts, report.invalid)
    if any(type(value) is not int or value < 0 for value in counters):
        return reject(ReconciliationReason.ACCOUNTING_MISMATCH)
    if report.invalid or report.errors != ():
        return reject(ReconciliationReason.INGESTION_INCOMPLETE)
    if (report.retrieved != report.normalized + report.invalid
            or report.normalized != report.inserted + report.duplicates + report.conflicts
            or report.retrieved != source.retrieved
            or (report.status is IngestionStatus.SUCCESS and report.retrieved == 0)
            or (report.status is IngestionStatus.SUCCESS_EMPTY and any(counters))):
        return reject(ReconciliationReason.ACCOUNTING_MISMATCH)
    if report.conflicts or snapshot.conflicts:
        return reject(ReconciliationReason.CONFLICT_PRESENT)
    source_by_ticket = {}
    for record in source.records:
        fingerprint = _fingerprint(record)
        previous = source_by_ticket.setdefault(record.deal_ticket, fingerprint)
        if previous != fingerprint:
            return reject(ReconciliationReason.CONFLICT_PRESENT)
    if (report.inserted > len(source_by_ticket) and len(source.records) == source.retrieved):
        return reject(ReconciliationReason.ACCOUNTING_MISMATCH)
    if (len(source.records) != source.retrieved
            or source.assurance is not SourceHistoryAssurance.VERIFIED_COMPLETE
            or any(not execution_in_range(r.executed_at_msc, attempt.planned_range) for r in source.records)
            or any(not execution_in_range(r.executed_at_msc, attempt.planned_range) for r in snapshot.records)):
        return insufficient()
    ledger_by_ticket = {record.deal_ticket: _fingerprint(record) for record in snapshot.records}
    if len(ledger_by_ticket) != len(snapshot.records) or source_by_ticket != ledger_by_ticket:
        return reject(ReconciliationReason.LEDGER_MISMATCH)
    return ReconciliationResult(attempt, report, ReconciliationStatus.APPROVED, None)
