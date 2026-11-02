"""Callable boundary for the future local engine; no live runtime work yet."""

from .config import EngineConfig, load_config
from .logging import configure_logging
from .safety import evaluate_startup_safety
from .execution import EngineRuntime, build_runtime


def compose_runtime(config: EngineConfig | None = None) -> EngineRuntime:
    """Construct one offline runtime only after config/logging/startup safety.

    No recovery dependency is instantiated: startup safety rejects recovery
    requests until supported wiring exists. The caller owns run/shutdown.
    """
    config = load_config() if config is None else config
    if not isinstance(config, EngineConfig):
        raise TypeError('config must be an EngineConfig')
    logger = configure_logging(config.log_level)
    decision = evaluate_startup_safety(config)
    runtime = build_runtime(decision)
    try:
        logger.getChild('entrypoint').info('engine bootstrap initialized mode=%s', config.mode.value)
        logger.getChild('entrypoint').info(
            'startup capabilities offline=%s dry_run=%s read_only=%s mt5_connection=%s read_only_mt5=%s recovery=%s orders=%s live=%s',
            decision.offline_runtime, runtime.policy.dry_run, runtime.policy.read_only, decision.mt5_connection,
            decision.read_only_mt5, decision.recovery_execution, decision.order_submission, decision.live_trading)
    except BaseException:
        runtime.shutdown()
        raise
    return runtime


def run_engine(config: EngineConfig | None = None) -> int:
    """Own the synchronous run/shutdown pair; propagate failures/cancellation."""
    runtime = compose_runtime(config)
    try:
        return runtime.run()
    finally:
        runtime.shutdown()


def main() -> int:
    """Return the bootstrap status; terminal interruption follows exit-code 130."""
    try:
        return run_engine()
    except KeyboardInterrupt:
        return 130
