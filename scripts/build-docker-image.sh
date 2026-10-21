#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

# Build the docker image with tag "build-env"
docker build -t build-env . 2>&1 | tee install_log.txt
