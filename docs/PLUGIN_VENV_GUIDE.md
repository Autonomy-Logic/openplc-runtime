# Virtual Environment Guide for Python Plugins

This document describes how to use separate virtual environments (venv) for Python plugins in the OpenPLC Runtime, allowing each plugin to have its own dependencies without conflicts.

## Overview

The separated VENV system allows you to:

* Let each Python plugin use specific library versions
* Avoid conflicts between dependencies of different plugins
* Simplify plugin development and maintenance
* Keep compatibility with existing plugins

## File Structure

```
openplc-runtime/
├── venvs/                          # Directory for virtual environments
│   ├── modbus_slave/               # venv for the Modbus plugin
│   └── mqtt_client/                # venv for the MQTT plugin
├── core/src/drivers/plugins/python/
│   ├── modbus_slave_plugin/
│   │   ├── simple_modbus.py
│   │   ├── modbus_slave_config.json
│   │   └── requirements.txt        # Plugin-specific dependencies
│   └── mqtt_plugin/
│       ├── plugin.py
│       ├── config.json
│       └── requirements.txt
└── scripts/
    └── manage_plugin_venvs.sh      # Management script
```

## How to Use

### 1. Creating a Plugin with a VENV

1. **Create the plugin directory:**

   ```bash
   mkdir core/src/drivers/plugins/python/my_plugin
   ```

2. **Create the requirements.txt file:**

   ```bash
   echo "pymodbus==3.6.4" > core/src/drivers/plugins/python/my_plugin/requirements.txt
   echo "paho-mqtt==2.1.0" >> core/src/drivers/plugins/python/my_plugin/requirements.txt
   ```

3. **Create the virtual environment:**

   ```bash
   ./scripts/manage_plugin_venvs.sh create my_plugin
   ```

4. **Configure plugins.conf:**

   ```
   my_plugin,./core/src/drivers/plugins/python/my_plugin/plugin.py,1,0,./config.json,./venvs/my_plugin
   ```

### 2. Managing Virtual Environments

#### Create a VENV for a plugin:

```bash
./scripts/manage_plugin_venvs.sh create PLUGIN_NAME
```

#### List all VENVs:

```bash
./scripts/manage_plugin_venvs.sh list
```

#### Install dependencies:

```bash
./scripts/manage_plugin_venvs.sh install PLUGIN_NAME
```

#### Remove a VENV:

```bash
./scripts/manage_plugin_venvs.sh remove PLUGIN_NAME
```

#### VENV information:

```bash
./scripts/manage_plugin_venvs.sh info PLUGIN_NAME
```

## plugins.conf Format

### New format (with VENV):

```
# name,path,enabled,type,config_path,venv_path
modbus_slave,./core/src/drivers/plugins/python/modbus_slave_plugin/simple_modbus.py,1,0,./config.json,./venvs/modbus_slave
```

### Old format (without VENV – still compatible):

```
# name,path,enabled,type,config_path
example_plugin,./core/src/drivers/examples/example_python_plugin.py,1,0,./example_config.ini
```

## Practical Example

### Modbus Plugin with a specific VENV:

1. **Create requirements.txt:**

   ```bash
   cat > core/src/drivers/plugins/python/modbus_slave_plugin/requirements.txt << EOF
   pymodbus==3.6.4
   asyncio-mqtt==0.16.2
   EOF
   ```

2. **Create the virtual environment:**

   ```bash
   ./scripts/manage_plugin_venvs.sh create modbus_slave
   ```

3. **Configure plugins.conf:**

   ```
   modbus_slave,./core/src/drivers/plugins/python/modbus_slave_plugin/simple_modbus.py,1,0,./core/src/drivers/plugins/python/modbus_slave_plugin/modbus_slave_config.json,./venvs/modbus_slave
   ```

4. **Verify installation:**

   ```bash
   ./scripts/manage_plugin_venvs.sh info modbus_slave
   ```

## Compatibility

* **Existing plugins:** Continue working normally without changes
* **Legacy system:** If `venv_path` is empty or missing, the system Python is used
* **Python versions:** Works with Python 3.6+

## Troubleshooting

### Plugin can’t find a module:

* Check if the venv was created: `./scripts/manage_plugin_venvs.sh list`
* Check if dependencies were installed: `./scripts/manage_plugin_venvs.sh info PLUGIN_NAME`
* Check the path in `plugins.conf`

### Dependency conflicts:

* Each plugin has its own isolated venv
* Use specific versions in `requirements.txt`
* Recreate the venv if needed: `./scripts/manage_plugin_venvs.sh remove PLUGIN_NAME && ./scripts/manage_plugin_venvs.sh create PLUGIN_NAME`

### Build error:

* Recompile after changes: `./scripts/compile.sh`
* Verify Python headers are installed: `sudo apt install python3-dev`

## Limitations

* Each venv uses additional disk space
* Slightly longer startup time
* Requires Python 3.3+ for the native `venv`

## Technical Architecture

### Implementation:

1. **plugin\_config.h:** Added `venv_path` field to the structure
2. **plugin\_config.c:** Parser updated to read the optional 6th field
3. **plugin\_driver.c:** Logic to set up `sys.path` before importing the plugin
4. **manage\_plugin\_venvs.sh:** Full management script

### Loading flow:

1. The system reads `plugins.conf`
2. If `venv_path` is specified, it sets up `sys.path` to include the venv’s site-packages
3. Imports the plugin’s Python module
4. Executes `init`/`start`/`stop`/`cleanup` functions as usual

This system maintains full compatibility with existing plugins while enabling dependency isolation when needed.
