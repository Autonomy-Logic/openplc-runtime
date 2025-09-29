#!/bin/bash
# OpenPLC Runtime Plugin Virtual Environment Manager
# Manages virtual environments for Python plugins to avoid dependency conflicts

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENVS_DIR="$PROJECT_ROOT/venvs"
PLUGINS_DIR="$PROJECT_ROOT/core/src/drivers/plugins/python"

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

# Check if Python3 is available
check_python() {
    if ! command -v python3 &> /dev/null; then
        log_error "python3 is not installed or not in PATH"
        exit 1
    fi
    
    local python_version=$(python3 --version | cut -d' ' -f2)
    log_info "Using Python version: $python_version"
}

# Create virtual environment for a plugin
create_plugin_venv() {
    local plugin_name="$1"
    
    if [ -z "$plugin_name" ]; then
        log_error "Plugin name is required"
        show_usage
        exit 1
    fi
    
    local venv_path="$VENVS_DIR/$plugin_name"
    local plugin_path="$PLUGINS_DIR/${plugin_name}"
    local requirements_file="$plugin_path/requirements.txt"
    
    log_info "Creating virtual environment for plugin: $plugin_name"
    
    # Create venvs directory if it doesn't exist
    mkdir -p "$VENVS_DIR"
    
    # Check if venv already exists
    if [ -d "$venv_path" ]; then
        log_warning "Virtual environment already exists at: $venv_path"
        read -p "Do you want to recreate it? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            log_info "Removing existing virtual environment..."
            rm -rf "$venv_path"
        else
            log_info "Keeping existing virtual environment"
            return 0
        fi
    fi
    
    # Create virtual environment
    log_info "Creating Python virtual environment at: $venv_path"
    python3 -m venv "$venv_path"
    
    # Upgrade pip
    log_info "Upgrading pip..."
    "$venv_path/bin/pip" install --upgrade pip
    
    # Install requirements if they exist
    if [ -f "$requirements_file" ]; then
        log_info "Installing dependencies from: $requirements_file"
        "$venv_path/bin/pip" install -r "$requirements_file"
        log_success "Dependencies installed successfully"
    else
        log_warning "No requirements.txt found at: $requirements_file"
        log_info "You can add dependencies later by creating requirements.txt and running:"
        log_info "  $0 install $plugin_name"
    fi
    
    log_success "Virtual environment created successfully at: $venv_path"
    log_info "To use this venv in plugins.conf, add the venv path as the 6th field:"
    log_info "  $plugin_name,./path/to/plugin.py,1,0,./path/to/config.json,$venv_path"
}

# Install dependencies for existing venv
install_dependencies() {
    local plugin_name="$1"
    
    if [ -z "$plugin_name" ]; then
        log_error "Plugin name is required"
        show_usage
        exit 1
    fi
    
    local venv_path="$VENVS_DIR/$plugin_name"
    local plugin_path="$PLUGINS_DIR/${plugin_name}"
    local requirements_file="$plugin_path/requirements.txt"
    
    if [ ! -d "$venv_path" ]; then
        log_error "Virtual environment not found: $venv_path"
        log_info "Create it first with: $0 create $plugin_name"
        exit 1
    fi
    
    if [ ! -f "$requirements_file" ]; then
        log_error "Requirements file not found: $requirements_file"
        exit 1
    fi
    
    log_info "Installing dependencies for plugin: $plugin_name"
    "$venv_path/bin/pip" install -r "$requirements_file"
    log_success "Dependencies installed successfully"
}

