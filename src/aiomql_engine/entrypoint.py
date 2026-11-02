"""Callable boundary for the future local engine; no live runtime work yet."""

from .config import EngineConfig, load_config


def run_engine(config: EngineConfig | None = None) -> int:
    """Load/validate settings only; no sessions, recovery, database or tasks."""
    config = load_config() if config is None else config
    if not isinstance(config, EngineConfig):
        raise TypeError('config must be an EngineConfig')
    return 0


def main() -> int:
    """Return the bootstrap status; terminal interruption follows exit-code 130."""
    try:
        return run_engine()
    except KeyboardInterrupt:
        return 130
