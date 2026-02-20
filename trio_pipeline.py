"""
trio_pipeline.py — An async data-fetching and processing pipeline built with Trio.

This project demonstrates 7 core Trio concepts:
  1. async/await keywords
  2. Starting tasks with nursery.start_soon()
  3. Error handling inside nurseries
  4. Coroutines and what happens without await
  5. Memory channels for inter-task communication
  6. Opening and closing nurseries correctly
  7. Sequential vs concurrent execution

Run with:  python trio_pipeline.py
"""

import trio  # Trio is the async library — it replaces asyncio with a simpler, safer API.
import time  # We use time.perf_counter() for wall-clock timing comparisons.


# ─── SECTION 1: Coroutine Definitions ────────────────────────────────────────
#
# An "async def" function is called a *coroutine function*.  When you call it,
# Python does NOT run its body — it returns a coroutine *object* instead.
# You must either `await` it or hand it to the scheduler (start_soon).
#
# ANTI-PATTERN (commented out to avoid a RuntimeWarning):
#   fetch_record("A", 1.0)          # ← This does NOTHING.  Python will warn:
#                                    #   "RuntimeWarning: coroutine 'fetch_record'
#                                    #    was never awaited"
# CORRECT usage:
#   await fetch_record("A", 1.0)    # ← Actually runs the function body.
#   nursery.start_soon(fetch_record, "A", 1.0)  # ← Schedules it concurrently.

async def fetch_record(record_id, delay, send_channel):
    """Simulate fetching a remote record after a network delay.

    Each fetched result is sent into `send_channel` so that processing
    tasks can consume it.  We use a channel instead of a return value
    because nursery.start_soon() has no way to capture return values —
    it fires and forgets.  Memory channels are Trio's idiomatic answer
    to "how do I get data out of a spawned task?"
    """
    # Simulate a failing record: record_id "C" always errors out.
    # This lets us demonstrate Trio's error-handling behavior.
    if record_id == "C":
        await trio.sleep(delay)  # Still sleep so the task "runs" for a bit.
        raise ValueError(f"Record '{record_id}' not found on remote server!")

    # trio.sleep() is a Trio-aware sleep.  Using Python's built-in
    # time.sleep() here would block the entire thread and prevent
    # ALL other tasks from running concurrently.  trio.sleep() yields
    # control back to the scheduler so other tasks can make progress.
    await trio.sleep(delay)

    record = {"id": record_id, "data": f"raw_payload_{record_id}"}
    print(f"  [fetch]   Retrieved record '{record_id}' after {delay:.1f}s")

    # Send the fetched record into the memory channel.
    # `await` is required here because send() may need to wait if
    # the channel's buffer is full (back-pressure).
    await send_channel.send(record)


async def process_record(record):
    """Simulate a CPU-light processing step on a single record.

    In a real pipeline this might validate data, transform fields,
    or enrich the record with derived values.
    """
    # Simulate a small processing delay.
    await trio.sleep(0.1)

    processed = {
        "id": record["id"],
        "result": record["data"].upper(),  # Simple transformation: uppercase.
        "status": "processed",
    }
    print(f"  [process] Processed record '{record['id']}' → {processed['result']}")
    return processed


# ─── SECTION 2: Memory Channel Setup & Pipeline ─────────────────────────────
#
# A memory channel is a pair: (send_end, receive_end).
#   - Producers call  `await send_end.send(value)`
#   - Consumers call  `async for value in receive_end:`
#
# Why not just append to a regular Python list?
#   1. A list has no concept of "waiting" — a consumer would have to
#      busy-loop (spin) checking `len(my_list)`, wasting CPU.
#   2. A list is not task-safe; concurrent reads/writes can corrupt it.
#   3. Memory channels support back-pressure: if the buffer is full,
#      the producer waits, naturally throttling fast producers.
#
# open_memory_channel(0)  → unbuffered; every send blocks until a receive.
# open_memory_channel(5)  → buffered; up to 5 items can queue before blocking.
# We use a small buffer (capacity 10) so fetchers aren't blocked unless
# the processors fall far behind.

