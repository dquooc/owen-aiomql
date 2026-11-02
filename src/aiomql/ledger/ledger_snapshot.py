"""Narrow immutable ledger read evidence; no source completeness inference."""

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import AccountRef, RecoveryRange
from .reconciliation_evidence import SourceRecordFingerprint


@dataclass(frozen=True, slots=True)
class LedgerRecordFingerprint:
    deal_ticket: int
    payload_hash: str
    executed_at_msc: int
    normalizer_version: int

    def __post_init__(self) -> None:
        # Identical scalar domains, without inventing a hash or normalizer.
        SourceRecordFingerprint(self.deal_ticket, self.payload_hash, self.executed_at_msc, self.normalizer_version)


@dataclass(frozen=True, slots=True)
class LedgerConflictEvidence:
    """An unresolved historical observation, with both execution timestamps."""

    original: LedgerRecordFingerprint
    alternative: LedgerRecordFingerprint

    def __post_init__(self) -> None:
        if not isinstance(self.original, LedgerRecordFingerprint) or not isinstance(self.alternative, LedgerRecordFingerprint):
            raise TypeError("conflict requires two LedgerRecordFingerprint values")
        if self.original.deal_ticket != self.alternative.deal_ticket:
            raise ValueError("conflict variants must belong to the same deal ticket")
        if self.original.payload_hash == self.alternative.payload_hash:
            raise ValueError("conflict variants must have different canonical payload hashes")


def execution_in_range(executed_at_msc: int, requested_range: RecoveryRange) -> bool:
    """Ledger-only closed numeric projection, not a source endpoint guarantee.

    Compare from_us <= stored_msc * 1000 <= to_us using integer arithmetic
    relative to the UTC epoch. No rounding, float timestamp, insertion time, or
    interpretation/correction of broker timestamps is involved. A submillisecond
    equal-endpoint range contains no integer-millisecond record. Raw timestamps
    remain exposed: this convention cannot prove that a source uses these same
    endpoint/timezone semantics. Out-of-view source records need stronger evidence.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def microseconds(value: datetime) -> int:
        delta = value - epoch
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds

    return microseconds(requested_range.from_time) <= executed_at_msc * 1000 <= microseconds(requested_range.to_time)


@dataclass(frozen=True, slots=True)
class LedgerSnapshotEvidence:
    """One consistent account/range read under execution_in_range's convention.

    Records are canonical rows. Conflicts are unresolved observations relevant
    when either original or alternative execution time belongs to the view.
    Construction checks structure; it does not prove database provenance, source
    completeness, or immutable state after the read transaction ends.
    """

    account: AccountRef
    requested_range: RecoveryRange
    records: tuple[LedgerRecordFingerprint, ...]
    conflicts: tuple[LedgerConflictEvidence, ...]

    def __post_init__(self) -> None:
        for name, expected in (("account", AccountRef), ("requested_range", RecoveryRange),
                               ("records", tuple), ("conflicts", tuple)):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        if any(not isinstance(record, LedgerRecordFingerprint) for record in self.records):
            raise TypeError("records must contain LedgerRecordFingerprint values")
        if any(not isinstance(conflict, LedgerConflictEvidence) for conflict in self.conflicts):
            raise TypeError("conflicts must contain LedgerConflictEvidence values")
