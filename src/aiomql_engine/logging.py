"""Explicit engine-only console handler ownership; no import-time setup."""

import logging
import sys
from threading import RLock
from typing import TextIO

from .config import LogLevel

_CONFIG_LOCK = RLock()
_FORMAT = '%(levelname)s %(name)s %(message)s'


class _EngineConsoleHandler(logging.StreamHandler):
    """Marker for the single handler owned by this bootstrap."""


def configure_logging(level: LogLevel, *, stream: TextIO | None = None) -> logging.Logger:
    """Configure only aiomql_engine; preserve root/library/user-attached handlers.

    Repeated initialization reuses the owned handler and updates its destination.
    This is not a generic secret scanner: callers must log only selected safe
    fields, never config objects, credential values or environment contents.
    """
    if not isinstance(level, LogLevel):
        raise TypeError('level must be a LogLevel')
    with _CONFIG_LOCK:
        logger = logging.getLogger('aiomql_engine')
        handler = next((h for h in logger.handlers if isinstance(h, _EngineConsoleHandler)), None)
        destination = sys.stderr if stream is None else stream
        if handler is None:
            handler = _EngineConsoleHandler(destination)
            logger.addHandler(handler)
        else:
            handler.setStream(destination)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.setLevel(logging.NOTSET)
        logger.setLevel(level.value)
        logger.propagate = False
        return logger
