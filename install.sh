#!/bin/bash
set -e

# Check for root privileges
check_root() 
{
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root" >&2
        echo "Example: sudo ./install.sh" >&2
        exit 1
    fi
}

# Make sure we are root before proceeding
check_root

# Detect the project root directory
# This works whether the script is called from project root, Docker, or anywhere else
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPLC_DIR="$SCRIPT_DIR"
VENV_DIR="$OPENPLC_DIR/venvs/runtime"
SCRIPTS_DIR="$OPENPLC_DIR/scripts"

# Ensure we're in the project directory
cd "$OPENPLC_DIR"

echo "OpenPLC Runtime Installation"
echo "Project directory: $OPENPLC_DIR"
echo "Working directory: $(pwd)"

install_dependencies() 
{
    source /etc/os-release
    echo "Distro: $ID"

    case "$ID" in
        ubuntu|debian)
            install_deps_apt "$1"
            ;;
        centos)
            if [[ "$VERSION_ID" == 7* ]]; then
                install_deps_yum "$1"
            else
                install_deps_dnf "$1"
            fi
            ;;
        rhel)
            if [[ "$VERSION_ID" == 7* ]]; then
                install_deps_yum "$1"
            else
                install_deps_dnf "$1"
            fi
            ;;
        fedora)
            install_deps_dnf "$1"
            ;;
        *)
            echo "Unsupported Linux distro: $ID" >&2
            return 1
            ;;
    esac
}

# For Ubuntu/Debian
install_deps_apt() { 
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev python3-pip python3-venv \
        gcc \
        make \
        cmake \
    && rm -rf /var/lib/apt/lists/*
}

# For CentOS 7/RHEL 7 (older)
install_deps_yum() {
    yum install -y \
        gcc gcc-c++ make cmake \
        python3 python3-devel python3-pip python3-venv \
        && yum clean all
}

# For Fedora/RHEL 8+/CentOS Stream
install_deps_dnf() {
    dnf install -y \
        gcc gcc-c++ make cmake \
        python3 python3-devel python3-pip python3-venv \
        && dnf clean all
}

compile_plc() {
    echo "Preparing build directory..."
    
    # Always clean build directory for Docker environment or when CMake cache exists
    # This prevents cross-contamination between Linux and Docker builds
    if [ -d "$OPENPLC_DIR/build" ] && [ -f "$OPENPLC_DIR/build/CMakeCache.txt" ]; then
        echo "Cleaning existing build directory to ensure clean build..."
        rm -rf "$OPENPLC_DIR/build"
    fi
    
    # Create build directory
    if ! mkdir -p "$OPENPLC_DIR/build"; then
        echo "ERROR: Failed to create build directory" >&2
        return 1
    fi
    
    cd "$OPENPLC_DIR/build" || {
        echo "ERROR: Failed to change to build directory" >&2
        return 1
    }
    
    echo "Running cmake configuration..."
    if ! cmake ..; then
        echo "ERROR: CMake configuration failed" >&2
        cd "$OPENPLC_DIR"
        return 1
    fi
    
    echo "Compiling with make (using $(nproc) cores)..."
    if ! make -j"$(nproc)"; then
        echo "ERROR: Compilation failed" >&2
        cd "$OPENPLC_DIR"
        return 1
    fi
    
    cd "$OPENPLC_DIR" || {
        echo "ERROR: Failed to return to main directory" >&2
        return 1
    }
    
    echo "SUCCESS: OpenPLC compiled successfully!"
    return 0
}

# Setup runtime directory (needed for both Linux and Docker)
mkdir -p /var/run/runtime
chmod 775 /var/run/runtime 2>/dev/null || true  # Ignore permission errors in Docker

# Make scripts executable
chmod +x "$OPENPLC_DIR/install.sh" 2>/dev/null || true
chmod +x "$OPENPLC_DIR/scripts/"* 2>/dev/null || true
chmod +x "$OPENPLC_DIR/start_openplc.sh" 2>/dev/null || true

install_dependencies
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python3" -m pip install -r "$OPENPLC_DIR/requirements.txt"
"$VENV_DIR/bin/python3" -m pip install -e .

echo "Dependencies installed..."
echo "Virtual environment created at $VENV_DIR"

echo "Compiling OpenPLC..."
if compile_plc; then
    echo "Build process completed successfully!"
    echo "OpenPLC Runtime v4 is ready to use."
    echo ""
    echo "To start the OpenPLC Runtime v4, run:"
    echo "sudo ./start_openplc.sh"

    # Create installation marker
    touch "$OPENPLC_DIR/.installed"
    echo "Installation completed at $(date)" > "$OPENPLC_DIR/.installed"

else
    echo "ERROR: Build process failed!" >&2
    echo "Please check the error messages above for details." >&2
    exit 1
fi