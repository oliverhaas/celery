# Asyncio Interface - Next Steps

This document lists the functions currently wrapped with `sync_to_async` that would need native async implementations in the underlying libraries (kombu, pyamqp, redis-py, etc.) to complete the asyncio migration.

## Current State

The Celery asyncio interface currently uses `asgiref.sync_to_async` to wrap blocking I/O operations. The wrappers are pushed as far down as possible, wrapping only the actual I/O boundaries.

**Architecture:** The `Backend` base class in `celery/backends/base.py` provides default async methods (e.g., `aforget()`, `asave_group()`, `await_for_pending()`) that wrap the sync methods with `sync_to_async`. Backend subclasses with native async support (like Redis) override these methods with native implementations.

**Redis Backend Native Async Support:** The Redis backend now has native asyncio support using `redis.asyncio`. When `redis>=4.2.0` is installed, the async methods (`aget()`, `aforget()`, `asave()`, etc.) use the native async Redis client instead of `sync_to_async` wrappers.

## Functions Wrapped with sync_to_async

### 1. Broker Communication (kombu/pyamqp)

| Wrapped Function | Location | What It Does |
|------------------|----------|--------------|
| `producer.publish()` | via `amqp.send_task_message()` | Publishes task message to broker |
| `producer_or_acquire()` | `celery/app/base.py` | Acquires producer from connection pool |
| `connection_or_acquire()` | `celery/app/base.py` | Acquires connection from pool |
| `mailbox._broadcast()` | via `control.broadcast()` | Broadcasts control commands to workers |

**Upstream library:** kombu (and pyamqp for AMQP transport)

**Required changes in kombu:**
- `kombu.Producer.publish()` → `kombu.Producer.apublish()`
- `kombu.pools.producers` → async context manager support
- `kombu.Connection` → async connection pool
- `kombu.pidbox.Mailbox._broadcast()` → async broadcast

### 2. Result Backend Operations

The `Backend` base class now provides async methods with `sync_to_async` defaults:

| Sync Method | Async Method | Default Implementation |
|-------------|--------------|------------------------|
| `wait_for_pending()` | `await_for_pending()` | `sync_to_async` wrapper |
| `forget()` | `aforget()` | `sync_to_async` wrapper |
| `remove_pending_result()` | `aremove_pending_result()` | `sync_to_async` wrapper |
| `save_group()` | `asave_group()` | `sync_to_async` wrapper |
| `delete_group()` | `adelete_group()` | `sync_to_async` wrapper |
| `restore_group()` | `arestore_group()` | `sync_to_async` wrapper |
| `iter_native()` | `aiter_native()` | `sync_to_async` wrapper |
| `get_many()` | `aget_many()` | `sync_to_async` wrapper (Redis has native) |

**Still wrapped in other locations:**

| Wrapped Function | Location | What It Does |
|------------------|----------|--------------|
| `backend.apply_chord()` | `celery/canvas.py` | Sets up chord callback |

**Native async implementations:**
- Redis backend: Uses `redis.asyncio` - overrides all base async methods with native implementations including `aiter_native()` and `aget_many()`
- Other backends: Use the default `sync_to_async` wrappers from base class

**To add native async to other backends:**
- Override the async methods in the backend subclass
- Leverage async support in underlying libraries (e.g., SQLAlchemy async for database backends)

### 3. Worker Control

| Wrapped Function | Location | What It Does |
|------------------|----------|--------------|
| `app.control.revoke()` | `celery/result.py` | Sends revoke command to workers |

**Upstream library:** kombu (uses `mailbox._broadcast()`)

**Required changes:**
- `Control.arevoke()` would need `Control.abroadcast()`
- Which needs async `Mailbox._broadcast()`
- Which needs async kombu connection/producer

### 4. Task Execution (eager mode)

**Async Task Support:** Celery now supports async task implementations via the `arun()` method:

| Method | What it does |
|--------|--------------|
| `run()` | Sync task body - implement this for sync tasks |
| `arun()` | Async task body - override this for async tasks (defaults to wrapping `run()` with `sync_to_async`) |
| `__call__()` | Calls `run()` directly - returns whatever `run()` returns (coroutine if `run()` is async) |
| `apply()` | Sync eager execution - uses sync tracer, handles coroutines with `asyncio.run()` |
| `aapply()` | Async eager execution - uses async tracer, awaits `arun()` |

**Defining tasks:**

