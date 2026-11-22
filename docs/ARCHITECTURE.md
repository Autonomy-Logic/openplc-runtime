# OpenPLC Runtime v4 Architecture

## Overview

OpenPLC Runtime v4 is a dual-process system that provides industrial automation capabilities through a REST API server for OpenPLC Editor communication and a real-time PLC execution engine.

## System Components

### 1. REST API Server Process (Python/Flask)

The REST API server is a Flask-based HTTPS application that provides:

- **REST API** for PLC control and management
- **WebSocket interface** for real-time debugging
- **Program compilation orchestration**
- **User authentication and security**
- **Runtime process management**

**Key Details:**
- Port: 8443 (HTTPS)
- Location: `webserver/app.py`
- TLS: Self-signed certificates (auto-generated)
- Authentication: JWT-based

### 2. PLC Runtime Core (C/C++)

The PLC runtime is a real-time execution engine that:

- **Executes compiled PLC programs** with deterministic timing
- **Manages I/O operations** through plugin drivers
- **Provides debug interface** for variable inspection
- **Monitors system health** via watchdog
- **Maintains lifecycle states** (INIT, RUNNING, STOPPED, ERROR, EMPTY)

**Key Details:**
- Executable: `build/plc_main`
- Location: `core/src/plc_app/`
- Scheduling: SCHED_FIFO (real-time priority)
- Requires: root privileges or CAP_SYS_NICE

## Inter-Process Communication

The two processes communicate via Unix domain sockets:

### PLC Runtime Socket
- **Path:** `/run/runtime/plc_runtime.socket`
- **Purpose:** Command and control (start, stop, status)
- **Protocol:** Text-based commands with synchronous responses
- **Implementation:** `core/src/plc_app/unix_socket.c` (server), `webserver/unixclient.py` (client)

### Log Socket
- **Path:** `/run/runtime/log_runtime.socket`
- **Purpose:** Real-time log streaming from PLC runtime to REST API server
- **Implementation:** `core/src/plc_app/utils/log.c`

## PLC Lifecycle States

The PLC runtime maintains the following states:

```
EMPTY → INIT → RUNNING ⟷ STOPPED → ERROR
```

### State Descriptions

- **EMPTY**: No PLC program loaded
- **INIT**: Program loaded, initializing
- **RUNNING**: Actively executing scan cycles
- **STOPPED**: Program loaded but not executing
- **ERROR**: Recoverable error state, except after a failed plugin stop (below)

### The Stop Sequence

When every plugin stops, a stop runs these steps in order:

1. Join the PLC thread, which ends the scan.
2. Flush the retained variables.
3. Call every running plugin's `stop_loop` (`plugin_driver_stop`).
4. Ask every initialised plugin that exports `detach_image` to detach from the I/O image
   (`plugin_driver_detach_images`), while the image is still allocated.
5. Clean up the journal and the debug-write journal.
6. Call `cleanup()` on every initialised plugin that did not detach
   (`plugin_driver_cleanup_attached`).
7. Under the image-tables mutex, clear the image pointers and free the image, which is refused
   while a plugin that did not detach is still initialised.
8. Release the Python blocks and unload the program library.
9. Publish STOPPED.

A plugin that detaches stays initialised with its threads, so a board package keeps reading the
run/stop switch while the PLC is stopped and can start it again. The runtime never calls its
`start_loop`; the next start reloads the plugin configuration, which cleans it up, unloads it and
initialises it again with the new image. The load rollbacks (a plugin `init()` failure or a PLC
thread that cannot be created) run steps 4, 6, 7 and 8 the same way, since no journal exists yet. See "Keep-alive across a stop" in
`core/src/drivers/README.md` for the plugin contract.

### The Start Gates

Every start path honours the run/stop mode switch: socket START (and `GET /api/start-plc`,
which forwards it), upload-then-start, the boot auto-start, a plugin's `request_plc_start` and
the correction that runs after a transition lands. The switch gate (`plc_switch_gate()` in
`core/src/plc_app/plc_switch.c`) reads:

- **RUN** when no plugin owns the switch, or when every owner has reported since its last
  `init()` and the last report is RUN;
- **STOP** when every owner has reported since its last `init()` and the last report is STOP;
- **UNKNOWN** while any owner has not reported since its last `init()`.

A plugin owns the switch once it reports a position, or when it declares it through the optional
`owns_mode_switch` symbol before its `init()`. Ownership is remembered per plugin across re-inits
and forgotten when the plugin leaves the configuration. A report stops counting when the plugin is
de-initialised (`cleanup()`), not when it detaches.

