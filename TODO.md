# celery-asyncio TODOs

## Time Limits / Task Cancellation

### Decision: Drop soft time limit, use task.cancel()

The old billiard-based Celery used signals (SIGUSR1) to raise `SoftTimeLimitExceeded` inside running tasks. With asyncio, we cannot inject exceptions into arbitrary code points.

**Research findings:**
- `task.cancel(msg)` is the only way to inject an exception into a running asyncio task
- It raises `CancelledError` at the next `await` point
- For sync tasks in ThreadPoolExecutor, there's no reliable way to interrupt them
- `PyThreadState_SetAsyncExc` exists but only works for pure Python code, not blocking I/O

**Decision:** Drop the soft time limit feature. Use `task.cancel()` for async tasks.

### How time limits work in celery-asyncio:

1. **Async tasks**: Use `asyncio.timeout()` or `asyncio.wait_for()` with timeout
   - Raises `asyncio.TimeoutError` / `CancelledError` at next `await`
   - Task can catch and handle gracefully if needed

2. **Sync tasks**: Hard timeout only
   - Worker stops waiting for result after timeout
   - Task may continue running in background (fire-and-forget)
   - No way to cleanly interrupt blocking sync code

3. **Migration note**: Tasks relying on `SoftTimeLimitExceeded` need refactoring
   - Use async tasks with try/except for `CancelledError`
   - Or redesign to use cooperative checking

## Other TODOs

- [ ] Implement hard time limit using `asyncio.wait_for()` / `asyncio.timeout()`
- [ ] Test worker with actual Redis transport (kombu-asyncio)
- [ ] Review and update test suite for asyncio compatibility
- [ ] Document migration path from celery to celery-asyncio
- [ ] Performance benchmarks vs traditional celery
- [ ] Consider removing `SoftTimeLimitExceeded` exception or repurposing it
