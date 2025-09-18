#!/bin/bash
set -euo pipefail

# Execute the PLC runtime and webserver
./build/plc_main &
sleep 1
./.venv/bin/python3 webserver/app.py
