#!/bin/bash
set -e

OPENPLC_DIR="$PWD"
VENV_DIR="$OPENPLC_DIR/.venv"

install_dependencies() {
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev python3-pip python3-venv \
        gcc \
        make \
        cmake \
    && rm -rf /var/lib/apt/lists/*
}

if [ "$1" = "docker" ]; then
    install_dependencies
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python3" -m pip install --upgrade pip
    "$VENV_DIR/bin/python3" -m pip install -r requirements.txt
fi

if [ "$1" = "linux" ]; then
    mkdir -p /var/run/runtime
    chmod 775 /var/run/runtime
    chmod +x install.sh
    chmod +x scripts/*
    install_dependencies
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python3" -m pip install --upgrade pip
    "$VENV_DIR/bin/python3" -m pip install -r requirements.txt
fi

echo "Dependencies installed."
