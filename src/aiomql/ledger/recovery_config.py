"""Stable recovery policy and injectable clock contracts, without orchestration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import RecoveryRange


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return an aware UTC instant; callers decide when to capture it.

    A future recovery attempt must call its injected Clock once, keep the local
    horizon, and let the planner validate it. No capture/retry wrapper is needed.
    """
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    """Explicit replay policy; a missing bootstrap is resolved by the planner.

    Preserve overlap exactly and reuse RecoveryRange's datetime validation and
    UTC canonicalization. This model neither reads clocks nor calculates ranges.
    """

    overlap: timedelta
    bootstrap_start: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.overlap, timedelta):
            raise TypeError("overlap must be a timedelta")
        if self.overlap < timedelta(0):
            raise ValueError("overlap must not be negative")
        if self.bootstrap_start is not None:
            canonical = RecoveryRange(from_time=self.bootstrap_start, to_time=self.bootstrap_start).from_time
            object.__setattr__(self, "bootstrap_start", canonical)
