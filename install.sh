#!/bin/bash
set -e

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

# Check for root privileges (skip on MSYS2/Windows)
check_root()
{
    if is_msys2; then
        # Root is not required/meaningful on MSYS2
        return 0
    fi
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root" >&2
        echo "Example: sudo ./install.sh" >&2
        exit 1
    fi
}

# Make sure we are root before proceeding (unless on MSYS2)
check_root

# Detect the project root directory
# This works whether the script is called from project root, Docker, or anywhere else
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPLC_DIR="$SCRIPT_DIR"
VENV_DIR="$OPENPLC_DIR/venvs/runtime"
SCRIPTS_DIR="$OPENPLC_DIR/scripts"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
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

# Check if systemd is available and functional
# Returns 0 if systemd is available, 1 otherwise
has_systemd_support() {
    # Skip on MSYS2/Windows
    if is_msys2; then
        return 1
    fi

    # Check if systemctl command exists
    if ! command -v systemctl >/dev/null 2>&1; then
        return 1
    fi

    # Check if systemd is running as PID 1
    # This handles Docker containers and GitHub Actions where systemctl may exist but systemd isn't PID 1
    if [ ! -d "/run/systemd/system" ]; then
        return 1
    fi

    # Additional check: verify PID 1 is actually systemd
    if [ -f "/proc/1/comm" ]; then
        local pid1_name
        pid1_name=$(cat /proc/1/comm 2>/dev/null)
        if [ "$pid1_name" != "systemd" ]; then
            return 1
        fi
    fi

    # Final check: can we actually communicate with systemd?
    # Use 'if' to prevent set -e from aborting on failure
    if ! systemctl show-environment >/dev/null 2>&1; then
        return 1
    fi

    return 0
}

# Install systemd service for OpenPLC Runtime
install_systemd_service() {
    local service_file="/etc/systemd/system/openplc-runtime.service"

    log_info "Installing OpenPLC Runtime systemd service..."

    # Create the service file
    cat > "$service_file" <<EOF
[Unit]
Description=OpenPLC Runtime v4 Service
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=5
User=root
Group=root
WorkingDirectory=$OPENPLC_DIR
ExecStart=$OPENPLC_DIR/start_openplc.sh

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd daemon to recognize the new service
    systemctl daemon-reload

    # Enable and start the service
    log_info "Enabling and starting OpenPLC Runtime service..."
    systemctl enable --now openplc-runtime.service

    log_success "OpenPLC Runtime service installed and started"
    return 0
}

# Ensure we're in the project directory
cd "$OPENPLC_DIR"

echo "OpenPLC Runtime Installation"
echo "Project directory: $OPENPLC_DIR"
echo "Working directory: $(pwd)"

install_dependencies()
{
    # Check for MSYS2 first (before trying to detect package manager)
    if is_msys2; then
        echo "Platform: MSYS2/Windows"
        install_deps_msys2
        return $?
    fi

    # Detect package manager instead of distro for broader compatibility
    # This automatically supports all distros using these package managers
    if command -v apt-get >/dev/null 2>&1; then
        echo "Package manager: apt (Debian/Ubuntu/Mint/Pop!_OS/etc.)"
        install_deps_apt
    elif command -v dnf >/dev/null 2>&1; then
        echo "Package manager: dnf (Fedora/RHEL 8+/Rocky/AlmaLinux/etc.)"
        install_deps_dnf
    elif command -v yum >/dev/null 2>&1; then
        echo "Package manager: yum (RHEL 7/CentOS 7)"
        install_deps_yum
    elif command -v pacman >/dev/null 2>&1; then
        echo "Package manager: pacman (Arch/Manjaro/EndeavourOS/etc.)"
        install_deps_pacman
    elif command -v zypper >/dev/null 2>&1; then
        echo "Package manager: zypper (openSUSE/SUSE)"
        install_deps_zypper
    elif command -v apk >/dev/null 2>&1; then
        echo "Package manager: apk (Alpine Linux)"
        install_deps_apk
    else
        echo "ERROR: No supported package manager found" >&2
        echo "Supported package managers: apt, dnf, yum, pacman, zypper, apk" >&2
        return 1
    fi
}

