#!/bin/bash
# Setup script for EtherCAT Discovery Service virtual environment
# Creates venvs/discovery/ and installs pysoem for network scanning
# Configures CAP_NET_RAW capability for raw socket access (required by EtherCAT)
#
# Note: If a venv already exists, it will be automatically recreated.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="$PROJECT_ROOT/venvs/discovery"
REQUIREMENTS_FILE="$SCRIPT_DIR/discovery/requirements.txt"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Detect if running on MSYS2/MinGW/Cygwin (Windows)
is_msys2() {
    case "$(uname -s)" in
        MSYS*|MINGW*|CYGWIN*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# Check if running as root
is_root() {
    [ "$(id -u)" -eq 0 ]
}

# Check if Python3 is available
check_python() {
    if ! command -v python3 &> /dev/null; then
        log_error "python3 is not installed or not in PATH"
        exit 1
    fi

    local python_version=$(python3 --version | cut -d' ' -f2)
    log_info "Using Python version: $python_version"
}

# Configure CAP_NET_RAW capability on the venv Python binary
# This allows pysoem to use raw sockets without running as root
configure_capabilities() {
    local python_bin="$VENV_PATH/bin/python3"

    # Skip on non-Linux systems
    if is_msys2; then
        log_info "Skipping capability configuration on Windows/MSYS2"
        return 0
    fi

    # Check if setcap is available
    if ! command -v setcap &> /dev/null; then
        log_warning "setcap not found. Install libcap2-bin package."
        log_warning "Without CAP_NET_RAW, discovery must run as root."
        log_info "To install: sudo apt-get install libcap2-bin"
        return 0
    fi

    # Check if we have permission to set capabilities
    if ! is_root; then
        log_warning "Not running as root. Cannot set CAP_NET_RAW capability."
        log_warning "Run this script with sudo to enable non-root EtherCAT scanning."
        log_info "Alternatively, always run the OpenPLC Runtime as root."
        return 0
    fi

    # Resolve symlink to get the actual Python binary
    local real_python_bin=$(readlink -f "$python_bin")

    log_info "Configuring CAP_NET_RAW capability on: $real_python_bin"

    # Set CAP_NET_RAW capability
    # cap_net_raw: allows raw socket operations (required for EtherCAT)
    # +ep: effective and permitted (active when process runs)
    if setcap cap_net_raw+ep "$real_python_bin" 2>/dev/null; then
        log_success "CAP_NET_RAW capability configured successfully"

        # Verify the capability was set
        if command -v getcap &> /dev/null; then
            local caps=$(getcap "$real_python_bin")
            log_info "Verification: $caps"
        fi
    else
        log_warning "Failed to set CAP_NET_RAW capability"
        log_warning "EtherCAT discovery will require root privileges"
    fi
}

# Main setup function
setup_discovery_venv() {
    log_info "Setting up EtherCAT Discovery Service venv..."

    # Create venvs directory if needed
    mkdir -p "$(dirname "$VENV_PATH")"

    # Check if venv already exists
    if [ -d "$VENV_PATH" ]; then
        log_info "Discovery venv already exists at: $VENV_PATH"
        log_info "Removing existing venv to recreate..."
        rm -rf "$VENV_PATH"
    fi

    # Create virtual environment
    log_info "Creating Python virtual environment at: $VENV_PATH"
    if is_msys2; then
        log_info "MSYS2 detected: using --system-site-packages"
        python3 -m venv --system-site-packages "$VENV_PATH"
    else
        python3 -m venv "$VENV_PATH"
    fi

    # Upgrade pip
    log_info "Upgrading pip..."
    "$VENV_PATH/bin/pip" install --upgrade pip

    # Install requirements
    if [ -f "$REQUIREMENTS_FILE" ]; then
        log_info "Installing dependencies from: $REQUIREMENTS_FILE"
        "$VENV_PATH/bin/pip" install -r "$REQUIREMENTS_FILE"
        log_success "Dependencies installed successfully"
    else
        log_error "Requirements file not found: $REQUIREMENTS_FILE"
        exit 1
    fi

    # Configure CAP_NET_RAW capability
    configure_capabilities

    log_success "Discovery venv created successfully at: $VENV_PATH"
    echo ""
    log_info "The EtherCAT discovery service is now available."
    log_info ""
    log_info "REST endpoints:"
    log_info "  GET  /api/discovery/ethercat/status     - Check service status"
    log_info "  GET  /api/discovery/ethercat/interfaces - List network interfaces"
    log_info "  POST /api/discovery/ethercat/scan       - Scan for EtherCAT slaves"
    log_info "  POST /api/discovery/ethercat/validate   - Validate configuration"
    log_info "  POST /api/discovery/ethercat/test       - Test connection to slave"
    echo ""

    if ! is_root; then
        log_warning "Script was not run as root."
        log_warning "To enable non-root EtherCAT scanning, run:"
        log_info "  sudo setcap cap_net_raw+ep $VENV_PATH/bin/python3"
    fi
}

# Run main
check_python
setup_discovery_venv
