"""Pure boundary helpers for future recovery planning."""

from datetime import datetime

from .models import RecoveryRange


def _plan_bootstrap_range(*, bootstrap_start: datetime, current_time: datetime) -> RecoveryRange:
    """Use explicit bootstrap bounds, validated and canonicalized by RecoveryRange."""
    return RecoveryRange(from_time=bootstrap_start, to_time=current_time)
