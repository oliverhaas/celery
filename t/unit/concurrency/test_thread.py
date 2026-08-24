import operator
import time

import pytest

import t.skip
from celery.concurrency import thread
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.functional import noop


def spin(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


class test_thread_TaskPool:

    def test_on_apply(self):
        from celery.concurrency import thread
        x = thread.TaskPool()
        try:
            x.on_apply(operator.add, (2, 2), {}, noop, noop)
        finally:
            x.stop()

    def test_info(self):
        from celery.concurrency import thread
        x = thread.TaskPool()
        try:
            assert x.info
        finally:
            x.stop()

    def test_on_stop(self):
        from celery.concurrency import thread
        x = thread.TaskPool()
        x.on_stop()
        with pytest.raises(RuntimeError):
            x.on_apply(operator.add, (2, 2), {}, noop, noop)

    def test_on_stop_cancels_pending_futures(self):
        import threading

        from celery.concurrency import thread

        x = thread.TaskPool(limit=1)

        started = threading.Event()
        shutdown = threading.Event()

        def blocking_task():
            started.set()
            shutdown.wait(timeout=5)

        stop_thread = None
        try:
            # Submit a long-running task to occupy the single thread
            x.on_apply(blocking_task, (), {}, noop, noop)

            # Wait until the first task is actually running
            assert started.wait(timeout=5), "Timed out waiting for blocking_task to start"

            # Submit another task — guaranteed to be pending
            result = x.on_apply(noop, (), {}, noop, noop)

            # Stop the pool in a background thread — should cancel the pending future
            def _run_on_stop():
                x.on_stop()

            stop_thread = threading.Thread(target=_run_on_stop)
            stop_thread.start()

            # Wait (bounded) until the pending future has been cancelled
            deadline = time.time() + 5.0
            while not result.f.cancelled() and time.time() < deadline:
                time.sleep(0.01)

            if not result.f.cancelled():
                pytest.fail("Pending futures should be cancelled on stop")

            # Once cancellation is observed, release the blocking thread so on_stop can finish
            shutdown.set()
            if stop_thread is not None:
                stop_thread.join(timeout=5.0)
        finally:
            # Release the blocking thread and ensure pool is stopped
            # even if the test fails, preventing thread leaks.
            # on_stop() is idempotent — safe to call twice.
            shutdown.set()
            if stop_thread is not None and stop_thread.is_alive():
                stop_thread.join(timeout=5.0)
            x.on_stop()

    @t.skip.if_pypy
    def test_hard_time_limit(self):
        x = thread.TaskPool(limit=1, timeout=0.1)
        exceeded = []
        try:
            x.on_apply(spin, (10.0,), {}, noop, noop,
                       timeout_callback=lambda *a: exceeded.append(a)).wait(timeout=5)
            assert exceeded == [(False, 0.1)]
        finally:
            x.stop()

    @t.skip.if_pypy
    def test_soft_time_limit(self):
        x = thread.TaskPool(limit=1)
        exceeded, caught = [], []

        def catching_task():
            try:
                spin(10.0)
            except SoftTimeLimitExceeded:
                caught.append(True)

        try:
            x.on_apply(catching_task, (), {}, noop, noop, soft_timeout=0.1,
                       timeout_callback=lambda *a: exceeded.append(a)).wait(timeout=5)
            assert exceeded == [(True, 0.1)]
            assert caught == [True]
        finally:
            x.stop()

    @t.skip.if_pypy
    def test_soft_time_limit_escalates_to_hard(self):
        x = thread.TaskPool(limit=1)
        exceeded = []

        def ignoring_task():
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    spin(10.0)
                except SoftTimeLimitExceeded:
                    pass

        try:
            x.on_apply(ignoring_task, (), {}, noop, noop, timeout=0.4,
                       soft_timeout=0.1,
                       timeout_callback=lambda *a: exceeded.append(a)).wait(timeout=5)
            assert exceeded == [(True, 0.1), (False, 0.4)]
        finally:
            x.stop()
