"""Callable boundary for the future local engine; no live runtime work yet."""

from .config import EngineConfig, load_config
from .logging import configure_logging
from .safety import evaluate_startup_safety
from .execution import build_runtime


def run_engine(config: EngineConfig | None = None) -> int:
    """Load settings and configure console only; no sessions, recovery or tasks."""
    config = load_config() if config is None else config
    if not isinstance(config, EngineConfig):
        raise TypeError('config must be an EngineConfig')
    logger = configure_logging(config.log_level)
    decision = evaluate_startup_safety(config)
    runtime = build_runtime(decision)
    logger.getChild('entrypoint').info('engine bootstrap initialized mode=%s', config.mode.value)
    logger.getChild('entrypoint').info(
        'startup capabilities offline=%s dry_run=%s read_only=%s mt5_connection=%s read_only_mt5=%s recovery=%s orders=%s live=%s',
        decision.offline_runtime, runtime.policy.dry_run, runtime.policy.read_only, decision.mt5_connection,
        decision.read_only_mt5, decision.recovery_execution, decision.order_submission, decision.live_trading)
    return 0


def main() -> int:
    """Return the bootstrap status; terminal interruption follows exit-code 130."""
    try:
        return run_engine()
    except KeyboardInterrupt:
        return 130