# For apt-based distros (Debian, Ubuntu, Linux Mint, Pop!_OS, elementary OS, Zorin, MX Linux, etc.)
install_deps_apt() {
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev python3-pip python3-venv \
        gcc \
        make \
        cmake \
        pkg-config \
        libffi-dev
}

# For yum-based distros (RHEL 7, CentOS 7, Amazon Linux)
install_deps_yum() {
    yum install -y \
        gcc gcc-c++ make cmake \
        python3 python3-devel python3-pip python3-venv \
    && yum clean all
}

# For dnf-based distros (Fedora, RHEL 8+, CentOS Stream, Rocky Linux, AlmaLinux, Oracle Linux 8+)
install_deps_dnf() {
    dnf install -y \
        gcc gcc-c++ make cmake \
        python3 python3-devel python3-pip python3-venv \
    && dnf clean all
}

# For pacman-based distros (Arch Linux, Manjaro, EndeavourOS, Garuda, ArcoLinux, etc.)
install_deps_pacman() {
    pacman -Sy --noconfirm
    pacman -S --noconfirm --needed \
        base-devel \
        gcc \
        make \
        cmake \
        pkgconf \
        python \
        python-pip \
        python-setuptools
}

# For zypper-based distros (openSUSE, SUSE Linux Enterprise)
install_deps_zypper() {
    zypper refresh && \
    zypper install -y \
        gcc gcc-c++ make cmake \
        python3 python3-devel python3-pip \
        pkg-config
}

# For apk-based distros (Alpine Linux)
install_deps_apk() {
    apk update && \
    apk add --no-cache \
        build-base \
        gcc \
        make \
        cmake \
        pkgconf \
        python3 python3-dev py3-pip
}

