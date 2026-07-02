# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build Commands

```bash
# Full installation (installs deps, creates venv, compiles runtime)
sudo ./install.sh

# Manual build (C/C++ runtime core)
mkdir -p build && cd build && cmake .. && make -j$(nproc)

# Start the runtime (requires root for real-time scheduling)
sudo ./start_openplc.sh

# Run web server only (for development)
source venvs/runtime/bin/activate
sudo python3 -m webserver.app

# Run PLC runtime only
sudo ./build/plc_main --print-logs
```

## Testing

```bash
# Sets up the test venv and runs the suite
bash scripts/run-pytest.sh
```

## Linting and Formatting

Pre-commit hooks handle formatting. Install with:
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

- **C/C++**: Clang-Format (LLVM style, 4-space indent, 100 char limit)
- **Python**: Black + isort + Ruff (100 char line length)

## Architecture Overview

OpenPLC Runtime v4 is a **dual-process industrial PLC runtime**:

### Process 1: REST API Server (Python/Flask)
- **Location**: `webserver/`
- **Port**: 8443 (HTTPS with self-signed TLS)
- **Purpose**: REST API for OpenPLC Editor, WebSocket debug interface, compilation orchestration
- **Entry point**: `webserver/app.py`

### Process 2: PLC Runtime Core (C/C++)
- **Location**: `core/src/plc_app/`
- **Executable**: `build/plc_main`
- **Purpose**: Real-time PLC execution with SCHED_FIFO priority
- **Entry point**: `core/src/plc_app/plc_main.c`

### Inter-Process Communication
- **Command socket**: `/run/runtime/plc_runtime.socket` (text protocol for start/stop/status)
- **Log socket**: `/run/runtime/log_runtime.socket` (real-time log streaming)
- **Client**: `webserver/unixclient.py`
- **Server**: `core/src/plc_app/unix_socket.c`

### PLC State Machine
```
EMPTY -> INIT -> RUNNING <-> STOPPED -> ERROR
```
State management: `core/src/plc_app/plc_state_manager.cpp`

### Plugin System
- **Config**: `plugins.conf`
- **Types**: Python (type=0) and Native C/C++ (type=1)
- **Driver code**: `core/src/drivers/`
- **Plugin examples**: `core/src/drivers/plugins/python/` and `core/src/drivers/plugins/native/`

### Key Subsystems
- **Scan cycle manager**: `core/src/plc_app/scan_cycle_manager.c` - deterministic timing
- **Debug handler**: `core/src/plc_app/debug_handler.c` - WebSocket debug protocol
- **Watchdog**: `core/src/plc_app/utils/watchdog.c` - health monitoring
- **Image tables**: `core/src/plc_app/image_tables.cpp` - I/O buffer management

## Code Style

- **C/C++**: 4-space indent, no tabs, `snake_case` functions, `snake_case_t` types, `UPPER_CASE` macros
- **Python**: PEP 8, type hints, 100 char lines
- **No emojis** anywhere in code, comments, or documentation (project standard)

### C/C++ Best Practices

- Check every return value that can fail (allocations, IO, pthread calls); handle every error path — no silent failures.
- Bounded string/buffer operations only (`snprintf`, explicit lengths); never `strcpy`, `sprintf`, or unchecked `memcpy` sizes.
- Every allocation has one clear owner responsible for freeing it, including on error paths.
- Real-time scan path: no allocation, blocking calls, file IO, or logging inside the PLC cycle.
- Shared state between the scan thread and other threads goes through the documented mutexes — no unsynchronized access.
- `const`-correct signatures; `static` for file-internal functions; in C++ prefer RAII over manual new/delete.

### Python Best Practices

- Type hints on every function signature; use dataclasses or TypedDict for structured data instead of loose dicts.
- Catch specific exceptions; never bare `except:` and never swallow errors silently — log with context.
- No mutable default arguments; use context managers (`with`) for files, sockets, and locks.
- Keep the ctypes mirror (`shared/plugin_runtime_args.py`) byte-compatible with the C structs it mirrors — changes on either side must update both.

## Key Directories

- `webserver/` - Flask REST API and WebSocket debug interface
- `core/src/plc_app/` - C/C++ real-time PLC runtime
- `core/src/drivers/` - Plugin driver system
- `core/generated/` - Generated PLC code from uploaded programs
- `scripts/` - Build, compile, and management scripts
- `build/` - CMake output (`plc_main` executable, `libplc_*.so` libraries)
- `venvs/` - Python virtual environments (runtime + per-plugin)
- `docs/` - Detailed documentation

## Compilation Flow

1. OpenPLC Editor uploads `program.zip` to `/api/upload-file`
2. Runtime validates, extracts to `core/generated/`
3. `scripts/compile.sh` compiles to `build/libplc_*.so`
4. Runtime loads shared library dynamically via `plcapp_manager.c`

## Git Workflow

- Base branch: `development` — feature branches start from it and PRs target it.
- Branch naming: `RTOP-<n>-<kebab-slug>` (e.g. `RTOP-129-discovery-service-infrastructure`). Non-ticket maintenance uses `chore/`, `fix/`, `docs/`.
- Commit messages: Conventional Commits (spec in `docs/DEVELOPMENT.md`), concise and focused on why.

## Issue Tracker

Jira, project key `RTOP`. Fetch and update tickets via the Atlassian MCP tools. Reference the ticket key (`RTOP-<n>`) in branch names and PR descriptions.
