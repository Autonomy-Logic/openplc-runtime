#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

set -euo pipefail

# Start the PLC webserver
./venvs/runtime/bin/python3 webserver/app.py