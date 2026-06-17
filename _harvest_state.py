"""
Persistent background-harvest state shared between the pipeline thread and the
Streamlit UI.

Streamlit re-executes dashboard.py on every rerun, resetting any module-level
variables defined there. State defined in a separate imported module survives
reruns because the module is cached in sys.modules after the first import.
"""
import threading

# Mutable state — written by the pipeline thread, read by the UI
running: bool       = False
done:    bool       = False
log:     list[str]  = []
stats:   dict       = {}
error:   str | None = None

lock = threading.Lock()


def reset() -> None:
    global running, done, log, stats, error
    with lock:
        running = False
        done    = False
        log     = []
        stats   = {}
        error   = None


def start() -> None:
    global running, done, log, stats, error
    with lock:
        running = True
        done    = False
        log     = []
        stats   = {}
        error   = None


def append_log(msg: str) -> None:
    with lock:
        log.append(msg)


def finish(stats_dict: dict) -> None:
    global running, done, stats
    with lock:
        stats   = stats_dict
        running = False
        done    = True


def fail(err: str) -> None:
    global running, done, error
    with lock:
        error   = err
        running = False
        done    = True


def snapshot() -> dict:
    """Thread-safe copy of current state for the UI to render."""
    with lock:
        return {
            "running": running,
            "done":    done,
            "log":     list(log),
            "stats":   dict(stats),
            "error":   error,
        }
