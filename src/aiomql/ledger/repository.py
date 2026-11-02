"""Storage-independent contracts for an account-scoped Deal Ledger."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
from typing import Any, Protocol
from uuid import UUID

from .models import AccountRef, LedgerCheckpoint, NormalizedDeal
from .models import RecoveryRange
from .ledger_snapshot import LedgerSnapshotEvidence


class LedgerSnapshotReader(Protocol):
    """Read canonical records and unresolved variants in one consistent snapshot.

    Membership follows ledger_snapshot.execution_in_range, not source endpoint
    assumptions. Unknown/mismatched accounts and decoding/storage errors raise.
    No registration, mutation, checkpoint access, or source calls are permitted.
    """

    def read_snapshot(self, *, account: AccountRef, requested_range: RecoveryRange) -> LedgerSnapshotEvidence: ...


class AccountIdentityError(ValueError):
    """A supplied account identity conflicts with the persisted account mapping."""


class AccountLookupRepository(Protocol):
    """Read-only lookup of registered accounts by their exact stable identity.

    Lookup never registers accounts or updates observed metadata. Storage and
    decoding failures propagate; None means only that the identity is unknown.
    """

    def resolve_account(self, *, broker_namespace: str, server: str, login: int) -> AccountRef | None:
        """Return the account registered for this exact broker/server/login tuple."""
        ...


class CheckpointRegressionError(ValueError):
    """The requested completed boundary precedes the persisted checkpoint."""


class CheckpointRepository(Protocol):
    """Independent storage boundary for account-scoped durable checkpoints.

    Implementations require registered accounts and never create them implicitly.
    Unknown accounts or conflicting write identities raise AccountIdentityError.
    """

    def get_checkpoint(self, mt5_account_id: UUID) -> LedgerCheckpoint | None:
        """Read one account's checkpoint; None means registered but no checkpoint."""
        ...

    def advance_checkpoint(self, account: AccountRef, completed_through: datetime) -> LedgerCheckpoint:
        """Create or advance a checkpoint using an aware completed boundary.

        Verify the registered account identity. Equal instants are idempotent,
        preserving updated_at; earlier instants raise CheckpointRegressionError.
        Generate updated_at in UTC on creation/advancement and return only after
        a durable commit. Compare/write atomically so concurrent callers cannot
        regress progress. Failures propagate without reporting advancement.
        """
        ...


class IngestOutcome(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: IngestOutcome
    deal_id: UUID
    observation_added: bool = False


@dataclass(frozen=True, slots=True)
class DealObservation:
    id: UUID
    deal_id: UUID
    payload_hash: str
    raw_payload_json: str
    observed_at: datetime
    ingestion_id: str
    normalizer_version: int

    @property
    def raw_payload(self) -> dict[str, Any]:
        return json.loads(self.raw_payload_json)


class DealRepository(AccountLookupRepository, Protocol):
    """A storage boundary; every Deal read and write is scoped to an account."""

    def register_account(self, account: AccountRef) -> AccountRef: ...

    def ingest(self, account: AccountRef, deal: NormalizedDeal) -> IngestResult: ...

    def get(self, mt5_account_id: UUID, deal_ticket: int) -> NormalizedDeal | None: ...

    def list_by_order(self, mt5_account_id: UUID, order_ticket: int) -> tuple[NormalizedDeal, ...]: ...

    def list_by_position(self, mt5_account_id: UUID, position_id: int) -> tuple[NormalizedDeal, ...]: ...

    def list_observations(self, mt5_account_id: UUID, deal_ticket: int) -> tuple[DealObservation, ...]: ...

    def close(self) -> None: ...