# For MSYS2 on Windows
install_deps_msys2() {
    echo "Installing dependencies via pacman (MSYS2)..."
    # Update package database (but don't do full system upgrade to avoid breaking frozen bundles)
    pacman -Sy --noconfirm
    # Install required packages
    # Note: python-cryptography is installed via pacman because pip cannot build
    # Rust-based packages on MSYS2/Cygwin.
    # Plugin venvs use --system-site-packages to access these pre-built packages.
    # bcrypt is skipped on MSYS2 - the OPC-UA plugin uses PBKDF2 fallback (Python stdlib).
    pacman -S --noconfirm --needed \
        base-devel \
        gcc \
        make \
        cmake \
        pkg-config \
        python \
        python-pip \
        python-setuptools \
        python-cryptography \
        git \
        sqlite3
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

# Function to setup plugin virtual environments
setup_plugin_venvs() {
    local plugins_dir="$OPENPLC_DIR/core/src/drivers/plugins/python"
    local manage_script="$OPENPLC_DIR/scripts/manage_plugin_venvs.sh"

    log_info "Checking for plugins that need virtual environments..."

    # Check if plugins directory exists
    if [ ! -d "$plugins_dir" ]; then
        log_warning "Plugins directory not found: $plugins_dir"
        return 0
    fi

    # Find directories with requirements.txt for all plugins (regardless of enabled status)
    local plugins_with_requirements=()
    while IFS= read -r -d '' requirements_file; do
        # Get the directory name (plugin name)
        local plugin_dir=$(dirname "$requirements_file")
        local plugin_name=$(basename "$plugin_dir")

        # Skip if it's in examples or shared directories (common libraries)
        if [[ "$plugin_dir" == *"/examples/"* ]] || [[ "$plugin_dir" == *"/shared/"* ]]; then
            log_info "Skipping $plugin_name (in examples/shared directory)"
            continue
        fi

        plugins_with_requirements+=("$plugin_name")
        log_info "Found plugin with requirements: $plugin_name"
    done < <(find "$plugins_dir" -name "requirements.txt" -type f -print0)

    # If no plugins found, return
    if [ ${#plugins_with_requirements[@]} -eq 0 ]; then
        log_info "No plugins with requirements.txt found"
        return 0
    fi

    log_info "Found ${#plugins_with_requirements[@]} plugin(s) that need virtual environments"

    # Create virtual environments for each plugin
    for plugin_name in "${plugins_with_requirements[@]}"; do
        local venv_path="$OPENPLC_DIR/venvs/$plugin_name"
        local requirements_file="$plugins_dir/$plugin_name/requirements.txt"

        if [ -d "$venv_path" ]; then
            log_info "Virtual environment already exists for $plugin_name"

            # Check if requirements.txt is newer than the venv (dependencies may have changed)
            if [ "$requirements_file" -nt "$venv_path" ]; then
                log_warning "Requirements file is newer than venv for $plugin_name"
                log_info "Updating dependencies for $plugin_name..."

                if bash "$manage_script" install "$plugin_name"; then
                    log_success "Dependencies updated for $plugin_name"
                else
                    log_error "Failed to update dependencies for $plugin_name"
                    return 1
                fi
            else
                log_info "Dependencies are up to date for $plugin_name"
            fi
        else
            log_info "Creating virtual environment for plugin: $plugin_name"

            if bash "$manage_script" create "$plugin_name"; then
                log_success "Virtual environment created for $plugin_name"
            else
                log_error "Failed to create virtual environment for $plugin_name"
                return 1
            fi
        fi
    done

    log_success "All plugin virtual environments are ready"
    return 0
}

# Function to build native plugins that have CMakeLists.txt
build_native_plugins() {
    local native_plugins_dir="$OPENPLC_DIR/core/src/drivers/plugins/native"
    local plugins_output_dir="$OPENPLC_DIR/build/plugins"

    log_info "Scanning for native plugins to build..."

    # Check if native plugins directory exists
    if [ ! -d "$native_plugins_dir" ]; then
        log_warning "Native plugins directory not found: $native_plugins_dir"
        return 0
    fi

    # Create plugins output directory
    mkdir -p "$plugins_output_dir"

    # Find directories with CMakeLists.txt (indicates buildable plugin)
    local plugins_found=0
    local plugins_built=0
    local plugins_failed=0

    for plugin_dir in "$native_plugins_dir"/*/; do
        # Skip if not a directory
        [ -d "$plugin_dir" ] || continue

        local plugin_name=$(basename "$plugin_dir")
        local cmake_file="$plugin_dir/CMakeLists.txt"

        # Skip if no CMakeLists.txt
        if [ ! -f "$cmake_file" ]; then
            continue
        fi

        plugins_found=$((plugins_found + 1))
        log_info "Found native plugin: $plugin_name"

        # Create build directory for this plugin
        local plugin_build_dir="$plugin_dir/build"

        # Clean existing build directory
        if [ -d "$plugin_build_dir" ]; then
            log_info "Cleaning existing build directory for $plugin_name..."
            rm -rf "$plugin_build_dir"
        fi

        mkdir -p "$plugin_build_dir"

        # Build the plugin
        log_info "Building $plugin_name..."
        (
            cd "$plugin_build_dir" || exit 1

            # Configure with cmake, passing OpenPLC root directory
            if ! cmake -DOPENPLC_ROOT="$OPENPLC_DIR" ..; then
                log_error "CMake configuration failed for $plugin_name"
                exit 1
            fi

            # Build with make
            if ! make -j"$(nproc)"; then
                log_error "Compilation failed for $plugin_name"
                exit 1
            fi
        )

        if [ $? -eq 0 ]; then
            # Copy built plugin to central plugins directory
            local built_lib=$(find "$plugin_build_dir" -name "*.so" -type f 2>/dev/null | head -1)
            if [ -n "$built_lib" ] && [ -f "$built_lib" ]; then
                cp "$built_lib" "$plugins_output_dir/"
                log_success "Built and installed: $plugin_name ($(basename "$built_lib"))"
                plugins_built=$((plugins_built + 1))
            else
                log_warning "No .so file found after building $plugin_name"
                plugins_failed=$((plugins_failed + 1))
            fi
        else
            log_error "Failed to build $plugin_name"
            plugins_failed=$((plugins_failed + 1))
        fi
    done

    if [ $plugins_found -eq 0 ]; then
        log_info "No native plugins with CMakeLists.txt found"
        return 0
    fi

    log_info "Native plugin build summary: $plugins_built/$plugins_found succeeded"

    if [ $plugins_failed -gt 0 ]; then
        log_warning "$plugins_failed plugin(s) failed to build"
        # Don't fail installation if some plugins fail - they may be optional
    fi

    if [ $plugins_built -gt 0 ]; then
        log_success "Native plugins built and installed to: $plugins_output_dir"
    fi

    return 0
}

# Setup runtime directory (needed for both Linux and Docker)
# On MSYS2, use /run/runtime which maps to the MSYS2 installation directory
if is_msys2; then
    mkdir -p /run/runtime 2>/dev/null || true
    chmod 775 /run/runtime 2>/dev/null || true
else
    mkdir -p /var/run/runtime
    chmod 775 /var/run/runtime 2>/dev/null || true  # Ignore permission errors in Docker

    # Create persistent data directory for native Linux installs
    # This directory stores .env and database files that must survive reboot
    # In Docker, /var/run/runtime is mounted as a persistent volume instead
    if has_systemd_support; then
        mkdir -p /var/lib/openplc-runtime
        chmod 755 /var/lib/openplc-runtime
        log_info "Created persistent data directory at /var/lib/openplc-runtime"
    fi
fi

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

setup_plugin_venvs

# Setup discovery service virtual environment (for EtherCAT network scanning)
log_info "Setting up Discovery Service virtual environment..."
if bash "$SCRIPTS_DIR/setup_discovery_venv.sh"; then
    log_success "Discovery Service virtual environment ready"
else
    log_warning "Discovery Service setup failed (EtherCAT scanning will not be available)"
fi

echo "Compiling OpenPLC..."
if compile_plc; then
    echo "Build process completed successfully!"

    # Build native plugins after main compilation
    echo "Building native plugins..."
    build_native_plugins

    # Create installation marker (must be done before starting the service)
    touch "$OPENPLC_DIR/.installed"
    echo "Installation completed at $(date)" > "$OPENPLC_DIR/.installed"

    # Check if systemd is available and install the service
    SYSTEMD_SERVICE_INSTALLED=0
    if has_systemd_support; then
        log_info "Systemd detected. Installing OpenPLC Runtime service..."
        if install_systemd_service; then
            SYSTEMD_SERVICE_INSTALLED=1
        else
            log_warning "Failed to install systemd service. You can start the runtime manually."
        fi
    else
        log_info "Systemd not available. Skipping service installation."
    fi

    echo ""
    echo "OpenPLC Runtime v4 is ready to use."
    echo ""

    if [ "$SYSTEMD_SERVICE_INSTALLED" -eq 1 ]; then
        echo "The OpenPLC Runtime service has been installed and started."
        echo "The runtime will automatically start on system boot."
        echo ""
        echo "Useful commands:"
        echo "  sudo systemctl status openplc-runtime   - Check service status"
        echo "  sudo systemctl stop openplc-runtime     - Stop the service"
        echo "  sudo systemctl start openplc-runtime    - Start the service"
        echo "  sudo systemctl restart openplc-runtime  - Restart the service"
        echo "  sudo journalctl -u openplc-runtime -f   - View service logs"
    else
        echo "To start the OpenPLC Runtime v4, run:"
        echo "sudo ./start_openplc.sh"
    fi

else
    echo "ERROR: Build process failed!" >&2
    echo "Please check the error messages above for details." >&2
    exit 1
fi
