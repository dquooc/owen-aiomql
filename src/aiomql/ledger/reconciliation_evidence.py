"""Immutable source-side facts for later reconciliation, without collection or I/O."""

from dataclasses import dataclass
from enum import StrEnum

from .models import AccountRef, RecoveryRange


class SourceHistoryAssurance(StrEnum):
    """Assurance of a completed, account/range-bound source call.

    The current MT5 contract supports CALL_COMPLETED_UNVERIFIED after both
    session guards pass. It cannot independently exclude omitted records,
    A -> B -> A session switches, or future broker corrections, and supplies no
    immutable snapshot guarantee. VERIFIED_COMPLETE requires independent support
    from a stronger contract for the requested account/range; it is never inferred
    from counts or ingestion status and does not promise permanent broker history.
    """

    CALL_COMPLETED_UNVERIFIED = "CALL_COMPLETED_UNVERIFIED"
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"


@dataclass(frozen=True, slots=True)
class SourceRecordFingerprint:
    """Carry one normalized source record's identity without recalculating it.

    payload_hash is normalize_deal's canonical raw-payload SHA-256 hexdigest.
    executed_at_msc preserves the source integer exactly; endpoint inclusivity,
    precision, and its mapping to range membership remain unspecified here.
    Version is supplied explicitly, not inferred or restricted to today's version.
    """

    deal_ticket: int
    payload_hash: str
    executed_at_msc: int
    normalizer_version: int

    def __post_init__(self) -> None:
        for name, minimum in (("deal_ticket", 1), ("executed_at_msc", 0), ("normalizer_version", 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < minimum:
                raise ValueError(f"{name} must be >= {minimum}")
        if not isinstance(self.payload_hash, str):
            raise TypeError("payload_hash must be a string")
        if len(self.payload_hash) != 64 or any(char not in "0123456789abcdef" for char in self.payload_hash):
            raise ValueError("payload_hash must be a canonical lowercase SHA-256 hexdigest")


@dataclass(frozen=True, slots=True)
class SourceBatchEvidence:
    """Describe the actual source batch belonging to one ingestion invocation.

    The collector must supply facts from that call, including its guards;
    construction does not prove provenance or approve reconciliation. AccountRef
    carries the stable UUID/broker/server/login identity; observed metadata is not
    an identity key. The existing RecoveryRange owns exact UTC endpoints and
    microseconds. ingestion_id is correlation, not stale-snapshot protection.

    Keep fingerprint order and repeated tickets. Missing fingerprints can reflect
    invalid or unprocessed records, so len(records) may be less than retrieved.
    This model checks structure only, without interpreting completeness or time
    membership. No ledger snapshot placeholder is included.
    """

    ingestion_id: str
    account: AccountRef
    requested_range: RecoveryRange
    assurance: SourceHistoryAssurance
    retrieved: int
    records: tuple[SourceRecordFingerprint, ...]

    def __post_init__(self) -> None:
        for name, expected in (("ingestion_id", str), ("account", AccountRef),
                               ("requested_range", RecoveryRange), ("assurance", SourceHistoryAssurance),
                               ("records", tuple)):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        if not self.ingestion_id.strip():
            raise ValueError("ingestion_id must be a non-empty string")
        if isinstance(self.retrieved, bool) or not isinstance(self.retrieved, int):
            raise TypeError("retrieved must be an integer")
        if self.retrieved < 0:
            raise ValueError("retrieved must not be negative")
        if any(not isinstance(record, SourceRecordFingerprint) for record in self.records):
            raise TypeError("records must contain only SourceRecordFingerprint values")
        if len(self.records) > self.retrieved:
            raise ValueError("records cannot outnumber retrieved source records")
