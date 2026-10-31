"""Pure boundary helpers for future recovery planning."""

from datetime import datetime, timedelta

from .models import LedgerCheckpoint, RecoveryRange


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
