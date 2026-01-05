# Celery Worker Asyncio Rewrite Plan

This document outlines a plan to rewrite the Celery worker's core event loop to use Python's native `asyncio` instead of Kombu's custom Hub/poller implementation.

## Current Architecture

### Overview

The Celery worker currently uses **Kombu's Hub** - a custom event loop implementation based on `select`/`poll`/`epoll` that predates Python's asyncio standardization.

```
┌─────────────────────────────────────────────────────────────────┐
│                        WorkController                            │
│  celery/worker/worker.py                                        │
├─────────────────────────────────────────────────────────────────┤
│  Blueprint (bootsteps):                                         │
│  ├── Hub         → Kombu's custom event loop                    │
│  ├── Pool        → Worker process pool (prefork/eventlet/gevent)│
│  ├── Timer       → Scheduled callbacks                          │
│  └── Consumer    → Message consumption                          │
│      └── Consumer Blueprint:                                    │
│          ├── Connection  → Broker connection                    │
│          ├── Tasks       → Task consumer setup                  │
│          ├── Control     → Remote control commands              │
│          └── Evloop      → Starts main loop                     │
└─────────────────────────────────────────────────────────────────┘
```

### Kombu Hub (Current Event Loop)

**Location:** `kombu/asynchronous/hub.py`

The Hub is a generator-based event loop that:
1. Uses `select`/`poll`/`epoll` via `kombu.utils.eventio.poll()`
2. Manages file descriptor callbacks (`readers`, `writers` dicts)
3. Fires timer callbacks via a priority queue
4. Yields after each poll iteration (generator pattern)

```python
# Simplified Hub.create_loop() - the core loop
while 1:
    # 1. Run ready callbacks (call_soon)
    for item in self._pop_ready():
        item()

    # 2. Fire timer callbacks
    poll_timeout = fire_timers()

    # 3. Run on_tick callbacks
    for tick_callback in self.on_tick:
        tick_callback()

    # 4. Poll file descriptors
    events = poll(poll_timeout)

    # 5. Dispatch fd callbacks
    for fd, event in events:
        cb, args = readers[fd] or writers[fd]
        cb(*args)

    yield  # Generator-based cooperative multitasking
```

### Worker Loops

**Location:** `celery/worker/loops.py`

Two loop implementations exist:

1. **`asynloop`** (async, non-blocking) - Uses Hub's generator loop
2. **`synloop`** (sync, blocking) - Falls back to `connection.drain_events()`

```python
# asynloop - Current non-blocking loop
def asynloop(obj, connection, consumer, blueprint, hub, qos, ...):
    loop = hub.create_loop()
    while blueprint.state == RUN:
        next(loop)  # Pull one event iteration
```

### What Gets Registered with Hub

1. **Broker connection FDs** - For incoming messages
2. **Result queue FDs** - For worker process results (AsynPool)
3. **Timers** - Heartbeats, ETA tasks, rate limiting
4. **on_tick callbacks** - Per-iteration hooks

---

## Why Rewrite?

### Problems with Current Approach

1. **Custom event loop** - Kombu's Hub duplicates asyncio functionality
2. **No async/await** - Cannot use modern Python async patterns
3. **Generator-based** - Less intuitive than async/await
4. **Limited ecosystem** - Cannot use asyncio-native libraries directly
5. **Maintenance burden** - Two event loop implementations to maintain

### Benefits of Asyncio

1. **Standard library** - Well-maintained, widely understood
2. **async/await syntax** - Cleaner, more readable code
3. **Ecosystem compatibility** - Works with aiohttp, asyncpg, aioredis, etc.
4. **Better debugging** - asyncio has mature debugging tools
5. **Performance** - Potential for better performance with native async I/O

---

## Proposed Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────┐
│                     AsyncWorkerController                        │
│  celery/worker/async_worker.py                                  │
├─────────────────────────────────────────────────────────────────┤
│  Uses asyncio.run() or asyncio.get_event_loop()                 │
│                                                                 │
│  Components (as async classes):                                 │
│  ├── AsyncConsumer      → Async message consumption             │
│  ├── AsyncPool          → Async worker pool interface           │
│  ├── AsyncTimer         → asyncio-based scheduling              │
│  └── AsyncControl       → Async remote control                  │
└─────────────────────────────────────────────────────────────────┘
```

### Core Components to Rewrite

#### 1. Main Event Loop

**Current:** `celery/worker/loops.py` → `asynloop()` using Hub generator

**New:** `celery/worker/async_loops.py` → `async_main_loop()` using asyncio

```python
# New asyncio-based main loop
async def async_main_loop(controller: AsyncWorkerController):
    """Main worker event loop using asyncio."""
    consumer = controller.consumer
    pool = controller.pool

    # Start background tasks
    async with asyncio.TaskGroup() as tg:
        # Message consumption task
        tg.create_task(consumer.consume_messages())

        # Result handling task (from worker processes)
        tg.create_task(pool.handle_results())

        # Heartbeat task
        tg.create_task(controller.heartbeat_loop())

        # Control command task
        tg.create_task(controller.control_loop())

        # Shutdown monitor
        tg.create_task(controller.shutdown_monitor())
