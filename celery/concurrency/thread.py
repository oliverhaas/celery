"""Thread execution pool."""
from __future__ import annotations

import ctypes
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any, Callable

from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import get_logger

from .base import BasePool, apply_target

__all__ = ('TaskPool',)

logger = get_logger(__name__)

IS_PYPY = hasattr(sys, 'pypy_version_info')

if TYPE_CHECKING:
    from typing import TypedDict

    PoolInfo = TypedDict('PoolInfo', {'max-concurrency': int, 'threads': int})

    # `TargetFunction` should be a Protocol that represents fast_trace_task and
    # trace_task_ret.
    TargetFunction = Callable[..., Any]


class _HardTimeLimit(BaseException):
    """Raised in the task's thread when the hard time limit is exceeded.

    Not an :exc:`Exception`, so that it passes through the task and
    :func:`~celery.app.trace.trace_task` uncaught, the same way
    :class:`gevent.Timeout` does for the gevent pool.
    """


class ApplyResult:
    def __init__(self, future: Future) -> None:
        self.f = future
        self.get = self.f.result

    def wait(self, timeout: float | None = None) -> None:
        wait([self.f], timeout)


class TaskPool(BasePool):
    """Thread Task Pool."""
    limit: int

    body_can_be_buffer = True
    signal_safe = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.timeout = kwargs.get('timeout')
        self.soft_timeout = kwargs.get('soft_timeout')
        super().__init__(*args, **kwargs)
        self.executor = ThreadPoolExecutor(max_workers=self.limit)
        self._timer = self.Timer()
        self._running: set[int] = set()
        self._mutex = threading.Lock()

    def _raise_in_thread(self, tid: int, exc: type[BaseException]) -> None:
        """Raise `exc` in the thread running a task.

        CPython delivers it once that thread next runs Python bytecode,
        so a task blocked in a system call keeps running until the call
        returns.
        """
        # Locked so the thread can't take the next task mid-injection.
        with self._mutex:
            if tid not in self._running:
                return
            if IS_PYPY:  # pragma: no cover
                logger.warning('cannot raise %s in task thread %s on PyPy',
                               exc.__name__, tid)
                return

            affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid), ctypes.py_object(exc))
            if affected == 0:
                logger.warning('failed to raise %s in task thread %s (not found)',
                               exc.__name__, tid)
            elif affected > 1:  # pragma: no cover
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(tid), None)
                logger.warning('failed to raise %s in task thread %s (affected=%s)',
                               exc.__name__, tid, affected)

    def _apply_timeout(self, target: TargetFunction,
                       args: tuple[Any, ...] | None,
                       kwargs: dict[str, Any] | None,
                       callback: Callable[..., Any] | None,
                       accept_callback: Callable[..., Any] | None,
                       tid: int, timeout: float | None,
                       soft_timeout: float | None,
                       timeout_callback: Callable[..., Any]) -> None:
        trefs = []
        if soft_timeout:
            trefs.append(self._timer.call_after(
                soft_timeout, self._on_soft_timeout,
                (tid, soft_timeout, timeout_callback)))
        if timeout:
            trefs.append(self._timer.call_after(
                timeout, self._raise_in_thread, (tid, _HardTimeLimit)))
        try:
            apply_target(target, args, kwargs, callback, accept_callback,
                         pid=tid, propagate=(_HardTimeLimit,))
        except _HardTimeLimit:
            timeout_callback(False, timeout)
        finally:
            for tref in trefs:
                tref.cancel()

    def _on_soft_timeout(self, tid: int, soft_timeout: float,
                         timeout_callback: Callable[..., Any]) -> None:
        timeout_callback(True, soft_timeout)
        self._raise_in_thread(tid, SoftTimeLimitExceeded)

    def on_stop(self) -> None:
        # Stopped after the tasks, so limits still apply while shutdown waits.
        self.executor.shutdown(cancel_futures=True)
        self._timer.stop()
        super().on_stop()

    def on_apply(
        self,
        target: TargetFunction,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        callback: Callable[..., Any] | None = None,
        accept_callback: Callable[..., Any] | None = None,
        timeout: float | None = None,
        soft_timeout: float | None = None,
        timeout_callback: Callable[..., Any] | None = None,
        **_: Any
    ) -> ApplyResult:
        timeout = self.timeout if timeout is None else timeout
        soft_timeout = (self.soft_timeout if soft_timeout is None
                        else soft_timeout)

        def run() -> None:
            tid = threading.get_ident()
            with self._mutex:
                self._running.add(tid)
            try:
                if (timeout or soft_timeout) and timeout_callback is not None:
                    self._apply_timeout(target, args, kwargs, callback,
                                        accept_callback, tid, timeout,
                                        soft_timeout, timeout_callback)
                else:
                    apply_target(target, args, kwargs, callback,
                                 accept_callback, pid=tid)
            finally:
                with self._mutex:
                    self._running.discard(tid)

        return ApplyResult(self.executor.submit(run))

    def _get_info(self) -> PoolInfo:
        info = super()._get_info()
        info.update({
            'max-concurrency': self.limit,
            'threads': len(self.executor._threads)
        })
        return info
