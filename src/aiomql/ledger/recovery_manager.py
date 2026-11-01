"""Read-only recovery preparation; ingestion and completion are not performed."""

from dataclasses import dataclass

from .account_lookup import verify_account_identity
from .models import AccountRef, LedgerCheckpoint, RecoveryRange
from .recovery import plan_recovery_range
from .recovery_config import Clock, RecoveryConfig
from .repository import AccountIdentityError, AccountLookupRepository, CheckpointRepository


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    """Verified preparation state; planned_range.to_time is the sole horizon.

    Produced by prepare() before ingestion. This is neither a recovery outcome
    nor proof of completed recovery, and it does not reserve a checkpoint.
    """

    account: AccountRef
    checkpoint_before: LedgerCheckpoint | None
    planned_range: RecoveryRange


class RecoveryManager:
    """Prepare account-scoped recovery using injected storage, policy, and clock.

    Preparation is async for later async ingestion composition. Its current
    collaborators run synchronously; no tasks or background work are created.
    """

    def __init__(self, *, account_lookup: AccountLookupRepository, checkpoints: CheckpointRepository,
                 config: RecoveryConfig, clock: Clock):
        if not isinstance(config, RecoveryConfig):
            raise TypeError("config must be a RecoveryConfig")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.account_lookup = account_lookup
        self.checkpoints = checkpoints
        self.config = config
        self.clock = clock

    async def prepare(self, *, account: AccountRef) -> RecoveryAttempt:
        """Verify identity, read checkpoint, capture one horizon, and plan.

        Failures propagate without retry. Identity/checkpoint failures occur
        before any clock read; clock results are validated by the pure planner.
        """
        account = verify_account_identity(account=account, repository=self.account_lookup)
        checkpoint = self.checkpoints.get_checkpoint(account.mt5_account_id)
        if checkpoint is not None:
            if not isinstance(checkpoint, LedgerCheckpoint):
                raise TypeError("checkpoint must be a LedgerCheckpoint or None")
            if checkpoint.mt5_account_id != account.mt5_account_id:
                raise AccountIdentityError(
                    f"checkpoint account UUID {checkpoint.mt5_account_id} differs from verified account "
                    f"UUID {account.mt5_account_id}")
        current_time = self.clock()
        planned_range = plan_recovery_range(checkpoint=checkpoint, current_time=current_time,
                                            overlap=self.config.overlap, bootstrap_start=self.config.bootstrap_start)
        return RecoveryAttempt(account=account, checkpoint_before=checkpoint, planned_range=planned_range)
