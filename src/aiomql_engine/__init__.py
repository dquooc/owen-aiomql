"""SDK-independent engine composition root; importing it performs no bootstrap."""

from .entrypoint import compose_runtime, main, run_engine

__all__ = ["main", "run_engine", "compose_runtime"]
