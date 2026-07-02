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
- **ERROR**: Recoverable error state

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
3. **PLC Cycle Thread**: Executes scan cycles with real-time priority
4. **Stats Thread**: Logs performance metrics
5. **Watchdog Thread**: Monitors heartbeat and terminates on hang
6. **Log Thread**: Manages log socket connection

## Real-Time Execution

The PLC cycle thread runs with SCHED_FIFO priority to ensure deterministic timing:

1. **Read Inputs**: Plugin drivers read from hardware
2. **Execute Logic**: Run compiled PLC program tasks (loaded via the `strucpp_get_config()` entry point)
3. **Write Outputs**: Plugin drivers write to hardware
4. **Sleep Until Next Cycle**: Precise timing using `clock_nanosleep()`

**Timing Configuration:**
- Scan cycle duration: Defined per task by the task interval in the compiled program configuration
- Timing stats tracked: min/max/avg scan time, cycle time, latency, overruns

## Plugin System

The runtime supports dynamically loaded plugins for hardware I/O:

- **Plugin Types**: Python and C/C++
- **Configuration**: `plugins.conf` file
- **Virtual Environments**: Isolated Python dependencies per plugin
- **Buffer Protection**: Mutex-protected I/O buffers during scan cycles

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

The watchdog monitors PLC health by tracking the `plc_heartbeat` atomic variable:

- **Update Frequency**: Every scan cycle
- **Timeout**: 2 seconds without update
- **Action**: Terminates process if PLC becomes unresponsive
- **State Awareness**: Only monitors during RUNNING state

**Implementation:** `core/src/plc_app/utils/watchdog.c`

## Performance Monitoring

The runtime tracks detailed timing statistics:

- **Scan Count**: Total cycles executed
- **Scan Time**: Time spent executing PLC logic
- **Cycle Time**: Total time per cycle (including sleep)
- **Cycle Latency**: Deviation from target timing
- **Overruns**: Cycles that exceeded target duration

Stats are logged every 5 seconds via the stats thread.

## Error Handling

### REST API Server
- Build failures tracked in `BuildStatus` enum
- Compilation logs streamed to OpenPLC Editor
- Graceful degradation on runtime disconnection

### PLC Runtime
- Signal handling for SIGINT (graceful shutdown)
- State transitions validated before execution
- Plugin failures isolated from core runtime
- Watchdog ensures process termination on hang

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