**Gate 1, before a transition begins.** RUN proceeds. STOP refuses with no transition
(`START:ERROR_SWITCH_STOP`). When a switch owner is registered (`plc_switch_has_owner()`, gate 1
RUN or UNKNOWN), socket START begins the start and replies only once gate 2 has decided (at most
`PLC_START_OUTCOME_WAIT_MS`, 12 s; past that the start is cancelled, lands STOPPED and the reply
is `START:ERROR`). With no owner, socket START replies as soon as the start begins. On UNKNOWN, a
plugin start request is refused, and the post-landing correction never starts. An owner enters the
registry only when `plugin_driver_init` declares it or the plugin reports, at boot or inside a
load. So on the first START after a plugin configuration adds an owner, with none registered,
`plc_switch_has_owner()` is false and socket START replies `START:OK` as soon as the start begins;
gate 2 can still refuse that start, with zero scans, and the PLC lands STOPPED.

**Gate 2, inside the start.** After the program loads and the plugins are re-initialised, and
before the PLC cycle thread is created, the start waits up to `PLC_SWITCH_REPORT_WAIT_MS` (2 s) for
every owner to report. RUN creates the thread. STOP, or UNKNOWN once the wait ran out, refuses: no
plugin `start_loop` and no scan, the load rollback steps of the stop sequence above (a keep-alive
owner detaches and keeps watching the switch), and the state lands STOPPED. When the socket START
waited for gate 2 (an owner was registered, see Gate 1), the reply is `START:ERROR_SWITCH_STOP` or
`START:ERROR_SWITCH_UNKNOWN`; on the first START after a plugin configuration adds an owner, with
none registered, the reply was already `START:OK`. The UNKNOWN refusal is logged as
`Start refused: no switch owner has reported a position within 2000 ms`. The socket reply comes as
soon as the gate refuses, before the rollback: until the state lands STOPPED, `STATUS` answers
`STATUS:TRANSITIONING` and other commands answer `COMMAND:BUSY`. A refusal never lands ERROR.

**Boot.** Inside the plugin request hold, the auto-start waits up to 2 s for a missing report.
STOP stays stopped, RUN starts, and still UNKNOWN stays stopped with the log line
`no switch owner has reported a position`.

`SWITCH` answers `SWITCH:RUN`, `SWITCH:STOP` or `SWITCH:UNKNOWN` from the same gate, and
`/api/status` reports it as `switchPosition` `"run"`, `"stop"` or `"unknown"`.

### A Plugin That Does Not Stop

A stop calls every running plugin's `stop_loop`. A Python `stop_loop` that returns
`False`, or raises, is a failed stop; any other result, `None` (no `return`)
included, is a successful stop. The runtime then logs
`Stop failed: plugin <name> did not stop; keeping the I/O image and entering ERROR
until the runtime restarts` for each such plugin and stops the teardown there: the
journal, the plugins' init state, the I/O image and the program library are all
kept, because the plugin's thread may still be using them. The state lands on
ERROR and stays there: every start or stop request is refused (`START:ERROR` on the
socket) until the runtime process restarts. On SIGINT or SIGTERM the process exits
without tearing down what the failed stop kept. A native `stop_loop` returns
nothing, so a native plugin cannot report a failed stop.

**State Manager:** `core/src/plc_app/plc_state_manager.cpp`

## Threading Model

### REST API Server Threads

1. **Main Flask Thread**: Handles HTTP/HTTPS requests from OpenPLC Editor
2. **WebSocket Thread**: Manages debug connections from OpenPLC Editor
3. **Compilation Thread**: Runs build process asynchronously
4. **Runtime Manager Thread**: Monitors PLC runtime process

### PLC Runtime Threads

1. **Main Thread**: Initialization and signal handling
2. **Unix Socket Thread**: Accepts and processes commands
3. **PLC Cycle Thread / Dispatcher**: Loads the program, then runs the GCD master-tick dispatcher at SCHED_FIFO 98
4. **Task Threads**: One per IEC TASK, SCHED_FIFO 49 - IEC priority (IEC 0..48, clamped)
5. **Watchdog Thread**: SCHED_FIFO 99; bounds stops and detects a stalled dispatcher
6. **Log Thread**: Manages log socket connection

## Real-Time Execution

The PLC cycle thread runs with SCHED_FIFO priority to ensure deterministic timing:

1. **Read Inputs**: Plugin drivers read from hardware
2. **Execute Logic**: Run compiled PLC program tasks (loaded via the `strucpp_get_config()` entry point)
3. **Write Outputs**: Plugin drivers write to hardware
4. **Sleep Until Next Cycle**: Precise timing using `clock_nanosleep()`

