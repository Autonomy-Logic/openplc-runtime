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
    mkdir -p "$OPENPLC_DIR/build"
    cd "$OPENPLC_DIR/build"
    cmake ..
    make -j"$(nproc)"
    cd "$OPENPLC_DIR"
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
#compile openplc
compile_plc

echo "OpenPLC compiled successfully."