#!/bin/bash
set -e

OPENPLC_DIR="$PWD"
VENV_DIR="$OPENPLC_DIR/.venv"
SCRIPTS_DIR="$OPENPLC_DIR/scripts"

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
    echo "Creating build directory..."
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

if [ "$1" = "linux" ]; then
    mkdir -p /var/run/runtime
    chmod 775 /var/run/runtime
    chmod +x install.sh
    chmod +x scripts/*
fi

install_dependencies
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip
"$VENV_DIR/bin/python3" -m pip install -r requirements.txt


echo "Dependencies installed..."
echo "Virtual environment created at $VENV_DIR"

echo "Compiling OpenPLC..."
if compile_plc; then
    echo "Build process completed successfully!"
    echo "OpenPLC Runtime is ready to use."
else
    echo "ERROR: Build process failed!" >&2
    echo "Please check the error messages above for details." >&2
    exit 1
fi