# OpenPLC Runtime v4

OpenPLC Runtime v4 is a headless industrial Programmable Logic Controller (PLC) runtime that can run IEC 61131-3 programs on standard computing hardware. It is designed to be controlled by the [OpenPLC Editor v4](https://github.com/Autonomy-Logic/openplc-editor) desktop application via a REST API or via the [Autonomy Edge Cloud](https://autonomy-edge.com)

## OpenPLC Runtime Components

OpenPLC Runtime v4 consists of two main components:

1. **REST API Server (Python/Flask)** - HTTPS interface on port 8443 for the OpenPLC Editor to upload programs, monitor compilation, and control execution
2. **PLC Runtime Core (C/C++)** - Real-time execution engine with deterministic scan cycles

The runtime executes programs created in the OpenPLC Editor, supporting IEC 61131-3 programming languages (Ladder Logic, Structured Text, Function Block Diagram, etc.).

## Quick Start

### Docker (Recommended)

The fastest way to get started:

```bash
docker pull ghcr.io/autonomy-logic/openplc-runtime:latest

docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  --cap-add=SYS_NICE \
  --cap-add=SYS_RESOURCE \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest
```

The runtime will start and listen on port 8443 for connections from the OpenPLC Editor. **Do not open https://localhost:8443 in a browser** - there is no web interface there as there was on the v3 runtime. Instead, open the OpenPLC Editor desktop application and configure the runtime IP address and credentials to connect.

**Prebuilt Binaries:** amd64, arm64, armv7

### Linux Installation

For native Linux installation:

```bash
# Clone repository
git clone https://github.com/Autonomy-Logic/openplc-runtime.git
cd openplc-runtime
git checkout development

# Install dependencies and compile
sudo ./install.sh

# Start the runtime
sudo ./start_openplc.sh
```

The runtime will start and listen on port 8443. Connect to it from the OpenPLC Editor desktop application by configuring the runtime IP address and logging in from the Editor.

**Supported Distributions:** Ubuntu, Debian, Fedora, CentOS, RHEL

**Requirements:**
- GCC compiler
- CMake
- Python 3.8+
- Root privileges (for real-time scheduling and port binding)

## How It Works

1. **Create Program** - Design your PLC program in OpenPLC Editor v4 using Ladder Logic, FBD, ST, or other IEC 61131-3 languages
2. **Compile in Editor** - The Editor compiles locally (JSON → XML → ST → C files) and packages sources into program.zip
3. **Upload** - The Editor uploads the ZIP file to the runtime via HTTPS POST to `/api/upload-file` with JWT authentication
4. **Compile on Runtime** - Runtime validates, extracts, and compiles the program using CMake (Editor polls `/api/compilation-status` for progress)
5. **Control** - The Editor controls PLC execution via `/api/start-plc` and `/api/stop-plc` endpoints
6. **Debug** - The Editor connects to the WebSocket debug interface at `/api/debug` for real-time variable monitoring

The runtime compiles uploaded programs into shared libraries and loads them dynamically. The PLC core executes with real-time priority (SCHED_FIFO) for deterministic timing.

**For detailed editor-runtime integration, see [docs/EDITOR_INTEGRATION.md](docs/EDITOR_INTEGRATION.md).**

## Key Features

- **Headless Service** - Controlled by OpenPLC Editor via REST API
- **Real-Time Execution** - Deterministic scan cycles with SCHED_FIFO priority scheduling
- **WebSocket Debug Interface** - Real-time variable inspection and forcing via Editor
- **Plugin System** - Extensible I/O drivers for various hardware platforms
- **Multi-Architecture** - Prebuilt binaries for x86_64, ARM64, and ARM32 platforms. Can run on virtually anything that can run Linux 
- **Docker Support** - Official multi-arch container images
- **Security** - TLS encryption, JWT authentication, comprehensive file upload validation

## Architecture

OpenPLC Runtime v4 uses a dual-process architecture:

- **REST API Server Process** - Flask application managing the REST API and WebSocket debug interface for OpenPLC Editor communication
- **PLC Runtime Process** - C/C++ real-time engine executing PLC programs with SCHED_FIFO priority

The processes communicate via Unix domain sockets (`/run/runtime/plc_runtime.socket`) for command/control and log streaming.

**For detailed architecture information, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).**

