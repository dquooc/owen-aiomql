"""Callable boundary for the future local engine; no live runtime work yet."""


def run_engine() -> int:
    """Run the currently empty bootstrap, without sessions, recovery or tasks."""
    return 0


def main() -> int:
    """Return the bootstrap status; terminal interruption follows exit-code 130."""
    try:
        return run_engine()
    except KeyboardInterrupt:
        return 130
