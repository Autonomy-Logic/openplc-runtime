#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

set -euo pipefail

# Start the PLC webserver
./venvs/runtime/bin/python3 webserver/app.py