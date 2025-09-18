#!/usr/bin/env bash
# Run container mounting current directory into /workspace
docker run --rm -it \
    -v $(pwd):/workdir \
    --cap-add=sys_nice \
    --ulimit rtprio=99 \
    --ulimit memlock=-1 \
    -p 8443:8443 \
    build-env