```

#### 2. Message Consumer

**Current:** `celery/worker/consumer/consumer.py` - Callback-based

**New:** Async iteration over messages

```python
class AsyncConsumer:
    """Async message consumer."""

    async def consume_messages(self):
        """Consume messages from broker asynchronously."""
        async for message in self.transport.aiter_messages():
            await self.on_message(message)

    async def on_message(self, message):
        """Handle incoming task message."""
        try:
            task_handler = self.get_task_handler(message)
            request = await self.create_request(message)

            # Check for revocation, ETA, rate limits
            if await self.should_execute(request):
                await self.dispatch_to_pool(request)

            await message.aack()
        except Exception as exc:
            await self.on_message_error(message, exc)
```

#### 3. Timer/Scheduling

**Current:** `kombu.asynchronous.timer.Timer` - Heap-based timer

**New:** Use `asyncio.call_later()` and `asyncio.call_at()`

```python
class AsyncTimer:
    """Asyncio-based timer for scheduled callbacks."""

    def __init__(self, loop: asyncio.AbstractEventLoop = None):
        self.loop = loop or asyncio.get_event_loop()
        self._handles: dict[str, asyncio.TimerHandle] = {}

    def call_later(self, delay: float, callback, *args) -> str:
        """Schedule callback after delay seconds."""
        handle_id = uuid()
        handle = self.loop.call_later(delay, callback, *args)
        self._handles[handle_id] = handle
        return handle_id

    def call_at(self, when: float, callback, *args) -> str:
        """Schedule callback at absolute time."""
        handle_id = uuid()
        handle = self.loop.call_at(when, callback, *args)
        self._handles[handle_id] = handle
        return handle_id

    def call_repeatedly(self, interval: float, callback, *args) -> str:
        """Schedule recurring callback."""
        handle_id = uuid()

        async def repeat():
            while handle_id in self._handles:
                callback(*args)
                await asyncio.sleep(interval)

        task = asyncio.create_task(repeat())
        self._handles[handle_id] = task
        return handle_id

    def cancel(self, handle_id: str):
        """Cancel scheduled callback."""
        handle = self._handles.pop(handle_id, None)
        if handle:
            handle.cancel()
```

#### 4. Pool Integration

**Current:** `celery/concurrency/asynpool.py` - FD-based result handling

**New:** Async interface to worker processes

```python
class AsyncPoolInterface:
    """Async interface to worker process pool."""

    def __init__(self, pool: BasePool):
        self.pool = pool
        self._result_queue: asyncio.Queue = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}

    async def apply_async(self, task_request: Request) -> asyncio.Future:
        """Submit task to pool and return future for result."""
        future = asyncio.get_event_loop().create_future()
        self._pending[task_request.id] = future

        # Submit to underlying pool (still uses multiprocessing)
        self.pool.apply_async(
            trace_task,
            args=task_request.trace_args,
            callback=lambda r: self._on_result(task_request.id, r),
            error_callback=lambda e: self._on_error(task_request.id, e),
        )

        return future

    async def handle_results(self):
        """Background task to process results from worker processes."""
        # Use asyncio's add_reader for result queue FD
        loop = asyncio.get_event_loop()
        result_fd = self.pool._outqueue._reader.fileno()

        while True:
            # Wait for result to be available
            await self._wait_readable(result_fd)

            # Read and process result
            result = self.pool._result_handler.get()
            task_id = result['task_id']

            if task_id in self._pending:
                future = self._pending.pop(task_id)
                if result.get('error'):
                    future.set_exception(result['error'])
                else:
                    future.set_result(result['value'])

    async def _wait_readable(self, fd: int):
        """Wait until fd is readable."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        loop.add_reader(fd, future.set_result, None)
        try:
            await future
        finally:
            loop.remove_reader(fd)
```

#### 5. Connection Management

**Current:** Sync kombu connection with FD registration

**New:** Async connection wrapper

```python
class AsyncConnection:
    """Async wrapper for kombu connection."""

    def __init__(self, connection: kombu.Connection):
        self._conn = connection
        self._transport = connection.transport

    async def aconnect(self):
        """Connect asynchronously."""
        # Until kombu has native async, use run_in_executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._conn.connect)

    async def aclose(self):
        """Close connection asynchronously."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._conn.close)

    async def adrain_events(self, timeout: float = None):
        """Drain events asynchronously."""
        # For transports with native async (future):
        # await self._transport.adrain_events(timeout)

        # Current: use FD-based async waiting
        fd = self._conn.connection.fileno()
        await self._wait_readable(fd, timeout)
        self._conn.drain_events(timeout=0)  # Non-blocking drain

    async def aiter_messages(self):
        """Async iterator over incoming messages."""
        while True:
            await self.adrain_events()
            while self._has_pending_messages():
                yield self._get_message()
