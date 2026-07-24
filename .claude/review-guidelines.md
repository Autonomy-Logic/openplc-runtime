# Review Guidelines

Apply the full checklist in `docs/pr-reviews/PR_REVIEW_CHECKLIST.md`. Priorities:

- Dual-process boundaries hold: PLC core (C/C++) and webserver (Python) communicate only via the documented IPC commands (`core/src/plc_app/unix_socket.c`).
- Real-time safety in the scan cycle: no blocking calls, allocation, or logging in the hot path.
- Plugin API compatibility: changes to `plugin_runtime_args` must keep the Python ctypes mirror (`shared/plugin_runtime_args.py`) in sync.
- New behavior comes with pytest coverage where the Python side is touched.
- C/C++: snake_case functions, 4-space indent, clang-format clean. Python: PEP 8, type hints.
- C/C++ and Python Best Practices in CLAUDE.md apply to every diff (bounded buffers, error paths handled, no blocking in the scan cycle, specific exceptions).
- Docs updated when documented behavior changes (README, CLAUDE.md, docs/).
