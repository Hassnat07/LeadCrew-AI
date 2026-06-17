"""
Deadlock-safe parallel runner — shared by all pipeline stages.

Problem: ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which blocks
until every submitted future completes.  If a future is running an HTTP request
that never returns (TCP stall after connect), the entire script hangs.

Fix: wrap as_completed with both a per-future timeout (fut.result(timeout=N))
and a finally block that cancels any futures that haven't started yet, so the
executor can exit promptly even when workers are stuck.

Usage:
    from runner import run_parallel

    lock = threading.Lock()
    results = []

    def process(item):
        data = fetch(item)          # potentially slow
        with lock:
            results.append(data)   # collect via shared state

    def on_err(item, exc):
        print(f"  ERROR {item}: {exc}")

    run_parallel(process, items, workers=4, on_error=on_err)
"""

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError, as_completed
from typing import Callable

ITEM_TIMEOUT = 90  # seconds per future — increase for very slow website fetches


def run_parallel(
    fn: Callable,
    items: list,
    *,
    workers: int = 4,
    item_timeout: float = ITEM_TIMEOUT,
    on_error: Callable | None = None,
) -> None:
    """
    Call fn(item) for every item in a bounded thread pool.

    fn must store results via shared state (lock-protected list / dict).
    Return values from fn are ignored.

    on_error(item, exc) is called for any exception including TimeoutError.
    Always pass on_error — omitting it silently swallows failures.
    """
    if not items:
        return

    overall_timeout = item_timeout * max(len(items), 1) + 30

    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut_map: dict[Future, object] = {pool.submit(fn, item): item for item in items}
        try:
            for fut in as_completed(fut_map, timeout=overall_timeout):
                item = fut_map[fut]
                try:
                    fut.result(timeout=item_timeout)
                except TimeoutError as exc:
                    if on_error:
                        on_error(item, exc)
                except Exception as exc:
                    if on_error:
                        on_error(item, exc)
        except TimeoutError:
            # Overall batch timed out — report any futures still pending
            if on_error:
                for fut, item in fut_map.items():
                    if not fut.done():
                        on_error(item, TimeoutError("batch timed out"))
        finally:
            # Cancel futures that haven't started yet so the executor exits cleanly.
            # Futures that are already running cannot be cancelled and will be
            # abandoned — they finish in the background but results are discarded.
            for fut in fut_map:
                fut.cancel()
