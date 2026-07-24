# Development Guide

## Overview

This guide covers setting up a development environment for OpenPLC Runtime v4, understanding the codebase structure, and contributing to the project.

## Development Environment Setup

### Prerequisites

**Required:**
- Linux (any distribution using apt, dnf, yum, pacman, zypper, or apk)
- GCC/G++ compiler
- CMake 3.28+ (install.sh installs a newer version automatically if the system one is older)
- Python 3.10+
- Git

**Optional:**
- Docker (for containerized development)
- VSCode or other IDE
- GDB (for debugging)

### Clone Repository

```bash
git clone https://github.com/Autonomy-Logic/openplc-runtime.git
cd openplc-runtime
git checkout development
```

### Install Dependencies

Run the installation script:

```bash
sudo ./install.sh
```

This will:
1. Detect your system package manager
2. Install system dependencies (gcc, cmake, python3, etc.)
3. Create Python virtual environment at `venvs/runtime/`
4. Install Python dependencies from `requirements.txt`
5. Compile the PLC runtime core (`build/plc_main`) and native plugins
6. Install and start a systemd service (`openplc-runtime`) when systemd is available

### Manual Dependency Installation

If you prefer manual installation:

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y build-essential gcc make cmake \
    python3-dev python3-pip python3-venv pkg-config
```

**Fedora/RHEL/CentOS:**
```bash
sudo dnf install -y gcc gcc-c++ make cmake \
    python3 python3-devel python3-pip python3-venv
```

**Python Environment:**
```bash
python3 -m venv venvs/runtime
source venvs/runtime/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
pip install -e .
```

**Compile Runtime:**
```bash
mkdir -p build
cd build
cmake ..
make -j$(nproc)
cd ..
```

## Project Structure

### Directory Layout

```
openplc-runtime/
├── webserver/              # Flask web application (Python)
│   ├── app.py             # Main application entry
│   ├── restapi.py         # REST API blueprint
│   ├── debug_websocket.py # WebSocket debug interface
│   ├── unixclient.py      # Unix socket client
│   ├── plcapp_management.py # Build orchestration
│   ├── runtimemanager.py  # Runtime process control
│   ├── credentials.py     # TLS certificate generation
│   ├── config.py          # Configuration management
│   └── logger/            # Logging subsystem
├── core/
│   ├── src/
│   │   ├── plc_app/       # PLC runtime source (C/C++)
│   │   │   ├── plc_main.c # Main entry point
│   │   │   ├── plc_state_manager.cpp/h # State management
│   │   │   ├── unix_socket.c/h # IPC server
│   │   │   ├── debug_handler.c/h # Debug protocol
│   │   │   ├── plcapp_manager.c/h # Program loading
│   │   │   ├── scan_cycle_manager.c/h # Scan cycle execution
│   │   │   └── utils/     # Utilities (log, watchdog, timing)
│   │   └── drivers/       # Plugin driver system
│   │       ├── plugin_driver.c/h # Plugin management
│   │       ├── plugin_config.c/h # Configuration parsing
│   │       └── plugins/   # Plugin implementations
│   └── generated/         # Generated PLC code (runtime)
├── scripts/               # Build and management scripts
│   ├── compile.sh         # Compile PLC program
│   ├── compile-clean.sh   # Clean and rename library
│   ├── manage_plugin_venvs.sh # Plugin venv management
│   ├── build-docker-image.sh # Production Docker build
│   ├── build-docker-image-dev.sh # Development Docker build
│   ├── run-image.sh       # Run production container
│   └── run-image-dev.sh   # Run development container
├── build/                 # Compilation output
│   ├── plc_main           # Compiled runtime executable
│   └── libplc_*.so        # Compiled PLC program libraries
├── venvs/                 # Python virtual environments
│   ├── runtime/           # Web server venv
│   └── {plugin_name}/     # Per-plugin venvs
├── docs/                  # Documentation
├── tests/                 # Test suite
├── .github/workflows/     # CI/CD pipelines
├── CMakeLists.txt         # CMake build configuration
├── Dockerfile             # Production container definition
├── requirements.txt       # Python dependencies
├── install.sh             # Installation script
└── start_openplc.sh       # Startup script
```

### Key Files

**Build Configuration:**
- `CMakeLists.txt` - CMake configuration for C/C++ compilation
- `requirements.txt` - Python dependencies
- `pyproject.toml` - Python package configuration

**Entry Points:**
- `webserver/app.py` - Web server main entry
- `core/src/plc_app/plc_main.c` - PLC runtime main entry
- `start_openplc.sh` - Startup script

**Configuration:**
- `plugins.conf` - Plugin configuration
- `.env` in the persistent data directory (`/var/lib/openplc-runtime` on native Linux, `/var/run/runtime` in Docker) - Runtime environment variables

## Running for Development

### Start Runtime

```bash
sudo ./start_openplc.sh
```

This will:
1. Check installation status
2. Setup plugin virtual environments
3. Activate runtime virtual environment
4. Start the web server (which manages the PLC runtime)

**Note:** The runtime will listen on https://localhost:8443 for connections from the OpenPLC Editor. Do not open this in a browser - there is no web interface. Connect from the OpenPLC Editor desktop application.

### Run Web Server Only

For web server development without the full startup script:

```bash
source venvs/runtime/bin/activate
sudo python3 -m webserver.app
```

### Run PLC Runtime Only

For PLC runtime development:

```bash
sudo ./build/plc_main
```

**Options:**
- `--print-logs` - Print logs to stdout in addition to socket
- `--print-debug` - Enable debug-level logging
- `--safe-mode` - Start in safe mode

### Development Mode

For faster iteration during development:

1. **Disable real-time scheduling** (edit `core/src/plc_app/utils/utils.c`):
   ```c
   // Comment out set_realtime_priority() call
   ```

2. **Enable debug symbols** (edit `CXXFLAGS` in `scripts/Makefile.strucpp`):
   ```make
   CXXFLAGS := -std=c++17 -g -O0 -pipe -fPIC ...  # Debug symbols, no optimization
   ```

3. **Increase log verbosity** (run with the debug flag):
   ```bash
   sudo ./build/plc_main --print-logs --print-debug
   ```

## Code Style and Standards

### C/C++ Code

**Style Guidelines:**
- Follow existing code style
- Use 4-space indentation
- No tabs
- Function names: `snake_case`
- Type names: `snake_case_t` suffix
- Macro names: `UPPER_CASE`
- No emojis anywhere in code or comments

**Best Practices:**
- Check return values
- Free allocated memory
- Close file descriptors
- Use const where appropriate
- Document complex logic
- Avoid global variables when possible

**Example:**
```c
int initialize_system(void)
{
    int result = 0;

    // Initialize subsystem
    if (subsystem_init() != 0)
    {
        log_error("Failed to initialize subsystem");
        return -1;
    }

    log_info("System initialized successfully");
    return 0;
}
```

### Python Code

**Style Guidelines:**
- Follow PEP 8
- Use 4-space indentation
- Type hints where appropriate
- Docstrings for public functions
- No emojis anywhere in code or comments

**Best Practices:**
- Use context managers for resources
- Handle exceptions appropriately
- Use logging instead of print
- Validate inputs
- Document complex logic

**Example:**
```python
def process_upload(file_path: str) -> tuple[bool, str]:
    """
    Process uploaded PLC program file.

    Args:
        file_path: Path to uploaded ZIP file

    Returns:
        Tuple of (success, error_message)
    """
    try:
        safe, valid_files = analyze_zip(file_path)
        if not safe:
            return False, "ZIP validation failed"

        safe_extract(file_path, "core/generated", valid_files)
        return True, ""

    except Exception as e:
        logger.error(f"Upload processing failed: {e}")
        return False, str(e)
