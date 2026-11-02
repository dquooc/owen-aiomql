"""Engine-only dry-run capability boundary; no live transport execution branch."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

from .safety import StartupSafetyDecision


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
    """Local composition handle only, without sessions, database or background work."""

    policy: BrokerWritePolicy
    broker_writes: BrokerWriteBarrier

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BrokerWritePolicy) or not isinstance(self.broker_writes, BrokerWriteBarrier):
            raise TypeError('runtime requires execution policy and broker-write barrier')
        if self.broker_writes.policy != self.policy:
            raise ValueError('runtime policy must match its broker-write barrier')


def build_runtime(decision: StartupSafetyDecision) -> EngineRuntime:
    """Project an approved startup decision into a non-live engine handle.

    Local computation/planning/logging remain usable. This does not execute any
    of them, run recovery, or add a read adapter before the Windows-only phase.
    """
    if not isinstance(decision, StartupSafetyDecision):
        raise TypeError('decision must be a StartupSafetyDecision')
    StartupSafetyDecision.__post_init__(decision)
    policy = BrokerWritePolicy(dry_run=decision.dry_run, read_only=decision.read_only)
    return EngineRuntime(policy, BrokerWriteBarrier(policy))