## Documentation

### Getting Started
- [Editor Integration](docs/EDITOR_INTEGRATION.md) - How OpenPLC Editor communicates with the runtime
- [Docker Deployment](docs/DOCKER.md) - Container usage and configuration
- [Compilation Flow](docs/COMPILATION_FLOW.md) - How programs are built and loaded
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues and solutions

### Advanced Topics
- [Architecture](docs/ARCHITECTURE.md) - System design and components
- [API Reference](docs/API.md) - Internal REST API for OpenPLC Editor
- [Debug Protocol](docs/DEBUG_PROTOCOL.md) - WebSocket debug interface
- [Security](docs/SECURITY.md) - Authentication, TLS, and file validation
- [Plugin System](docs/PLUGIN_VENV_GUIDE.md) - Hardware I/O plugins
- [Development Guide](docs/DEVELOPMENT.md) - Contributing and development setup

### Additional Resources
- [Plugin Driver Documentation](core/src/drivers/README.md) - Plugin driver layer details
- [WebSocket Debug Details](webserver/DEBUG_WEBSOCKET.md) - Debug WebSocket implementation
- [Logger Module](webserver/logger/logger_module_documentation.md) - Logging subsystem

## REST API

The runtime provides an internal REST API used by the OpenPLC Editor. The API is not intended for direct end-user interaction but can be used for advanced integration or diagnostics.

**Authentication Required:** All endpoints except `/api/create-user` (first user), `/api/login`, and `/api/get-users-info` require JWT authentication via `Authorization: Bearer <token>` header.

**Key Endpoints:**
- `POST /api/create-user` - Create user account
- `POST /api/login` - Login and receive JWT token
- `POST /api/upload-file` - Upload program ZIP (multipart/form-data)
- `GET /api/compilation-status` - Get compilation status and logs
- `GET /api/status` - Get PLC runtime status
- `GET /api/start-plc` - Start PLC execution
- `GET /api/stop-plc` - Stop PLC execution
- `GET /api/runtime-logs` - Get runtime logs

**For complete API documentation with authentication flow and examples, see [docs/API.md](docs/API.md).**

## WebSocket Debug Interface

The OpenPLC Editor uses a WebSocket interface for real-time debugging. Advanced integrators can also use this interface:

```javascript
import { io } from 'socket.io-client';

// Connect with JWT authentication
const socket = io('https://localhost:8443', {
  path: '/socket.io',
  transports: ['websocket'],
  auth: { token: jwt_token },
  rejectUnauthorized: false  // For self-signed certificates
});

// Listen for connection
socket.on('connect', () => {
  console.log('Connected to runtime');
});

// Send debug command (hex-encoded)
socket.emit('debug_command', {
  command: '44 00 03 00 00 00 01 00 02'  // Get variables 0, 1, 2
});

// Receive response
socket.on('debug_response', (response) => {
  console.log(response.data);
});
```

**For complete debug protocol documentation, see [docs/DEBUG_PROTOCOL.md](docs/DEBUG_PROTOCOL.md) and [webserver/DEBUG_WEBSOCKET.md](webserver/DEBUG_WEBSOCKET.md).**

## Docker Usage

### Basic Usage

```bash
# Pull image
docker pull ghcr.io/autonomy-logic/openplc-runtime:latest

# Run with persistent storage
docker run -d \
  --name openplc-runtime \
  -p 8443:8443 \
  -v openplc-runtime-data:/var/run/runtime \
  ghcr.io/autonomy-logic/openplc-runtime:latest

# View logs
docker logs -f openplc-runtime

# Stop container
docker stop openplc-runtime
```

### Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  openplc-runtime:
    image: ghcr.io/autonomy-logic/openplc-runtime:latest
    container_name: openplc-runtime
    ports:
      - "8443:8443"
    volumes:
      - openplc-runtime-data:/var/run/runtime
    restart: unless-stopped

volumes:
  openplc-runtime-data:
```

Start with: `docker-compose up -d`

**For complete Docker documentation, see [docs/DOCKER.md](docs/DOCKER.md).**

## Building from Source

### Prerequisites

Install dependencies:

```bash
# Ubuntu/Debian
sudo apt-get install build-essential gcc make cmake \
  python3-dev python3-pip python3-venv

# Fedora/RHEL/CentOS
sudo dnf install gcc gcc-c++ make cmake \
  python3 python3-devel python3-pip python3-venv
```

### Build Steps

```bash
# Clone repository
git clone https://github.com/Autonomy-Logic/openplc-runtime.git
cd openplc-runtime
git checkout development

# Run installation script
sudo ./install.sh
```

The installation script will:
1. Detect your Linux distribution
2. Install system dependencies
3. Create Python virtual environment at `venvs/runtime/`
4. Install Python dependencies
5. Compile the PLC runtime core with CMake

### Manual Build

For manual compilation:

```bash
# Create Python virtual environment
python3 -m venv venvs/runtime
source venvs/runtime/bin/activate
pip install -r requirements.txt
pip install -e .

# Compile runtime core
mkdir -p build
cd build
cmake ..
make -j$(nproc)
cd ..
```

### Starting the Runtime

```bash
sudo ./start_openplc.sh
```

The startup script will:
1. Check installation status
2. Setup plugin virtual environments (if plugins have requirements.txt)
3. Activate runtime virtual environment
4. Start the web server (which automatically manages the PLC runtime process)

**Note:** Root privileges are required for:
- Real-time scheduling (SCHED_FIFO priority)
- Binding to port 8443
- Creating Unix domain sockets in `/run/runtime/`

## Plugin System

OpenPLC Runtime supports plugins for hardware I/O:

**Plugin Types:**
- Python plugins (with isolated virtual environments)
- C/C++ plugins

**Configuration:** Edit `plugins.conf` to enable/disable plugins

**Example:**
```
# name,path,enabled,type,config_path,venv_path
modbus_slave,./core/src/drivers/plugins/python/modbus_slave_plugin/simple_modbus.py,1,0,./config.json,./venvs/modbus_slave
```

**Managing Plugin Virtual Environments:**
```bash
# Create venv for plugin
sudo bash scripts/manage_plugin_venvs.sh create plugin_name

# Install dependencies
sudo bash scripts/manage_plugin_venvs.sh install plugin_name

# List all plugin venvs
sudo bash scripts/manage_plugin_venvs.sh list
```

**For complete plugin documentation, see [docs/PLUGIN_VENV_GUIDE.md](docs/PLUGIN_VENV_GUIDE.md) and [core/src/drivers/README.md](core/src/drivers/README.md).**

## Security

### TLS/HTTPS

The runtime automatically generates self-signed TLS certificates on first run:
- Certificate: `webserver/certOPENPLC.pem`
- Private key: `webserver/keyOPENPLC.pem`

The OpenPLC Editor handles self-signed certificates automatically. For advanced integrators using the API directly, you'll need to configure your HTTP client to accept self-signed certificates (e.g., `curl -k` or `rejectUnauthorized: false`).

### File Upload Security

Uploaded ZIP files undergo comprehensive security validation:
- Path traversal prevention
- Size limits (10 MB per file, 50 MB total)
- ZIP bomb detection (compression ratio check)
- Extension whitelist (blocks .exe, .dll, .sh, .bat, .js, .vbs, .scr)
- macOS metadata removal

### Authentication

The runtime uses JWT-based authentication:
- First user creation via `POST /api/create-user` (no auth required)
- Login via `POST /api/login` returns JWT access token
- All subsequent requests require `Authorization: Bearer <token>` header
- Secrets stored in `/var/run/runtime/.env`
- Password hashing with PBKDF2-SHA256 (600,000 iterations), salt, and pepper

**For complete security documentation, see [docs/SECURITY.md](docs/SECURITY.md).**

## Troubleshooting

### Common Issues

**Cannot connect from OpenPLC Editor:**
```bash
# Check if runtime is running
ps aux | grep python3 | grep webserver

