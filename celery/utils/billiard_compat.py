"""Compatibility module providing billiard-like functionality without billiard.

celery-asyncio uses asyncio with threads instead of multiprocessing,
so we provide minimal stubs for the billiard interfaces that are still
referenced in the codebase.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import traceback
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = (
    # Exceptions
    "SoftTimeLimitExceeded",
    "TimeLimitExceeded",
    "WorkerLostError",
    "Terminated",
    "RestartFreqExceeded",
    # Exception info
    "ExceptionInfo",
    "ExceptionWithTraceback",
    # Process utilities
    "current_process",
    "cpu_count",
    # Signal constants
    "TERM_SIGNAME",
    "REMAP_SIGTERM",
    # Functions
    "reset_signals",
    "restart_state",
    "close_open_fds",
    "get_fdmax",
    "set_pdeathsig",
    "ensure_multiprocessing",
)


# =============================================================================
# Exceptions
# =============================================================================


class SoftTimeLimitExceeded(Exception):
    """The soft time limit has been exceeded."""


class TimeLimitExceeded(Exception):
    """The time limit has been exceeded."""


class WorkerLostError(Exception):
    """The worker processing the task has been lost."""


class Terminated(Exception):
    """The process was terminated."""


class RestartFreqExceeded(Exception):
    """Too many restarts within a time window."""


# =============================================================================
# Exception Info - for storing traceback information
# =============================================================================


class ExceptionInfo:
    """Exception wrapping an exception and its traceback.

    This is a simplified version of billiard.einfo.ExceptionInfo.
    """

    exception: BaseException | None
    type: type[BaseException] | None
    tb: TracebackType | None
    internal: bool

    def __init__(
        self,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None] | None = None,
        internal: bool = False,
    ):
        self.internal = internal
        if exc_info is None:
            exc_info = sys.exc_info()
        self.type, self.exception, self.tb = exc_info
        self.traceback = "".join(traceback.format_exception(*exc_info)) if exc_info[0] else ""

    @property
    def exc_info(self) -> tuple[type[BaseException] | None, BaseException | None, TracebackType | None]:
        return self.type, self.exception, self.tb

    def __str__(self) -> str:
        return self.traceback

    def __repr__(self) -> str:
        return f"<{type(self).__name__}: {self.exception!r}>"


class ExceptionWithTraceback:
    """Exception wrapper that includes stringified traceback.

    Used for pickling exceptions across process boundaries.
    In asyncio mode, we don't cross process boundaries, but
    this is still used for serialization to result backends.
    """

    exc: BaseException
    tb: str

    def __init__(self, exc: BaseException, tb: str | None = None):
        self.exc = exc
        self.tb = tb or ""

    def __reduce__(self) -> tuple[Callable, tuple[BaseException, str]]:
        return self.__class__, (self.exc, self.tb)


# =============================================================================
# Process utilities
# =============================================================================


class _FakeProcess:
    """Fake process object for threading-based execution."""

    _name: str | None = None
    _ident: int | None = None

    @property
    def name(self) -> str:
        return self._name or f"Thread-{threading.current_thread().ident}"

    @name.setter
    def name(self, value: str) -> None:
        self._name = value

    @property
    def ident(self) -> int | None:
        return self._ident or os.getpid()

    @property
    def pid(self) -> int | None:
        return self.ident

    def __reduce__(self) -> tuple[Callable[[], "_FakeProcess"], tuple[()]]:
        return current_process, ()


_current_process: _FakeProcess | None = None


def current_process() -> _FakeProcess:
    """Return the current process object."""
    global _current_process
    if _current_process is None:
        _current_process = _FakeProcess()
    return _current_process


def cpu_count() -> int:
    """Return the number of CPUs."""
    return os.cpu_count() or 1


# =============================================================================
# Signal handling
# =============================================================================

# Signal name for TERM signal
TERM_SIGNAME = "SIGTERM"

# Whether to remap SIGTERM to another signal
REMAP_SIGTERM = os.environ.get("REMAP_SIGTERM")


def reset_signals() -> None:
    """Reset signal handlers to defaults.

    In asyncio mode, we don't need to do much here since
    we're not forking processes.
    """
    # Only reset signals that make sense in the main process
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):
            # Signal can't be caught/set in this context
            pass


class _RestartState:
    """Track restart frequency."""

    def __init__(self, maxR: int = 100, maxT: int = 100) -> None:
        self.maxR = maxR
        self.maxT = maxT
        self.R = 0
        self.T: float | None = None

    def step(self, now: float | None = None) -> None:
        """Record a restart."""
        import time

        now = now or time.time()
        if self.T is None:
            self.T = now
        self.R += 1
        if self.R >= self.maxR:
            if now - self.T < self.maxT:
                raise RestartFreqExceeded(f"Max restarts ({self.maxR}) exceeded in {self.maxT}s")
            self.R = 0
            self.T = now


def restart_state(maxR: int = 100, maxT: int = 100) -> _RestartState:
    """Create a restart state tracker."""
    return _RestartState(maxR, maxT)


# =============================================================================
# File descriptor utilities
# =============================================================================


def get_fdmax(default: int = 1024) -> int:
    """Return the maximum file descriptor number."""
    try:
        import resource

        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, ValueError):
        return default


def close_open_fds(keep: set[int] | None = None) -> None:
    """Close all open file descriptors except those in keep.

    In asyncio mode, this is generally not needed since we
    don't fork processes, but we keep it for compatibility.
    """
    keep = keep or set()
    keep.add(0)  # stdin
    keep.add(1)  # stdout
    keep.add(2)  # stderr

    fdmax = get_fdmax()
    for fd in range(3, fdmax):
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def set_pdeathsig(sig: int = signal.SIGTERM) -> bool:
    """Set parent death signal.

    This is a Linux-specific feature that signals the child
    when the parent process dies. In asyncio mode with threads,
    this isn't needed.
    """
    # In asyncio mode, we don't need this
    return False


def ensure_multiprocessing() -> None:
    """Ensure multiprocessing is available.

    In asyncio mode, we don't use multiprocessing, so this is a no-op.
    """
    pass


# =============================================================================
# Queue for testing compatibility
# =============================================================================


class Queue:
    """Simple queue for testing compatibility.

    Wraps asyncio.Queue for sync usage.
    """

    def __init__(self, maxsize: int = 0) -> None:
        import queue

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        self._queue.put(item, block=block, timeout=timeout)

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        return self._queue.get(block=block, timeout=timeout)

    def put_nowait(self, item: Any) -> None:
        self._queue.put_nowait(item)

    def get_nowait(self) -> Any:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()
