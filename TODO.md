# celery-asyncio TODOs

## Time Limits / Task Cancellation

The old billiard-based Celery used signals (SIGUSR1) to raise `SoftTimeLimitExceeded` inside running tasks. With asyncio, we need a different approach since we can't inject exceptions into arbitrary code points.

### Options to explore:

1. **asyncio.timeout() context manager** (Python 3.11+)
   - Wrap task execution in `async with asyncio.timeout(soft_limit):`
   - Raises `asyncio.TimeoutError` which can be caught and converted to `SoftTimeLimitExceeded`
   - Only works for async tasks, not sync tasks running in thread pool

2. **Callback-based approach**
   - Add `on_soft_timeout()` and `on_hard_timeout()` handlers to tasks
   - Less flexible than exceptions but predictable
   - Would require tasks to be designed with this in mind

3. **Task cancellation with custom exception**
   - Use `task.cancel(msg="soft_timeout")`
   - Raises `asyncio.CancelledError` which can be inspected
   - Task can catch and handle gracefully

4. **Cooperative timeout checking**
   - Provide `task.check_timeout()` method that tasks can call periodically
   - Raises `SoftTimeLimitExceeded` if timeout exceeded
   - Requires task cooperation

### Decision needed:
- For **async tasks**: `asyncio.timeout()` seems most natural
- For **sync tasks in thread pool**: May need threading events or cooperative checking
- Consider if we want to support raising exceptions mid-execution at all, or move to callback model

## Other TODOs

- [ ] Implement time limit mechanism for asyncio tasks
- [ ] Test worker with actual Redis transport (kombu-asyncio)
- [ ] Review and update test suite for asyncio compatibility
- [ ] Document migration path from celery to celery-asyncio
- [ ] Performance benchmarks vs traditional celery
