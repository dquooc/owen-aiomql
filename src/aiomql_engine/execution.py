"""Engine-only dry-run capability boundary; no live transport execution branch."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

from .safety import StartupSafetyDecision
from .mt5_readonly import ReadOnlySession


class BrokerWriteDeniedError(PermissionError):
    """The engine denied mutation before touching an adapter or request."""


class BrokerRequestWriter(Protocol):
    """Future request transport seam, not an SDK or a new trading API."""

    async def submit(self, request: Mapping[str, object]) -> object: ...


@dataclass(frozen=True, slots=True)
class BrokerWritePolicy:
    dry_run: bool = True
    read_only: bool = True

    def __post_init__(self) -> None:
        if type(self.dry_run) is not bool or type(self.read_only) is not bool:
            raise TypeError('execution policy flags must be bool')
        if not self.dry_run or not self.read_only:
            raise ValueError('only dry-run/read-only engine execution is implemented')

    @property
    def local_computation(self) -> bool:
        return True

    @property
    def broker_writes(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class BrokerWriteBarrier:
    """Single request seam covering all engine broker mutations.

    An optional opaque writer can be bound for future composition/tests, but A9
    deliberately contains NO call to it, even for a malformed/future policy.
    Startup binds none. Direct legacy library calls outside this engine seam are
    outside its scope; this is an application boundary, not a Python sandbox.
    """

    policy: BrokerWritePolicy
    _writer: BrokerRequestWriter | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BrokerWritePolicy):
            raise TypeError('policy must be a BrokerWritePolicy')

    async def submit(self, request: Mapping[str, object]) -> NoReturn:
        """Deny before inspecting request/adapter; never fabricate a broker result."""
        raise BrokerWriteDeniedError('broker mutation is disabled in the engine runtime')


@dataclass(frozen=True, slots=True)
class EngineRuntime:
    """Explicit offline or read-only session ownership; no recovery/background work."""

    policy: BrokerWritePolicy
    broker_writes: BrokerWriteBarrier
    read_only_session: ReadOnlySession | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BrokerWritePolicy) or not isinstance(self.broker_writes, BrokerWriteBarrier):
            raise TypeError('runtime requires execution policy and broker-write barrier')
        if self.broker_writes.policy != self.policy:
            raise ValueError('runtime policy must match its broker-write barrier')

    def run(self) -> int:
        """Open the explicitly supplied read session, without fetching history.

        Offline runs remain resource-free. The caller owns shutdown after a
        successful run; errors/cancellation during open release the session here.
        """
        BrokerWritePolicy.__post_init__(self.policy)
        if self.read_only_session is not None:
            try:
                self.read_only_session.open()
            except BaseException:
                self.shutdown()
                raise
        return 0

    def shutdown(self) -> None:
        """Release the session; its shutdown is idempotent. Logger ownership is unchanged."""
        if self.read_only_session is not None:
            self.read_only_session.shutdown()


def build_runtime(decision: StartupSafetyDecision, *, read_only_session: ReadOnlySession | None = None) -> EngineRuntime:
    """Bind an optional read session after the gate, without opening it or recovery."""
    if not isinstance(decision, StartupSafetyDecision):
        raise TypeError('decision must be a StartupSafetyDecision')
    StartupSafetyDecision.__post_init__(decision)
    if read_only_session is not None and not decision.read_only_mt5:
        raise ValueError('startup safety prohibits an MT5 session')
    policy = BrokerWritePolicy(dry_run=decision.dry_run, read_only=decision.read_only)
    return EngineRuntime(policy, BrokerWriteBarrier(policy), read_only_session)