```

---

## Implementation Phases

### Phase 1: Core Asyncio Loop

**Goal:** Replace Hub with asyncio event loop, keeping existing components.

1. Create `celery/worker/async_loops.py` with `async_main_loop()`
2. Create `AsyncTimer` wrapping asyncio scheduling
3. Add `--async-worker` flag to opt into new implementation
4. Maintain backward compatibility with Hub-based loop

**Files to create/modify:**
- `celery/worker/async_loops.py` (new)
- `celery/worker/async_timer.py` (new)
- `celery/worker/worker.py` (add async worker option)
- `celery/bin/worker.py` (add CLI flag)

### Phase 2: Async Consumer

**Goal:** Rewrite message consumption to use async/await.

1. Create `AsyncConsumer` class
2. Implement async message iteration
3. Async task handler dispatch
4. Async ack/reject handling

**Files to create/modify:**
- `celery/worker/consumer/async_consumer.py` (new)
- `celery/worker/consumer/async_tasks.py` (new)
- `celery/worker/async_strategy.py` (new)

### Phase 3: Async Pool Interface

**Goal:** Async interface to worker process pool.

1. Create `AsyncPoolInterface` wrapper
2. Async result handling using `add_reader()`
3. Async task submission with futures
4. Integrate with asyncio task group

**Files to create/modify:**
- `celery/concurrency/async_pool.py` (new)
- `celery/worker/async_request.py` (new)

### Phase 4: Full Integration

**Goal:** Complete async worker implementation.

1. Async control commands (pidbox)
2. Async heartbeat handling
3. Async graceful shutdown
4. Performance optimization

**Files to create/modify:**
- `celery/worker/consumer/async_control.py` (new)
- `celery/worker/async_heartbeat.py` (new)

### Phase 5: Native Transport Support

**Goal:** Leverage native async transports when available.

1. Integrate with Kombu's async transport interface (once available)
2. Native async for Redis transport
3. Native async for AMQP transport (via aio-pika or async py-amqp)

---

## Compatibility Considerations

### Backward Compatibility

1. **Opt-in initially** - New async worker via `--async-worker` flag
2. **Existing code works** - Hub-based worker remains default
3. **Gradual migration** - Can switch back if issues arise

### Transport Compatibility

| Transport | Native Async | Fallback Strategy |
|-----------|-------------|-------------------|
| Redis | Yes (`redis.asyncio`) | `run_in_executor` |
| AMQP | Future (`aio-pika`) | `run_in_executor` |
| SQS | Yes (`aiobotocore`) | `run_in_executor` |
| Others | No | `run_in_executor` |

### Pool Compatibility

| Pool Type | Async Compatibility | Notes |
|-----------|---------------------|-------|
| prefork | Via FD monitoring | Result queue FDs work with asyncio |
| eventlet | Native green | Already async-compatible |
| gevent | Native green | Already async-compatible |
| solo | Direct | No pool, runs in main process |
| threads | Via executor | ThreadPoolExecutor integration |

---

## Code Examples

### New Main Worker Entry Point

```python
# celery/worker/async_worker.py

