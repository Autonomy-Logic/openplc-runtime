#!/bin/bash
set -euo pipefail

# Start the PLC webserver
./.venv/bin/python3 webserver/app.py