# Check if port 8443 is listening
sudo netstat -tlnp | grep 8443

# Check firewall
sudo ufw status
```

**Compilation failed:**
```bash
# Check runtime logs
sudo journalctl -u openplc-runtime -n 50

# Check if runtime directory exists
ls -la /run/runtime/
```

**Permission errors:**
```bash
# Ensure running with sudo
sudo ./start_openplc.sh

# Check socket directory permissions
ls -la /run/runtime/
```

**For complete troubleshooting guide, see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).**

## Development

### Setup Development Environment

```bash
git clone https://github.com/Autonomy-Logic/openplc-runtime.git
cd openplc-runtime
git checkout development
sudo ./install.sh
```

### Running Tests

```bash
bash scripts/run-pytest.sh
```

### Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

### Code Style

- **C/C++:** Follow existing style, 4-space indentation, no tabs
- **Python:** Follow PEP 8, type hints, docstrings
- **No emojis** anywhere in code, comments, or documentation (project standard)

### Contributing

1. Fork the repository
2. Create a feature branch from `development`
3. Make your changes
4. Submit a pull request

**For complete development guide, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).**

## Project Structure

```
openplc-runtime/
├── webserver/              # Flask web application (Python)
│   ├── app.py             # Main application entry
│   ├── restapi.py         # REST API blueprint
│   ├── debug_websocket.py # WebSocket debug interface
│   └── ...
├── core/
│   ├── src/plc_app/       # PLC runtime source (C/C++)
│   │   ├── plc_main.c     # Main entry point
│   │   ├── plc_state_manager.c/h # State management
│   │   ├── unix_socket.c/h # IPC server
│   │   └── utils/         # Utilities (log, watchdog, timing)
│   └── src/drivers/       # Plugin driver system
├── scripts/               # Build and management scripts
│   ├── compile.sh         # Compile PLC program
│   ├── compile-clean.sh   # Clean and rename library
│   └── manage_plugin_venvs.sh # Plugin venv management
├── build/                 # Compilation output
│   ├── plc_main           # Compiled runtime executable
│   └── libplc_*.so        # Compiled PLC program libraries
├── docs/                  # Documentation
├── CMakeLists.txt         # CMake build configuration
├── Dockerfile             # Container definition
├── install.sh             # Installation script
└── start_openplc.sh       # Startup script
```

## System Requirements

### Minimum Requirements
- **CPU:** 1 GHz single-core
- **RAM:** 512 MB
- **Disk:** 500 MB free space
- **OS:** Linux (Ubuntu 20.04+, Debian 11+, Fedora, CentOS, RHEL)

### Recommended Requirements
- **CPU:** 2 GHz dual-core or better
- **RAM:** 1 GB or more
- **Disk:** 1 GB free space
- **OS:** Ubuntu 22.04 LTS or Debian 12

### Real-Time Performance
For deterministic real-time performance:
- Dedicated CPU core recommended
- Real-time kernel (PREEMPT_RT) optional but beneficial
- Minimal background processes
- Root privileges for SCHED_FIFO scheduling

## License

See LICENSE file for details.

## Support

- **Issues:** https://github.com/Autonomy-Logic/openplc-runtime/issues
- **Documentation:** See `docs/` directory
- **OpenPLC Editor:** https://github.com/Autonomy-Logic/openplc-editor

## Related Projects

- [OpenPLC Editor v4](https://github.com/Autonomy-Logic/openplc-editor) - PLC programming environment
- [OpenPLC v3](https://github.com/thiagoralves/OpenPLC_v3) - Previous version

## Acknowledgments

OpenPLC Runtime v4 is developed and maintained by Autonomy Logic.