class AsyncWorkerController:
    """Asyncio-based worker controller."""

    def __init__(self, app, **kwargs):
        self.app = app
        self.consumer = None
        self.pool = None
        self.timer = AsyncTimer()
        self._shutdown = asyncio.Event()

    async def start(self):
        """Start the async worker."""
        # Initialize components
        self.pool = await self._create_pool()
        self.consumer = await self._create_consumer()

        # Run main loop
        try:
            await async_main_loop(self)
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Graceful shutdown."""
        self._shutdown.set()
        await self.consumer.close()
        await self.pool.close()

    def run(self):
        """Entry point - runs the asyncio event loop."""
        asyncio.run(self.start())
```

### New Async Consumer

```python
# celery/worker/consumer/async_consumer.py

class AsyncConsumer:
    """Asyncio-based message consumer."""

    def __init__(self, controller: AsyncWorkerController):
        self.controller = controller
        self.app = controller.app
        self.connection = None
        self.task_consumer = None
        self._strategies = {}

    async def start(self):
        """Start consuming messages."""
        self.connection = await self._connect()
        self.task_consumer = self._create_task_consumer()
        await self._start_consuming()

    async def consume_messages(self):
        """Main consumption loop."""
        async for message in self._iter_messages():
            asyncio.create_task(self._handle_message(message))

    async def _handle_message(self, message):
        """Handle a single message."""
        try:
            request = self._create_request(message)

            # Check revocation
            if request.is_revoked():
                await message.areject(requeue=False)
                return

            # Handle ETA
            if request.eta:
                self.controller.timer.call_at(
                    request.eta.timestamp(),
                    self._execute_request,
                    request,
                )
                await message.aack()
                return

            # Execute immediately
            await self._execute_request(request)
            await message.aack()

        except Exception as exc:
            await self._handle_error(message, exc)

    async def _execute_request(self, request):
        """Execute task request."""
        await self.controller.pool.apply_async(request)
```

### New Async Loop

```python
# celery/worker/async_loops.py

async def async_main_loop(controller: AsyncWorkerController):
    """Main asyncio event loop for the worker."""

    async def consume_task():
        """Task for message consumption."""
        await controller.consumer.consume_messages()

    async def results_task():
        """Task for handling worker process results."""
        await controller.pool.handle_results()

    async def heartbeat_task():
        """Task for broker heartbeats."""
        while not controller._shutdown.is_set():
            await controller.send_heartbeat()
            await asyncio.sleep(controller.heartbeat_interval)

    async def control_task():
        """Task for control commands."""
        await controller.control.listen()

    async def shutdown_monitor():
        """Monitor for shutdown signal."""
        await controller._shutdown.wait()
        raise asyncio.CancelledError("Shutdown requested")

    # Run all tasks concurrently
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(consume_task())
            tg.create_task(results_task())
            tg.create_task(heartbeat_task())
            tg.create_task(control_task())
            tg.create_task(shutdown_monitor())
    except* asyncio.CancelledError:
        logger.info("Worker shutting down...")
```

---

## Migration Path

### For Users

1. **Phase 1:** Test with `celery worker --async-worker` (opt-in)
2. **Phase 2:** Report issues, provide feedback
3. **Phase 3:** Async worker becomes default (major version)
4. **Phase 4:** Hub-based worker deprecated
5. **Phase 5:** Hub-based worker removed

### For Plugin Authors

1. **Bootsteps:** Will need async versions (`async def start()`)
2. **Signals:** May need async signal handlers
3. **Custom pools:** Need to implement async interface

---

## Open Questions

1. **Bootsteps compatibility:** How to handle existing sync bootsteps?
   - Option A: Wrap with `run_in_executor()`
   - Option B: Require async bootsteps for async worker

2. **Signal handlers:** Should signals become async?
   - Some signals may need to await I/O
   - Could break existing signal handlers

3. **Green pools (eventlet/gevent):** How to integrate?
   - They have their own event loops
   - May need special handling or separate implementation

4. **Debugging:** How to handle asyncio debugging?
   - Enable asyncio debug mode in development
   - Proper exception handling in task groups

5. **Performance:** Expected performance impact?
   - Benchmark async vs Hub-based worker
   - Profile context switching overhead

---

## Dependencies

### Required Changes in Kombu

For optimal performance, Kombu should provide:

1. `Transport.aiter_messages()` - Async message iterator
2. `Channel.abasic_publish()` - Async message publishing
3. `Connection.aconnect()`, `Connection.aclose()` - Async connection

See `docs/kombu-asyncio-implementation-plan.md` for details.

### Fallback Without Kombu Changes

Without native Kombu async support:

1. Use `loop.add_reader(fd, callback)` for connection FD
2. Use `loop.run_in_executor()` for blocking operations
3. Wrap `drain_events()` with executor calls

This works but is suboptimal - effectively the same as `sync_to_async`.

---

## Timeline Considerations

| Phase | Description | Dependency |
|-------|-------------|------------|
| Phase 1 | Core asyncio loop | None |
| Phase 2 | Async consumer | Phase 1 |
| Phase 3 | Async pool interface | Phase 1 |
| Phase 4 | Full integration | Phases 1-3 |
| Phase 5 | Native transport | Kombu async support |

Phases 1-4 can proceed without Kombu changes (using `run_in_executor`).
Phase 5 requires Kombu async implementation.

---

## References

- [Python asyncio documentation](https://docs.python.org/3/library/asyncio.html)
- [kombu/asynchronous/hub.py](https://github.com/celery/kombu/blob/main/kombu/asynchronous/hub.py) - Current Hub implementation
- [celery/worker/loops.py](../celery/worker/loops.py) - Current worker loops
- [docs/kombu-asyncio-implementation-plan.md](kombu-asyncio-implementation-plan.md) - Kombu async plan
