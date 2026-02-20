# Trio Async Pipeline — A Concept-by-Concept Tutorial

## Introduction: Why Async?

Imagine a restaurant kitchen with a single chef. When the chef puts bread in the toaster, they don't stand there staring at it — they chop vegetables, plate a salad, check the oven. The chef is one person (one thread), but they juggle many tasks by **switching** between them whenever one is waiting on something external (the toaster, the oven).

That is exactly what asynchronous programming does for your code. A traditional (synchronous) program that fetches 5 records from a remote API will wait for each response before starting the next request — like a chef who stares at the toaster. An async program fires off all 5 requests, and while each one is "in the toaster," the program does other useful work.

**Trio** is a Python library that makes async programming safe and readable. It is an alternative to Python's built-in `asyncio`, designed around a principle called *structured concurrency* that prevents an entire class of bugs related to orphaned tasks and unhandled errors.

This tutorial walks through the `trio_pipeline.py` project concept by concept.

---

## Concept 1 — `async` / `await`

### What is a coroutine?

A function defined with `async def` is called a **coroutine function**. When you call it, Python does *not* run its body. Instead, it creates a lightweight **coroutine object** — a kind of paused recipe that the event loop can resume later.

```python
async def fetch_record(record_id, delay, send_channel):
    await trio.sleep(delay)       # Pause here; let other tasks run.
    await send_channel.send(...)  # Pause again if the channel is full.
```

### What does `await` do?

`await` tells the runtime: *"I need the result of this async operation. While I'm waiting, feel free to run other tasks."* Under the hood, it suspends the current coroutine and returns control to Trio's scheduler.

### What goes wrong without `await`?

```python
# BAD — this creates a coroutine object and immediately discards it:
fetch_record("A", 1.0, channel)

# Python will emit:
#   RuntimeWarning: coroutine 'fetch_record' was never awaited

# GOOD:
await fetch_record("A", 1.0, channel)          # runs it sequentially
nursery.start_soon(fetch_record, "A", 1.0, channel)  # schedules it concurrently
```

The key insight: calling an `async def` function without `await` is like writing a recipe on a card and then throwing it in the trash — the food never gets cooked.

---

## Concept 2 — Nurseries (Structured Concurrency)

### The metaphor

A **nursery** is like a supervised playroom for child tasks. The rule is simple:

> *The nursery door does not open until every child inside has finished (or been cancelled).*

This is what makes Trio different from raw threads or `asyncio.create_task()`. There are no orphaned tasks floating around — every concurrent operation has a clear parent scope.

### The canonical pattern

```python
async with trio.open_nursery() as nursery:
    nursery.start_soon(task_a)
    nursery.start_soon(task_b)
# ← We only reach this line when BOTH task_a and task_b are done.
```

### The anti-pattern

```python
# WRONG — will raise TypeError:
nursery = trio.open_nursery()      # This returns an async context manager,
nursery.start_soon(task_a)         # not a usable nursery object.

# You MUST use `async with` because the nursery needs to:
#   1. Set up a cancel scope (requires async setup)
#   2. Wait for all children to finish (requires async teardown)
```

### Why this matters

If `task_a` raises an unhandled exception, the nursery immediately **cancels** `task_b` and every other sibling, then re-raises the error. No task silently continues after a failure. This is Trio's core safety guarantee.

---

## Concept 3 — Concurrent vs Sequential Execution

Here is the most tangible benefit of async. Our pipeline fetches 5 records with these simulated delays:

| Record | Delay |
|--------|-------|
| A      | 1.0 s |
| B      | 0.5 s |
| C      | 0.8 s |
| D      | 1.2 s |
| E      | 0.3 s |

### Sequential (one at a time)

```
Time  0s    1s    2s    3s    3.8s
      |─A───|─B──|─C───|─D────|─E|
      Total: 1.0 + 0.5 + 0.8 + 1.2 + 0.3 = 3.8 seconds
```

### Concurrent (all at once in a nursery)

```
Time  0s         1.2s
      |─A────────|
      |─B───|
      |─C─────|  (fails, but others continue)
      |─D──────────|
      |─E─|
      Total ≈ max(1.0, 0.5, 0.8, 1.2, 0.3) = 1.2 seconds
```

**3× faster** — and the speedup grows with the number of tasks. With 100 network requests that each take 1 second, sequential takes ~100 seconds while concurrent takes ~1 second.

---

## Concept 4 — Memory Channels

### The problem

