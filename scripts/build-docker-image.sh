#!/usr/bin/env bash
# Build the docker image with tag "build-env"
docker build -t build-env . 2>&1 | tee install_log.txt
