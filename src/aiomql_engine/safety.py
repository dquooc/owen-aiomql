"""Pure fail-closed startup decisions, without environment or runtime access."""

from dataclasses import dataclass, fields

from .config import EngineConfig, EngineMode


class StartupSafetyError(ValueError):
    """Startup requests contradictory or currently unsupported capabilities."""


@dataclass(frozen=True, slots=True)
class StartupSafetyDecision:
    offline_runtime: bool
    mt5_connection: bool
    read_only_mt5: bool
    recovery_execution: bool = False
    order_submission: bool = False
    live_trading: bool = False
    dry_run: bool = True
    read_only: bool = True

    def __post_init__(self) -> None:
        if any(type(getattr(self, field.name)) is not bool for field in fields(self)):
            raise TypeError('startup capabilities must be bool')
        if (not self.dry_run or not self.read_only or self.order_submission or self.live_trading
                or self.recovery_execution or self.offline_runtime and self.mt5_connection
                or self.read_only_mt5 != self.mt5_connection):
            raise StartupSafetyError('unsupported or contradictory startup capabilities')


def evaluate_startup_safety(config: EngineConfig) -> StartupSafetyDecision:
    """Authorize only local bootstrap and optional FUTURE read-only connection.

    A permission is not runtime wiring. Recovery and all broker writes remain
    unsupported. Reject requests explicitly, without repairing flags or exposing
    config values. Recheck capability domains without normalizing/mutating config.
    """
    if not isinstance(config, EngineConfig):
        raise TypeError('config must be an EngineConfig')
    if not isinstance(config.mode, EngineMode):
        raise StartupSafetyError('invalid engine mode')
    for name in ('mt5_enabled', 'recovery_enabled', 'dry_run', 'read_only',
                 'live_trading_enabled', 'order_submission_enabled'):
        if type(getattr(config, name)) is not bool:
            raise StartupSafetyError('invalid startup flag type')
    if config.live_trading_enabled or config.order_submission_enabled or not config.dry_run or not config.read_only:
        raise StartupSafetyError('startup does not permit trading or broker mutation')
    offline = config.mode is EngineMode.OFFLINE
    if offline and config.mt5_enabled:
        raise StartupSafetyError('offline startup prohibits MT5 connection')
    if config.recovery_enabled:
        raise StartupSafetyError('recovery runtime is not wired for startup')
    return StartupSafetyDecision(offline_runtime=offline, mt5_connection=config.mt5_enabled,
                                  read_only_mt5=config.mt5_enabled)
