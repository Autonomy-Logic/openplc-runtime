# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import os
import threading

_halt = threading.Event()
_worker = None


def init(runtime_args_capsule):
    """Accept the runtime arguments; this fixture reads none of them."""
    return True


def _spin():
    """Run until cleanup() sets the halt event."""
    while not _halt.wait(0.05):
        pass


def start_loop():
    """Start a worker thread that only cleanup() ends."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return True
    _halt.clear()
    _worker = threading.Thread(target=_spin, name="stopfail", daemon=True)
    _worker.start()
    print("stopfail: start_loop started the worker", flush=True)
    return True


def _halt_worker():
    """Stop and join the worker thread."""
    global _worker
    _halt.set()
    if _worker is not None:
        _worker.join(timeout=5)
        _worker = None


def _stop_without_result():
    """Stop and join the worker thread, with no return statement, so the result is None."""
    _halt_worker()
    print("stopfail: stop_loop returning None", flush=True)


def stop_loop():
    """Fail the stop with the worker left running: return False, or raise if STOPFAIL_MODE=raise.

    With STOPFAIL_MODE=none, stop and join the worker, then return None.
    """
    mode = os.environ.get("STOPFAIL_MODE")
    if mode == "none":
        return _stop_without_result()
    if mode == "raise":
        print("stopfail: stop_loop raising", flush=True)
        raise RuntimeError("stopfail: stop_loop raised on purpose")
    print("stopfail: stop_loop returning False", flush=True)
    return False


def cleanup():
    """Stop and join the worker thread."""
    _halt_worker()
    return True
