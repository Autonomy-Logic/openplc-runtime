#!/usr/bin/env bash
# Run OpenPLC Runtime container
# Uses host network and NET_RAW capability for EtherCAT support

set -e

docker run --rm -it \
    -v $(pwd)/core:/workdir/core \
    -v $(pwd)/plugins.conf:/workdir/plugins.conf \
    -v openplc-runtime-data:/var/run/runtime \
    --cap-add=sys_nice \
    --cap-add=NET_RAW \
    --network=host \
    --ulimit rtprio=99 \
    --ulimit memlock=-1 \
    build-env "$@"
