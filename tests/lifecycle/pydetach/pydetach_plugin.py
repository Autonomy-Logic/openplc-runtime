# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

import os

_args = None


def owns_mode_switch():
    """Declare switch ownership when PYDETACH_OWNS_SWITCH=1; this fixture never reports."""
    return os.environ.get("PYDETACH_OWNS_SWITCH") == "1"


def init(runtime_args_capsule):
    """Keep the runtime arguments capsule and report the init."""
    global _args
    _args = runtime_args_capsule
    print("pydetach: init", flush=True)
    return True


def start_loop():
    """Report a successful start; this fixture runs no thread."""
    return True


def stop_loop():
    """Report a successful stop."""
    return True


def detach_image():
    """Drop the runtime arguments capsule and accept the detach."""
    global _args
    _args = None
    print("pydetach: detached", flush=True)
    return True


def cleanup():
    """Drop the runtime arguments capsule and report the cleanup."""
    global _args
    _args = None
    print("pydetach: cleanup", flush=True)
    return True
