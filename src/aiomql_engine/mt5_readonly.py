"""Engine-owned connection/read seam; importing this module never loads the SDK.

The async read methods structurally implement ledger.source.MetaTraderHistoryClient.
No aiomql root import is needed (that root eagerly imports legacy trading code).
Normalization, source evidence and reconciliation remain owned by the ledger.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import import_module
from threading import RLock
from typing import Any, Protocol

from .config import EngineConfig
from .safety import evaluate_startup_safety


class ReadOnlySessionError(RuntimeError):
    """Sanitized connection/read failure, without SDK exception text."""


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: int
    server: str


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    connected: bool


class ReadOnlySession(Protocol):
    def open(self) -> None: ...
    def shutdown(self) -> None: ...
    async def account_info(self) -> AccountInfo: ...
    async def terminal_info(self) -> TerminalInfo: ...
    async def history_deals_get(self, *, date_from: datetime, date_to: datetime) -> Any: ...


SessionFactory = Callable[[EngineConfig], ReadOnlySession]


def _load_sdk() -> Any:
    return import_module('MetaTrader5')


@dataclass(frozen=True, slots=True, repr=False)
class _ReadCalls:
    initialize: Callable[..., Any]
    account_info: Callable[..., Any]
    terminal_info: Callable[..., Any]
    history_deals_get: Callable[..., Any]
    shutdown: Callable[..., Any]


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


class MT5ReadOnlySession:
    """One explicit terminal session, with no login/account-switch or writer API.

    Uses the already authenticated terminal. Each native call is synchronous;
    async read signatures adapt the existing source protocol without a worker,
    event-loop ownership, retry or scheduler. Native calls cannot be interrupted
    mid-call; cancellation is checked before reads. One lock serializes session
    operations, including both account guards and history. This is an application
    boundary, not a Python sandbox or proof against external A -> B -> A switches.
    """

    __slots__ = ('__config', '__loader', '__calls', '__attempted', '__opened', '__closed', '__lock')

    def __init__(self, config: EngineConfig, *, sdk_loader: Callable[[], Any] = _load_sdk):
        if not evaluate_startup_safety(config).read_only_mt5:
            raise ValueError('read-only MT5 connectivity must be explicitly enabled')
        config.validate_mt5_identity()
        self.__config = config
        self.__loader = sdk_loader
        self.__calls: _ReadCalls | None = None
        self.__attempted = False
        self.__opened = False
        self.__closed = False
        self.__lock = RLock()

    def _invoke(self, stage: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
        except Exception:
            raise ReadOnlySessionError(f'MT5 {stage} failed; SDK details withheld') from None

    def _cleanup_after_error(self) -> None:
        try:
            self.shutdown()
        except Exception:
            # Preserve the original failure/cancellation. Shutdown was attempted
            # once and the session is permanently closed even if native cleanup failed.
            pass

    def open(self) -> None:
        with self.__lock:
            if self.__closed:
                raise ReadOnlySessionError('MT5 session is closed')
            if self.__opened:
                return
            try:
                sdk = self._invoke('SDK loading', self.__loader)
                # Retain only named read/lifecycle callables, never the SDK facade.
                self.__calls = self._invoke('read API binding', lambda: _ReadCalls(
                    sdk.initialize, sdk.account_info, sdk.terminal_info,
                    sdk.history_deals_get, sdk.shutdown))
                self.__attempted = True
                if self._invoke('initialize', self.__calls.initialize,
                                timeout=self.__config.mt5_timeout_ms) is not True:
                    raise ReadOnlySessionError('MT5 initialization failed')
                self._invoke('account verification', self._account)
                self._invoke('terminal verification', self._terminal)
                self.__opened = True
            except BaseException:
                self._cleanup_after_error()
                raise

    def _account(self) -> AccountInfo:
        value = self._invoke('account read', self.__calls.account_info)
        login, server = _field(value, 'login'), _field(value, 'server')
        if (type(login) is not int or login != self.__config.mt5_expected_login
                or not isinstance(server, str) or server != self.__config.mt5_expected_server):
            raise ReadOnlySessionError('MT5 account identity mismatch or unavailable')
        return AccountInfo(login=login, server=server)

    def _terminal(self) -> TerminalInfo:
        value = self._invoke('terminal read', self.__calls.terminal_info)
        if _field(value, 'connected') is not True:
            raise ReadOnlySessionError('MT5 terminal is not connected')
        return TerminalInfo(connected=True)

    async def _read(self, operation: Callable[[], Any]) -> Any:
        try:
            # A cancelled task must not enter a synchronous native read.
            await asyncio.sleep(0)
            with self.__lock:
                if not self.__opened or self.__closed:
                    raise ReadOnlySessionError('MT5 session is not open')
                return self._invoke('read', operation)
        except BaseException:
            self._cleanup_after_error()
            raise

    async def account_info(self) -> AccountInfo:
        return await self._read(self._account)

    async def terminal_info(self) -> TerminalInfo:
        return await self._read(self._terminal)

    async def history_deals_get(self, *, date_from: datetime, date_to: datetime) -> Any:
        # Transport bounds only; source collection/record validation stays in
        # MetaTraderDealHistorySource / DealIngestor, without duplicating evidence.
        for value in (date_from, date_to):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('history boundaries must be timezone-aware')
        start, end = date_from.astimezone(timezone.utc), date_to.astimezone(timezone.utc)
        if start > end or end - start > timedelta(days=self.__config.mt5_max_history_days):
            raise ValueError('history range is reversed or exceeds the configured limit')

        def fetch() -> Any:
            self._account()
            self._terminal()
            result = self._invoke('history read', self.__calls.history_deals_get, start, end)
            self._account()
            if result is None:
                raise ReadOnlySessionError('MT5 history is unavailable')
            return result

        return await self._read(fetch)

    def shutdown(self) -> None:
        with self.__lock:
            if self.__closed:
                return
            self.__closed = True
            self.__opened = False
            calls, self.__calls = self.__calls, None
            self.__loader = None
            if self.__attempted and calls is not None:
                self._invoke('shutdown', calls.shutdown)