```

## Building and Testing

### Build Runtime Core

```bash
cd build
cmake ..
make -j$(nproc)
```

### Clean Build

```bash
rm -rf build/
mkdir build
cd build
cmake ..
make -j$(nproc)
```

### Run Tests

```bash
# Sets up the test venv and runs the suite
bash scripts/run-pytest.sh
```

### Development Container

Build and run development container:

```bash
bash scripts/build-docker-image-dev.sh
bash scripts/run-image-dev.sh
```

## Debugging

### GDB Debugging

Debug the PLC runtime:

```bash
sudo gdb ./build/plc_main
(gdb) run --print-logs
(gdb) break plc_main.c:68
(gdb) continue
(gdb) backtrace
```

### Python Debugging

Debug the web server:

```bash
source venvs/runtime/bin/activate
sudo python3 -m pdb -m webserver.app
```

### Log Analysis

View runtime logs:

```bash
curl -k https://localhost:8443/api/runtime-logs | jq
```

View compilation logs:

```bash
curl -k https://localhost:8443/api/compilation-status | jq
```

### Network Debugging

Monitor API calls:

```bash
# Terminal 1: Start runtime
sudo ./start_openplc.sh

# Terminal 2: Monitor traffic
sudo tcpdump -i lo -A 'port 8443'
```

## Pre-commit Hooks

### Setup

Install pre-commit:

```bash
pip install pre-commit
pre-commit install
```

### Run Manually

```bash
pre-commit run --all-files
```

### Skip Hooks

For quick fixes (use sparingly):

```bash
git commit --no-verify -m "Quick fix"
```

## Contributing

### Workflow

1. **Fork the repository** on GitHub
2. **Clone your fork:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/openplc-runtime.git
   cd openplc-runtime
   git remote add upstream https://github.com/Autonomy-Logic/openplc-runtime.git
   ```

3. **Create a feature branch:**
   ```bash
   git checkout development
   git pull upstream development
   git checkout -b feature/my-feature
   ```

