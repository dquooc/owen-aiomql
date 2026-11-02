"""Process-local account ownership across tasks, threads and event loops."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import AsyncIterator
from uuid import UUID


@dataclass(eq=False, slots=True)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    ready: asyncio.Future[None]
    task: asyncio.Task | None


@dataclass(slots=True)
class _Entry:
    owner: _Waiter
    waiting: deque[_Waiter] = field(default_factory=deque)


class AccountRecoveryCoordinator:
    """FIFO ownership per supplied durable UUID; no distributed lease.

    The manager verifies registration as the first operation under ownership,
    before checkpoint/clock/source work. A short threading mutex protects only
    registry changes; it is NEVER held over awaits or application I/O. Each waiter
    owns a future on its own loop; handoff uses call_soon_threadsafe, without
    worker tasks/polling. Entries disappear when no owner/waiter remains.

    Managers share the module default unless a different coordination domain is
    explicitly injected. Reentrant same-task acquisition is rejected. Abruptly
    abandoning a live owner/event loop is not a recoverable lease protocol.
    """

    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[UUID, _Entry] = {}

    @asynccontextmanager
    async def hold(self, account_id: UUID) -> AsyncIterator[None]:
        if not isinstance(account_id, UUID) or account_id.int == 0:
            raise ValueError("coordination requires a non-nil account UUID")
        loop = asyncio.get_running_loop()
        waiter = _Waiter(loop, loop.create_future(), asyncio.current_task())
        with self._guard:
            entry = self._entries.get(account_id)
            if entry is None:
                self._entries[account_id] = _Entry(waiter)
                immediate = True
            else:
                if entry.owner.task is waiter.task:
                    raise RuntimeError("account recovery ownership is not reentrant")
                entry.waiting.append(waiter)
                immediate = False
        if not immediate:
            try:
                await waiter.ready
            except BaseException:
                # Cancellation can race with grant delivery. Release if already
                # owner, otherwise remove ONLY this queued waiter.
                self._release(account_id, waiter)
                raise
        try:
            yield
        finally:
            self._release(account_id, waiter)

    @staticmethod
    def _wake(waiter: _Waiter) -> None:
        if not waiter.ready.done():
            waiter.ready.set_result(None)

    def _release(self, account_id: UUID, waiter: _Waiter) -> None:
        with self._guard:
            entry = self._entries.get(account_id)
            if entry is None:
                return
            if entry.owner is not waiter:
                entry.waiting.remove(waiter)
                return
            while entry.waiting:
                following = entry.waiting.popleft()
                if following.loop.is_closed():
                    continue
                entry.owner = following
                try:
                    following.loop.call_soon_threadsafe(self._wake, following)
                except RuntimeError:
                    if not following.loop.is_closed():
                        raise
                    continue
                return
            del self._entries[account_id]


_DEFAULT_COORDINATOR = AccountRecoveryCoordinator()