`nursery.start_soon()` is a fire-and-forget operation. It schedules a task but gives you **no way to get a return value** from it. So how do concurrent tasks communicate their results?

### The solution: memory channels

A memory channel is a **pipe** with two ends:

- A **send end** — producers push items in
- A **receive end** — consumers pull items out

```python
send_channel, receive_channel = trio.open_memory_channel(10)
```

### Why not a regular Python list?

| Feature               | `list`             | Memory Channel       |
|-----------------------|--------------------|----------------------|
| Waiting for new items | Busy-loop (wasteful)| `await` (efficient)  |
| Task safety           | Not safe            | Fully safe           |
| Back-pressure         | None                | Built-in             |
| End-of-stream signal  | Manual flag         | Close the send end   |

### Buffering: `0` vs `N`

- `trio.open_memory_channel(0)` — **unbuffered**. Every `send()` blocks until a matching `receive()` happens. Like a relay race baton handoff — the sender waits with arm outstretched until the receiver grabs it.

- `trio.open_memory_channel(5)` — **buffered** (capacity 5). Up to 5 items can sit in the channel waiting to be consumed. The sender only blocks if all 5 slots are full. This decouples fast producers from slow consumers.

In our pipeline we use capacity 10 — enough buffer so fetchers rarely block, while still providing back-pressure if processing falls behind.

### The producer-consumer flow

```
Fetcher A ──send()──┐
Fetcher B ──send()──┤
Fetcher D ──send()──┤──► [channel buffer] ──► async for record in receive_channel:
Fetcher E ──send()──┘                            processed = await process_record(record)
```

---

## Concept 5 — Error Handling

### Trio's strict rule

In Trio, if **any** child task raises an unhandled exception, the nursery:

1. **Cancels** every other running child task
2. **Waits** for all children to finish their cancellation cleanup
3. **Re-raises** the exception to the parent scope

This is intentional. It prevents the dangerous situation where one task has failed but others continue operating on assumptions that are no longer valid.

### When you want partial failures

Sometimes — like in our pipeline — you *want* some tasks to fail without killing the rest. The solution is to catch exceptions **inside the task itself**:

```python
async def safe_fetch(record_id, delay):
    try:
        await fetch_record(record_id, delay, send_channel)
    except ValueError as exc:
        print(f"Skipping record '{record_id}': {exc}")
        # The nursery never sees this error — it was handled here.
```

### When NOT to catch

If an error means the whole operation is invalid (e.g., a database connection died), let it propagate. The nursery will cleanly cancel all sibling tasks and you'll get a clear traceback. Fighting Trio's cancellation is almost always the wrong choice.

---

## Mental Model Summary

> **Trio is like a well-run kitchen.** There is one chef (one thread) who juggles many dishes (tasks) by switching between them whenever one is waiting on an oven or toaster (`await`). All the dishes for a single order are managed inside a nursery — the order isn't served until every dish is ready (or one is ruined, in which case you cancel the whole order and report the problem). Dishes pass from the prep station to the plating station through a serving window (memory channel) that prevents the prep cooks from piling up plates faster than the platers can handle them (back-pressure). The chef never leaves a dish unattended in a corner — structured concurrency guarantees that every task is accounted for, every time.

---

## What to Try Next

Here are 5 modifications you can make to deepen your understanding. Each one uses only concepts from this tutorial — no new libraries needed.

1. **Batch processing** — Change the pipeline so that instead of processing records one at a time from the channel, it collects records in batches of 2 and processes each batch together. *Hint: accumulate items with `receive_channel.receive()` rather than `async for`, and spawn a process task per batch.*

2. **Add a timeout** — Wrap the entire pipeline in a `trio.move_on_after(2.0)` cancel scope so that any fetch taking longer than 2 seconds is cancelled. Observe which records get dropped and why. *This teaches you about cancel scopes without needing new concepts.*

3. **Scale to more consumers** — The pipeline already uses 2 parallel consumers. Try increasing to 4 consumers and adding a longer processing delay (e.g., 0.5s). Observe how the workload gets distributed. *This teaches you how channel fan-out behaves as you scale consumers.*

4. **Progress reporting** — Add a separate task that prints a progress update every 0.5 seconds ("Waiting... 3 records still in flight"). Use a shared counter protected by careful design (no locks needed in Trio — think about why). *This teaches you about Trio's cooperative concurrency model.*

5. **Retry failed records** — Instead of skipping record "C", retry it up to 3 times with a 0.2-second delay between attempts. If it still fails after 3 tries, then skip it. *This teaches you about combining loops with structured concurrency.*