```python
from celery import Task

# Option 1: Sync task (most common)
@app.task
def sync_task(x, y):
    return x + y

# Option 2: Async task (override arun())
class AsyncTask(Task):
    name = 'async_task'

    def run(self, x, y):
        # Required by base class, but not used in async context
        raise NotImplementedError('Use async methods for this task')

    async def arun(self, x, y):
        # Your async implementation
        result = await some_async_operation(x, y)
        return result

# Option 3: Hybrid task (sync run, auto-wrapped for async)
@app.task
def hybrid_task(x, y):
    # Works both sync and async
    # When called via aapply/adelay, arun() wraps this with sync_to_async
    return x + y
```

**Usage:**

```python
# Sync task - all paths work
sync_task(1, 2)              # Direct call
sync_task.delay(1, 2)        # Eager sync
await sync_task.adelay(1, 2) # Eager async (wraps run with sync_to_async)

# Async task - use async paths
await async_task.arun(1, 2)       # Direct async call
await async_task.adelay(1, 2)     # Eager async
await async_task.aapply(args=(1, 2))  # Eager async with options
```

**Key implementation files:**
- `celery/app/task.py` - `run()`, `arun()`, `__call__()`
- `celery/app/trace.py` - `build_tracer()` (sync), `build_async_tracer()` (async)

## Priority for Upstream Work

### Phase 1: kombu async support (HIGH)
These are the most critical as they block all task dispatch:

1. **Async producer/publish**
   - `Producer.apublish()`
   - Async connection pool management

2. **Async connection handling**
   - `Connection.aconnect()`, `Connection.aclose()`
   - Async context managers for connection/producer pools

### Phase 2: Backend async support (HIGH)
Critical for result retrieval:

1. **Redis backend** (most common) - **IMPLEMENTED**
   - Uses `redis.asyncio` for native async operations
   - `await backend.await_for_pending()` using async Redis client
   - `await backend.aforget()`, `await backend.asave_group()`, etc.
   - Automatically used when `redis>=4.2.0` is installed

2. **Other backends** (TODO)
   - Database backends can use SQLAlchemy async
   - Memcached, etc.

### Phase 3: Control commands (MEDIUM)
For task revocation and worker control:

1. **Async mailbox/broadcast**
   - Needs async kombu first (Phase 1)

## Implementation Strategy

### Option A: Add async support to kombu
- Add async alternatives to kombu's core classes
- Maintain backwards compatibility
- This is the "proper" solution

### Option B: Use aiormq/aio-pika directly
- Bypass kombu for async operations
- Use AMQP-native async libraries
- More work but potentially cleaner

### Current Approach
The `sync_to_async` wrappers allow Celery to be used from async code today, with the understanding that:
- Task dispatch runs in a thread pool (not blocking the event loop)
- Result waiting runs in a thread pool
- True async performance requires upstream library changes

## File Reference

Current async implementations in Celery:
- [celery/app/base.py](../celery/app/base.py) - `asend_task()`
- [celery/app/task.py](../celery/app/task.py):
  - `run()` - sync task body (user implements this for sync tasks)
  - `arun()` - async task body (user overrides for async tasks, default wraps `run()` with `sync_to_async`)
  - `__call__()` - direct invocation (calls `run()`, returns coroutine if `run()` is async)
  - `adelay()`, `aapply_async()`, `aapply()`, `aretry()` - async dispatch/execution
- [celery/app/trace.py](../celery/app/trace.py):
  - `build_tracer()` - sync tracer (handles coroutines with `asyncio.run()` if `run()` returns one)
  - `build_async_tracer()` - async tracer (awaits `arun()`)
- [celery/canvas.py](../celery/canvas.py) - `adelay()`, `aapply_async()`, `aapply()` for Signature, chain, group, chord
- [celery/result.py](../celery/result.py) - `aget()`, `acollect()`, `aget_leaf()`, `aforget()`, `arevoke()`, `ajoin()`, `ajoin_native()`, `aiter_native()`, etc.
- [celery/backends/base.py](../celery/backends/base.py) - Base async methods with `sync_to_async` defaults:
  - `aforget()`, `asave_group()`, `adelete_group()`, `arestore_group()`
  - `await_for_pending()`, `aremove_pending_result()`, `aiter_native()`
- [celery/backends/redis.py](../celery/backends/redis.py) - Native async Redis backend methods (overrides base):
  - `async_client` property - async Redis client using `redis.asyncio`
  - `aget()`, `amget()`, `aset()`, `adelete()`, `aincr()`, `aexpire()`
  - `aget_task_meta()`, `await_for_pending()`, `await_for()`
  - `aforget()`, `asave_group()`, `adelete_group()`, `arestore_group()`
  - `aget_many()`, `aiter_native()` - batch result fetching
