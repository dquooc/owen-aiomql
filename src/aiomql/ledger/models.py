"""Pure data contracts for historical MT5 deals.

The caller must supply a persisted mt5_account_id. AccountRef deliberately does
not create one: assigning a new UUID on restart would split one account's ledger.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AccountRef:
    mt5_account_id: UUID
    broker_namespace: str
    server: str
    login: int
    company_observed: str
    trade_mode: int

    def __post_init__(self) -> None:
        if isinstance(self.mt5_account_id, str):
            object.__setattr__(self, "mt5_account_id", UUID(self.mt5_account_id))
        if not isinstance(self.mt5_account_id, UUID):
            raise TypeError("mt5_account_id must be a persisted UUID")
        if self.mt5_account_id.int == 0:
            raise ValueError("mt5_account_id cannot be the nil UUID")
        for name in ("broker_namespace", "server"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.company_observed, str):
            raise TypeError("company_observed must be a string")
        if isinstance(self.login, bool) or not isinstance(self.login, int) or self.login <= 0:
            raise ValueError("login must be a positive integer")
        if isinstance(self.trade_mode, bool) or not isinstance(self.trade_mode, int):
            raise TypeError("trade_mode must be an integer code")


@dataclass(frozen=True, slots=True)
class LedgerCheckpoint:
    """An explicitly verified completed boundary for an existing ledger account.

    completed_through includes the boundary and is never inferred from deal or
    broker execution times. updated_at records checkpoint creation/advancement;
    it is audit metadata and may precede completed_through.
    """

    mt5_account_id: UUID
    completed_through: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.mt5_account_id, UUID):
            raise TypeError("mt5_account_id must be a persisted UUID")
        if self.mt5_account_id.int == 0:
            raise ValueError("mt5_account_id cannot be the nil UUID")
        for name in ("completed_through", "updated_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware datetime")
            object.__setattr__(self, name, value.astimezone(timezone.utc))


@dataclass(frozen=True, slots=True)
class RecoveryRange:
    """An aware UTC history interval; equal endpoints are valid."""

    from_time: datetime
    to_time: datetime

    def __post_init__(self) -> None:
        for name in ("from_time", "to_time"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware datetime")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.from_time > self.to_time:
            raise ValueError("from_time must not be after to_time")


@dataclass(frozen=True, slots=True)
class NormalizedDeal:
    id: UUID
    mt5_account_id: UUID
    deal_ticket: int
    order_ticket: int
    position_id: int
    symbol: str
    deal_type: int
    deal_entry: int
    magic: int
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    broker_fee: Decimal
    broker_time_seconds: int
    executed_at_msc: int
    reason: int | None
    sl: Decimal | None
    tp: Decimal | None
    comment: str | None
    external_id: str | None
    raw_payload_json: str = field(repr=False)
    payload_hash: str
    normalizer_version: int
    first_seen_at: datetime
    ingestion_id: str

    @property
    def raw_payload(self) -> dict[str, Any]:
        """Return a fresh SDK snapshot so callers cannot mutate the hashed copy."""
        return json.loads(self.raw_payload_json)
