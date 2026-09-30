# openplc-runtime

OpenPLC Runtime v4: the PLC runtime (Python REST API server plus a C/C++ real-time core) that runs programs built by the OpenPLC Editor.

## Autonomy development rules

These rules are identical in every Autonomy repository and are maintained in the MisterFlow plugin
(`Autonomy-Logic/skills`, `plugins/autonomy/harness/repository-rules.md`). Change them there, not here.

- Tracked work starts with `autonomy:misterflow`: load it yourself before changing product code, fixing a
  bug, implementing or preparing a PR, even when no Jira key was mentioned. Only answering questions and typo or wording fixes that
  change no behaviour are exempt. "There is no ticket" or "skip the process" does not make product
  work untracked: offer to create the task instead of changing code. This file describes only this
  repository's commands, architecture and code conventions; for process, MisterFlow and the Confluence
  process pages win over anything written here.
- Knowledge boundary: when data is missing or uncertain, say there is not enough information to answer
  reliably. Never fill a gap with a plausible assumption. Keep verified facts, inferences and missing
  data visibly separate, and say which is which.
- Language: answer in the developer's language. Jira, Confluence and GitHub text is always English.
- Branches: `feature/<KEY>-<slug>` for demands and `bugfix/<KEY>-<slug>` for bugs, created from the
  integration branch named below. A production hotfix is a `bugfix/<KEY>-<slug>` branch from `main` and
  a PR. Never commit or push directly to the integration branch or `main`. One Jira key per branch: work
  for another key starts on its own branch before any edit. The key goes in the branch name and the PR
  title, never in commit messages, code or comments.
- Commits: never commit on your own initiative. Propose the commit at a natural checkpoint, such as a
  finished and verified plan phase, and make it only after the developer confirms. Commit and push are
  separate commands, each confirmed on its own, never chained; opening a PR and merging each need their
  own confirmation too. When asked for a commit message or a commit, do not edit files you were not
  asked to change: report problems, such as a forbidden comment, and let the developer decide.
- Scope: a rewrite or refactor beyond the current task is a new demand, proposed as a separate task and
  never mixed into the current branch. Never stash, reset, `checkout -- .` or otherwise discard the
  developer's changes, and never install anything outside the repository, without asking.
- Tests: every demand ships with unit tests, an end-to-end test and a manual test by the developer, with
  evidence for each before any PR is opened, a draft PR included. Where this repository has no
  interface of its own, the end-to-end test runs through the interface or protocol that uses it. A
  repository with no code to unit test, such as documentation or local tooling scripts, uses its own
  validation checks in place of unit tests.
- Typing: `any` in TypeScript and `typing.Any` in Python are forbidden. Use concrete types, or `unknown`
  or `object` narrowed where the data enters.
- Comments: technical and minimal, at most 256 characters each; formal API documentation (JSDoc,
  docstrings, Doxygen) may be longer. Never write business rules, product strategy or rationale, Jira
  keys, names of people or customers, internal links or anything sensitive in a comment. Review the
  comments in the changed files before every commit.

Integration branch: `development`. Jira project: `RTOP`.

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
# Python: creates venvs/test-env, runs tests/pytest and core/src/drivers/plugins/python/
bash scripts/run-pytest.sh

# C unit tests (Ceedling, project.yml, tests/test_*.c); coverage with gcov:all
ceedling test:all
ceedling gcov:all

# Runtime C++ that Ceedling does not reach (tests/host/test_*.cpp, C++17)
./tests/host/run.sh

# Bootloader (Go), same checks as CI
cd bootloader && gofmt -l . && go vet ./... && go test -race -count=1 ./...
```

- CI (`.github/workflows/tests.yml`, on every PR): gofmt, go vet and `go test -race` for `bootloader/`;
  pytest over `tests/pytest` excluding the `plugins`, `modbus_master` and `modbus_slave` suites (red on
  the base branch), plus the named plugin suites known green; shellcheck on the installer scripts.
- End-to-end: a program deployed to the runtime and exercised over its protocols (e.g. Modbus) or from
  the editor/Edge. Existing suites: `tests/lifecycle/` (boots a real `plc_main` with a compiled program and
  drives it over the command socket, Linux or its container, see its README) and `tests/integration/`
  (bootloader against a real registry in a Docker test host, `tests/integration/harness.sh up|seed|test`,
  with `stubruntime/` as a Go stand-in runtime). For the integrated stack use local-dev-toolkit
  (`./dev rebuild runtime`, `./dev runtime sync`).
- The developer's manual test is required for every demand.

## Linting and Formatting

Pre-commit hooks handle formatting. Install with:
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

- **C/C++**: Clang-Format (LLVM style, 4-space indent, 100 char limit); the hook matches only `.c`/`.h`,
  so `.cpp` files are not formatted by pre-commit
- **Python**: Black + isort + Ruff (100 char line length) and pylint

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
Starts and stops pass through `TRANSITIONING_TO_RUN` and `TRANSITIONING_TO_STOP` (appended to the enum in
`plc_state_manager.h`; `RUNNING` is published only when the first scan is released).
State management: `core/src/plc_app/plc_state_manager.cpp`

### Plugin System
- **Config**: `plugins.conf`, created at runtime by copying `plugins_default.conf` when missing (`core/src/drivers/plugin_driver.c`)
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

- Check every return value that can fail (allocations, IO, pthread calls); handle every error path; no silent failures.
- Bounded string/buffer operations only (`snprintf`, explicit lengths); never `strcpy`, `sprintf`, or unchecked `memcpy` sizes.
- Every allocation has one clear owner responsible for freeing it, including on error paths.
- Real-time scan path: no allocation, blocking calls, file IO, or logging inside the PLC cycle.
- Shared state between the scan thread and other threads goes through the documented mutexes; no unsynchronized access.
- `const`-correct signatures; `static` for file-internal functions; in C++ prefer RAII over manual new/delete.

### Python Best Practices

- Type hints on every function signature; model structured data with dataclasses, `TypedDict`, `Protocol` or concrete types instead of loose dicts.
- Catch specific exceptions; never bare `except:` and never swallow errors silently; log with context.
- No mutable default arguments; use context managers (`with`) for files, sockets, and locks.
- Keep the ctypes mirror (`core/src/drivers/plugins/python/shared/plugin_runtime_args.py`) byte-compatible with the C structs it mirrors; changes on either side must update both.

## Key Directories

- `webserver/` - Flask REST API and WebSocket debug interface
- `core/src/plc_app/` - C/C++ real-time PLC runtime
- `core/src/drivers/` - Plugin driver system
- `core/generated/` - Generated PLC code from uploaded programs
- `scripts/` - Build, compile, and management scripts
- `build/` - CMake output (`plc_main` executable, `libplc_*.so` libraries)
- `venvs/` - Python virtual environments (runtime + per-plugin)
- `bootloader/` - Go service that starts and maintains the runtime container and stays reachable to install a new runtime version
- `windows/` - Windows installer (MSYS2 + Inno Setup)
- `docs/` - Detailed documentation

## Compilation Flow

1. OpenPLC Editor uploads `program.zip` to `/api/upload-file`
2. Runtime validates, extracts to `core/generated/`
3. `scripts/compile.sh` compiles to `build/libplc_*.so`
4. Runtime loads shared library dynamically via `plcapp_manager.c`

The pipeline is STruC++ only (`scripts/Makefile.strucpp`); `compile.sh` rejects MatIEC-generated sources.

## Commit Messages

Conventional Commits (spec in `docs/DEVELOPMENT.md`), concise and focused on why.
