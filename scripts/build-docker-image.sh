#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# Build the docker image with tag "build-env"
docker build -t build-env . 2>&1 | tee install_log.txt
