"""Callable boundary for the future local engine; no live runtime work yet."""

from .config import EngineConfig, load_config
from .logging import configure_logging


def run_engine(config: EngineConfig | None = None) -> int:
    """Load settings and configure console only; no sessions, recovery or tasks."""
    config = load_config() if config is None else config
    if not isinstance(config, EngineConfig):
        raise TypeError('config must be an EngineConfig')
    logger = configure_logging(config.log_level)
    logger.getChild('entrypoint').info('engine bootstrap initialized mode=%s', config.mode.value)
    return 0


def main() -> int:
    """Return the bootstrap status; terminal interruption follows exit-code 130."""
    try:
        return run_engine()
    except KeyboardInterrupt:
        return 130