4. **Make changes and commit:**
   ```bash
   git add .
   git commit -m "Add my feature"
   ```

5. **Push to your fork:**
   ```bash
   git push origin feature/my-feature
   ```

6. **Create Pull Request** on GitHub

### Commit Messages

Follow conventional commit format:

```
type(scope): subject

body

footer
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `docs` - Documentation changes
- `style` - Code style changes (formatting)
- `refactor` - Code refactoring
- `test` - Test changes
- `chore` - Build/tooling changes

**Example:**
```
feat(webserver): add rate limiting to API endpoints

Implement rate limiting using Flask-Limiter to prevent
abuse of API endpoints. Limits are configurable via
environment variables.

RTOP-123
```

### Pull Request Guidelines

**Before submitting:**
- Ensure all tests pass
- Update documentation
- Follow code style guidelines
- Add tests for new features
- Rebase on latest development branch

**PR Description should include:**
- Summary of changes
- Motivation and context
- Testing performed
- Screenshots (if UI changes)
- Related issues

## Architecture Deep Dive

### Inter-Process Communication

The web server and PLC runtime communicate via Unix domain sockets:

**Command Socket:** `/run/runtime/plc_runtime.socket`
- Synchronous request-response
- Text-based protocol
- Commands: START, STOP, STATUS, PING, STATS, DEBUG:<hex>, PLUGIN_CMD:<json>

**Log Socket:** `/run/runtime/log_runtime.socket`
- Asynchronous log streaming
- Binary protocol with structured messages
- Buffered when socket unavailable

### State Management

PLC lifecycle states:

```
EMPTY → INIT → RUNNING ⟷ STOPPED → ERROR
```

State transitions are validated and logged. See `core/src/plc_app/plc_state_manager.cpp`.

### Plugin System

Plugins extend I/O capabilities. Both Python and native C/C++ plugins are supported:

1. **Configuration:** `plugins.conf` defines enabled plugins (type 0=Python, 1=Native)
2. **Loading:** `plugin_driver_load_config()` parses configuration and loads symbols
3. **Initialization:** `plugin_driver_init()` calls each plugin's `init()` function
4. **Execution:** `plugin_driver_start()` calls each plugin's `start_loop()` function
5. **Cleanup:** `plugin_driver_destroy()` calls `cleanup()` and releases resources

See `core/src/drivers/README.md` for detailed plugin development documentation.

### Debug Protocol

WebSocket-based debug interface:

1. **Authentication:** JWT token required
2. **Commands:** Hex-encoded binary protocol
3. **Function Codes:** 0x41-0x45 for different operations
4. **Responses:** Hex-encoded with 0x7E success indicator

See [Debug Protocol](DEBUG_PROTOCOL.md) for details.

## Performance Profiling

### CPU Profiling

Profile the PLC runtime:

```bash
sudo perf record -g ./build/plc_main
sudo perf report
```

### Memory Profiling

Check for memory leaks:

```bash
valgrind --leak-check=full --show-leak-kinds=all ./build/plc_main
```

### Timing Analysis

Runtime tracks detailed timing statistics:
- Scan time (PLC logic execution)
- Cycle time (total including sleep)
- Cycle latency (deviation from target)
- Overruns (cycles exceeding target)

View stats in runtime logs every 5 seconds.

## Documentation

### Building Documentation

Documentation is written in Markdown and located in `docs/`.

### Adding Documentation

When adding features:
1. Update relevant docs in `docs/`
2. Update README.md if user-facing
3. Add inline code comments
4. Update API documentation if endpoints changed

## Release Process

### Version Numbering

Follow Semantic Versioning (SemVer):
- MAJOR.MINOR.PATCH
- Example: 4.0.1

### Creating a Release

1. Update version numbers
2. Update CHANGELOG.md
3. Tag release:
   ```bash
   git tag -a v4.0.1 -m "Release v4.0.1"
   git push origin v4.0.1
   ```
4. GitHub Actions builds and publishes Docker images

## Troubleshooting Development Issues

### Build Errors

**CMake cache issues:**
```bash
rm -rf build/
mkdir build && cd build && cmake .. && make
```

**Missing dependencies:**
```bash
sudo ./install.sh
```

### Runtime Crashes

**Enable core dumps:**
```bash
ulimit -c unlimited
sudo ./build/plc_main
# After crash:
gdb ./build/plc_main core
```

### Python Import Errors

**Reinstall in development mode:**
```bash
source venvs/runtime/bin/activate
pip install -e .
```

## Related Documentation

- [Editor Integration](EDITOR_INTEGRATION.md) - How OpenPLC Editor connects to runtime
- [Architecture](ARCHITECTURE.md) - System architecture
- [API Reference](API.md) - REST API documentation
- [Compilation Flow](COMPILATION_FLOW.md) - Build pipeline
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
- [Security](SECURITY.md) - Security considerations
