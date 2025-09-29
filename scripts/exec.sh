#!/bin/bash
set -euo pipefail

# Start the PLC webserver
./venvs/runtime/bin/python3 webserver/app.py