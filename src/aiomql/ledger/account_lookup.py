"""Read-only persisted account identity verification, independent of history."""

from .models import AccountRef
from .repository import AccountIdentityError, AccountLookupRepository


def verify_account_identity(*, account: AccountRef, repository: AccountLookupRepository) -> AccountRef:
    """Verify the registered tuple-to-UUID mapping and return the supplied account.

    Observed company/trade-mode metadata is neither compared nor persisted.
    Unknown identities and UUID mismatches fail even without deals/checkpoints.
    Repository failures propagate; this helper performs no registration or I/O
    beyond the single read-only lookup.
    """
    if not isinstance(account, AccountRef):
        raise TypeError("account must be an AccountRef")
    stored = repository.resolve_account(broker_namespace=account.broker_namespace,
                                        server=account.server, login=account.login)
    identity = (f"broker_namespace={account.broker_namespace!r}, server={account.server!r}, "
                f"login={account.login}")
    if stored is None:
        raise AccountIdentityError(f"account UUID {account.mt5_account_id} is not registered for {identity}")
    if stored.mt5_account_id != account.mt5_account_id:
        raise AccountIdentityError(
            f"account UUID {account.mt5_account_id} differs from registered UUID {stored.mt5_account_id} "
            f"for {identity}")
    return account
