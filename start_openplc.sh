#!/bin/bash
set -euo pipefail

# Detect the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENPLC_DIR="$SCRIPT_DIR"
VENV_DIR="$OPENPLC_DIR/venvs/runtime"

# Ensure we're in the project directory
cd "$OPENPLC_DIR"

check_root() 
{
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root" >&2
        echo "Example: sudo ./start_openplc.sh" >&2
        exit 1
    fi
}

check_installation()
{
    if [ ! -f "$OPENPLC_DIR/.installed" ]; then
        echo "ERROR: OpenPLC Runtime v4 is not installed." >&2
        echo "Please run the install script first:" >&2
        echo "  sudo ./install.sh" >&2
        exit 1
    fi
}

# Startup checks
check_installation
check_root

echo "Starting OpenPLC Runtime"
echo "Project directory: $OPENPLC_DIR"
echo "Working directory: $(pwd)"

# MANAGE PLUGIN VENVS
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

setup_runtime_venv() {
    if [ -d "$VENV_DIR" ]; then
        log_info "Runtime virtual environment already exists."
    else
        log_info "Creating runtime virtual environment..."
        python3 -m venv "$VENV_DIR"
        if [ $? -ne 0 ]; then
            log_error "Failed to create runtime virtual environment."
            exit 1
        fi
            "$VENV_DIR/bin/python3" -m pip install --upgrade pip
            "$VENV_DIR/bin/python3" -m pip install -r "$OPENPLC_DIR/requirements.txt"
            source "$VENV_DIR/bin/activate"
            log_success "Runtime virtual environment created and activated."
    fi
}

# Function to get enabled plugins from plugins.conf
get_enabled_plugins() {
    local plugins_conf="$OPENPLC_DIR/plugins.conf"
    local enabled_plugins=()
    
    if [ ! -f "$plugins_conf" ]; then
        log_warning "plugins.conf not found: $plugins_conf"
        return 0
    fi
    
    while IFS= read -r line; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        
        # Parse the line: name,path,enabled,type,config_path,venv_path
        IFS=',' read -ra FIELDS <<< "$line"
        local plugin_name="${FIELDS[0]}"
        local enabled="${FIELDS[2]}"
        
        # Check if plugin is enabled (1)
        if [[ "$enabled" == "1" ]]; then
            enabled_plugins+=("$plugin_name")
        fi
    done < "$plugins_conf"
    
    printf '%s\n' "${enabled_plugins[@]}"
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
    
    # Get enabled plugins from plugins.conf
    local enabled_plugins=()
    while IFS= read -r plugin_name; do
        [[ -n "$plugin_name" ]] && enabled_plugins+=("$plugin_name")
    done < <(get_enabled_plugins)
    
    if [ ${#enabled_plugins[@]} -eq 0 ]; then
        log_info "No enabled plugins found in plugins.conf"
        return 0
    fi
    
    log_info "Enabled plugins: ${enabled_plugins[*]}"
    
    # Find directories with requirements.txt, but only for enabled plugins
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
        
        # Check if this plugin is enabled in plugins.conf
        local is_enabled=false
        for enabled_plugin in "${enabled_plugins[@]}"; do
            if [[ "$plugin_name" == "$enabled_plugin" ]]; then
                is_enabled=true
                break
            fi
        done
        
        if [[ "$is_enabled" == "true" ]]; then
            plugins_with_requirements+=("$plugin_name")
            log_info "Found enabled plugin with requirements: $plugin_name"
        else
            log_info "Skipping $plugin_name (not enabled in plugins.conf)"
        fi
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

# Setup plugin virtual environments
setup_plugin_venvs
setup_runtime_venv

# Start the PLC webserver
"$VENV_DIR/bin/python3" "$OPENPLC_DIR/webserver/app.py"
