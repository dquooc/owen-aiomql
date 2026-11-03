"""Pure bootstrap settings; separate from the trading Config's persistent state."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
import os
from pathlib import Path
import re


class EngineMode(StrEnum):
    OFFLINE = 'offline'
    READ_ONLY = 'read_only'


class LogLevel(StrEnum):
    DEBUG = 'DEBUG'
    INFO = 'INFO'
    WARNING = 'WARNING'
    ERROR = 'ERROR'
    CRITICAL = 'CRITICAL'


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Requests only; constructing/loading settings never activates runtime work.

    Trading/order submission are unsupported in this bootstrap. Keeping flags
    explicit does not implement the later startup-safety or live authorization
    boundary. Paths are validated lexically, without reading/creating files.
    """

    mode: EngineMode = EngineMode.OFFLINE
    database_path: str | os.PathLike[str] = field(default=Path('aiomql.sqlite3'), repr=False)
    log_level: LogLevel = LogLevel.INFO
    mt5_enabled: bool = False
    recovery_enabled: bool = False
    recovery_overlap: timedelta = timedelta(0)
    dry_run: bool = True
    read_only: bool = True
    live_trading_enabled: bool = False
    order_submission_enabled: bool = False
    mt5_expected_login: int | None = field(default=None, repr=False)
    mt5_expected_server: str | None = field(default=None, repr=False)
    mt5_timeout_ms: int = 10000
    mt5_max_history_days: int = 7

    def __post_init__(self) -> None:
        for name, expected in (('mode', EngineMode), ('log_level', LogLevel), ('recovery_overlap', timedelta)):
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f'{name} must be {expected.__name__}')
        if self.recovery_overlap < timedelta(0):
            raise ValueError('recovery_overlap must be nonnegative')
        for name in ('mt5_enabled', 'recovery_enabled', 'dry_run', 'read_only',
                     'live_trading_enabled', 'order_submission_enabled'):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f'{name} must be bool')
        if self.live_trading_enabled or self.order_submission_enabled or not self.dry_run or not self.read_only:
            raise ValueError('bootstrap supports only dry-run/read-only with trading and submission disabled')
        if self.mt5_expected_login is not None and (type(self.mt5_expected_login) is not int or self.mt5_expected_login <= 0):
            raise ValueError('mt5_expected_login must be a positive integer')
        if self.mt5_expected_server is not None and (not isinstance(self.mt5_expected_server, str)
                                                    or not self.mt5_expected_server.strip()):
            raise ValueError('mt5_expected_server must be nonblank text')
        for name in ('mt5_timeout_ms', 'mt5_max_history_days'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.mt5_timeout_ms > 2147483647 or self.mt5_max_history_days > 999999999:
            raise ValueError('MT5 timeout or history limit is out of range')
        if not isinstance(self.database_path, (str, os.PathLike)):
            raise TypeError('database_path must be text path-like')
        try:
            raw = os.fspath(self.database_path)
            if not isinstance(raw, str) or not raw.strip() or '\x00' in raw:
                raise ValueError
            path = Path(raw)
            if path.name in ('', '.', '..'):
                raise ValueError
        except (TypeError, ValueError, OSError):
            raise ValueError('database_path must name a file') from None
        object.__setattr__(self, 'database_path', path)

    def validate_mt5_identity(self) -> None:
        """Require an explicit binding before constructing any connection adapter."""
        self.__post_init__()
        if self.mt5_expected_login is None or self.mt5_expected_server is None:
            raise ValueError('MT5 connectivity requires expected login and server')


def load_config(environ: Mapping[str, str] | None = None) -> EngineConfig:
    """Read known AIOMQL_ENGINE_* names only; unknown names are ignored.

    No .env file, credential variable, filesystem or environment mutation. Parsing
    errors identify the setting but never reproduce its supplied value.
    """
    env = os.environ if environ is None else environ
    values = {}
    for suffix, field_name, parser in (
        ('MODE', 'mode', EngineMode), ('LOG_LEVEL', 'log_level', LogLevel),
        ('DATABASE_PATH', 'database_path', str),
        ('MT5_ENABLED', 'mt5_enabled', _boolean), ('RECOVERY_ENABLED', 'recovery_enabled', _boolean),
        ('DRY_RUN', 'dry_run', _boolean), ('READ_ONLY', 'read_only', _boolean),
        ('LIVE_TRADING_ENABLED', 'live_trading_enabled', _boolean),
        ('ORDER_SUBMISSION_ENABLED', 'order_submission_enabled', _boolean),
        ('RECOVERY_OVERLAP_MS', 'recovery_overlap', _duration),
        ('MT5_EXPECTED_LOGIN', 'mt5_expected_login', _positive_integer),
        ('MT5_EXPECTED_SERVER', 'mt5_expected_server', _server),
        ('MT5_TIMEOUT_MS', 'mt5_timeout_ms', _positive_integer),
        ('MT5_MAX_HISTORY_DAYS', 'mt5_max_history_days', _positive_integer),
    ):
        name = 'AIOMQL_ENGINE_' + suffix
        raw = env.get(name)
        if raw is None:
            continue
        try:
            if not isinstance(raw, str):
                raise ValueError
            values[field_name] = parser(raw)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f'invalid {name}') from None
    return EngineConfig(**values)


def _boolean(value: str) -> bool:
    if value not in ('true', 'false'):
        raise ValueError
    return value == 'true'


def _duration(value: str) -> timedelta:
    if re.fullmatch(r'[0-9]+', value) is None:
        raise ValueError
    return timedelta(milliseconds=int(value))


def _positive_integer(value: str) -> int:
    if re.fullmatch(r'[0-9]+', value) is None or int(value) <= 0:
        raise ValueError
    return int(value)


def _server(value: str) -> str:
    if not value.strip():
        raise ValueError
    return value