**Timing Configuration:**
- Scan cycle duration: Defined per task by the task interval in the compiled program configuration
- Timing stats tracked per task, from the second cycle on: min/max/avg scan time, cycle time,
  latency against the scheduled tick with its dispatch delay and wake latency, and overruns; and for
  the dispatcher, its wake jitter and the duration of the plugins' `cycle_start` hooks (see
  [Performance Monitoring](#performance-monitoring))

## Plugin System

The runtime supports dynamically loaded plugins for hardware I/O:

- **Plugin Types**: Python and C/C++
- **Configuration**: `plugins.conf` file
- **Virtual Environments**: Isolated Python dependencies per plugin
- **Buffer Protection**: Mutex-protected I/O buffers during scan cycles
- **Keep-alive across a stop**: A plugin that exports the optional `detach_image` symbol drops its
  I/O image pointers on a stop and stays initialised with its threads until the next start reloads
  it; any other plugin gets `cleanup()` on the stop (see [The Stop Sequence](#the-stop-sequence))

**Documentation:** See `docs/PLUGIN_VENV_GUIDE.md` and `core/src/drivers/README.md`

## Security Architecture

### TLS/HTTPS
- Self-signed certificates generated at first run
- Certificate files: `webserver/certOPENPLC.pem`, `webserver/keyOPENPLC.pem`
- Hostname validation to prevent injection attacks

### Authentication
- JWT tokens for API and WebSocket access
- Secret key stored in `.env` under the persistent data directory (see Data Persistence)
- 256-bit cryptographic pepper for password hashing

### File Upload Security
- ZIP file validation (path traversal, size limits, compression ratio)
- Disallowed extensions: .exe, .dll, .sh, .bat, .js, .vbs, .scr
- Maximum file size: 10 MB per file, 50 MB total
- macOS metadata stripped during extraction

**Implementation:** `webserver/plcapp_management.py`, `webserver/credentials.py`

## Data Persistence

### Runtime Data Directory
**Location:** `/var/run/runtime/` (ephemeral)

Contains:
- Socket files (created at runtime)

### Persistent Data Directory
**Location:** `/var/lib/openplc-runtime/` on native Linux; `/var/run/runtime/` in Docker (mounted as a persistent volume)

Contains:
- `.env` - Environment variables (JWT secret, database URI, pepper)
- `restapi.db` - SQLite database for user accounts

### Docker Volumes
When running in Docker, mount `/var/run/runtime` as a named volume for persistence:
```bash
docker run -v openplc-runtime-data:/var/run/runtime ...
```

## Watchdog System

Three layers, from the gentlest to the last resort. The numbers below are the
defaults of the constants in `core/src/plc_app/task_policy.h`.

1. **Stuck task (dispatcher).** A task found still in the same scan on 10
   consecutive due ticks (10 of its own periods) trips the dispatcher. It claims a
   stop, drains every task, and the stop lands in ERROR. The count resets whenever
   the task is found idle, so a task whose scans finish within 10 periods never
   trips. Ticks replayed after a late dispatcher count as missed periods too.
   A task's first scan (initialisation, e.g. Python function block start-up) is
   not counted; the main watchdog trips it only if it runs past 10 s.
2. **Drain and abort (dispatcher).** On every stop each in-flight scan may run until
   10 periods after its release (10 s for a first scan). A task still inside IEC
   program code then gets `SIGUSR2`, whose handler jumps to the task's recovery
   point. A stop that aborted a task lands in ERROR. On every stop, plugin writes
   are fenced off (the journal is closed), all `%Q` outputs are written to 0 and
   pushed by a final I/O cycle, and held for 500 ms before plugins stop (a plugin
   that fails to stop latches ERROR, see "A Plugin That Does Not Stop"). Runtime
   code reached from IEC bodies that must not be interrupted (the Python block
   loader) blocks `SIGUSR2`; user C function blocks are not protected, and an abort
   landing inside one that holds a C-library lock ends in layer 3.
3. **Process exit (watchdog thread, FIFO 99).** If an aborted task does not exit
   within 2 s, a stop exceeds max(10 x the longest task interval, 10 s) + 30.5 s,
   or the dispatcher has not ticked for max(10 base ticks, 1 s), the runtime calls
   `_exit(42)` after writing `/run/runtime/watchdog_fault`. The webserver restarts
   the runtime with `--safe-mode --fault` (the marker forces the same when the exit
   code is not visible). That boot reports ERROR, does not load the program, and
   first starts the configured plugins just long enough to drive all outputs to 0.
   If that step itself hangs, the next exit records it in the marker and the
   following boot skips it, relying on the hardware safe-state watchdog.

All runtime mutexes that can take it use `PTHREAD_PRIO_INHERIT`, and state reads
are lock free, so a starved normal-priority thread cannot block the dispatcher or
the watchdog on a single CPU.

**Implementation:** `core/src/plc_app/utils/watchdog.c`, `core/src/plc_app/plc_state_manager.cpp`

## Performance Monitoring

The runtime tracks timing statistics per IEC task (min, max and average, in microseconds):

- **Scan Count**: Scans executed, the first included
- **Scan Time**: Duration of the scan body
- **Cycle Time**: From one scan start to the next scan start
- **Cycle Latency**: Scan start minus the scheduled tick that released the scan; never negative
- **Overruns**: Scans that ended after their next scheduled tick, one per scan
- **Dispatch Delay** (`dispatch_delay`): The task's release by the dispatcher minus the scheduled tick
- **Wake Latency** (`wake_latency`): Scan start minus the task's release; for each scan,
  `cycle_latency = dispatch_delay + wake_latency` in nanoseconds, and within a few microseconds in
  the reported values, which are truncated to microseconds one by one

The top-level `dispatcher` object tracks the dispatcher thread (min, max and average, in
microseconds):

- **Tick Count** (`tick_count`): Base ticks since the dispatcher started, the first included
- **Wake Jitter** (`wake_jitter`): The dispatcher's wake time minus the tick deadline, per base tick
- **Cycle Start Time** (`cycle_start_time`): Duration of the native plugins' `cycle_start` hooks,
  per task-bearing tick; its average uses the base-tick window, so it follows changes more slowly
  when the tasks run slower than the base tick

Every metric except Scan Count and Tick Count starts from the second sample after each PLC start:
the first scan, which may run long while the program initialises, the first tick and the first
`cycle_start` are left out of all of them. Until a task has a Scan Time, a Cycle Time and a Cycle
Latency sample, normally when its third scan starts, every min, max and average of that task is
`null`, Dispatch Delay and Wake Latency included, while its Scan Count and Overruns show the real
counts. A dispatcher metric with no sample yet is `null`. The statistics are returned by the `STATS`
command on the Unix socket and by `GET /api/status?include_stats=true`.

## Error Handling

### REST API Server
- Build failures tracked in `BuildStatus` enum
- Compilation logs streamed to OpenPLC Editor
- Graceful degradation on runtime disconnection

### PLC Runtime
- Signal handling for SIGINT (graceful shutdown)
- State transitions validated before execution
- Plugin failures isolated from core runtime
- Watchdog stops a stuck program, and exits for a safe-mode restart when it cannot

## Directory Structure

```
openplc-runtime/
├── webserver/              # Flask REST API server
│   ├── app.py             # Main application entry
│   ├── restapi.py         # REST API blueprint
│   ├── debug_websocket.py # WebSocket debug interface
│   ├── unixclient.py      # Unix socket client
│   ├── plcapp_management.py # Build orchestration
│   ├── runtimemanager.py  # Runtime process control
│   ├── credentials.py     # TLS certificate generation
│   └── config.py          # Configuration management
├── core/
│   ├── src/plc_app/       # PLC runtime source
│   │   ├── plc_main.c     # Main entry point
│   │   ├── plc_state_manager.cpp/h # State management
│   │   ├── unix_socket.c/h # IPC server
│   │   ├── debug_handler.c/h # Debug protocol
│   │   └── utils/         # Utilities (log, watchdog, timing)
│   ├── src/drivers/       # Plugin driver system
│   └── generated/         # Generated PLC code (runtime)
├── scripts/               # Build and management scripts
│   ├── compile.sh         # Compile PLC program
│   ├── compile-clean.sh   # Clean and rename library
│   └── manage_plugin_venvs.sh # Plugin venv management
├── build/                 # Compilation output
│   ├── plc_main           # Compiled runtime executable
│   └── libplc_*.so        # Compiled PLC program libraries
└── venvs/                 # Python virtual environments
    ├── runtime/           # Web server venv
    └── {plugin_name}/     # Per-plugin venvs
```

## Deployment Models

### Native Linux
- Direct installation via `install.sh`
- System dependencies installed via package manager
- Runtime started with `start_openplc.sh`

### Docker Container
- Official image: `ghcr.io/autonomy-logic/openplc-runtime:latest`
- Multi-architecture support: amd64, arm64, armv7
- Persistent volume required for `/var/run/runtime`
- Port 8443 exposed for HTTPS access

### Development
- Local build with CMake
- Development container with test environment
- Pre-commit hooks for code quality

## Related Documentation

- [Editor Integration](EDITOR_INTEGRATION.md) - How OpenPLC Editor connects to runtime
- [Compilation Flow](COMPILATION_FLOW.md) - Build pipeline details
- [API Reference](API.md) - REST endpoints and responses
- [Debug Protocol](DEBUG_PROTOCOL.md) - WebSocket debug interface
- [Plugin System](PLUGIN_VENV_GUIDE.md) - Hardware I/O plugins
- [Security](SECURITY.md) - Authentication and file validation
- [Docker Deployment](DOCKER.md) - Container usage
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