async def pipeline(record_ids):
    """Orchestrate the full fetch → channel → process pipeline."""

    delays = {"A": 1.0, "B": 0.5, "C": 0.8, "D": 1.2, "E": 0.3}
    results = []  # Collect successfully processed records here.

    # Create a memory channel with a buffer of 10 slots.
    send_channel, receive_channel = trio.open_memory_channel(10)

    # ─── SECTION 3: Nursery and Concurrency ──────────────────────────────
    #
    # A nursery is Trio's unit of concurrency.  All tasks spawned inside
    # it MUST finish (or be cancelled) before the `async with` block exits.
    # This is called "structured concurrency" — no task can outlive its
    # parent scope, which prevents resource leaks and orphan tasks.
    #
    # CORRECT pattern:
    #   async with trio.open_nursery() as nursery:
    #       nursery.start_soon(some_task)
    #
    # ANTI-PATTERN (will raise TypeError):
    #   nursery = trio.open_nursery()   # ← Missing `async with`!
    #   nursery.start_soon(some_task)   # ← nursery is not usable this way.
    #
    # The `async with` is mandatory because the nursery needs to set up
    # a cancel scope and wait for child tasks — both require async operations.

    # ─── SECTION 4: Error Handling ───────────────────────────────────────
    #
    # Trio's strict rule: if ANY child task raises an unhandled exception,
    # the nursery *cancels every sibling task* and then re-raises the
    # exception.  This prevents half-finished work from silently continuing.
    #
    # To let some tasks fail without killing the whole nursery, we wrap
    # the error-prone work in a try/except INSIDE the task itself
    # (see the safe_fetch helper below).
    #
    # nursery.start_soon(fn, arg1, arg2) schedules `fn(arg1, arg2)` to
    # run concurrently.  It does NOT call fn immediately — it registers
    # the task and returns right away.  This is different from
    #   fn(arg1, arg2)     ← creates a coroutine object, does nothing
    #   await fn(arg1, arg2)  ← runs it, but sequentially (blocks here)
    # start_soon is the way to get TRUE concurrency inside a nursery.

    async def safe_fetch(rid, delay, sc):
        """Wrapper that catches per-record errors so one bad record
        doesn't cancel the entire pipeline."""
        try:
            await fetch_record(rid, delay, sc)
        except ValueError as exc:
            # Log the error but keep the nursery alive for other tasks.
            print(f"  [error]   Skipping record '{rid}': {exc}")

    async def producer(record_ids, sc):
        """Spawn all fetch tasks concurrently, then close the send channel."""
        async with sc:
            # A nested nursery groups all fetchers together.  When every
            # fetcher finishes, the nursery exits and we close the send
            # channel — signaling consumers that no more items are coming.
            async with trio.open_nursery() as fetch_nursery:
                for rid in record_ids:
                    delay = delays.get(rid, 0.5)
                    # start_soon schedules safe_fetch; it returns immediately.
                    fetch_nursery.start_soon(safe_fetch, rid, delay, sc)
            # All fetchers done → sc.__aexit__ closes the send channel.

    async def consumer(rc):
        """Read records from the channel and process each one.
        Multiple consumers can read from the same channel safely —
        each item is delivered to exactly one consumer."""
        async with rc:
            async for record in rc:
                processed = await process_record(record)
                results.append(processed)

    # The outer nursery runs producers and consumers CONCURRENTLY.
    # Processors start consuming records as soon as fetchers send them,
    # rather than waiting for all fetches to complete first.
    async with trio.open_nursery() as nursery:
        nursery.start_soon(producer, record_ids, send_channel)
        # Spawn 2 parallel consumer tasks to process records concurrently.
        # Both read from the same receive channel — Trio ensures each
        # item is delivered to exactly one consumer (no duplicates).
        nursery.start_soon(consumer, receive_channel.clone())
        nursery.start_soon(consumer, receive_channel)

    return results


# ─── SECTION 5: Sequential vs Concurrent Comparison ─────────────────────────
#
# To appreciate concurrency, compare the total wall-clock time:
#
# Sequential fetching (one after another):
#   Total time = 1.0 + 0.5 + 0.8 + 1.2 + 0.3 = 3.8 seconds
#
# Concurrent fetching (all at once inside a nursery):
#   Total time ≈ max(1.0, 0.5, 0.8, 1.2, 0.3) = 1.2 seconds
#
# That's a 3× speedup for just 5 records.  With hundreds of records
# the difference is dramatic.
#
# ┌─────────── Sequential ────────────────────────────────────────────┐
# │ A ██████████                                                      │
# │              B █████                                              │
# │                     C ████████                                    │
# │                               D ████████████                      │
# │                                             E ███                 │
# │ Total: ──────────────────────────────────────────► 3.8s           │
# └───────────────────────────────────────────────────────────────────┘
#
# ┌─────────── Concurrent ────────────────────────────────────────────┐
# │ A ██████████                                                      │
# │ B █████                                                           │
# │ C ████████  (fails, but others continue)                          │
# │ D ████████████                                                    │
# │ E ███                                                             │
# │ Total: ────────────► 1.2s                                         │
# └───────────────────────────────────────────────────────────────────┘

async def main():
    """Entry point: drive the pipeline and display results."""
    print("=" * 60)
    print("  Trio Async Pipeline Demo")
    print("=" * 60)
    print()

    record_ids = ["A", "B", "C", "D", "E"]  # "C" will deliberately fail.

    print("Starting concurrent pipeline...")
    start = time.perf_counter()  # Wall-clock timer — not affected by sleeps.

    results = await pipeline(record_ids)

    elapsed = time.perf_counter() - start
    print()
    print(f"Pipeline finished in {elapsed:.2f}s (concurrent).")
    print(f"  Sequential estimate: ~3.8s  |  Concurrent actual: ~{elapsed:.1f}s")
    print()

    print("Successfully processed records:")
    for r in results:
        print(f"  • {r['id']}: {r['result']}  [{r['status']}]")

    print()
    print(f"Total: {len(results)} succeeded, "
          f"{len(record_ids) - len(results)} failed/skipped.")
    print("=" * 60)


# ─── SECTION 6: Entry Point ─────────────────────────────────────────────────
#
# trio.run() is the bridge between synchronous Python and the async world.
# It creates the Trio event loop, runs the given coroutine to completion,
# and then tears everything down cleanly.  You call it exactly once,
# at the top level of your program.

if __name__ == "__main__":
    trio.run(main)