# List all virtual environments
list_venvs() {
    log_info "Listing plugin virtual environments in: $VENVS_DIR"
    
    if [ ! -d "$VENVS_DIR" ]; then
        log_warning "No virtual environments directory found at: $VENVS_DIR"
        return 0
    fi
    
    local count=0
    for venv_dir in "$VENVS_DIR"/*; do
        if [ -d "$venv_dir" ] && [ -f "$venv_dir/bin/python" ]; then
            local venv_name=$(basename "$venv_dir")
            local python_version=$("$venv_dir/bin/python" --version 2>&1 | cut -d' ' -f2)
            local pip_packages=$("$venv_dir/bin/pip" list --format=freeze | wc -l)
            
            echo -e "${GREEN}$venv_name${NC}"
            echo "  Path: $venv_dir"
            echo "  Python: $python_version"
            echo "  Packages: $pip_packages installed"
            echo
            ((count++))
        fi
    done
    
    if [ $count -eq 0 ]; then
        log_warning "No virtual environments found"
    else
        log_success "Found $count virtual environment(s)"
    fi
}

# Remove virtual environment
remove_venv() {
    local plugin_name="$1"
    
    if [ -z "$plugin_name" ]; then
        log_error "Plugin name is required"
        show_usage
        exit 1
    fi
    
    local venv_path="$VENVS_DIR/$plugin_name"
    
    if [ ! -d "$venv_path" ]; then
        log_error "Virtual environment not found: $venv_path"
        exit 1
    fi
    
    log_warning "This will permanently remove the virtual environment for: $plugin_name"
    read -p "Are you sure? (y/N): " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Removing virtual environment: $venv_path"
        rm -rf "$venv_path"
        log_success "Virtual environment removed successfully"
    else
        log_info "Operation cancelled"
    fi
}

# Show package information for a venv
show_info() {
    local plugin_name="$1"
    
    if [ -z "$plugin_name" ]; then
        log_error "Plugin name is required"
        show_usage
        exit 1
    fi
    
    local venv_path="$VENVS_DIR/$plugin_name"
    
    if [ ! -d "$venv_path" ]; then
        log_error "Virtual environment not found: $venv_path"
        exit 1
    fi
    
    log_info "Virtual environment information for: $plugin_name"
    echo "Path: $venv_path"
    echo "Python version: $("$venv_path/bin/python" --version)"
    echo "Pip version: $("$venv_path/bin/pip" --version)"
    echo
    log_info "Installed packages:"
    "$venv_path/bin/pip" list
}

# Show usage information
show_usage() {
    echo "OpenPLC Runtime Plugin Virtual Environment Manager"
    echo
    echo "Usage: $0 COMMAND [PLUGIN_NAME]"
    echo
    echo "Commands:"
    echo "  create PLUGIN_NAME     Create virtual environment for plugin"
    echo "  install PLUGIN_NAME    Install dependencies for existing venv"
    echo "  list                   List all plugin virtual environments"
    echo "  remove PLUGIN_NAME     Remove virtual environment for plugin"
    echo "  info PLUGIN_NAME       Show information about plugin venv"
    echo "  help                   Show this help message"
    echo
    echo "Examples:"
    echo "  $0 create modbus       # Create venv for modbus plugin"
    echo "  $0 list                # List all plugin venvs"
    echo "  $0 remove modbus       # Remove modbus plugin venv"
    echo
    echo "Notes:"
    echo "  - Plugin requirements should be in: $PLUGINS_DIR/PLUGIN_NAME_plugin/requirements.txt"
    echo "  - Virtual environments are created in: $VENVS_DIR/"
    echo "  - Add venv path to plugins.conf as the 6th field to use it"
}

# Main function
main() {
    local command="$1"
    local plugin_name="$2"
    
    # Check Python availability
    check_python
    
    case "$command" in
        "create")
            create_plugin_venv "$plugin_name"
            ;;
        "install")
            install_dependencies "$plugin_name"
            ;;
        "list")
            list_venvs
            ;;
        "remove")
            remove_venv "$plugin_name"
            ;;
        "info")
            show_info "$plugin_name"
            ;;
        "help"|"--help"|"-h"|"")
            show_usage
            ;;
        *)
            log_error "Unknown command: $command"
            show_usage
            exit 1
            ;;
    esac
}

# Run main function with all arguments
main "$@"
