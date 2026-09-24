#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# Run container mounting only source code (preserves built venv)
# Named volume for persistent runtime data (DB, .env, etc)
docker run --rm -it \
    -v $(pwd)/core:/workdir/core \
    -v $(pwd)/plugins.conf:/workdir/plugins.conf \
    -v openplc-runtime-data:/var/run/runtime \
    --cap-add=sys_nice \
    --ulimit rtprio=99 \
    --ulimit memlock=-1 \
    -p 8443:8443 \
    openplc-dev "$@"
