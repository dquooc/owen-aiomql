"""Pure recovery range planning without clock reads or I/O."""

from datetime import datetime, timedelta

from .models import LedgerCheckpoint, RecoveryRange


def plan_recovery_range(
    *,
    checkpoint: LedgerCheckpoint | None,
    current_time: datetime,
    overlap: timedelta,
    bootstrap_start: datetime | None = None,
) -> RecoveryRange:
    """Plan checkpoint replay or explicit bootstrap through the supplied time.

    Validate checkpoint type, current time, then non-negative overlap in order.
    With a checkpoint, bootstrap_start is ignored entirely. Otherwise an explicit
    bootstrap start is required, and overlap does not alter that initial range.
    Branch ordering errors and conversion/arithmetic overflow propagate.
    """
    if checkpoint is not None and not isinstance(checkpoint, LedgerCheckpoint):
        raise TypeError("checkpoint must be a LedgerCheckpoint or None")
    canonical_current_time = RecoveryRange(from_time=current_time, to_time=current_time).from_time
    if not isinstance(overlap, timedelta):
        raise TypeError("overlap must be a timedelta")
    if overlap < timedelta(0):
        raise ValueError("overlap must not be negative")

    if checkpoint is None:
        if bootstrap_start is None:
            raise ValueError("bootstrap_start is required when checkpoint is None")
        return _plan_bootstrap_range(bootstrap_start=bootstrap_start, current_time=canonical_current_time)
    return _plan_checkpoint_range(checkpoint=checkpoint, current_time=canonical_current_time, overlap=overlap)


def _plan_bootstrap_range(*, bootstrap_start: datetime, current_time: datetime) -> RecoveryRange:
    """Use explicit bootstrap bounds, validated and canonicalized by RecoveryRange."""
    return RecoveryRange(from_time=bootstrap_start, to_time=current_time)


def _plan_checkpoint_range(*, checkpoint: LedgerCheckpoint, current_time: datetime,
                           overlap: timedelta) -> RecoveryRange:
    """Replay overlap before an inclusive checkpoint, never hiding a future one."""
    if not isinstance(checkpoint, LedgerCheckpoint):
        raise TypeError("checkpoint must be a LedgerCheckpoint")
    if not isinstance(overlap, timedelta):
        raise TypeError("overlap must be a timedelta")
    if overlap < timedelta(0):
        raise ValueError("overlap must not be negative")
    # Validate current_time and reject future checkpoints before subtracting.
    completed_range = RecoveryRange(from_time=checkpoint.completed_through, to_time=current_time)
    return RecoveryRange(from_time=completed_range.from_time - overlap, to_time=completed_range.to_time)
